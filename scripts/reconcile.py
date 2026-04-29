#!/usr/bin/env python3
"""
YNAB reconciliation helper.

Sub-commands:
  accounts                     List YNAB accounts (open only)
  analyze ARGS                  Match CSV against YNAB account, classify diffs
  apply ARGS                    Execute a plan (deletes/creates/balance adj)

The analyze + apply commands read/write JSON so Claude can drive the
flow conversationally: analyze produces a structured diff report,
Claude reviews with the user and emits a plan, apply executes it.

Match algorithm and CSV format quirks are documented in
`references/reconciliation-guide.md`.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

YNAB_API = "https://api.ynab.com/v1"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> tuple[str, str]:
    """Resolve (api_key, budget_id) from env or ~/.config/ynab/config.json."""
    api_key = os.environ.get("YNAB_API_KEY")
    budget_id = os.environ.get("YNAB_BUDGET_ID")
    cfg_path = Path.home() / ".config" / "ynab" / "config.json"
    if (not api_key or not budget_id) and cfg_path.exists():
        cfg = json.loads(cfg_path.read_text())
        api_key = api_key or cfg.get("api_key")
        budget_id = budget_id or cfg.get("budget_id")
    if not api_key or not budget_id:
        sys.exit(
            "ERROR: YNAB_API_KEY/YNAB_BUDGET_ID not set and "
            "~/.config/ynab/config.json missing or incomplete."
        )
    return api_key, budget_id

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _req(method: str, path: str, token: str, body: dict | None = None) -> dict:
    url = f"{YNAB_API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
    try:
        return json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode()
        sys.exit(f"YNAB API {e.code}: {body_txt}")

def get(path, token):  return _req("GET", path, token)
def post(path, token, body):  return _req("POST", path, token, body)
def put(path, token, body):   return _req("PUT", path, token, body)
def delete(path, token):      return _req("DELETE", path, token)

# ---------------------------------------------------------------------------
# CSV parsing — auto-detect format
# ---------------------------------------------------------------------------

@dataclass
class CsvTxn:
    date: date
    amount: Decimal           # signed: negative outflow, positive inflow
    merchant: str
    raw_status: str = ""
    raw_kind: str = ""
    raw_card: str = ""
    skip_reason: str = ""

def _parse_eu_amount(s: str) -> Decimal | None:
    """Italian decimals: '1.234,56' or '+103,98' → Decimal."""
    if s is None or s == "":
        return None
    s = s.strip()
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".").replace("+", "")
    return Decimal(s)

def parse_csv(path: Path) -> list[CsvTxn]:
    """Auto-detect bank format from header columns."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        # detect delimiter
        delim = ";" if sample.count(";") > sample.count(",") else ","
        reader = csv.DictReader(f, delimiter=delim)
        headers = [h.strip() for h in (reader.fieldnames or [])]
        rows = list(reader)

    hdr_set = {h.lower() for h in headers}

    # Bleap-style: date, billing_amount, status, kind, card_last_four, ...
    if "billing_amount" in hdr_set and "status" in hdr_set:
        return _parse_bleap(rows)
    # Italian banks Sella/etc: DATA CONTABILE; USCITE; ENTRATE; CAUSALE; DESCRIZIONE
    if any("uscite" in h.lower() for h in headers) and any("entrate" in h.lower() for h in headers):
        return _parse_it_bank(rows)

    sys.exit(f"ERROR: unrecognized CSV format. Headers: {headers}")

