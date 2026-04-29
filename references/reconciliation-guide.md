# YNAB Reconciliation — Strategies and Gotchas

Distilled from real-world reconciliation runs against multiple
account types (Italian bank export, Bleap-style multi-card CSV).
This is the reference Claude pulls when running `reconcile`.

## The 5-Phase Flow

When the user asks to reconcile, run this loop **per account**:

1. **Collect inputs** — for each account ask: real balance + path to
   CSV export (if any). Skip accounts without a CSV; reconciliation
   without a CSV is just "trust YNAB".
2. **Analyze** — run `scripts/reconcile.py analyze` to produce a
   structured diff report. Inspect each bucket:
   - `csv_only` — transactions in CSV but not in YNAB → candidates
     to ADD.
   - `ynab_only` — transactions in YNAB but not in CSV → candidates
     to DELETE or to leave (mis-class on other card / cash / pre-
     CSV-period).
   - `cross_account_dup_candidates` — YNAB-only entries that have
     matches on **other** YNAB accounts. The user paid the
     transaction once but tracked it on two accounts → delete the
     mis-classified one.
   - `sibling_pairs` — same-amount, near-date pairs **inside the
     reconcile target**. When one has `import_id` (CSV-derived)
     and the other doesn't (manual), the manual entry is usually
     the duplicate.
3. **Decide** — for every action item the user confirms or rejects.
   "CSV is law" is the default rule for cards: CSV records what the
   account actually paid; manual entries the user typed in for that
   same charge on this account are duplicates.
4. **Apply** — emit a plan JSON, run `scripts/reconcile.py apply`.
5. **Close residual gap** — if the YNAB balance still differs from
   real after fixing all identified items, add a single "Balance
   Adjustment" transaction. Use a non-reserved payee name (e.g.
   "Riconciliazione <account> <date>"). Keep the residual small
   (under ~3% of account balance) — a large residual means
   something material was missed.

## CSV format quirks

### Italian banks (Sella, ING Direct, etc.)

- 6 columns: `DATA CONTABILE; DATA VALUTA; USCITE; ENTRATE; CAUSALE; DESCRIZIONE OPERAZIONE`
- Semicolon-separated, comma decimals (`-18,00`), DD/MM/YYYY dates
- **The sign is already in the column** — `USCITE` is negative,
  `ENTRATE` is positive (with `+`). Don't flip it.
- **"Saldo iniziale" / "Saldo finale" rows have empty CAUSALE** and
  should be skipped — but be careful: the substring "Saldo" can
  appear inside legitimate transaction descriptions (e.g.
  `Note: Saldo fattura n.8`). Filter only rows with **empty
  CAUSALE AND DESCRIZIONE starting with "Saldo"**.

### Bleap-style multi-card export

- Columns: `date,clearing_date,merchant_name,transaction_amount,
  transaction_currency,billing_amount,billing_currency,
  transaction_type_description,status,card_last_four,mcc_code,kind`
- ISO-8601 dates with timezone, dot decimals
- Multi-currency: the `transaction_amount` is in the original
  currency, `billing_amount` is always in your settlement currency.
  **Always use `billing_amount`** for matching against YNAB.
- `card_last_four` distinguishes primary vs secondary card. Both
  draw against the same account; both must reconcile against the
  same YNAB account.
- **Status semantics**:
  - `Approved` — settled, count it
  - `Refund` (also `kind=Refund`) — inflow, count as positive
  - `Reversal` — appears as a **pair** of rows for the same
    (merchant, amount): one with `kind=Payment` and one with
    `kind=Reversal`, both with positive `billing_amount`. They net
    to zero. **Skip both rows** — the original payment was reversed
    by the merchant before settlement.
  - `InsufficientFunds` — declined, never debited. **Skip**.
    Audit each entry: was it retried and Approved later? Did the
    user pay on another card? The reconciler reports skipped
    entries so the user can verify.
  - `Other` — ambiguous. Possible meanings: pre-auth declined later,
    cash withdrawal, partial settlement. Treat as `Approved` by
    default but flag for manual review.
- **Probiller-style pre-auth pair**: occasionally an `Other` row
  precedes an `Approved` row for the same merchant, same amount,
  on the same card, within a few days. The `Other` is a pre-auth
  that wasn't reversed; only the `Approved` is real. If both ended
  up in YNAB, delete the one matching the `Other`.

## Match algorithm

```
score = 100 - date_diff_days * 10 + payee_bonus
match if amount within ±0.02 EUR AND date within 7 days AND score > 0
payee_bonus = +50 if first 5 chars of normalized merchant appear
              in normalized YNAB payee or memo
```

- Greedy 1:1 assignment: rank all candidate (csv, ynab) pairs by
  score, walk best-first, mark both used.
- For **split transactions** (YNAB category = "Split"): try summing
  2-3 CSV entries with the same merchant prefix on nearby dates
  against a single YNAB total. Common with multi-charge purchases
  (e.g. one Kyphi YNAB -48 = CSV +5 refund + -53 charge).

