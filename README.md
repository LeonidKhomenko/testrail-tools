# testrail-tools

AI-created tools to work with TestRail using the TestRail API.

## Included

- `testrail_report_final_fast_Version5_2.py`  
  Turbo analyzer for TestRail cases:
  - project/suite selection
  - section filters (quick by IDs + hierarchical picker)
  - optional include subsections
  - history check (`get_results_for_case`) to detect whether case was ever executed
  - additional protection signal: case present in open runs
  - CSV/XLSX export

## Requirements

- Python 3.8+
- `requests`
- `openpyxl` (optional, only for XLSX export)

Install dependencies:

```bash
pip install -r requirements.txt
```

## Usage

Run:

```bash
python3 testrail_report_final_fast_Version5_2.py
```

You will be prompted for:
- TestRail URL (example: `https://yourcompany.testrail.io`)
- email
- API token
- project/suite/section selection
- output format (CSV/XLSX)

## Notes

- This tool **does not delete** anything.
- It is intended as a **safe pre-delete analysis** helper.
- `safe_to_delete_candidate=YES` is a candidate signal only; always review report before any future cleanup actions.

## License

MIT