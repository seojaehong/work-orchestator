#!/usr/bin/env python3
"""
bigcase.ai 로그인 후 cases 테이블의 빈 holding_points를 크롤링으로 채우기
__NEXT_DATA__ JSON에서 fulltext.reasoning 추출
60초 딜레이로 rate limit 준수
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from http.cookiejar import CookieJar

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

BATCH_SIZE = 50
CRAWL_DELAY = 60

def encode_bigcase_url(raw_url):
    """bigcase.ai URL의 한글/공백을 올바르게 퍼센트 인코딩"""
    parsed = urllib.parse.urlparse(raw_url)
    path_parts = parsed.path.split("/")
    encoded_parts = [urllib.parse.quote(p, safe="") for p in path_parts]
    encoded_path = "/".join(encoded_parts)
    return urllib.parse.urlunparse((
        parsed.scheme, parsed.netloc, encoded_path,
        parsed.params, parsed.query, parsed.fragment,
    ))

def fetch_case_page(url):
    encoded_url = encode_bigcase_url(url)
    req = urllib.request.Request(encoded_url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"    fetch error: {e}", flush=True)
        return None

def extract_next_data(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None

def clean_html(text):
    if not text:
        return ""
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    text = unescape(text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def extract_holding_from_next_data(data):
    try:
        props = data.get("props", {}).get("pageProps", {})
        detail = props.get("caseDetail", {})

        parts = []

        # AI 요약
        ai_summary = detail.get("ai_full_summary_md", "") or ""
        if ai_summary and len(ai_summary) > 50:
            parts.append(f"[AI요약] {clean_html(ai_summary)}")

        # 판결 전문 — reasoning
        fulltext = detail.get("fulltext", {}) or {}
        reasoning = fulltext.get("reasoning", "") or ""
        if reasoning:
            cleaned = clean_html(reasoning)
            # 【판시사항】【판결요지】 추출
            issue_m = re.search(r'【\s*판시사항\s*】\s*(.*?)(?=【|$)', cleaned, re.S)
            verdict_m = re.search(r'【\s*판결요지\s*】\s*(.*?)(?=【|$)', cleaned, re.S)
            reason_m = re.search(r'【\s*이\s*유\s*】\s*(.*?)(?=【|$)', cleaned, re.S)

            if issue_m and issue_m.group(1).strip():
                parts.append(f"[판시사항] {issue_m.group(1).strip()}")
            if verdict_m and verdict_m.group(1).strip():
                parts.append(f"[판결요지] {verdict_m.group(1).strip()}")

            if not issue_m and not verdict_m:
                if reason_m and reason_m.group(1).strip():
                    reason_text = reason_m.group(1).strip()
                    if len(reason_text) > 2000:
                        reason_text = reason_text[:2000] + "..."
                    parts.append(f"[이유] {reason_text}")
                elif cleaned:
                    excerpt = cleaned[:2000] + ("..." if len(cleaned) > 2000 else "")
                    parts.append(f"[본문발췌] {excerpt}")

        # fulltext가 없으면 AI 요약만이라도
        if not parts and ai_summary:
            parts.append(f"[AI요약] {clean_html(ai_summary)[:2000]}")

        return "\n\n".join(parts) if parts else ""
    except Exception as e:
        print(f"    parse error: {e}", flush=True)
        return ""

def supabase_get(table, params):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{urllib.parse.urlencode(params, safe='*,.()')}"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 416:
            return []
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
    urllib.request.urlopen(req, timeout=30)

def main():
    print("🔍 bigcase.ai 크롤링 시작 (비인증, 60초 딜레이)", flush=True)

    offset = 0
    total_updated = 0
    total_failed = 0
    total_empty = 0

    while True:
        rows = supabase_get("cases", {
            "select": "id,url,original_url,case_number,title",
            "holding_points": "eq.",
            "original_url": "like.*bigcase.ai*",
            "limit": BATCH_SIZE,
            "offset": offset,
            "order": "id.asc",
        })

        if not rows:
            break

        print(f"\n--- 배치 {offset // BATCH_SIZE + 1} ({len(rows)}건) ---", flush=True)
        updated_before = total_updated

        for row in rows:
            # url은 내부 URL 마이그레이션 이후 노란봉투법.com이므로
            # 크롤링 대상은 original_url을 써야 한다.
            case_url = row.get("original_url") or row["url"]
            case_id = row["id"]

            print(f"  [{case_id}] {row['case_number']}...", end=" ", flush=True)

            html = fetch_case_page(case_url)
            if not html:
                print("❌ fetch 실패", flush=True)
                total_failed += 1
                time.sleep(CRAWL_DELAY)
                continue

            next_data = extract_next_data(html)
            if not next_data:
                print("❌ __NEXT_DATA__ 없음", flush=True)
                total_failed += 1
                time.sleep(CRAWL_DELAY)
                continue

            holding = extract_holding_from_next_data(next_data)
            if not holding:
                print("⚠️ 내용 없음", flush=True)
                total_empty += 1
                time.sleep(CRAWL_DELAY)
                continue

            try:
                supabase_patch("cases", case_id, {"holding_points": holding})
                total_updated += 1
                print(f"✓ {len(holding)}자", flush=True)
            except Exception as e:
                print(f"❌ PATCH: {e}", flush=True)
                total_failed += 1

            time.sleep(CRAWL_DELAY)

        # 보강에 성공한 행은 holding_points=eq. 필터에서 빠져나가므로,
        # 남은(미보강) 건수만큼만 offset을 전진시켜야 다음 배치가 건너뛰지 않는다.
        offset += len(rows) - (total_updated - updated_before)

    print(f"\n=== 완료 ===", flush=True)
    print(f"업데이트: {total_updated}건", flush=True)
    print(f"실패: {total_failed}건", flush=True)
    print(f"내용없음: {total_empty}건", flush=True)

if __name__ == "__main__":
    main()