## Cross-account duplicate detection

The single biggest source of reconciliation gaps: **the user paid
once but tracked the transaction twice**, e.g.:

- Manual entry on Gnosis "Netflix -19.99" + auto-import on Bleap
  "Netflix -19.99 same day"
- Manual entry on Gnosis "Amazon -663.27 robot" + auto-import on
  ING Credit "Amazon -663.27 same day"
- Manual entry on Gnosis "McDonalds -3.90" + Crypto Card auto-import

**Rule**: if a transaction is in the CSV for account X, account X
paid it. Any matching entry on another account is the misclassified
duplicate — delete that one.

The reconciler hunts these via `cross_account_dup_candidates`:
amount ±0.02 + date ±5 days + payee fuzzy overlap, across all
accounts in the budget.

## Sibling pair detection (intra-account)

After `analyze` runs once and the user has confirmed CSV-only ADDs,
re-run analyze and look for `sibling_pairs` with
`asymmetric_import: true` — a pre-existing manual entry plus a new
CSV-imported entry for the same charge. Examples seen in practice:

- Subito.it ("Ciabatta smart") manual + "Subito.it S.r.l." CSV
- Pafory manual + Pafory CSV
- Apple TV manual + Apple CSV
- Easy Park "08:08 PayPal" manual + Easypark Italia Srl CSV
- Hera manual (no memo) + Gruppo Hera CSV

The CSV one is canonical; delete the manual.

The first-pass match can miss these because it only matches CSV→
YNAB once per CSV row. The intra-YNAB sibling sweep catches them.

## Hera-pattern: payee normalization gotcha

Pure prefix-3 normalization on payee can miss pairs like
`"Hera"` vs `"Gruppo Hera"` because `nor("hera") = "hera"` while
`nor("gruppoh")[:3] = "gru"`. **Fall back** to amount + date pair
search and inspect manually whenever neither side has a memo.

## YNAB API gotchas

### Reserved payee names (POST blocked)

These payee names are reserved for internal YNAB use and POST
fails with `400`:

- `Transfer :`
- `Starting Balance`
- `Manual Balance Adjustment`
- `Reconciliation Balance Adjustment`

For balance adjustment use a custom name like
`Riconciliazione <account> <YYYY-MM-DD>`.

### Batch POST auto-merge

`POST /transactions` with `{transactions: [...]}` and `import_id`
will sometimes **silently merge a request entry into an existing
YNAB entry** if it sees a near-match on amount/date/payee. The
response reports the merged entry as `created` with no flag. The
balance delta will be smaller than `sum(amount_milli)` over the
input list.

**Workaround**: when you absolutely need a new entry to be created
(e.g. a separate purchase on the same day at the same merchant for
the same amount), use a single-transaction POST
(`{transaction: {…}}`) **without** `import_id`. That bypasses the
de-dup heuristic.

### Transfers vs regular transactions

To create a real transfer between two accounts, the destination
account's `transfer_payee_id` must be used as `payee_id` (not
`payee_name`). The reconciler does **not** create transfers — that
flow lives in `transfer.sh`.

### Rate limit

200 requests/hour. The reconciler sleeps 0.4s between batched
deletes/creates. For very large reconciliations (>200 actions in
one run), split into hourly batches.

## "CSV is law" caveats

The "CSV is law" rule is a strong default but there are edge cases
where the user manual entry is right and the CSV is missing data:

- **CSV export cutoff**: a transaction made minutes before the
  export may not be in the CSV yet (settlement latency). Manual
  entries dated yesterday/today should be checked against the CSV's
  `clearing_date`, not just `date`.
- **Multi-currency rounding**: CSV `billing_amount` may differ by
  €0.01 from a manual entry the user entered before settlement.
- **Card-network mismatch**: some merchants charge through PayPal
  or another payment processor whose name doesn't match the
  visible merchant. The CSV shows `PADDLE.NET* EMERGENTL1` while
  the user entered `VoiceMonkey`. These are sibling pairs, treat
  as such.

## Final balance adjustment

If after all fixes the balance still differs:

1. **Verify the gap is small** (under ~3% of balance, ideally
   under €50 absolute). A larger gap usually means something was
   missed — investigate before adjusting.
2. Add one transaction:
   - account_id = the reconciling account
   - date = today
   - amount = `(real_balance - ynab_balance) * 1000` milliunits
   - payee_name = `Riconciliazione <account> <date>` (must NOT
     start with a reserved name)
   - category_id = Inflow:Ready-to-Assign for positive,
     Uncategorized for negative
   - cleared = `cleared`, approved = `true`
   - memo = brief description of why a residual remained
     (e.g. "Latency Bleap settlement; possible mis-class on other card")

Document in the memo what the residual most likely represents so
future reconciles don't re-investigate the same noise.