def _parse_bleap(rows: list[dict]) -> list[CsvTxn]:
    out = []
    for r in rows:
        status = (r.get("status") or "").strip()
        kind = (r.get("kind") or "").strip()
        if status == "InsufficientFunds":
            out.append(CsvTxn(
                date=datetime.fromisoformat(r["date"].replace("Z", "+00:00")).date(),
                amount=-Decimal(r["billing_amount"]),
                merchant=r.get("merchant_name", ""),
                raw_status=status, raw_kind=kind,
                raw_card=r.get("card_last_four", ""),
                skip_reason="InsufficientFunds (declined)",
            ))
            continue
        if status == "Reversal":
            out.append(CsvTxn(
                date=datetime.fromisoformat(r["date"].replace("Z", "+00:00")).date(),
                amount=Decimal(r["billing_amount"]) * (1 if kind == "Reversal" else -1),
                merchant=r.get("merchant_name", ""),
                raw_status=status, raw_kind=kind,
                raw_card=r.get("card_last_four", ""),
                skip_reason="Reversal pair (net-0)",
            ))
            continue
        bill = Decimal(r["billing_amount"])
        amt = bill if (kind == "Refund" or status == "Refund") else -bill
        out.append(CsvTxn(
            date=datetime.fromisoformat(r["date"].replace("Z", "+00:00")).date(),
            amount=amt,
            merchant=r.get("merchant_name", ""),
            raw_status=status, raw_kind=kind,
            raw_card=r.get("card_last_four", ""),
        ))
    return out

def _parse_it_bank(rows: list[dict]) -> list[CsvTxn]:
    out = []
    for r in rows:
        # Find columns by tolerant lookup
        get_col = lambda *names: next((r[k] for n in names for k in r if k.lower() == n.lower()), None)
        desc = (get_col("DESCRIZIONE OPERAZIONE", "DESCRIZIONE") or "").strip()
        causale = (get_col("CAUSALE") or "").strip()
        # Skip summary rows: empty causale + desc starts with "saldo"
        if not causale and desc.lower().startswith("saldo"):
            continue
        u = _parse_eu_amount(get_col("USCITE"))
        e = _parse_eu_amount(get_col("ENTRATE"))
        amt = u if u is not None else e
        if amt is None:
            continue
        d = datetime.strptime(get_col("DATA CONTABILE", "DATA"), "%d/%m/%Y").date()
        # Italian banks store sign in the column; preserve as-is.
        out.append(CsvTxn(date=d, amount=amt, merchant=desc[:120], raw_status=causale))
    return out

# ---------------------------------------------------------------------------
# Match algorithm
# ---------------------------------------------------------------------------

def _normp(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())[:6]

def match(csv_txns: list[CsvTxn], y_reg: list[dict],
          amount_tol: Decimal = Decimal("0.02"),
          date_tol_days: int = 7) -> tuple[set[int], set[int]]:
    """Greedy 1:1 matching. Returns (used_csv_idx, used_ynab_idx)."""
    cands = []
    for ci, c in enumerate(csv_txns):
        if c.skip_reason:
            continue
        mn = _normp(c.merchant)
        for yi, t in enumerate(y_reg):
            if abs(Decimal(t["amount"]) / 1000 - c.amount) > amount_tol:
                continue
            td = datetime.fromisoformat(t["date"]).date()
            ddiff = abs((td - c.date).days)
            if ddiff > date_tol_days:
                continue
            payee = _normp(t.get("payee_name"))
            memo = _normp(t.get("memo"))
            bonus = 50 if mn and (mn[:5] in payee or mn[:5] in memo) else 0
            score = 100 - ddiff * 10 + bonus
            cands.append((-score, ci, yi))
    cands.sort()
    used_c, used_y = set(), set()
    for _, ci, yi in cands:
        if ci in used_c or yi in used_y:
            continue
        used_c.add(ci)
        used_y.add(yi)
    return used_c, used_y

# ---------------------------------------------------------------------------
# Cross-account duplicate hunt
# ---------------------------------------------------------------------------

def hunt_cross_account_dups(y_only: list[dict], all_other: list[dict],
                            accs: dict[str, str]) -> list[dict]:
    """Each YNAB-only entry: find candidates on OTHER accounts with
    same amount ±0.02 and date ±5d, payee fuzzy-overlap.
    Returns list of {gnosis_txn, candidates: [...]}.
    """
    result = []
    for t in y_only:
        a = Decimal(t["amount"]) / 1000
        td = datetime.fromisoformat(t["date"]).date()
        np_ = _normp(t.get("payee_name"))
        cands = []
        for o in all_other:
            oa = Decimal(o["amount"]) / 1000
            if abs(oa - a) > Decimal("0.02"):
                continue
            od = datetime.fromisoformat(o["date"]).date()
            if abs((od - td).days) > 5:
                continue
            op = _normp(o.get("payee_name"))
            if (np_ and op and (np_[:4] in op or op[:4] in np_)) or abs((od - td).days) <= 1:
                cands.append({
                    "id": o["id"],
                    "date": o["date"],
                    "amount": float(oa),
                    "account": accs.get(o["account_id"], "?"),
                    "payee": (o.get("payee_name") or "")[:40],
                    "memo": (o.get("memo") or "")[:60],
                })
        if cands:
            result.append({
                "ynab_txn": _summarize_txn(t),
                "candidates": cands,
            })
    return result

