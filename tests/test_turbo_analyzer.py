import pytest
import importlib.util
from pathlib import Path


def load_module():
    root = Path(__file__).resolve().parents[1]
    target = root / "testrail_report_final_fast_Version5_2.py"
    spec = importlib.util.spec_from_file_location("tr_tool", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeClient:
    def __init__(self, tests_by_run, results_by_pair, malformed_tests=False, malformed_results=False):
        self.tests_by_run = tests_by_run
        self.results_by_pair = results_by_pair
        self.malformed_tests = malformed_tests
        self.malformed_results = malformed_results
        self.results_calls = []

    def get_tests(self, run_id, offset=0, limit=250):
        if self.malformed_tests:
            return {"tests": "not-a-list"}
        pages = self.tests_by_run.get(run_id, [])
        page_idx = offset // limit
        if page_idx >= len(pages):
            return {"tests": [], "_links": {"next": None}}
        page = pages[page_idx]
        next_link = "/next" if page_idx < len(pages) - 1 else None
        return {"tests": page, "_links": {"next": next_link}}

    def get_results_for_case(self, run_id, case_id, offset=0, limit=1):
        self.results_calls.append((run_id, case_id))
        if self.malformed_results:
            return {"results": "oops"}
        val = self.results_by_pair.get((run_id, case_id), [])
        return {"results": val}


def test_does_not_query_absent_pairs():
    mod = load_module()

    candidate_cases = [
        {"id": 1001, "link": "l1", "title": "t1", "section": "s"},
        {"id": 1002, "link": "l2", "title": "t2", "section": "s"},
        {"id": 1003, "link": "l3", "title": "t3", "section": "s"},
    ]
    runs = [{"id": 1, "is_completed": False}]
    tests_by_run = {
        1: [[{"id": 11, "case_id": 1001}, {"id": 12, "case_id": 1003}]]
    }
    results_by_pair = {
        (1, 1001): [{"id": 500}],
        (1, 1003): []
    }

    client = FakeClient(tests_by_run, results_by_pair)
    rows = mod.analyze_case_history_turbo(client, candidate_cases, runs, set(), workers=2)

    assert (1, 1002) not in client.results_calls
    status = {r["case_id"]: r["status"] for r in rows}
    assert status[1001] == "has_history"
    assert status[1003] == "in_open_run_no_results"
    assert status[1002] == "never_executed"


def test_malformed_tests_raises_runtime_error():
    mod = load_module()
    candidate_cases = [{"id": 1001, "link": "l1", "title": "t1", "section": "s"}]
    runs = [{"id": 1, "is_completed": False}]
    client = FakeClient({}, {}, malformed_tests=True)

    with pytest.raises(RuntimeError) as e:
        mod.analyze_case_history_turbo(client, candidate_cases, runs, set(), workers=1)
    assert "Run 1" in str(e.value)


def test_malformed_results_raises_runtime_error():
    mod = load_module()
    candidate_cases = [{"id": 1001, "link": "l1", "title": "t1", "section": "s"}]
    runs = [{"id": 1, "is_completed": False}]
    tests_by_run = {1: [[{"id": 11, "case_id": 1001}]]}
    client = FakeClient(tests_by_run, {}, malformed_results=True)

    with pytest.raises(RuntimeError) as e:
        mod.analyze_case_history_turbo(client, candidate_cases, runs, set(), workers=1)
    assert "Run 1" in str(e.value)


def test_duplicate_test_ids_across_pages_abort():
    mod = load_module()
    candidate_cases = [{"id": 1001, "link": "l1", "title": "t1", "section": "s"}]
    runs = [{"id": 1, "is_completed": False}]
    tests_by_run = {
        1: [
            [{"id": 11, "case_id": 1001}],
            [{"id": 11, "case_id": 1001}],
        ]
    }
    client = FakeClient(tests_by_run, {(1, 1001): []})

    with pytest.raises(RuntimeError) as e:
        mod.analyze_case_history_turbo(client, candidate_cases, runs, set(), workers=1)
    assert "Run 1" in str(e.value)


def test_status_derivation():
    mod = load_module()
    candidate_cases = [
        {"id": 1, "link": "l1", "title": "a", "section": "s"},
        {"id": 2, "link": "l2", "title": "b", "section": "s"},
        {"id": 3, "link": "l3", "title": "c", "section": "s"},
    ]
    runs = [
        {"id": 10, "is_completed": False},
        {"id": 20, "is_completed": True},
    ]
    tests_by_run = {
        10: [[{"id": 101, "case_id": 1}, {"id": 102, "case_id": 2}]],
        20: [[{"id": 201, "case_id": 1}]],
    }
    results_by_pair = {
        (10, 1): [{"id": 700}],
        (10, 2): [],
        (20, 1): [{"id": 701}],
    }
    client = FakeClient(tests_by_run, results_by_pair)

    rows = mod.analyze_case_history_turbo(client, candidate_cases, runs, set(), workers=2)
    by_id = {r["case_id"]: r for r in rows}

    assert by_id[1]["status"] == "has_history"
    assert by_id[1]["safe_to_delete_candidate"] == "NO"

    assert by_id[2]["status"] == "in_open_run_no_results"
    assert by_id[2]["safe_to_delete_candidate"] == "NO"

    assert by_id[3]["status"] == "never_executed"
    assert by_id[3]["safe_to_delete_candidate"] == "YES"