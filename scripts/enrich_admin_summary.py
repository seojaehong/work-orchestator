#!/usr/bin/env python3
"""
admin_interpretations.summary 빈 건 보강 스크립트

빈 summary 행들은 holding_points에 원문(제목/번호/회시일자 헤더 + 【질 의】/【회 시】)을
그대로 담고 있다. 외부 크롤링 없이 그 안의 질의 부분을 뽑아 summary로 채운다.
(기존 정상 행들의 summary도 질의 본문 형식이라 포맷이 일치한다)

  python3 enrich_admin_summary.py            # 실행
  python3 enrich_admin_summary.py --dry-run  # 쓰지 않고 추출 결과만 출력
  python3 enrich_admin_summary.py --limit 50
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://mewqgevgdgghhatqtuos.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

BATCH_SIZE = 100
MAX_SUMMARY = 1200
MIN_SUMMARY = 15

# 【질 의】 ~ 【회 시】 사이
RE_QUESTION = re.compile(
    r'【\s*질\s*의\s*】(.*?)(?=【\s*회\s*[시답]\s*】|【\s*회\s*신\s*】|$)', re.S)
# 헤더 블록: "... 회시일자 \n 2019-03-21" 까지가 크롤링 껍데기
RE_HEADER = re.compile(r'회\s*시\s*일\s*자\s*\n?\s*\d{4}[-.]\d{1,2}[-.]\d{1,2}', re.S)


def supabase_get(path, params=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, safe="*,.()")
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Prefer": "count=exact",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode()), resp.headers.get("content-range", "")
    except urllib.error.HTTPError as e:
        if e.code == 416:
            return [], ""
        raise


def supabase_patch(table, id_val, payload):
    encoded_id = urllib.parse.quote(str(id_val), safe="")
    url = f"{SUPABASE_URL}/rest/v1/{table}?id=eq.{encoded_id}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="PATCH", headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json; charset=utf-8",
        "Prefer": "return=minimal",
    })
    urllib.request.urlopen(req, timeout=60)


def tidy(text):
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n', text)
    return text.strip()


def truncate(text):
    if len(text) <= MAX_SUMMARY:
        return text
    cut = text[:MAX_SUMMARY]
    # 문장 경계에서 자른다
    for sep in ('\n', '. ', '함 ', '음 ', '부 '):
        idx = cut.rfind(sep)
        if idx > MAX_SUMMARY * 0.6:
            return cut[:idx + len(sep)].strip()
    return cut.strip()


def extract_summary(row):
    """(summary, method) — 뽑을 수 없으면 (None, 사유)"""
    hp = row.get("holding_points") or ""
    if not hp.strip():
        return None, "holding_points 없음"

    m = RE_QUESTION.search(hp)
    if m:
        body = tidy(m.group(1))
        if len(body) >= MIN_SUMMARY:
            return truncate(body), "question"

    # 질의 마커가 없는 통보·회신 유형: 헤더 블록을 걷어내고 본문 앞부분을 쓴다
    h = RE_HEADER.search(hp)
    if h:
        body = tidy(hp[h.end():])
        # 제목이 본문 앞에 한 번 더 반복되는 경우 제거
        title = (row.get("title") or "").strip()
        if title and body.startswith(title):
            body = body[len(title):].strip()
        if len(body) >= MIN_SUMMARY:
            return truncate(body), "body"

    if len(hp.strip()) < 200 and "원문 링크" in hp:
        # 수집 단계에서 본문을 못 가져오고 링크만 남은 행 — 여기서는 손댈 수 없다
        return None, "원문 링크만 있음(수집 미완)"
    return None, "질의·본문 추출 실패"


def main():
    dry_run = "--dry-run" in sys.argv
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    if not SUPABASE_KEY:
        print("SUPABASE_SERVICE_KEY 없음", file=sys.stderr)
        return 1

    _, cr = supabase_get("admin_interpretations", {
        "summary": "eq.", "select": "id", "limit": 1})
    total = int(cr.split("/")[-1]) if cr else 0
    print(f"=== admin_interpretations.summary 빈 건: {total} ===", flush=True)

    updated = failed = skipped = 0
    methods = {}
    offset = 0  # 보강 성공 행은 결과집합에서 빠지므로, 실패한 만큼만 전진시킨다

    while True:
        if limit is not None and updated + skipped >= limit:
            break
        rows, _ = supabase_get("admin_interpretations", {
            "summary": "eq.",
            "select": "id,title,holding_points",
            "order": "id",
            "offset": offset,
            "limit": BATCH_SIZE,
        })
        if not rows:
            break

        stuck = 0
        for row in rows:
            if limit is not None and updated + skipped >= limit:
                break
            summary, method = extract_summary(row)
            if not summary:
                print(f"  [{row['id']}] ⏭ {method}", flush=True)
                skipped += 1
                stuck += 1
                continue
            if dry_run:
                print(f"  [{row['id']}] ({method}) {summary[:120]}", flush=True)
                updated += 1
                stuck += 1  # dry-run은 쓰지 않으므로 그대로 남는다
                methods[method] = methods.get(method, 0) + 1
                continue
            try:
                supabase_patch("admin_interpretations", row["id"], {"summary": summary})
                updated += 1
                methods[method] = methods.get(method, 0) + 1
                if updated % 50 == 0:
                    print(f"  ... {updated}건 완료", flush=True)
            except Exception as e:
                print(f"  [{row['id']}] ❌ {e}", flush=True)
                failed += 1
                stuck += 1

        offset += stuck
        if stuck == len(rows):
            # 이 배치에서 하나도 못 고쳤으면 다음 배치로 넘어간다
            continue

    print(f"\n=== 완료: 업데이트 {updated}, 추출실패 {skipped}, 오류 {failed} ===")
    print(f"    방식별: {methods}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