def hunt_sibling_pairs(y_reg: list[dict]) -> list[dict]:
    """Find Gnosis-on-Gnosis pairs same amount + date ±2d where one
    has import_id and one doesn't. Catches manual entries the user
    entered before a CSV-import created the same transaction."""
    pairs = []
    seen = set()
    for i, t in enumerate(y_reg):
        a = Decimal(t["amount"]) / 1000
        td = datetime.fromisoformat(t["date"]).date()
        for j, o in enumerate(y_reg):
            if i >= j:
                continue
            oa = Decimal(o["amount"]) / 1000
            if abs(oa - a) > Decimal("0.02"):
                continue
            od = datetime.fromisoformat(o["date"]).date()
            if abs((od - td).days) > 2:
                continue
            key = tuple(sorted([t["id"], o["id"]]))
            if key in seen:
                continue
            seen.add(key)
            ti, oi = t.get("import_id"), o.get("import_id")
            asym = bool(ti) ^ bool(oi)  # one has import_id, other doesn't
            pairs.append({
                "amount": float(a),
                "asymmetric_import": asym,
                "a": _summarize_txn(t),
                "b": _summarize_txn(o),
            })
    return pairs

def _summarize_txn(t: dict) -> dict:
    return {
        "id": t["id"],
        "date": t["date"],
        "amount": Decimal(t["amount"]) / 1000,
        "payee": (t.get("payee_name") or "")[:40],
        "memo": (t.get("memo") or "")[:60],
        "cleared": t.get("cleared", ""),
        "import_id": t.get("import_id") or "",
    }

# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------

def cmd_accounts(args):
    token, budget = load_config()
    accs = get(f"/budgets/{budget}/accounts", token)["data"]["accounts"]
    out = [
        {
            "id": a["id"],
            "name": a["name"],
            "type": a["type"],
            "balance": Decimal(a["balance"]) / 1000,
            "transfer_payee_id": a["transfer_payee_id"],
        }
        for a in accs if not a["closed"]
    ]
    print(json.dumps(out, indent=2, default=str))

