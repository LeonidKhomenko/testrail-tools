#!/usr/bin/env python3
"""
TestRail Case History Analyzer - Version 5.2 Turbo (safe pre-delete analysis)

Turbo improvements:
- Parallel history checks with ThreadPoolExecutor
- Batched + cached result checks per (run_id, case_id)
- Pre-filtering by candidate case IDs for open run membership
- Progress reporting for long-running scans

What it does:
- Finds cases in selected project/suite and optional sections (one or many).
- Supports include-subsections mode + hierarchical section picker.
- Checks execution history using get_results_for_case (authoritative for "ever executed").
- Also checks whether a case is currently present in OPEN runs (protective signal).
- Produces report with statuses:
    - never_executed
    - has_history
    - in_open_run_no_results

No deletion is performed in this script.
"""

import requests
import csv
import sys
import threading
from base64 import b64encode
from datetime import datetime
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor, as_completed


class TestRailAPI:
    def __init__(self, base_url, email, api_token):
        self.base_url = base_url.strip().rstrip('/')
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ValueError(
                'Invalid TestRail URL. Example: https://renesas.testrail.io'
            )

        auth_string = b64encode(f'{email}:{api_token}'.encode()).decode()
        self._headers = {
            'Authorization': f'Basic {auth_string}',
            'Content-Type': 'application/json'
        }

        self._local = threading.local()

    def _get_session(self):
        if not hasattr(self._local, 'session'):
            session = requests.Session()
            session.headers.update(self._headers)
            self._local.session = session
        return self._local.session

    def api_request(self, method, endpoint, json_data=None):
        url = f'{self.base_url}/index.php?/api/v2{endpoint}'
        session = self._get_session()

        try:
            if method == 'GET':
                response = session.get(url, timeout=60)
            elif method == 'POST':
                response = session.post(url, json=json_data, timeout=60)
            else:
                raise ValueError(f'Unsupported method: {method}')

            response.raise_for_status()
            if response.text:
                return response.json()
            return None

        except requests.exceptions.RequestException as e:
            print(f'❌ API Error: {e}')
            if getattr(e, 'response', None) is not None:
                print(f'Response: {e.response.text}')
            raise

    def get_projects(self):
        return self.api_request('GET', '/get_projects')

    def get_suites(self, project_id):
        return self.api_request('GET', f'/get_suites/{project_id}')

    def get_sections(self, project_id, suite_id=None, offset=0, limit=250):
        endpoint = f'/get_sections/{project_id}&offset={offset}&limit={limit}'
        if suite_id:
            endpoint += f'&suite_id={suite_id}'
        return self.api_request('GET', endpoint)

    def get_cases(self, project_id, suite_id=None, offset=0, limit=250, section_id=None):
        endpoint = f'/get_cases/{project_id}&offset={offset}&limit={limit}'
        if suite_id:
            endpoint += f'&suite_id={suite_id}'
        if section_id:
            endpoint += f'&section_id={section_id}'
        return self.api_request('GET', endpoint)

    def get_plans(self, project_id, offset=0, limit=250):
        return self.api_request('GET', f'/get_plans/{project_id}&offset={offset}&limit={limit}')

    def get_plan(self, plan_id):
        return self.api_request('GET', f'/get_plan/{plan_id}')

    def get_runs(self, project_id, suite_id=None, is_completed=None, offset=0, limit=250):
        endpoint = f'/get_runs/{project_id}&offset={offset}&limit={limit}'
        if suite_id:
            endpoint += f'&suite_id={suite_id}'
        if is_completed is not None:
            endpoint += f'&is_completed={1 if is_completed else 0}'
        return self.api_request('GET', endpoint)

    def get_tests(self, run_id, offset=0, limit=250):
        return self.api_request('GET', f'/get_tests/{run_id}&offset={offset}&limit={limit}')

    def get_results_for_case(self, run_id, case_id, offset=0, limit=250):
        endpoint = f'/get_results_for_case/{run_id}/{case_id}&offset={offset}&limit={limit}'
        return self.api_request('GET', endpoint)


