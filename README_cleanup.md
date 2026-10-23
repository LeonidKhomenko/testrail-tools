# TestRail Safe Cleanup (`testrail_safe_cleanup.py`)

Safe helper to quarantine obsolete TestRail test cases **without deleting them**.

## What it does

- Supports two modes:
  - `dry-run` — validates data and shows what would be processed
  - `quarantine` — moves selected cases into quarantine section and prefixes title
- Works in **interactive mode by default** (analyzer-like UX).
- Can auto-detect `project_id` and `suite_id` from CSV (if columns exist).
- Uses **next N unprocessed** selection logic (not “first N rows every run”).
- Creates audit and backup files for traceability.

---

## Safety guarantees

- No delete API calls.
- Explicit confirmation required in quarantine mode:
  - You must type exactly `QUARANTINE` (unless `--yes` is used).
- Post-update verification for each case.
- Audit log (`.jsonl`) + backup snapshot (`.backup.json`).

---

## Candidate source CSV

Expected minimum columns:

- `case_id`
- `status`
- `safe_to_delete_candidate`

Only rows where:

- `status == never_executed`
- `safe_to_delete_candidate == YES`

are treated as candidates.

Optional columns:

- `project_id`
- `suite_id`
- `title`
- `section`

If `project_id` / `suite_id` are present and consistent, they are auto-used as scope.

> Do not use checkpoint CSV. Use a final reviewed analyzer report.

---

## Selection logic: “next N unprocessed”

The script checks live TestRail state for each candidate and classifies:

- **processed**: case is already in quarantine section **and** title has prefix
- **partial**: only one condition is true (included for healing)
- **unprocessed**: neither condition is true

Then it selects:

- **next N from unprocessed queue** (CSV order preserved)

So if first 10 were already processed in a previous run, the next run picks the next 10 unprocessed cases.

---

## Interactive run (recommended)

```bash
python3 testrail_safe_cleanup.py
```

Typical interactive flow:

1. choose mode (`dry-run` / `quarantine`)
2. enter URL, email, CSV path
3. enter token (hidden input)
4. confirm or provide scope (project/suite)
5. set batch size (`next N unprocessed`)
6. review plan
7. confirm by typing `QUARANTINE` (quarantine mode)

---

## Non-interactive examples

### Dry run

```bash
python3 testrail_safe_cleanup.py \
  --dry-run \
  --url "https://renesas.testrail.io" \
  --email "you@company.com" \
  --input-csv "./ATC_UART_547.csv" \
  --batch-size 10
```

### Quarantine batch

```bash
python3 testrail_safe_cleanup.py \
  --quarantine \
  --url "https://renesas.testrail.io" \
  --email "you@company.com" \
  --input-csv "./ATC_UART_547.csv" \
  --batch-size 10
```

---

## Useful flags

- `--batch-size N`  
  Number of **next unprocessed** candidates for current run.
- `--quarantine-path "_Quarantine/ToDelete"`  
  Target section path (created if missing in quarantine mode).
- `--title-prefix "[TO_DELETE_CANDIDATE] "`  
  Prefix added to title.
- `--skip-prefixed`  
  Skip cases already prefixed but not in quarantine section.
- `--yes`  
  Skip interactive QUARANTINE confirmation.
- `--non-interactive`  
  Fail on missing params instead of prompting.

---

## Artifacts

- `cleanup_audit_*.jsonl` — append-only event log
- `cleanup_audit_*.jsonl.backup.json` — snapshot before updates

Keep these files private (they may contain internal test metadata).

---

## Troubleshooting

### HTTP 401
- Verify email/token pair belongs to the same TestRail account.
- Regenerate API token and retry.
- Confirm URL is correct and HTTPS.

### “Aborted by user” after confirmation
- Type exactly `QUARANTINE` in English layout.
- Avoid extra spaces or non-Latin characters in the confirmation input.

### No cases selected
- Either all candidates are already processed,
- or CSV filter conditions are not matched (`never_executed` + `YES`).

---

## Important limitation

This script does **not** re-calculate execution history.  
It trusts the reviewed analyzer CSV as input.