def cmd_analyze(args):
    token, budget = load_config()
    csv_path = Path(args.csv).expanduser().resolve()
    if not csv_path.exists():
        sys.exit(f"ERROR: CSV not found: {csv_path}")

    csv_txns = parse_csv(csv_path)
    since = args.since or "2026-01-01"

    # YNAB this account
    y_resp = get(f"/budgets/{budget}/accounts/{args.account_id}/transactions?since_date={since}", token)
    y_all = [t for t in y_resp["data"]["transactions"] if not t["deleted"]]
    y_transfers = [t for t in y_all if (t.get("payee_name") or "").startswith("Transfer :")]
    y_reg = [t for t in y_all if not (t.get("payee_name") or "").startswith("Transfer :")]

    # YNAB other accounts (for cross-account dup hunt)
    all_resp = get(f"/budgets/{budget}/transactions?since_date={since}", token)
    all_other = [t for t in all_resp["data"]["transactions"]
                 if not t["deleted"] and t["account_id"] != args.account_id]
    accs_map = {a["id"]: a["name"]
                for a in get(f"/budgets/{budget}/accounts", token)["data"]["accounts"]}

    # Match
    used_c, used_y = match(csv_txns, y_reg)
    csv_only = [c for i, c in enumerate(csv_txns) if i not in used_c and not c.skip_reason]
    csv_skipped = [c for c in csv_txns if c.skip_reason]
    y_only = [t for i, t in enumerate(y_reg) if i not in used_y]

    # Cross-account dup hunt for y_only
    cross_dups = hunt_cross_account_dups(y_only, all_other, accs_map)

    # Sibling pairs (intra-Gnosis)
    siblings = hunt_sibling_pairs(y_reg)

    # Account current balance
    acc = get(f"/budgets/{budget}/accounts/{args.account_id}", token)["data"]["account"]
    bal = Decimal(acc["balance"]) / 1000

    report = {
        "account": {"id": acc["id"], "name": acc["name"], "balance": float(bal)},
        "real_balance": float(args.real_balance) if args.real_balance is not None else None,
        "gap": float(args.real_balance - float(bal)) if args.real_balance is not None else None,
        "csv": {
            "path": str(csv_path),
            "total": len(csv_txns),
            "effective": len(csv_txns) - len(csv_skipped),
            "skipped": [
                {"date": str(c.date), "amount": float(c.amount), "merchant": c.merchant,
                 "reason": c.skip_reason} for c in csv_skipped
            ],
            "netto_effective": float(sum(c.amount for c in csv_txns if not c.skip_reason)),
        },
        "ynab": {
            "regular": len(y_reg),
            "transfers": len(y_transfers),
            "netto_regular": float(sum(Decimal(t["amount"]) / 1000 for t in y_reg)),
        },
        "matched": len(used_c),
        "csv_only": [
            {"date": str(c.date), "amount": float(c.amount), "merchant": c.merchant,
             "card": c.raw_card, "status": c.raw_status, "kind": c.raw_kind}
            for c in csv_only
        ],
        "ynab_only": [
            {**_summarize_txn(t), "amount": float(_summarize_txn(t)["amount"])}
            for t in y_only
        ],
        "cross_account_dup_candidates": [
            {
                "ynab_txn": {**c["ynab_txn"], "amount": float(c["ynab_txn"]["amount"])},
                "candidates": c["candidates"],
            }
            for c in cross_dups
        ],
        "sibling_pairs": [
            {**p, "a": {**p["a"], "amount": float(p["a"]["amount"])},
                  "b": {**p["b"], "amount": float(p["b"]["amount"])}}
            for p in siblings
        ],
    }
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, default=str))
        print(f"Wrote report to {args.out}")
    else:
        print(json.dumps(report, indent=2, default=str))