def normalize_list_response(response, key):
    if response is None:
        return []
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        return response.get(key, [])
    return []


def fetch_all_sections(client, project_id, suite_id=None):
    all_sections = []
    offset = 0
    limit = 250

    while True:
        response = client.get_sections(project_id, suite_id=suite_id, offset=offset, limit=limit)
        chunk = normalize_list_response(response, 'sections')
        if not chunk:
            break
        all_sections.extend(chunk)
        if len(chunk) < limit:
            break
        offset += limit

    return all_sections


def build_section_maps(sections):
    by_id = {s['id']: s for s in sections}
    children = {}
    for s in sections:
        pid = s.get('parent_id')
        children.setdefault(pid, []).append(s['id'])

    path_cache = {}

    def build_path(sid):
        if sid in path_cache:
            return path_cache[sid]
        sec = by_id[sid]
        pid = sec.get('parent_id')
        if pid and pid in by_id:
            p = f"{build_path(pid)} > {sec['name']}"
        else:
            p = sec['name']
        path_cache[sid] = p
        return p

    section_path = {sid: build_path(sid) for sid in by_id.keys()}
    return by_id, children, section_path


def collect_descendants(children_map, roots):
    result = set()
    stack = list(roots)
    while stack:
        sid = stack.pop()
        if sid in result:
            continue
        result.add(sid)
        for c in children_map.get(sid, []):
            stack.append(c)
    return result


def parse_section_selection(user_input):
    raw = user_input.replace(',', ' ').split()
    ids = []
    for token in raw:
        ids.append(int(token))
    return ids


def choose_sections_interactive(by_id, children_map, section_path):
    top_level_ids = sorted(children_map.get(None, []), key=lambda sid: section_path[sid].lower())
    if not top_level_ids:
        return None

    selected = set()
    current_parent = None

    def current_children(parent_id):
        ids = children_map.get(parent_id, [])
        return sorted(ids, key=lambda sid: by_id[sid]['name'].lower())

    while True:
        kids = current_children(current_parent)
        if current_parent is None:
            print('\n=== TOP-LEVEL SECTIONS ===')
        else:
            print(f'\n=== SUBSECTIONS OF: {section_path[current_parent]} ===')

        if not kids:
            print('  (No child sections)')
        else:
            for idx, sid in enumerate(kids, 1):
                mark = '✓' if sid in selected else ' '
                print(f'  {idx:>2}. [{mark}] {by_id[sid]["name"]} (ID: {sid})')

        print('\nActions:')
        print('  a  - add ALL sections from current level (with all descendants)')
        print('  d  - drill down into one subsection')
        print('  s  - select one/many sections from current level (with descendants)')
        print('  c  - select CURRENT section fully (current + all descendants)')
        print('  u  - go UP one level')
        print('  l  - list current selected sections')
        print('  f  - finish selection')
        print('  x  - use ALL sections in suite (no filtering)')

        action = input('\nChoose action [a/d/s/c/u/l/f/x]: ').strip().lower()

        if action == 'x':
            return None

        if action == 'f':
            if selected:
                return selected
            print('⚠️ Nothing selected yet.')
            continue

        if action == 'l':
            if not selected:
                print('  (No sections selected yet)')
            else:
                print('\nSelected sections:')
                for sid in sorted(selected):
                    print(f'  - {sid}: {section_path[sid]}')
            continue

        if action == 'u':
            if current_parent is None:
                print('⚠️ Already at top level.')
                continue
            parent_of_current = by_id[current_parent].get('parent_id')
            current_parent = parent_of_current if parent_of_current in by_id else None
            continue

        if action == 'c':
            if current_parent is None:
                print('⚠️ At ROOT there is no single current section. Use "a" or "s".')
                continue
            desc = collect_descendants(children_map, [current_parent])
            selected.update(desc)
            print(f'✅ Added current section tree: {section_path[current_parent]} ({len(desc)} sections)')
            continue

        if action == 'a':
            if not kids:
                print('⚠️ No sections on this level.')
                continue
            total_added = 0
            for sid in kids:
                desc = collect_descendants(children_map, [sid])
                before = len(selected)
                selected.update(desc)
                total_added += len(selected) - before
            print(f'✅ Added all from current level (+{total_added} sections)')
            continue

        if action == 'd':
            if not kids:
                print('⚠️ No subsections to drill down into.')
                continue
            raw = input('Enter subsection number to drill into: ').strip()
            try:
                idx = int(raw)
                if idx < 1 or idx > len(kids):
                    raise ValueError
                current_parent = kids[idx - 1]
            except ValueError:
                print('❌ Invalid number.')
            continue

        if action == 's':
            if not kids:
                print('⚠️ No sections on this level.')
                continue
            raw = input('Enter numbers (comma/space), e.g. 1,3,5: ').strip()
            try:
                nums = parse_section_selection(raw)
                bad = [n for n in nums if n < 1 or n > len(kids)]
                if bad:
                    print(f'❌ Invalid item numbers: {bad}')
                    continue
                total_added = 0
                for n in nums:
                    sid = kids[n - 1]
                    desc = collect_descendants(children_map, [sid])
                    before = len(selected)
                    selected.update(desc)
                    total_added += len(selected) - before
                print(f'✅ Added selected section trees (+{total_added} sections)')
            except ValueError:
                print('❌ Invalid input format.')
            continue

        print('❌ Unknown action.')


