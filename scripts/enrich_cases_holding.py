#!/usr/bin/env python3
"""
cases 테이블 holding_points 빈 건 보강 스크립트
1단계: law.go.kr API에서 판례내용 가져와서 판시사항/판결요지 파싱
2단계: 파싱 실패 시 본문에서 핵심 추출
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

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://mewqgevgdgghhatqtuos.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
LAW_OC = "iceamericano9"

BATCH_SIZE = 50
SLEEP_BETWEEN = 1.0  # law.go.kr rate limit

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
        with urllib.request.urlopen(req, timeout=30) as resp:
            count = resp.headers.get("content-range", "")
            data = json.loads(resp.read().decode())
            return data, count
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
    urllib.request.urlopen(req, timeout=30)

def extract_id_from_url(url):
    m = re.search(r'ID=(\d+)', url)
    return m.group(1) if m else None

def fetch_law_content(prec_id):
    url = f"https://www.law.go.kr/DRF/lawService.do?OC={LAW_OC}&target=prec&ID={prec_id}&type=JSON"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            if "PrecService" in data:
                return data["PrecService"]
    except Exception as e:
        print(f"  API error for ID={prec_id}: {e}", file=sys.stderr)
    return None

def clean_html(text):
    if not text:
        return ""
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    text = unescape(text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def parse_holding_from_content(content_text):
    """판례내용에서 【판시사항】【판결요지】 섹션 추출"""
    clean = clean_html(content_text)

    sections = []

    # 【판시사항】 추출
    m = re.search(r'【\s*판시사항\s*】\s*(.*?)(?=【|$)', clean, re.S)
    if m and m.group(1).strip():
        sections.append(f"[판시사항] {m.group(1).strip()}")

    # 【판결요지】 추출
    m = re.search(r'【\s*판결요지\s*】\s*(.*?)(?=【|$)', clean, re.S)
    if m and m.group(1).strip():
        sections.append(f"[판결요지] {m.group(1).strip()}")

    # 【이유】에서 핵심 추출 (판시사항/판결요지가 없을 때)
    if not sections:
        m = re.search(r'【\s*이\s*유\s*】\s*(.*?)(?=【|$)', clean, re.S)
        if m and m.group(1).strip():
            reason = m.group(1).strip()
            # 이유 전체가 너무 길면 앞 2000자만
            if len(reason) > 2000:
                reason = reason[:2000] + "..."
            sections.append(f"[이유] {reason}")

    # 아무것도 없으면 본문 앞부분
    if not sections and clean:
        excerpt = clean[:2000] + ("..." if len(clean) > 2000 else "")
        sections.append(f"[본문발췌] {excerpt}")

    return "\n\n".join(sections)

def main():
    if not SUPABASE_KEY:
        print("SUPABASE_SERVICE_KEY 환경변수 필요", file=sys.stderr)
        sys.exit(1)

    # 대상 조회
    offset = 0
    total_updated = 0
    total_failed = 0
    total_no_content = 0

    while True:
        rows, count_header = supabase_get("cases", {
            "select": "id,url,original_url,case_number,title",
            "holding_points": "eq.",
            "original_url": "like.*law.go.kr*",
            "limit": BATCH_SIZE,
            "offset": offset,
            "order": "id.asc",
        })

        if not rows:
            break

        print(f"\n--- 배치 {offset // BATCH_SIZE + 1} ({len(rows)}건) ---", flush=True)
        updated_before = total_updated

        for row in rows:
            case_id = row["id"]
            # url은 2026-08 내부 URL 마이그레이션 이후 노란봉투법.com을 가리킨다.
            # 법제처 원본 ID는 original_url에만 남아 있다.
            url = row.get("original_url") or row["url"]
            prec_id = extract_id_from_url(url)

            if not prec_id:
                print(f"  [{case_id}] URL에서 ID 추출 실패: {url}")
                total_failed += 1
                continue

            prec = fetch_law_content(prec_id)
            time.sleep(SLEEP_BETWEEN)

            if not prec:
                print(f"  [{case_id}] API 응답 없음 (ID={prec_id})")
                total_no_content += 1
                continue

            # 판시사항/판결요지 직접 필드
            issue = clean_html(prec.get("판시사항", ""))
            summary_text = clean_html(prec.get("판결요지", ""))
            content = prec.get("판례내용", "")

            if issue or summary_text:
                parts = []
                if issue:
                    parts.append(f"[판시사항] {issue}")
                if summary_text:
                    parts.append(f"[판결요지] {summary_text}")
                holding = "\n\n".join(parts)
            else:
                holding = parse_holding_from_content(content)

            if not holding:
                print(f"  [{case_id}] 파싱 결과 없음 (ID={prec_id})")
                total_no_content += 1
                continue

            # decision_date도 같이 채우기
            patch = {"holding_points": holding}
            decision_date_raw = prec.get("선고일자", "")
            if decision_date_raw and len(decision_date_raw) == 8:
                patch["decision_date"] = f"{decision_date_raw[:4]}-{decision_date_raw[4:6]}-{decision_date_raw[6:8]}"

            try:
                supabase_patch("cases", case_id, patch)
                total_updated += 1
                print(f"  [{case_id}] ✓ {row['case_number']} — {len(holding)}자")
            except Exception as e:
                print(f"  [{case_id}] PATCH 실패: {e}")
                total_failed += 1

        # 보강에 성공한 행은 holding_points=eq. 필터에서 빠져나가므로,
        # 남은(미보강) 건수만큼만 offset을 전진시켜야 다음 배치가 건너뛰지 않는다.
        offset += len(rows) - (total_updated - updated_before)

    print(f"\n=== 완료 ===")
    print(f"업데이트: {total_updated}건")
    print(f"실패: {total_failed}건")
    print(f"내용없음: {total_no_content}건")

if __name__ == "__main__":
    main()
