---
name: ynab-api
description: "YNAB (You Need A Budget) budget management via API. Add transactions, track goals, monitor spending, create transfers, generate reports, and reconcile accounts against bank/card CSV exports. Use this skill whenever the user mentions YNAB, budget tracking, spending analysis, budget goals, Age of Money, account reconciliation, or wants to manage their personal finances -- even if they just say 'add an expense', 'how much did I spend', 'check my budget', 'upcoming bills', 'riconcilia', or 'reconcile' without naming YNAB explicitly. Also use for automated budget reports and financial summaries."
user-invocable: true
metadata: {"requiredEnv": ["YNAB_API_KEY", "YNAB_BUDGET_ID"]}
---

# YNAB Budget Management

Manage your YNAB budget via the API with ready-to-use bash + Python scripts. Requires `curl`, `jq`, and `python3` (3.10+, no external deps).

## Configuration

Set environment variables `YNAB_API_KEY` and `YNAB_BUDGET_ID`, or create `~/.config/ynab/config.json`:

```json
{
  "api_key": "YOUR_YNAB_TOKEN",
  "budget_id": "YOUR_BUDGET_ID",
  "monthly_target": 2000
}
```

The `monthly_target` field sets your monthly spending cap (used by `daily-spending-report.sh`). Can also be set via `YNAB_MONTHLY_TARGET` env var.

Get your token at https://app.ynab.com/settings/developer. Find your Budget ID in the YNAB URL.

## Available Scripts

All scripts are in `{baseDir}/scripts/` and output to stdout.

| Script | Purpose |
|--------|---------|
| `daily-spending-report.sh` | Yesterday's expenses by category + monthly budget progress + analysis |
| `daily-budget-check.sh` | Morning overview: Age of Money, upcoming bills, overspending alerts |
| `goals-progress.sh [month]` | Visual progress bars for category goals |
| `scheduled-upcoming.sh [days]` | Upcoming scheduled transactions (default: 7 days) |
| `month-comparison.sh [m1] [m2]` | Month-over-month spending comparison |
| `transfer.sh SRC DEST AMT DATE [MEMO]` | Create a properly linked account transfer |
| `ynab-helper.sh <command>` | General helper: search payees, list categories, add transactions |
| `setup-automation.sh` | Test config and list available scripts |
| `reconcile.py <subcmd>` | Reconcile YNAB accounts against bank/card CSV exports (`accounts`, `analyze`, `apply`) |

## Reconciliation Workflow

When the user says "riconcilia", "reconcile", "compare with the bank",
or supplies a CSV export, drive this conversational loop:

1. **List accounts** with `python3 scripts/reconcile.py accounts`. Show the user the
   open accounts and ask which they want to reconcile.
2. **Collect inputs** for each chosen account:
   - The real balance (from the bank/card app)
   - Path to the CSV export, if any
3. **Analyze** with
   `python3 scripts/reconcile.py analyze --account-id <UUID> --csv <path> --real-balance <N> --out /tmp/diff.json`
   This produces a structured diff: `csv_only`, `ynab_only`,
   `cross_account_dup_candidates`, `sibling_pairs`, plus skipped CSV
   rows (Reversal pairs, InsufficientFunds).
4. **Walk the buckets with the user**, deciding for each item:
   - `csv_only` → ADD to YNAB (CSV is law for that account)
   - `cross_account_dup_candidates` → DELETE the YNAB-only entry
     (the user paid once but tracked it on two accounts)
   - `sibling_pairs` with asymmetric `import_id` → DELETE the manual
     entry (the CSV import is canonical)
   - `ynab_only` without a candidate match → leave (cash, pre-CSV
     period, mis-class on a card without a CSV)
5. **Build a plan JSON** with `deletes`, `creates`, `balance_adjust`
   and run `python3 scripts/reconcile.py apply --plan /tmp/plan.json`. The
   tool sleeps between calls to respect the 200/hr rate limit.
6. **Verify** by re-reading the account balance and confirming it
   matches the real balance.

If a small residual gap remains (≤ ~3% of balance), close it with a
single Balance Adjustment in the plan. Document the most likely
cause in the memo (settlement latency, mis-class on a card without
a CSV, etc.) so future reconciles don't re-investigate the noise.

**MUST read [references/reconciliation-guide.md](references/reconciliation-guide.md)
BEFORE the first `apply`** — it covers CSV format quirks per bank,
the match algorithm, YNAB API gotchas (reserved payee names that
return 400, batch POST auto-merge that silently dedupes), and
"CSV is law" caveats. Skipping the guide is how you ship a wrong
reconcile.

### Plan JSON shape