def cmd_apply(args):
    """Execute a plan JSON.

    Plan shape:
    {
      "deletes": ["txn_id", ...],
      "creates": [{
        "account_id": "...", "date": "YYYY-MM-DD", "amount_milli": -1234,
        "payee_name": "...", "category_id": "...", "memo": "...",
        "force_no_import_id": true|false
      }, ...],
      "balance_adjust": {
        "account_id": "...", "amount_milli": -3061,
        "payee_name": "Riconciliazione …",
        "memo": "..."
      } | null
    }
    """
    token, budget = load_config()
    plan = json.loads(Path(args.plan).read_text())
    dry = bool(getattr(args, "dry_run", False))

    print(f"== {'DRY-RUN' if dry else 'APPLYING'} PLAN ==", file=sys.stderr)

    # 1) Deletes
    for tid in plan.get("deletes", []):
        if dry:
            # Resolve current state without modifying
            try:
                resp = get(f"/budgets/{budget}/transactions/{tid}", token)
                t = resp["data"]["transaction"]
                print(f"WOULD DELETE  {t['date']}  {t['amount']/1000:>+9.2f}  "
                      f"{(t.get('payee_name') or '')[:30]}", file=sys.stderr)
            except SystemExit:
                print(f"WOULD DELETE  {tid}  (lookup failed — verify id)", file=sys.stderr)
            continue
        resp = delete(f"/budgets/{budget}/transactions/{tid}", token)
        t = resp["data"]["transaction"]
        print(f"DELETED  {t['date']}  {t['amount']/1000:>+9.2f}  "
              f"{(t.get('payee_name') or '')[:30]}", file=sys.stderr)
        time.sleep(0.4)

    # 2) Creates (singolo per evitare auto-merge)
    for c in plan.get("creates", []):
        body = {
            "account_id": c["account_id"],
            "date": c["date"],
            "amount": int(c["amount_milli"]),
            "payee_name": c["payee_name"][:50],
            "category_id": c.get("category_id"),
            "approved": True,
            "cleared": c.get("cleared", "cleared"),
            "memo": (c.get("memo") or "")[:200],
        }
        if not c.get("force_no_import_id") and c.get("import_id"):
            body["import_id"] = c["import_id"][:36]
        if dry:
            print(f"WOULD CREATE  {body['date']}  {body['amount']/1000:>+9.2f}  "
                  f"{body['payee_name'][:30]}", file=sys.stderr)
            continue
        resp = post(f"/budgets/{budget}/transactions", token, {"transaction": body})
        t = resp["data"]["transaction"]
        print(f"CREATED  {t['date']}  {t['amount']/1000:>+9.2f}  "
              f"{(t.get('payee_name') or '')[:30]}", file=sys.stderr)
        time.sleep(0.4)

    # 3) Balance adjustment
    adj = plan.get("balance_adjust")
    if adj:
        # Reserved payee names blocked by YNAB API
        forbidden = ("Transfer :", "Starting Balance",
                     "Manual Balance Adjustment",
                     "Reconciliation Balance Adjustment")
        name = adj.get("payee_name") or "Riconciliazione"
        if any(name.startswith(p) for p in forbidden):
            sys.exit(f"ERROR: '{name}' is a YNAB-reserved payee name. "
                     "Use a custom name like 'Riconciliazione <account> <date>'.")
        body = {
            "account_id": adj["account_id"],
            "date": adj.get("date") or date.today().isoformat(),
            "amount": int(adj["amount_milli"]),
            "payee_name": name[:50],
            "category_id": adj.get("category_id"),
            "approved": True,
            "cleared": "cleared",
            "memo": (adj.get("memo") or "Reconciliation balance adjustment")[:200],
        }
        if dry:
            print(f"WOULD ADJ  {body['date']}  {body['amount']/1000:>+9.2f}  "
                  f"{body['payee_name'][:30]}", file=sys.stderr)
        else:
            resp = post(f"/budgets/{budget}/transactions", token, {"transaction": body})
            t = resp["data"]["transaction"]
            print(f"BAL ADJ  {t['date']}  {t['amount']/1000:>+9.2f}  "
                  f"{(t.get('payee_name') or '')[:30]}", file=sys.stderr)

    # Verify
    if not dry and ("balance_adjust" in plan or plan.get("deletes") or plan.get("creates")):
        acc_id = (plan.get("balance_adjust") or {}).get("account_id") \
                 or (plan.get("creates") or [{}])[0].get("account_id")
        if acc_id:
            acc = get(f"/budgets/{budget}/accounts/{acc_id}", token)["data"]["account"]
            print(f"\nFINAL bal {acc['name']}: {acc['balance']/1000:.2f}", file=sys.stderr)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="YNAB reconciliation tool")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_acc = sub.add_parser("accounts", help="List YNAB open accounts")
    p_acc.set_defaults(func=cmd_accounts)

    p_an = sub.add_parser("analyze", help="Match CSV vs YNAB account, output diff JSON")
    p_an.add_argument("--account-id", required=True)
    p_an.add_argument("--csv", required=True)
    p_an.add_argument("--real-balance", type=float, default=None,
                      help="Optional real bank/card balance to compute gap")
    p_an.add_argument("--since", default=None,
                      help="YYYY-MM-DD (default 2026-01-01)")
    p_an.add_argument("--out", default=None,
                      help="Write JSON to file instead of stdout")
    p_an.set_defaults(func=cmd_analyze)

    p_ap = sub.add_parser("apply", help="Execute a plan JSON (deletes/creates/balance adj)")
    p_ap.add_argument("--plan", required=True)
    p_ap.add_argument("--dry-run", action="store_true",
                      help="Preview the plan without hitting the API. "
                           "Resolves delete IDs to current state for verification.")
    p_ap.set_defaults(func=cmd_apply)

    args = ap.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
