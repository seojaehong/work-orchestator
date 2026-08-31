#!/usr/bin/env python3
"""
ilabor cases의 빈 holding_points 보강
bigcase.ai에서 사건번호+법원으로 검색하여 본문 추출
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

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
BATCH_SIZE = 50
CRAWL_DELAY = 60

def supabase_get(table, params):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{urllib.parse.urlencode(params, safe='*,.()')}"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 416: return []
        raise

def supabase_patch(table, id_val, payload):
    encoded_id = urllib.parse.quote(str(id_val), safe="")
    url = f"{SUPABASE_URL}/rest/v1/{table}?id=eq.{encoded_id}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="PATCH", headers={
        "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json; charset=utf-8", "Prefer": "return=minimal",
    })
    urllib.request.urlopen(req, timeout=30)

def clean_html(text):
    if not text: return ""
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    text = unescape(text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def guess_court(case_number):
    """사건번호에서 법원 추측"""
    if "헌" in case_number:
        return "헌법재판소"
    suffixes_supreme = ["두", "도", "다"]
    for s in suffixes_supreme:
        if re.search(rf'\d{s}', case_number):
            return "대법원"
    if "구합" in case_number or "가합" in case_number:
        return None
    if "누" in case_number:
        return "대법원"
    return "대법원"

COURT_FALLBACKS = ["대법원", "서울고등법원", "서울중앙지방법원"]

def fetch_from_bigcase(case_number, court_hint=None):
    courts_to_try = [court_hint] if court_hint else []
    courts_to_try.extend([c for c in COURT_FALLBACKS if c != court_hint])

    for court in courts_to_try:
        if not court:
            continue
        encoded_court = urllib.parse.quote(court, safe="")
        encoded_case = urllib.parse.quote(case_number, safe="")
        url = f"https://bigcase.ai/cases/{encoded_court}/{encoded_case}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                html = resp.read().decode("utf-8", errors="replace")
                m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
                if not m:
                    continue
                data = json.loads(m.group(1))
                detail = data.get("props", {}).get("pageProps", {}).get("caseDetail", {})
                if detail and detail.get("case_number"):
                    return detail
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise
        except Exception:
            continue
    return None

def extract_holding(detail):
    parts = []
    ai = detail.get("ai_full_summary_md", "") or ""
    ft = detail.get("fulltext", {}) or {}
    reasoning = clean_html(ft.get("reasoning", "") or "")

    if reasoning:
        issue_m = re.search(r'【\s*판시사항\s*】\s*(.*?)(?=【|$)', reasoning, re.S)
        verdict_m = re.search(r'【\s*판결요지\s*】\s*(.*?)(?=【|$)', reasoning, re.S)
        reason_m = re.search(r'【\s*이\s*유\s*】\s*(.*?)(?=【|$)', reasoning, re.S)

        if issue_m and issue_m.group(1).strip():
            parts.append(f"[판시사항] {issue_m.group(1).strip()}")
        if verdict_m and verdict_m.group(1).strip():
            parts.append(f"[판결요지] {verdict_m.group(1).strip()}")
        if not issue_m and not verdict_m:
            if reason_m and reason_m.group(1).strip():
                parts.append(f"[이유] {reason_m.group(1).strip()[:2000]}")
            elif reasoning:
                parts.append(f"[본문발췌] {reasoning[:2000]}")

    if not parts and ai:
        parts.append(f"[AI요약] {clean_html(ai)[:2000]}")

    return "\n\n".join(parts)

def main():
    print("=== ilabor cases holding_points 보강 (bigcase.ai) ===", flush=True)
    offset = 0
    updated = 0
    not_found = 0
    failed = 0

    while True:
        rows = supabase_get("cases", {
            "select": "id,case_number,title",
            "holding_points": "eq.",
            "original_url": "like.*ilabor*",
            "limit": BATCH_SIZE,
            "offset": offset,
            "order": "id.asc",
        })
        if not rows: break

        print(f"\n--- 배치 {offset // BATCH_SIZE + 1} ({len(rows)}건) ---", flush=True)
        updated_before = updated

        for row in rows:
            case_id = row["id"]
            case_num = row["case_number"]
            court = guess_court(case_num)
            print(f"  [{case_id}] {case_num} ({court})...", end=" ", flush=True)

            detail = fetch_from_bigcase(case_num, court)
            if not detail:
                print("❌ 미발견", flush=True)
                not_found += 1
                time.sleep(CRAWL_DELAY)
                continue

            holding = extract_holding(detail)
            if not holding:
                print("⚠️ 내용 없음", flush=True)
                not_found += 1
                time.sleep(CRAWL_DELAY)
                continue

            try:
                supabase_patch("cases", case_id, {"holding_points": holding})
                updated += 1
                print(f"✓ {len(holding)}자", flush=True)
            except Exception as e:
                print(f"❌ {e}", flush=True)
                failed += 1

            time.sleep(CRAWL_DELAY)

        # 보강에 성공한 행은 holding_points=eq. 필터에서 빠져나가므로,
        # 남은(미보강) 건수만큼만 offset을 전진시켜야 다음 배치가 건너뛰지 않는다.
        offset += len(rows) - (updated - updated_before)

    print(f"\n=== 완료: 업데이트 {updated}, 미발견 {not_found}, 실패 {failed} ===", flush=True)

if __name__ == "__main__":
    main()
