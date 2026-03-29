from pathlib import Path
import hashlib
import json
import os
import subprocess
import time
from urllib import request, parse
from urllib.error import HTTPError

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
RAW_CHAT_IDS = os.environ.get("TELEGRAM_CHAT_ID", "")
ALLOWED_CHAT_IDS = {
    item.strip()
    for item in RAW_CHAT_IDS.replace("\n", ",").split(",")
    if item.strip()
}
DEFAULT_CHAT_ID = next(iter(ALLOWED_CHAT_IDS), "")
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

ROOT = Path.home() / "work-orchestrator"
HANDOFFS = ROOT / "handoffs"
STATUS = ROOT / "status"
RUNTIME_STATE = ROOT / "runtime" / "state"
LAST_UPDATE_FILE = RUNTIME_STATE / "telegram_last_update.json"
PROCESSED_FILE = RUNTIME_STATE / "telegram_processed.json"

MAX_PROCESSED = 200
GROUP_TASK_PREFIXES = ("task:",)
GROUP_PING_KEYWORDS = ("@jehus_listenerbot", "listenerbot", "리스너봇")


def tg_api(method: str, payload: dict | None = None):
    payload = payload or {}
    data = parse.urlencode(payload).encode()
    req = request.Request(f"{BASE_URL}/{method}", data=data)
    with request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_message(text: str, chat_id: str | int | None = None):
    target_chat_id = chat_id or DEFAULT_CHAT_ID
    if not target_chat_id:
        raise ValueError("No target chat_id configured")
    return tg_api("sendMessage", {"chat_id": str(target_chat_id), "text": text[:4000]})


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_last_update_id() -> int:
    data = load_json(LAST_UPDATE_FILE, {"last_update_id": 0})
    return int(data.get("last_update_id", 0))


def save_last_update_id(update_id: int):
    save_json(LAST_UPDATE_FILE, {"last_update_id": update_id})


def load_processed():
    data = load_json(PROCESSED_FILE, {"processed": []})
    return data.get("processed", [])


def save_processed(items):
    save_json(PROCESSED_FILE, {"processed": items[-MAX_PROCESSED:]})


def get_updates(offset: int):
    return tg_api("getUpdates", {"timeout": 20, "offset": offset})


def extract_message(update: dict) -> dict | None:
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", "")).strip()
    chat_type = str(chat.get("type", "")).strip()
    text = (message.get("text") or "").strip()

    if not chat_id or not text:
        return None
    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        return None

    return {
        "chat_id": chat_id,
        "chat_type": chat_type,
        "text": text,
    }


def make_message_key(update_id: int, text: str) -> str:
    return hashlib.sha1(f"{update_id}:{text}".encode("utf-8", errors="ignore")).hexdigest()


def enqueue_task(update_id: int, text: str) -> str:
    task_id = f"tg-{update_id}"
    HANDOFFS.mkdir(parents=True, exist_ok=True)
    STATUS.mkdir(parents=True, exist_ok=True)

    handoff = (
        f"# {task_id}\n\n"
        "## Project\n"
        "onedrive-work\n\n"
        "## Workspace\n"
        "/home/ubuntu/onedrive\n\n"
        "## UserPrompt\n"
        f"{text}\n"
    )
    (HANDOFFS / f"{task_id}.md").write_text(handoff, encoding="utf-8")

    status = {
        "task_id": task_id,
        "project": "onedrive-work",
        "workspace": "/home/ubuntu/onedrive",
        "owner": "claude-worker",
        "status": "queued",
        "branch": None,
        "handoff_file": str(HANDOFFS / f"{task_id}.md"),
        "result_file": None,
        "review_summary": None,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (STATUS / f"{task_id}.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    return task_id


def launch_worker(task_id: str):
    subprocess.Popen(
        ["python3", str(ROOT / "orchestrator" / "claude_job_runner.py"), task_id],
        cwd=str(ROOT),
        start_new_session=True,
    )


def is_group_chat(chat_type: str) -> bool:
    return chat_type in {"group", "supergroup"}


def is_group_ping(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in GROUP_PING_KEYWORDS)


def is_group_task(text: str) -> bool:
    lowered = text.lower()
    return lowered.startswith(GROUP_TASK_PREFIXES)


def main():
    last_update_id = load_last_update_id()
    processed = load_processed()

    while True:
        try:
            updates = get_updates(last_update_id + 1)
            for update in updates.get("result", []):
                update_id = update["update_id"]
                save_last_update_id(update_id)
                last_update_id = update_id

                message = extract_message(update)
                if not message:
                    continue

                text = message["text"]
                chat_id = message["chat_id"]
                chat_type = message["chat_type"]

                message_key = make_message_key(update_id, text)
                if message_key in processed:
                    continue

                processed.append(message_key)
                save_processed(processed)

                if is_group_chat(chat_type):
                    if is_group_ping(text):
                        send_message("listenerbot online", chat_id)
                        continue
                    if not is_group_task(text):
                        continue

                task_id = enqueue_task(update_id, text)
                send_message(f"📋 작업 접수: {task_id}\n곧 처리합니다.", chat_id)
                launch_worker(task_id)

        except KeyboardInterrupt:
            raise
        except HTTPError as e:
            if getattr(e, "code", None) == 409:
                time.sleep(2)
                continue
            try:
                send_message(f"리스너 오류: {type(e).__name__}: {str(e)[:300]}")
            except Exception:
                pass
            time.sleep(5)
        except Exception as e:
            try:
                send_message(f"리스너 오류: {type(e).__name__}: {str(e)[:300]}")
            except Exception:
                pass
            time.sleep(5)


if __name__ == "__main__":
    main()
