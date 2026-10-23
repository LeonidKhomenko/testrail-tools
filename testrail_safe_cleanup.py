#!/usr/bin/env python3
"""
testrail_safe_cleanup.py

Safe TestRail cleanup helper (NO DELETE), with:
- interactive wizard by default
- optional CLI overrides
- scope auto-detection from CSV (project_id/suite_id) with fallback prompt
- next-N UNPROCESSED selection (not first-N rows)
- dry-run and quarantine modes
- strict confirmation + diagnostics
- audit + backup

Requirements:
    pip install requests

Notes:
- Never deletes cases.
- "Processed" means BOTH:
    1) case.section_id == quarantine_section_id
    2) case.title starts with title_prefix
- If only one condition is true -> "partial" (included in candidates to fix)
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import requests

DEFAULT_QUARANTINE_PATH = "_Quarantine/ToDelete"
DEFAULT_TITLE_PREFIX = "[TO_DELETE_CANDIDATE] "
DEFAULT_BATCH_SIZE = 10
REQUEST_TIMEOUT = (15, 60)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_int(s: str, default: Optional[int] = None) -> Optional[int]:
    s = (s or "").strip()
    if not s:
        return default
    try:
        return int(s)
    except ValueError:
        return default


def ask(prompt: str, default: Optional[str] = None) -> str:
    if default is None:
        return input(prompt).strip()
    raw = input(f"{prompt} [{default}]: ").strip()
    return raw if raw else default


def ask_yes_no(prompt: str, default_yes: bool = True) -> bool:
    default_label = "Y/n" if default_yes else "y/N"
    raw = input(f"{prompt} ({default_label}): ").strip().lower()
    if not raw:
        return default_yes
    return raw in ("y", "yes")


def mask_email(email: str) -> str:
    if "@" not in email:
        return "***"
    left, right = email.split("@", 1)
    if len(left) <= 2:
        left_m = left[0] + "*" * (len(left) - 1)
    else:
        left_m = left[0] + "*" * (len(left) - 2) + left[-1]
    return f"{left_m}@{right}"


def confirm_quarantine() -> bool:
    try:
        raw_confirm = input("Type 'QUARANTINE' to continue: ")
    except EOFError:
        print("❌ Aborted: confirmation input reached EOF.")
        return False

    confirm = raw_confirm.strip()
    if confirm == "QUARANTINE":
        return True

    print(f"Confirmation received: {raw_confirm!r}")
    print(f"After stripping whitespace: {confirm!r}")
    print("Character codes: " + " ".join(f"U+{ord(ch):04X}" for ch in confirm))
    print("❌ Aborted: confirmation does not exactly match QUARANTINE.")
    return False


class TestRailAPI:
    def __init__(self, base_url: str, email: str, token: str):
        self.base_url = base_url.strip().rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("Invalid TestRail URL. Use HTTPS, e.g. https://company.testrail.io")
        if not email.strip():
            raise ValueError("Email must not be empty")
        if not token.strip():
            raise ValueError("Token must not be empty")

        self.session = requests.Session()
        self.session.auth = (email, token)
        self.session.headers.update({"Accept": "application/json"})
        self.next_request_at = 0.0

    def close(self):
        self.session.close()

    def _request(self, method: str, endpoint: str, payload=None):
        url = f"{self.base_url}/index.php?/api/v2{endpoint}"
        attempts = 5 if method == "GET" else 1

        for attempt in range(attempts):
            delay = max(0.0, self.next_request_at - time.monotonic())
            if delay > 0:
                time.sleep(delay)
            self.next_request_at = time.monotonic() + 0.35

            try:
                resp = self.session.request(
                    method=method,
                    url=url,
                    json=payload,
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=False,
                )
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt >= attempts - 1:
                    raise RuntimeError(f"{method} {endpoint}: network error ({e})")
                sleep_s = min(2 ** (attempt + 1), 20)
                print(f"⏳ transient network issue, retry in {sleep_s}s ...")
                time.sleep(sleep_s)
                continue

            with resp:
                if method == "GET" and resp.status_code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        wait_s = float(retry_after) if retry_after else min(2 ** (attempt + 1), 20)
                    except ValueError:
                        wait_s = min(2 ** (attempt + 1), 20)
                    print(f"⏳ read retry HTTP {resp.status_code}, waiting {wait_s:.0f}s ...")
                    time.sleep(wait_s)
                    continue

                if not (200 <= resp.status_code < 300):
                    raise RuntimeError(f"{method} {endpoint}: HTTP {resp.status_code}")

                if not resp.content:
                    return None
                return resp.json()

        raise RuntimeError(f"{method} {endpoint}: exhausted retries")

    def get(self, endpoint: str):
        return self._request("GET", endpoint)

    def post(self, endpoint: str, payload: dict):
        return self._request("POST", endpoint, payload)


@dataclass
class CandidateRow:
    case_id: int
    row_index: int
    title_csv: str = ""
    section_csv: str = ""
    project_id_csv: Optional[int] = None
    suite_id_csv: Optional[int] = None


def load_candidates_and_scope(csv_path: str) -> Tuple[List[CandidateRow], Optional[int], Optional[int]]:
    p = Path(csv_path)
    if not p.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    if "checkpoint" in p.name.lower():
        raise ValueError("Checkpoint CSV is not allowed. Use FINAL analyzer CSV.")

    candidates: List[CandidateRow] = []
    seen = set()

    csv_project_ids = set()
    csv_suite_ids = set()

    with p.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = set(reader.fieldnames or [])
        required = {"case_id", "status", "safe_to_delete_candidate"}
        missing = required - headers
        if missing:
            raise ValueError(f"CSV missing required columns: {sorted(missing)}")

        has_project_id = "project_id" in headers
        has_suite_id = "suite_id" in headers

        for row_idx, row in enumerate(reader, start=2):
            status = (row.get("status") or "").strip().lower()
            safe = (row.get("safe_to_delete_candidate") or "").strip().upper()
            if not (status == "never_executed" and safe == "YES"):
                continue

            try:
                cid = int(str(row.get("case_id", "")).strip())
                if cid <= 0:
                    raise ValueError
            except ValueError:
                raise ValueError(f"Invalid case_id at CSV line {row_idx}")

            if cid in seen:
                raise ValueError(f"Duplicate candidate case_id C{cid} in CSV")
            seen.add(cid)

            proj_val = None
            suite_val = None

            if has_project_id and (row.get("project_id") or "").strip():
                try:
                    proj_val = int(str(row["project_id"]).strip())
                except ValueError:
                    raise ValueError(f"Invalid project_id at CSV line {row_idx}")
                csv_project_ids.add(proj_val)

            if has_suite_id and (row.get("suite_id") or "").strip():
                try:
                    suite_val = int(str(row["suite_id"]).strip())
                except ValueError:
                    raise ValueError(f"Invalid suite_id at CSV line {row_idx}")
                csv_suite_ids.add(suite_val)

            candidates.append(
                CandidateRow(
                    case_id=cid,
                    row_index=row_idx,
                    title_csv=(row.get("title") or "").strip(),
                    section_csv=(row.get("section") or "").strip(),
                    project_id_csv=proj_val,
                    suite_id_csv=suite_val,
                )
            )

    if len(csv_project_ids) > 1:
        raise ValueError(f"CSV has multiple project_id values: {sorted(csv_project_ids)}")
    if len(csv_suite_ids) > 1:
        raise ValueError(f"CSV has multiple suite_id values: {sorted(csv_suite_ids)}")

    project_id_csv = next(iter(csv_project_ids)) if csv_project_ids else None
    suite_id_csv = next(iter(csv_suite_ids)) if csv_suite_ids else None
    return candidates, project_id_csv, suite_id_csv


def fetch_sections(client: TestRailAPI, project_id: int, suite_id: int) -> List[dict]:
    out = []
    seen_ids = set()
    offset = 0
    limit = 250

    while True:
        resp = client.get(f"/get_sections/{project_id}&suite_id={suite_id}&offset={offset}&limit={limit}")
        chunk = resp if isinstance(resp, list) else (resp.get("sections") if isinstance(resp, dict) else None)
        if not isinstance(chunk, list):
            raise RuntimeError("Invalid sections response")

        for s in chunk:
            if not isinstance(s, dict) or "id" not in s:
                raise RuntimeError("Malformed section object")
            sid = s["id"]
            if sid in seen_ids:
                raise RuntimeError(f"Duplicate section id detected: {sid}")
            seen_ids.add(sid)
            out.append(s)

        has_next = False
        if isinstance(resp, dict) and "_links" in resp and isinstance(resp["_links"], dict):
            has_next = bool(resp["_links"].get("next"))
        else:
            has_next = len(chunk) == limit

        if not has_next:
            break
        offset += len(chunk)

    return out


def find_child_section(sections: List[dict], parent_id: Optional[int], name: str) -> Optional[dict]:
    matches = [s for s in sections if s.get("parent_id") == parent_id and str(s.get("name", "")).strip() == name]
    if len(matches) > 1:
        raise RuntimeError(f"Ambiguous section name under same parent: {name}")
    return matches[0] if matches else None


def ensure_quarantine_section(client: TestRailAPI, project_id: int, suite_id: int, path: str, sections: List[dict], journal):
    parent_id = None
    for part in [x.strip() for x in path.split("/") if x.strip()]:
        existing = find_child_section(sections, parent_id, part)
        if existing:
            parent_id = existing["id"]
            continue

        payload = {"suite_id": suite_id, "name": part}
        if parent_id is not None:
            payload["parent_id"] = parent_id

        journal("section_create_intent", {"name": part, "parent_id": parent_id})
        created = client.post(f"/add_section/{project_id}", payload)

        if not isinstance(created, dict) or "id" not in created:
            raise RuntimeError(f"Failed to create section '{part}'")
        if created.get("suite_id") != suite_id:
            raise RuntimeError("Created section in unexpected suite")

        sections.append(created)
        journal("section_created", {"id": created["id"], "name": created.get("name"), "parent_id": created.get("parent_id")})
        parent_id = created["id"]

    return parent_id


class JsonlAudit:
    def __init__(self, path: str):
        self.path = Path(path)
        self.f = self.path.open("x", encoding="utf-8")

    def write(self, event: str, data: dict):
        rec = {"timestamp": utc_now(), "event": event, **data}
        self.f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.f.flush()
        os.fsync(self.f.fileno())

    def close(self):
        self.f.close()


def validate_case_in_scope(case: dict, case_id: int, suite_id: int, section_ids: set):
    if not isinstance(case, dict):
        raise RuntimeError(f"C{case_id}: invalid case response")
    if case.get("id") != case_id:
        raise RuntimeError(f"C{case_id}: response ID mismatch")
    if case.get("suite_id") != suite_id:
        raise RuntimeError(f"C{case_id}: not in expected suite")
    if case.get("section_id") not in section_ids:
        raise RuntimeError(f"C{case_id}: section not in suite scope")
    if case.get("is_deleted"):
        raise RuntimeError(f"C{case_id}: case marked deleted")
    if not isinstance(case.get("title"), str):
        raise RuntimeError(f"C{case_id}: invalid title")


def case_processed(case_obj: dict, quarantine_section_id: int, title_prefix: str) -> bool:
    return case_obj.get("section_id") == quarantine_section_id and str(case_obj.get("title", "")).startswith(title_prefix)


def case_partial(case_obj: dict, quarantine_section_id: int, title_prefix: str) -> bool:
    in_q = case_obj.get("section_id") == quarantine_section_id
    has_p = str(case_obj.get("title", "")).startswith(title_prefix)
    return (in_q and not has_p) or (has_p and not in_q)


def choose_next_unprocessed(
    candidate_rows: List[CandidateRow],
    live_cases: Dict[int, dict],
    quarantine_section_id: int,
    title_prefix: str,
    batch_size: int
):
    already = []
    partial = []
    unprocessed = []

    for row in candidate_rows:
        c = live_cases[row.case_id]
        if case_processed(c, quarantine_section_id, title_prefix):
            already.append(row.case_id)
        elif case_partial(c, quarantine_section_id, title_prefix):
            partial.append(row.case_id)
            unprocessed.append(row.case_id)
        else:
            unprocessed.append(row.case_id)

    selected = unprocessed[:batch_size]
    return selected, already, partial, unprocessed


def build_arg_parser():
    p = argparse.ArgumentParser(description="Safe TestRail cleanup (dry-run/quarantine, no delete).")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--quarantine", action="store_true")

    p.add_argument("--url")
    p.add_argument("--email")
    p.add_argument("--token")
    p.add_argument("--input-csv")
    p.add_argument("--project-id", type=int)
    p.add_argument("--suite-id", type=int)
    p.add_argument("--quarantine-path", default=DEFAULT_QUARANTINE_PATH)
    p.add_argument("--title-prefix", default=DEFAULT_TITLE_PREFIX)
    p.add_argument("--batch-size", type=int, default=None)
    p.add_argument("--skip-prefixed", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--audit-log", default=None)
    p.add_argument("--non-interactive", action="store_true", help="Fail instead of prompting missing values.")
    return p


def resolve_mode(args) -> str:
    if args.quarantine and args.dry_run:
        raise ValueError("Choose only one mode: --dry-run OR --quarantine")
    if args.quarantine:
        return "quarantine"
    if args.dry_run:
        return "dry-run"
    return "interactive"


def resolve_effective_config(args):
    mode = resolve_mode(args)
    interactive = not args.non_interactive

    if mode == "interactive":
        m = ask("Mode (dry-run/quarantine)", "dry-run").lower()
        mode = "quarantine" if m.startswith("q") else "dry-run"

    url = args.url
    email = args.email
    token = args.token
    input_csv = args.input_csv
    project_id_cli = args.project_id
    suite_id_cli = args.suite_id

    if interactive:
        if not url:
            url = ask("TestRail URL", "https://renesas.testrail.io")
        if not email:
            email = ask("TestRail email")
        if not input_csv:
            input_csv = ask("Input CSV path")
    else:
        missing = [k for k, v in [("url", url), ("email", email), ("input-csv", input_csv)] if not v]
        if missing:
            raise ValueError(f"Missing required parameters in non-interactive mode: {missing}")

    candidates, csv_project_id, csv_suite_id = load_candidates_and_scope(input_csv)

    project_id = project_id_cli if project_id_cli else csv_project_id
    suite_id = suite_id_cli if suite_id_cli else csv_suite_id

    if project_id_cli and csv_project_id and project_id_cli != csv_project_id:
        print(f"⚠️ project_id mismatch: CLI={project_id_cli}, CSV={csv_project_id}")
        if interactive:
            if not ask_yes_no("Continue with CLI project_id?", default_yes=False):
                raise ValueError("Aborted due to project_id mismatch")
        else:
            raise ValueError("project_id mismatch between CLI and CSV")

    if suite_id_cli and csv_suite_id and suite_id_cli != csv_suite_id:
        print(f"⚠️ suite_id mismatch: CLI={suite_id_cli}, CSV={csv_suite_id}")
        if interactive:
            if not ask_yes_no("Continue with CLI suite_id?", default_yes=False):
                raise ValueError("Aborted due to suite_id mismatch")
        else:
            raise ValueError("suite_id mismatch between CLI and CSV")

    if interactive:
        if not project_id:
            project_id = parse_int(ask("Project ID"), None)
        if not suite_id:
            suite_id = parse_int(ask("Suite ID"), None)

    if not project_id or project_id <= 0:
        raise ValueError("project_id is required and must be positive")
    if not suite_id or suite_id <= 0:
        raise ValueError("suite_id is required and must be positive")

    q_path = args.quarantine_path or DEFAULT_QUARANTINE_PATH
    t_prefix = args.title_prefix if args.title_prefix is not None else DEFAULT_TITLE_PREFIX

    if interactive and not args.quarantine_path:
        q_path = ask("Quarantine section path", DEFAULT_QUARANTINE_PATH)
    if interactive and args.title_prefix is None:
        t_prefix = ask("Title prefix", DEFAULT_TITLE_PREFIX)

    batch_size = args.batch_size
    if batch_size is None:
        if interactive:
            batch_size = parse_int(ask("Batch size (next N unprocessed cases)", str(DEFAULT_BATCH_SIZE)), DEFAULT_BATCH_SIZE)
        else:
            batch_size = DEFAULT_BATCH_SIZE
    if batch_size <= 0:
        raise ValueError("batch_size must be > 0")

    skip_prefixed = args.skip_prefixed
    if interactive and not args.skip_prefixed:
        skip_prefixed = ask_yes_no("Skip cases that already have prefix but are not in quarantine section?", default_yes=False)

    if not token:
        token = os.environ.get("TESTRAIL_TOKEN", "")
    if not token and interactive:
        token = getpass.getpass("Enter TestRail API token: ")
    if not token:
        raise ValueError("API token is required")

    audit_log = args.audit_log
    if not audit_log:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        audit_log = f"cleanup_audit_{stamp}.jsonl"

    return {
        "mode": mode,
        "url": url.strip(),
        "email": email.strip(),
        "token": token,
        "input_csv": input_csv,
        "project_id": int(project_id),
        "suite_id": int(suite_id),
        "quarantine_path": "/".join([x.strip() for x in q_path.split("/") if x.strip()]),
        "title_prefix": t_prefix,
        "batch_size": int(batch_size),
        "skip_prefixed": bool(skip_prefixed),
        "yes": bool(args.yes),
        "audit_log": audit_log,
        "candidates": candidates,
        "csv_project_id": csv_project_id,
        "csv_suite_id": csv_suite_id,
    }


def run_workflow(cfg: dict) -> int:
    print(f"Running script: {Path(__file__).resolve()}")
    print(f"Mode: {cfg['mode']}")
    print(f"URL: {cfg['url']}")
    print(f"User: {mask_email(cfg['email'])}")
    print(f"CSV: {cfg['input_csv']}")
    print(f"Scope: project_id={cfg['project_id']}, suite_id={cfg['suite_id']}")
    print(f"Batch size (next N unprocessed): {cfg['batch_size']}")

    client = TestRailAPI(cfg["url"], cfg["email"], cfg["token"])
    try:
        project = client.get(f"/get_project/{cfg['project_id']}")
        suite = client.get(f"/get_suite/{cfg['suite_id']}")
        if not isinstance(project, dict) or project.get("id") != cfg["project_id"]:
            raise RuntimeError("Invalid project response")
        if not isinstance(suite, dict) or suite.get("id") != cfg["suite_id"]:
            raise RuntimeError("Invalid suite response")
        if suite.get("project_id") != cfg["project_id"]:
            raise RuntimeError("Suite does not belong to selected project")

        sections = fetch_sections(client, cfg["project_id"], cfg["suite_id"])
        section_ids = {s["id"] for s in sections}

        q_parts = [x.strip() for x in cfg["quarantine_path"].split("/") if x.strip()]
        parent_id = None
        q_section = None
        for part in q_parts:
            q_section = find_child_section(sections, parent_id, part)
            if q_section is None:
                q_section = None
                break
            parent_id = q_section["id"]

        quarantine_exists = q_section is not None
        quarantine_section_id = q_section["id"] if q_section else -1

        live_cases: Dict[int, dict] = {}
        total = len(cfg["candidates"])
        for i, row in enumerate(cfg["candidates"], 1):
            case_obj = client.get(f"/get_case/{row.case_id}")
            validate_case_in_scope(case_obj, row.case_id, cfg["suite_id"], section_ids)
            live_cases[row.case_id] = case_obj
            if i % 25 == 0 or i == total:
                print(f"Validated {i}/{total} candidate cases")

        selected, already_done, partial, unprocessed_all = choose_next_unprocessed(
            cfg["candidates"],
            live_cases,
            quarantine_section_id=quarantine_section_id,
            title_prefix=cfg["title_prefix"],
            batch_size=cfg["batch_size"],
        )

        print("\n--- PLAN ---")
        print(f"Project: {project.get('name')} ({cfg['project_id']})")
        print(f"Suite:   {suite.get('name')} ({cfg['suite_id']})")
        print(f"Target section path: {cfg['quarantine_path']} ({'exists' if quarantine_exists else 'will be created'})")
        print(f"Total CSV candidates: {len(cfg['candidates'])}")
        print(f"Already processed:    {len(already_done)}")
        print(f"Partial state:        {len(partial)}")
        print(f"Unprocessed total:    {len(unprocessed_all)}")
        print(f"Selected this batch:  {len(selected)} (next N unprocessed)\n")

        if partial:
            print("⚠️ Partial cases detected (will be included in unprocessed queue):")
            print("   " + ", ".join([f"C{x}" for x in partial[:20]]) + (" ..." if len(partial) > 20 else ""))

        if not selected:
            print("✅ Nothing to do: no unprocessed candidates left.")
            return 0

        print("\nPreview selected cases:")
        print("-" * 100)
        print(f"{'case_id':>10} | {'section_id':>10} | {'title':<70}")
        print("-" * 100)
        for cid in selected[:50]:
            c = live_cases[cid]
            title = c.get("title", "").replace("\n", " ")
            if len(title) > 70:
                title = title[:67] + "..."
            print(f"{cid:>10} | {c.get('section_id'):>10} | {title:<70}")
        print("-" * 100)

        print("\nWARNING: execution history is not revalidated by cleanup script.")
        print("Use only FINAL reviewed analyzer CSV.")

        if cfg["mode"] == "dry-run":
            print("✅ Dry run completed. No changes made.")
            return 0

        if not cfg["yes"]:
            if not confirm_quarantine():
                return 1
        else:
            print("⚠️ --yes used: interactive confirmation skipped.")

        audit = JsonlAudit(cfg["audit_log"])
        try:
            audit.write("started", {
                "mode": "quarantine",
                "url": cfg["url"],
                "email_masked": mask_email(cfg["email"]),
                "project_id": cfg["project_id"],
                "suite_id": cfg["suite_id"],
                "input_csv": str(Path(cfg["input_csv"]).resolve()),
                "quarantine_path": cfg["quarantine_path"],
                "title_prefix": cfg["title_prefix"],
                "batch_size": cfg["batch_size"],
                "selected_case_ids": selected,
                "already_processed_case_ids": already_done,
                "partial_case_ids": partial,
            })

            backup_path = cfg["audit_log"] + ".backup.json"
            with open(backup_path, "x", encoding="utf-8") as bf:
                json.dump({
                    "timestamp": utc_now(),
                    "project": project,
                    "suite": suite,
                    "sections": sections,
                    "selected_before": {str(cid): live_cases[cid] for cid in selected},
                }, bf, ensure_ascii=False, indent=2)
            audit.write("backup_saved", {"path": backup_path})
            print(f"Backup saved: {backup_path}")

            quarantine_section_id = ensure_quarantine_section(
                client=client,
                project_id=cfg["project_id"],
                suite_id=cfg["suite_id"],
                path=cfg["quarantine_path"],
                sections=sections,
                journal=lambda e, d: audit.write(e, d),
            )
            section_ids.add(quarantine_section_id)

            updated = 0
            skipped = 0

            for idx, cid in enumerate(selected, 1):
                before = client.get(f"/get_case/{cid}")
                validate_case_in_scope(before, cid, cfg["suite_id"], section_ids)

                if case_processed(before, quarantine_section_id, cfg["title_prefix"]):
                    audit.write("skipped_already_processed", {"case_id": cid, "before": before})
                    skipped += 1
                    print(f"⏭️  [{idx}/{len(selected)}] C{cid}: already processed")
                    continue

                if cfg["skip_prefixed"] and str(before.get("title", "")).startswith(cfg["title_prefix"]) and before.get("section_id") != quarantine_section_id:
                    audit.write("skipped_prefixed_not_in_q", {"case_id": cid, "before": before})
                    skipped += 1
                    print(f"⏭️  [{idx}/{len(selected)}] C{cid}: prefixed but not in quarantine section (skipped)")
                    continue

                new_title = before.get("title", "")
                if not new_title.startswith(cfg["title_prefix"]):
                    new_title = cfg["title_prefix"] + new_title

                payload = {"section_id": quarantine_section_id, "title": new_title}
                audit.write("update_intent", {"case_id": cid, "before": before, "payload": payload})

                client.post(f"/update_case/{cid}", payload)

                after = client.get(f"/get_case/{cid}")
                validate_case_in_scope(after, cid, cfg["suite_id"], section_ids)
                if after.get("section_id") != quarantine_section_id or after.get("title") != new_title:
                    raise RuntimeError(f"C{cid}: post-update verification failed")

                audit.write("update_verified", {"case_id": cid, "after": after})
                updated += 1
                print(f"✅ [{idx}/{len(selected)}] C{cid}: updated")

            audit.write("completed", {"updated": updated, "skipped": skipped, "selected": len(selected)})
            print("\n📊 Summary")
            print(f"Selected: {len(selected)}")
            print(f"Updated:  {updated}")
            print(f"Skipped:  {skipped}")
            print(f"Audit:    {cfg['audit_log']}")
            return 0

        except BaseException as e:
            audit.write("stopped", {"error_type": type(e).__name__, "error": str(e)})
            print("❌ Stopped. Some changes may already be applied. Inspect audit and TestRail before retry.")
            raise
        finally:
            audit.close()

    finally:
        client.close()


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    try:
        cfg = resolve_effective_config(args)
    except Exception as e:
        print(f"❌ Config error: {e}")
        return 1

    try:
        return run_workflow(cfg)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except Exception as e:
        print(f"\nError: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())