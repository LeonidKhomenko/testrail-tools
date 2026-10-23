# TestRail Tools — Cleanup/Analyzer Handoff Context

**Last updated:** 2026-10-22  
**Owner:** Leonid Khomenko  
**Repository:** `LeonidKhomenko/testrail-tools`

---

## 1) Goal

Make cleanup workflow safer and easier:

- avoid passing many CLI params manually each run,
- use analyzer CSV as source of truth for scope (`project_id`, `suite_id`),
- process candidates in **next N unprocessed** batches,
- keep full auditability (backup + audit logs),
- avoid deletions (quarantine only).

---

## 2) Current state (implemented)

### Analyzer (`testrail_report_final_fast_Version5_4_7.py`)
- Exports `project_id` and `suite_id` in report rows.
- CSV includes:
  - `case_id`, `project_id`, `suite_id`, `link`, `title`, `section`,
  - `automation_id`, `has_automation_id`, `status`, `safe_to_delete_candidate`.
- XLSX export updated to include `Project ID` and `Suite ID`.
- Fixed indentation bug in checkpoint config block (`if cp_enabled` / `try/except`).

### Cleanup (`testrail_safe_cleanup.py`)
- Interactive mode by default (analyzer-like UX).
- Reads candidates from CSV with filters:
  - `status == never_executed`
  - `safe_to_delete_candidate == YES`
- Auto-detects scope from CSV:
  - `project_id`, `suite_id`
- Batch selection logic:
  - **next N unprocessed**, not first N rows.
- “Processed” case definition:
  1) case in quarantine section
  2) title has configured prefix
- “Partial” state detected and included in queue for healing.
- Supports `dry-run` and `quarantine`.
- Writes audit (`.jsonl`) and backup snapshot (`.backup.json`).

---

## 3) Important behavior decisions

1. **No delete operations** — quarantine only.  
2. **CSV is trusted input** for cleanup candidate list (execution history is not recomputed by cleanup).  
3. **Scope consistency is required**:
   - in one CSV, `project_id/suite_id` should be consistent.
4. **Batching semantics**:
   - each run should move forward to next not-yet-processed cases.
5. **Confirmation-test mode was removed** from cleanup script/README.

---

## 4) Known caveats

- Cleanup expects one logical scope per CSV; mixed `suite_id` in one file can break assumptions.
- If analyzer is run in “All suites” mode, verify how `suite_id` is populated per case before using that CSV for cleanup.
- XLSX and CSV columns should stay aligned when adding/removing fields in future.

---

## 5) Recommended runbook

### A. Generate analyzer report
1. Run analyzer.
2. Select project/suite (prefer single suite for cleanup pipeline).
3. Export CSV (or XLSX + CSV if needed).
4. Validate required columns exist:  
   `case_id, project_id, suite_id, status, safe_to_delete_candidate`.

### B. Cleanup dry-run
1. Run cleanup in interactive mode:
   ```bash
   python3 testrail_safe_cleanup.py
   ```
2. Choose `dry-run`.
3. Verify planned batch and counts:
   - already processed
   - partial
   - unprocessed
   - selected this batch (next N unprocessed)

### C. Cleanup quarantine
1. Re-run cleanup and choose `quarantine`.
2. Confirm with exact `QUARANTINE`.
3. Keep audit + backup artifacts.

---

## 6) Validation checklist (before quarantine)

- [ ] CSV is FINAL reviewed (not checkpoint).
- [ ] CSV contains `project_id` and `suite_id`.
- [ ] All candidate rows belong to expected scope.
- [ ] Dry-run output matches expectations.
- [ ] Batch size is set intentionally.
- [ ] Backup/audit path is available and writable.

---

## 7) Suggested next tasks

- [ ] Add lightweight unit tests for:
  - next-N unprocessed selection,
  - partial-state handling,
  - CSV scope extraction/validation.
- [ ] Add a small `examples/` folder:
  - sample input CSV (sanitized),
  - sample audit snippet.
- [ ] Add CI syntax check:
  - `python3 -m py_compile testrail_safe_cleanup.py`
  - `python3 -m py_compile testrail_report_final_fast_Version5_4_7.py`
- [ ] Optional: create a compact non-interactive wrapper script for scheduled dry-runs.

---

## 8) Quick troubleshooting notes

### `IndentationError` in analyzer
Usually around checkpoint config block. Verify:
- `except ValueError` is aligned with matching `try`,
- `else:` belongs to `if cp_enabled`.

### HTTP 401
- Check URL/email/token combination.
- Confirm token belongs to same TestRail account.

### Cleanup selects 0 cases unexpectedly
- Check CSV filters (`never_executed` + `YES`).
- Check if cases are already processed (section + prefix).
- Review dry-run summary.

---

## 9) Files to review first tomorrow

1. `testrail_report_final_fast_Version5_4_7.py`
2. `testrail_safe_cleanup.py`
3. `README_cleanup.md`
4. `docs/cleanup_handoff.md` (this file)

---

## 10) Session note

Work paused intentionally; continue from this handoff file in VSCode tomorrow.