def fetch_all_cases(client, project_id, suite_id=None):
    all_cases = []
    offset = 0
    limit = 250
    while True:
        response = client.get_cases(project_id, suite_id=suite_id, offset=offset, limit=limit)
        chunk = normalize_list_response(response, 'cases')
        if not chunk:
            break
        all_cases.extend(chunk)
        if len(chunk) < limit:
            break
        offset += limit
    return all_cases


def fetch_runs_from_plans(client, project_id, suite_id=None):
    runs = {}
    plans_offset = 0
    plans_limit = 250

    while True:
        response = client.get_plans(project_id, offset=plans_offset, limit=plans_limit)
        plans = normalize_list_response(response, 'plans')
        if not plans:
            break

        for plan in plans:
            plan_id = plan['id']
            plan_name = plan['name']
            detail = client.get_plan(plan_id)
            if not detail:
                continue

            for entry in detail.get('entries', []):
                entry_suite_id = entry.get('suite_id')
                if suite_id and entry_suite_id != suite_id:
                    continue
                for run in entry.get('runs', []):
                    rid = run['id']
                    if rid not in runs:
                        runs[rid] = {
                            'id': rid,
                            'name': run.get('name', f'Run {rid}'),
                            'suite_id': entry_suite_id,
                            'is_completed': run.get('is_completed', False),
                            'source': f'plan:{plan_name}'
                        }

        if len(plans) < plans_limit:
            break
        plans_offset += plans_limit

    return runs


def fetch_standalone_runs(client, project_id, suite_id=None):
    runs = {}
    offset = 0
    limit = 250
    while True:
        response = client.get_runs(project_id, suite_id=suite_id, is_completed=None, offset=offset, limit=limit)
        chunk = normalize_list_response(response, 'runs')
        if not chunk:
            break

        for run in chunk:
            rid = run['id']
            runs[rid] = {
                'id': rid,
                'name': run.get('name', f'Run {rid}'),
                'suite_id': run.get('suite_id'),
                'is_completed': run.get('is_completed', False),
                'source': 'standalone'
            }

        if len(chunk) < limit:
            break
        offset += limit

    return runs


