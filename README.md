# TestRail Case History Analyzer (v5.4.7)

A Python script for analyzing TestRail test case execution history, focused on identifying cases that were **never executed** (or have no detected results under the selected scan policy).

## Features

- Works with TestRail API (`/api/v2`)
- Interactive selection of:
  - Project
  - Suite
  - Section scope (all / by IDs / hierarchical picker)
- Two-stage execution history scan:
  1. **Stage 1**: scans **NON-CI runs** for all candidate cases
  2. **Stage 2**: scans **CI runs** only for automated cases that are still unresolved after Stage 1
- Automated case detection via confirmed field:
  - `custom_case_automation_id`
- Output export:
  - CSV or XLSX
- Reliability and performance:
  - Retry/backoff for 429/5xx
  - Request throttling
  - Checkpoint CSV export every N completed runs (default: 50)
  - Optional in-memory cache for `get_tests`

---

## Status meanings

- `has_history` — execution history found
- `in_open_run_no_results` — case is present in open runs but has no results yet
- `never_executed` — no results found under current scan policy

> Note: Cases without `automation_id` are intentionally skipped in CI stage.

---

## Requirements

- Python 3.9+
- Dependencies:
  - `requests`
  - `openpyxl` (optional, only for XLSX export)

Install dependencies:

```bash
pip install requests openpyxl
```

---

## Files

- `testrail_report_final_fast_Version5_4_7.py` — main analyzer script
- `README.md` — this documentation

---

## How to run

```bash
python testrail_report_final_fast_Version5_4_7.py
```

The script prompts for:
1. TestRail URL
2. Email
3. API token
4. Project / Suite / Section scope
5. Worker count
6. Checkpoint settings
7. Cache settings
8. Output format and report filename

---

## Recommended defaults

- `workers`: 4–8
- `checkpoint export`: enabled
- `checkpoint every N runs`: 50
- `get_tests cache`: enabled

---

## Checkpoint file examples

During execution, checkpoint files may be produced, e.g.:

- `testrail_v547_YYYYMMDD_HHMMSS_checkpoint_stage1_non_ci_run50.csv`
- `testrail_v547_YYYYMMDD_HHMMSS_checkpoint_stage2_ci_unresolved_auto_run100.csv`

---

## Safety notes

- Never commit API tokens or credentials.
- If `custom_case_automation_id` is missing in a case payload, the script fails fast by design (to avoid unsafe CI-skip decisions).
- Interpret `never_executed` within the selected scope (project/suite/sections) and current scan policy.

---

## What was optimized in v5.4.7

- Removed “latest Nightly only” truncation behavior
- Prioritized non-CI scanning first
- Limited CI scanning to automated cases
- Limited Stage 2 to unresolved automated cases only
- Added checkpoint export
- Added in-memory `get_tests` cache
- Fixed automation field key to `custom_case_automation_id`

---

## Quick sanity checks in logs

After a run, verify these lines:
- `Candidate cases after section filter`
- `Automation field presence stats`
- `Stage 2 candidate automated cases (unresolved after Stage 1)`
- `Summary`

These give a fast confidence check that the logic and optimizations are active.
