#!/usr/bin/env python3
"""
news.summary 빈 건 보강 스크립트

2026-03~05 수집분 589건은 summary가 빈 문자열이다. news 테이블에는 본문 컬럼이
없으므로 url(외부 기사 원문 주소)을 다시 크롤링해 gpt-4o-mini로 요약을 만든다.
요약 생성 방식은 기존 수집 파이프라인(supabase/daily_news_update.py)과 동일하게 맞췄다.

  python3 enrich_news_summary.py --dry-run --limit 10
  python3 enrich_news_summary.py
"""
import json
import os
import re
import sys
import time
import html as htmllib
import urllib.error
import urllib.parse
import urllib.request

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://mewqgevgdgghhatqtuos.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")

BATCH_SIZE = 50
MAX_SUMMARY = 1000
MIN_ARTICLE = 200
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


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


def decode_body(raw, headers):
    charset = None
    ctype = headers.get("Content-Type", "")
    m = re.search(r'charset=["\']?([\w-]+)', ctype, re.I)
    if m:
        charset = m.group(1)
    if not charset:
        m = re.search(rb'charset=["\']?([\w-]+)', raw[:2000], re.I)
        if m:
            charset = m.group(1).decode("ascii", "ignore")
    for enc in [charset, "utf-8", "euc-kr", "cp949"]:
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="ignore")


def fetch_article_text(url):
    """기사 URL에서 본문 텍스트 추출 (daily_news_update.py 와 동일한 단순 방식)"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read(600000)
        html = decode_body(raw, resp.headers)
    html = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.S | re.I)
    html = re.sub(r'<style[^>]*>.*?</style>', ' ', html, flags=re.S | re.I)
    html = re.sub(r'<!--.*?-->', ' ', html, flags=re.S)
    text = re.sub(r'<[^>]+>', ' ', html)
    text = htmllib.unescape(text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:3000]


def generate_summary(title, article_text):
    prompt = f"""다음 뉴스 기사를 2~3문장으로 핵심만 요약하세요.

제목: {title}
본문:
{article_text[:2000]}

요약:"""
    body = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 200,
        "temperature": 0.2,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {OPENAI_KEY}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"].strip()


def main():
    dry_run = "--dry-run" in sys.argv
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    if not SUPABASE_KEY or not OPENAI_KEY:
        print("SUPABASE_SERVICE_KEY / OPENAI_API_KEY 없음", file=sys.stderr)
        return 1

    _, cr = supabase_get("news", {"summary": "eq.", "select": "id", "limit": 1})
    total = int(cr.split("/")[-1]) if cr else 0
    print(f"=== news.summary 빈 건: {total} ===", flush=True)

    updated = crawl_fail = llm_fail = 0
    # 보강 성공 행은 결과집합(summary=eq.)에서 빠지므로 실패한 만큼만 offset 을 전진시킨다
    offset = 0

    while True:
        if limit is not None and updated + crawl_fail + llm_fail >= limit:
            break
        rows, _ = supabase_get("news", {
            "summary": "eq.",
            "select": "id,title,url,published_at",
            "order": "published_at.desc",
            "offset": offset,
            "limit": BATCH_SIZE,
        })
        if not rows:
            break

        batch_stuck = 0
        for row in rows:
            if limit is not None and updated + crawl_fail + llm_fail >= limit:
                break
            title = (row.get("title") or "").strip()
            url = (row.get("url") or "").strip()
            if not url.startswith("http"):
                crawl_fail += 1
                batch_stuck += 1
                print(f"  - [url없음] {title[:40]}", flush=True)
                continue
            try:
                text = fetch_article_text(url)
            except Exception as e:
                crawl_fail += 1
                batch_stuck += 1
                print(f"  x [크롤실패] {title[:40]} :: {type(e).__name__} {e}", flush=True)
                continue
            if len(text) < MIN_ARTICLE:
                crawl_fail += 1
                batch_stuck += 1
                print(f"  x [본문부족 {len(text)}자] {title[:40]}", flush=True)
                continue
            try:
                summary = generate_summary(title, text)
            except Exception as e:
                llm_fail += 1
                batch_stuck += 1
                print(f"  x [요약실패] {title[:40]} :: {type(e).__name__} {e}", flush=True)
                time.sleep(2)
                continue
            summary = summary.strip()[:MAX_SUMMARY]
            if len(summary) < 20:
                llm_fail += 1
                batch_stuck += 1
                continue
            if dry_run:
                print(f"  DRY {title[:40]}\n      -> {summary[:120]}", flush=True)
                batch_stuck += 1
            else:
                try:
                    supabase_patch("news", row["id"], {"summary": summary})
                except Exception as e:
                    print(f"  x [저장실패] {row['id']} :: {e}", flush=True)
                    batch_stuck += 1
                    continue
            updated += 1
            if updated % 20 == 0:
                print(f"  ... 진행 {updated}건 (크롤실패 {crawl_fail}, 요약실패 {llm_fail})",
                      flush=True)
            time.sleep(0.4)

        offset += batch_stuck
        if batch_stuck == len(rows) and not rows:
            break

    print(f"=== 완료: 보강 {updated} / 크롤실패 {crawl_fail} / 요약실패 {llm_fail} ===",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
