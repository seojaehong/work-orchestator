from pathlib import Path
from datetime import datetime
import json
import time
import re

ROOT = Path.home() / "work-orchestrator"
HANDOFFS = ROOT / "handoffs"
RESULTS = ROOT / "results"
STATUS = ROOT / "status"
PROJECTS = ROOT / "projects"

POLL_SECONDS = 30

def now_iso():
    return datetime.utcnow().isoformat() + "Z"

def list_handoffs():
    return sorted(HANDOFFS.glob("*.md"))

def list_results():
    return sorted(RESULTS.glob("*.md"))

def status_path_for(task_id: str) -> Path:
    return STATUS / f"{task_id}.json"

def result_path_for(task_id: str) -> Path:
    return RESULTS / f"{task_id}.md"

def load_status(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

def save_status(task_id: str, payload: dict):
    path = status_path_for(task_id)
    payload["updated_at"] = now_iso()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

def parse_project_from_handoff(handoff_path: Path) -> str:
    try:
        text = handoff_path.read_text(encoding="utf-8")
    except Exception:
        return "unknown"

    match = re.search(r"^##\s*Project\s*$\n(.+)$", text, flags=re.MULTILINE)
    if match:
        return match.group(1).strip()

    match_inline = re.search(r"^##\s*Project\s*:?[\t ]*(.+)$", text, flags=re.MULTILINE)
    if match_inline:
        return match_inline.group(1).strip()

    return "unknown"

def ensure_status_for_handoffs():
    created = 0
    updated = 0

    for handoff in list_handoffs():
        task_id = handoff.stem
        status_path = status_path_for(task_id)
        parsed_project = parse_project_from_handoff(handoff)

        if not status_path.exists():
            payload = {
                "task_id": task_id,
                "project": parsed_project,
                "owner": "unassigned",
                "status": "queued",
                "branch": None,
                "handoff_file": str(handoff),
                "result_file": None,
                "review_summary": None,
            }
            save_status(task_id, payload)
            created += 1
            continue

        existing = load_status(status_path)
        if not existing:
            payload = {
                "task_id": task_id,
                "project": parsed_project,
                "owner": "unassigned",
                "status": "queued",
                "branch": None,
                "handoff_file": str(handoff),
                "result_file": None,
                "review_summary": None,
            }
            save_status(task_id, payload)
            updated += 1
            continue

        changed = False
        if existing.get("project") in (None, "", "unknown") and parsed_project != "unknown":
            existing["project"] = parsed_project
            changed = True

        if existing.get("handoff_file") != str(handoff):
            existing["handoff_file"] = str(handoff)
            changed = True

        if changed:
            save_status(task_id, existing)
            updated += 1

    return created, updated

def sync_results_to_status():
    updated = 0

    for result in list_results():
        task_id = result.stem
        status_path = status_path_for(task_id)
        existing = load_status(status_path)

        if not existing:
            payload = {
                "task_id": task_id,
                "project": "unknown",
                "owner": "unassigned",
                "status": "done",
                "branch": None,
                "handoff_file": None,
                "result_file": str(result),
                "review_summary": "result file detected",
            }
            save_status(task_id, payload)
            updated += 1
            continue

        changed = False
        if existing.get("result_file") != str(result):
            existing["result_file"] = str(result)
            changed = True

        if existing.get("status") == "queued":
            existing["status"] = "done"
            changed = True

        if not existing.get("review_summary"):
            existing["review_summary"] = "result file detected"
            changed = True

        if changed:
            save_status(task_id, existing)
            updated += 1

    return updated

def summarize_status():
    counts = {}
    items = []
    for path in sorted(STATUS.glob("*.json")):
        data = load_status(path)
        if not data:
            counts["invalid"] = counts.get("invalid", 0) + 1
            continue
        st = data.get("status", "unknown")
        counts[st] = counts.get(st, 0) + 1
        items.append(data)
    return counts, items

def print_summary(created_count: int, handoff_updates: int, result_updates: int):
    counts, items = summarize_status()
    print("=== Work Orchestrator ===")
    print(f"root: {ROOT}")
    print(f"handoffs: {len(list_handoffs())}")
    print(f"results: {len(list_results())}")
    print(f"status files: {len(list(items))}")
    print(f"new status created this tick: {created_count}")
    print(f"handoff sync updates this tick: {handoff_updates}")
    print(f"result sync updates this tick: {result_updates}")
    print(f"status summary: {counts}")
    print(f"timestamp: {now_iso()}")
    print("-" * 40)

def main():
    print("orchestrator loop started")
    while True:
        created, handoff_updates = ensure_status_for_handoffs()
        result_updates = sync_results_to_status()
        print_summary(created, handoff_updates, result_updates)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