def fetch_tests_case_presence_in_open_runs(client, runs, candidate_case_ids=None):
    case_ids_in_open_runs = set()
    candidate_case_ids = set(candidate_case_ids or [])

    open_runs = [r for r in runs if not r.get('is_completed', False)]
    print(f'📌 Checking open runs: {len(open_runs)}')

    for idx, run in enumerate(open_runs, 1):
        run_id = run['id']
        offset = 0
        limit = 250
        while True:
            response = client.get_tests(run_id, offset=offset, limit=limit)
            tests = normalize_list_response(response, 'tests')
            if not tests:
                break
            for t in tests:
                cid = t.get('case_id')
                if not cid:
                    continue
                if candidate_case_ids and cid not in candidate_case_ids:
                    continue
                case_ids_in_open_runs.add(cid)
            if len(tests) < limit:
                break
            offset += limit

        if idx % 50 == 0:
            print(f'  ✓ Processed {idx}/{len(open_runs)} open runs')

    return case_ids_in_open_runs


def case_has_any_results_cached(client, run_id, case_id, result_cache, cache_lock):
    key = (run_id, case_id)

    with cache_lock:
        if key in result_cache:
            return result_cache[key]

    offset = 0
    limit = 250
    has_results = False

    while True:
        response = client.get_results_for_case(run_id, case_id, offset=offset, limit=limit)
        results = normalize_list_response(response, 'results')

        if results:
            has_results = True
            break

        if len(results) < limit:
            break

        offset += limit

    with cache_lock:
        result_cache[key] = has_results

    return has_results


def analyze_single_case(client, case, sorted_runs, case_ids_in_open_runs, result_cache, cache_lock):
    cid = case['id']
    has_history = False

    for run in sorted_runs:
        if case_has_any_results_cached(client, run['id'], cid, result_cache, cache_lock):
            has_history = True
            break

    if has_history:
        status = 'has_history'
        safe_to_delete = 'NO'
    else:
        if cid in case_ids_in_open_runs:
            status = 'in_open_run_no_results'
            safe_to_delete = 'NO'
        else:
            status = 'never_executed'
            safe_to_delete = 'YES'

    return {
        'case_id': cid,
        'link': case['link'],
        'title': case['title'],
        'section': case['section'],
        'status': status,
        'safe_to_delete_candidate': safe_to_delete
    }


def analyze_case_history_turbo(client, candidate_cases, all_runs, case_ids_in_open_runs, workers=12):
    sorted_runs = sorted(all_runs, key=lambda r: (r.get('is_completed', False), r['id']))

    result_cache = {}
    cache_lock = threading.Lock()

    report_rows = []
    total = len(candidate_cases)
    done = 0

    print(f'🔎 Turbo history check for {total} candidate cases using {workers} workers...')

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(
                analyze_single_case,
                client,
                case,
                sorted_runs,
                case_ids_in_open_runs,
                result_cache,
                cache_lock
            ): case for case in candidate_cases
        }

        for future in as_completed(future_map):
            row = future.result()
            report_rows.append(row)
            done += 1
            if done % 50 == 0 or done == total:
                print(f'  ✓ Processed {done}/{total} cases')

    report_rows.sort(key=lambda r: r['case_id'])
    return report_rows