```json
{
  "deletes": [
    "txn-uuid-1",
    "txn-uuid-2"
  ],
  "creates": [
    {
      "account_id": "uuid",
      "date": "2026-04-29",
      "amount_milli": -2990,
      "payee_name": "ASPIAG SERVICE S.R.L.",
      "category_id": "uncategorized-uuid",
      "memo": "card-8228 Approved/Payment",
      "import_id": "YNAB:-2990:2026-04-29:aspiag",
      "force_no_import_id": false
    }
  ],
  "balance_adjust": {
    "account_id": "uuid",
    "date": "2026-04-29",
    "amount_milli": -3061,
    "payee_name": "Riconciliazione Gnosis 2026-04-29",
    "category_id": "uncategorized-uuid",
    "memo": "Chiusura gap residuo 30d — possibili spese mis-class altra carta"
  }
}
```

Notes:
- `amount_milli` is signed: negative = outflow, positive = inflow
  (`int(amount_eur * 1000)`).
- `category_id` is required for creates that affect the budget.
  Use the Uncategorized id for outflows when category is unknown,
  Inflow:Ready-to-Assign id for unbudgeted inflows. Get both via
  `GET /budgets/{id}/categories`.
- `import_id` is optional. Use it for tx CSV-derived for idempotency
  (re-running the plan won't duplicate).
- `force_no_import_id: true` forces YNAB to create a brand-new
  entry instead of auto-merging into a near-match — needed when
  adding a separate purchase at the same merchant on the same day
  for the same amount.
- `payee_name` for `balance_adjust` MUST NOT start with a YNAB-
  reserved name (`Transfer :`, `Starting Balance`,
  `Manual Balance Adjustment`, `Reconciliation Balance Adjustment`)
  — POST will 400. Use `Riconciliazione <account> <date>` instead.

### Dry-run before apply

Pass `--dry-run` to `reconcile.py apply` to preview the plan
(deletes resolved to current state, creates and balance_adjust
echoed) without hitting the API. Use this on the user's first
reconciliation, or whenever the plan touches > 20 transactions.

## Key API Concepts

### Amounts use milliunits
YNAB API represents all amounts in milliunits: `10.00` = `10000`, `-10.00` = `-10000`. Always divide by 1000 when displaying, multiply by 1000 when submitting.

### Always categorize transactions
Never create transactions without a category -- it breaks budget tracking. When encountering an unfamiliar merchant, search past transactions for the same payee and reuse the category for consistency.

### Check for pending transactions before adding
Before creating a new transaction, check if an unapproved one already exists for the same amount. If found, approve it instead. This avoids duplicates from bank imports.

### Transfers require transfer_payee_id
To create a real linked transfer between accounts, use the destination account's `transfer_payee_id` (not `payee_name`). Using `payee_name` creates a regular transaction that YNAB won't recognize as a transfer. See [references/api-guide.md](references/api-guide.md) for the full transfer guide.

### Split transactions
Transactions with category "Split" contain `subtransactions`. Always expand them to show subcategories in reports -- never show "Split" as a category name.

## Common API Operations

```bash
YNAB_API="https://api.ynab.com/v1"

# Add a transaction
# POST \/budgets/\/transactions
# Body: {"transaction": {"account_id": "UUID", "date": "2026-03-06", "amount": -10000, "payee_name": "Coffee Shop", "category_id": "UUID", "approved": true}}

# Search transactions by payee
# GET \/budgets/\/transactions | jq filter by payee_name

# List categories
# GET \/budgets/\/categories
```

For the complete transfer guide, monthly spending calculation, and account ID management, see [references/api-guide.md](references/api-guide.md). For category naming examples, see [references/category-examples.md](references/category-examples.md).

## Agent Guidance

- Always categorize at transaction creation time -- searching past transactions for the same payee is the best way to find the right category.
- For transfers, always use `transfer_payee_id` from the destination account. Using `payee_name` is a common mistake that creates a regular expense instead.
- When calculating monthly spending, only count `amount < 0` and consider excluding non-discretionary categories (taxes, transfers).
- Rate limit is ~200 requests/hour. Cache account and category data when doing bulk operations.
- Never log or display full API keys in output.
- When running `daily-spending-report.sh`, the script outputs an "ANALYSIS DATA" section with raw metrics. Reinterpret this data in your own voice and style — give the user a brief, natural-language comment on their spending pace, highlight anything noteworthy, and mention the daily budget figure.

## Troubleshooting

- **401 Unauthorized**: Token invalid or expired -- regenerate at https://app.ynab.com/settings/developer
- **404 Not Found**: Budget ID wrong -- check the YNAB URL
- **429 Too Many Requests**: Rate limit -- add delays between bulk calls
- **Transfer not linking**: Using `payee_name` instead of `transfer_payee_id`

API docs: https://api.ynab.com