def save_to_csv(data, filename):
    if not data:
        print('⚠️ No data to save')
        return

    fieldnames = [
        'case_id', 'link', 'title', 'section',
        'status', 'safe_to_delete_candidate'
    ]

    with open(filename, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(data)

    print(f'✅ CSV saved: {filename}')


def save_to_xlsx(data, filename):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        print('⚠️ openpyxl not installed, fallback to CSV.')
        save_to_csv(data, filename.replace('.xlsx', '.csv'))
        return

    if not data:
        print('⚠️ No data to save')
        return

    wb = Workbook()
    ws = wb.active
    ws.title = 'Case History Analysis'

    headers = [
        'Case ID', 'Link', 'Title', 'Section',
        'Status', 'Safe To Delete Candidate'
    ]
    ws.append(headers)

    header_fill = PatternFill(start_color='366092', end_color='366092', fill_type='solid')
    header_font = Font(bold=True, color='FFFFFF')

    for c in ws[1]:
        c.fill = header_fill
        c.font = header_font
        c.alignment = Alignment(horizontal='center', vertical='center')

    for row in data:
        ws.append([
            row['case_id'],
            row['link'],
            row['title'],
            row['section'],
            row['status'],
            row['safe_to_delete_candidate']
        ])

    for row_idx in range(2, len(data) + 2):
        cell = ws[f'B{row_idx}']
        cell.hyperlink = cell.value
        cell.style = 'Hyperlink'

    ws.column_dimensions['A'].width = 12
    ws.column_dimensions['B'].width = 65
    ws.column_dimensions['C'].width = 60
    ws.column_dimensions['D'].width = 60
    ws.column_dimensions['E'].width = 28
    ws.column_dimensions['F'].width = 28

    wb.save(filename)
    print(f'✅ XLSX saved: {filename}')


def main():
    print('🚀 TestRail Case History Analyzer (Version 5.2 Turbo)\n')

    testrail_url = input('Enter TestRail URL (e.g., https://renesas.testrail.io): ').strip()
    email = input('Enter TestRail email: ').strip()
    api_token = input('Enter TestRail API token: ').strip()

    try:
        client = TestRailAPI(testrail_url, email, api_token)
    except ValueError as e:
        print(f'❌ {e}')
        sys.exit(1)

    print('\n📦 Fetching projects...')
    projects = normalize_list_response(client.get_projects(), 'projects')
    if not projects:
        print('❌ No projects found')
        sys.exit(1)

    print('\nAvailable projects:')
    for i, p in enumerate(projects, 1):
        print(f'  {i}. {p["name"]} (ID: {p["id"]})')

    try:
        p_idx = int(input('\nSelect project number: ').strip()) - 1
        project = projects[p_idx]
    except (ValueError, IndexError):
        print('❌ Invalid project selection')
        sys.exit(1)

    project_id = project['id']
    project_name = project['name']

    print('\n📚 Fetching test suites...')
    suites = normalize_list_response(client.get_suites(project_id), 'suites')

    suite_id = None
    suite_name = 'All Suites'

    if suites:
        print('\nAvailable test suites:')
        print('  0. All suites')
        for i, s in enumerate(suites, 1):
            print(f'  {i}. {s["name"]} (ID: {s["id"]})')

        try:
            s_choice = int(input('\nSelect suite number (0 for all): ').strip())
            if s_choice > 0:
                suite = suites[s_choice - 1]
                suite_id = suite['id']
                suite_name = suite['name']
        except (ValueError, IndexError):
            print('❌ Invalid suite selection')
            sys.exit(1)
    else:
        print('ℹ️ No suites returned (single-suite mode project maybe). Continue with all.')

    print('\n📚 Fetching sections...')
    sections = fetch_all_sections(client, project_id, suite_id=suite_id)
    by_id, children_map, section_path = build_section_maps(sections)

    selected_section_ids = None

    if sections:
        print('\nSection filter mode:')
        print('  0. All sections')
        print('  1. Quick select by section IDs')
        print('  2. Hierarchical step-by-step picker (recommended)')
        mode = input('Select mode (0/1/2): ').strip()

        if mode == '1':
            print('\nAvailable sections (ID -> Path):')
            for sid in sorted(section_path.keys()):
                print(f'  {sid} -> {section_path[sid]}')
            raw_ids = input('\nEnter section ID(s), comma/space separated: ').strip()
            try:
                base_ids = parse_section_selection(raw_ids)
            except ValueError:
                print('❌ Invalid section IDs format')
                sys.exit(1)
            missing = [sid for sid in base_ids if sid not in by_id]
            if missing:
                print(f'❌ Unknown section ID(s): {missing}')
                sys.exit(1)
            include_subsections = input('Include subsections? (y/n): ').strip().lower() == 'y'
            selected_section_ids = collect_descendants(children_map, base_ids) if include_subsections else set(base_ids)
            print(f'✅ Selected {len(selected_section_ids)} section(s)')
        elif mode == '2':
            selected_section_ids = choose_sections_interactive(by_id, children_map, section_path)
            if selected_section_ids is None:
                print('✅ Using all sections (no filtering)')
            else:
                print(f'✅ Selected {len(selected_section_ids)} section(s) via hierarchy')
        else:
            print('✅ Using all sections')
    else:
        print('ℹ️ No sections found, fallback to all cases')

    start_time = datetime.now()

    print('\n📋 Fetching all cases...')
    all_cases = fetch_all_cases(client, project_id, suite_id=suite_id)

    case_rows = []
    for c in all_cases:
        sid = c.get('section_id')
        section_name = section_path.get(sid, f'Section {sid}')
        case_rows.append({
            'id': c['id'],
            'title': c.get('title', ''),
            'section_id': sid,
            'section': section_name,
            'link': f'{testrail_url.rstrip("/")}/index.php?/cases/view/{c["id"]}'
        })

    if selected_section_ids is not None:
        case_rows = [c for c in case_rows if c['section_id'] in selected_section_ids]

    print(f'📋 Candidate cases after section filter: {len(case_rows)}')

    if not case_rows:
        print('⚠️ No candidate cases found with current filters.')
        sys.exit(0)

    print('\n🏃 Fetching runs from plans...')
    runs_from_plans = fetch_runs_from_plans(client, project_id, suite_id=suite_id)

    print('🏃 Fetching standalone runs...')
    standalone_runs = fetch_standalone_runs(client, project_id, suite_id=suite_id)

    merged_runs = {**runs_from_plans, **standalone_runs}
    all_runs = list(merged_runs.values())

    print(f'✅ Total unique runs considered: {len(all_runs)}')

    candidate_case_ids = {c['id'] for c in case_rows}

    if not all_runs:
        print('⚠️ No runs found. Cases will be marked never_executed unless present in open runs (none).')
        history_report = []
        for c in case_rows:
            history_report.append({
                'case_id': c['id'],
                'link': c['link'],
                'title': c['title'],
                'section': c['section'],
                'status': 'never_executed',
                'safe_to_delete_candidate': 'YES'
            })
    else:
        case_ids_in_open_runs = fetch_tests_case_presence_in_open_runs(
            client,
            all_runs,
            candidate_case_ids=candidate_case_ids
        )

        workers_raw = input('\nTurbo workers count [default 12]: ').strip()
        try:
            workers = int(workers_raw) if workers_raw else 12
            if workers < 1:
                workers = 1
            if workers > 32:
                workers = 32
        except ValueError:
            workers = 12

        history_report = analyze_case_history_turbo(
            client,
            case_rows,
            all_runs,
            case_ids_in_open_runs,
            workers=workers
        )

    end_time = datetime.now()
    sec = (end_time - start_time).total_seconds()

    total = len(history_report)
    never_exec = sum(1 for r in history_report if r['status'] == 'never_executed')
    has_hist = sum(1 for r in history_report if r['status'] == 'has_history')
    in_open = sum(1 for r in history_report if r['status'] == 'in_open_run_no_results')

    print('\n📈 Summary')
    print(f'   Project: {project_name}')
    print(f'   Suite: {suite_name}')
    print(f'   Candidate cases: {total}')
    print(f'   never_executed: {never_exec}')
    print(f'   has_history: {has_hist}')
    print(f'   in_open_run_no_results: {in_open}')
    print(f'   ⏱️ Time taken: {sec:.1f} sec')

    output_format = input('\nSave report as (1=CSV, 2=XLSX): ').strip()
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    if output_format == '2':
        out = f'testrail_case_history_analysis_turbo_{stamp}.xlsx'
        save_to_xlsx(history_report, out)
    else:
        out = f'testrail_case_history_analysis_turbo_{stamp}.csv'
        save_to_csv(history_report, out)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n⚠️ Interrupted by user.')
        sys.exit(130)
    except Exception as ex:
        print(f'\n❌ Fatal error: {ex}')
        sys.exit(1)