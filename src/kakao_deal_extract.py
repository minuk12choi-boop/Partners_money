#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kakao_deal_extract.py

PC 카카오톡에서 Ctrl+S로 내보낸 대화 txt를 읽어
쿠팡 상품 링크를 추출하고, 단축링크를 실제 상품 URL로 확정한 뒤
중복을 걸러 pending.csv 로 내보낸다.

사용법:
    python kakao_deal_extract.py export.txt
    python kakao_deal_extract.py export.txt --no-resolve   # 네트워크 없이 파싱만
    python kakao_deal_extract.py --list-pending            # 미발행 목록 보기
    python kakao_deal_extract.py --mark-posted 1234567     # 발행 완료 처리

의존성:
    pip install requests
"""

import argparse
import csv
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

try:
    import requests
except ImportError:
    requests = None

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deals.db")
CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pending.csv")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# ---------------------------------------------------------------- 정규식

# 쿠팡 링크 형태 전부
RE_COUPANG = re.compile(
    r"https?://(?:"
    r"link\.coupang\.com/[^\s\)\]\}<>\"']+"
    r"|coupa\.ng/[^\s\)\]\}<>\"']+"
    r"|(?:www\.|m\.)?coupang\.com/(?:vp|vm)/products/[^\s\)\]\}<>\"']+"
    r")",
    re.IGNORECASE,
)

# PC 카톡: [닉네임] [오후 3:24] 메시지
RE_PC_LINE = re.compile(r"^\[(?P<who>[^\]]+)\]\s*\[(?P<when>[^\]]+)\]\s*(?P<msg>.*)$")
# 모바일 카톡: 2026년 7월 28일 오후 3:24, 닉네임 : 메시지
RE_MO_LINE = re.compile(r"^(?P<when>\d{4}년 \d{1,2}월 \d{1,2}일 [^,]+),\s*(?P<who>[^:]+)\s*:\s*(?P<msg>.*)$")
# 날짜 구분선
RE_DATE_SEP = re.compile(r"^-{3,}\s*(?P<date>\d{4}년 \d{1,2}월 \d{1,2}일[^-]*?)\s*-{3,}$")

RE_PRICE = re.compile(r"([0-9][0-9,]{2,})\s*원")
RE_PRODUCT_ID = re.compile(r"/(?:vp|vm)/products/(\d+)")


# ---------------------------------------------------------------- DB

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS deals (
            product_id   TEXT PRIMARY KEY,
            title        TEXT,
            price        INTEGER,
            source_url   TEXT,
            product_url  TEXT,
            raw_message  TEXT,
            chat_time    TEXT,
            found_at     TEXT,
            affiliate_url TEXT,
            posted_at    TEXT
        )
    """)
    # 해석 실패한 단축링크도 기억해서 매번 재시도하지 않게
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_urls (
            url        TEXT PRIMARY KEY,
            product_id TEXT,
            checked_at TEXT
        )
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------- 파싱

def parse_export(path):
    """카톡 export txt -> [{who, when, msg}] """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()

    messages = []
    current_date = ""
    cur = None

    for line in lines:
        m = RE_DATE_SEP.match(line.strip())
        if m:
            current_date = m.group("date").strip()
            continue

        m = RE_PC_LINE.match(line)
        if m:
            if cur:
                messages.append(cur)
            cur = {
                "who": m.group("who").strip(),
                "when": f"{current_date} {m.group('when').strip()}".strip(),
                "msg": m.group("msg"),
            }
            continue

        m = RE_MO_LINE.match(line)
        if m:
            if cur:
                messages.append(cur)
            cur = {
                "who": m.group("who").strip(),
                "when": m.group("when").strip(),
                "msg": m.group("msg"),
            }
            continue

        # 여러 줄 메시지의 뒷줄
        if cur is not None:
            cur["msg"] += "\n" + line

    if cur:
        messages.append(cur)
    return messages


def guess_title(msg, url):
    """메시지에서 상품명으로 쓸 만한 줄을 고른다.
    딜방 메시지는 보통 첫 줄이 상품명, 그 아래에 가격/링크가 붙는다."""
    candidates = []
    for raw in msg.split("\n"):
        line = RE_COUPANG.sub("", raw).strip()
        # 앞머리의 기호·이모지를 떼어낸다.
        # 실제 방은 '✅ 상품명' 형태라 기존의 [\[\(\-\*\s#>] 목록으로는
        # 이모지가 남았다. \W 로 잡으면 한글·숫자는 보존된다.
        line = re.sub(r"^[\W_]+", "", line)
        line = re.sub(r"\s{2,}", " ", line).strip()
        if len(line) < 4:
            continue
        # 가격만 있는 줄, 안내문구 제외
        if re.fullmatch(r"[0-9,원\s~/\(\)배송무료최저가]+", line):
            continue
        if any(k in line for k in ("파트너스", "수수료", "쿠팡파트너스", "일정액",
                                   "쉐어링크", "토스쇼핑")):
            continue
        candidates.append(line)
    return candidates[0][:120] if candidates else ""


# '역대 최저가 2,980원' / '최저가 23,800원' — 실제 판매가가 적히는 자리.
# '원' 을 빠뜨린 표기가 실제로 있다('역대 최저가 15,420'). '최저가' 가
# 이미 앵커 역할을 하므로 '원' 은 선택으로 둔다.
RE_PRICE_LOWEST = re.compile(r"최저가\s*([0-9][0-9,]{2,})\s*원?")
# 이 표현이 들어간 줄의 금액은 가격이 아니라 '할인액'이다
RE_DISCOUNT_LINE = re.compile(r"평균가\s*대비|할인가?\s*$|정가|원가")


def guess_price(msg):
    """실제 판매가를 고른다.

    이 방의 표기는 이렇다.

        🚨 역대 최저가 2,980원 🚨          <- 실제 판매가
        ↳ 평균가 대비 🔻 2,439원 🔻 46%    <- 할인 '금액'

    최솟값을 고르면 할인액 2,439원을 가격으로 쓰게 된다(실측 확인).
    그래서 '최저가 N원' 을 먼저 찾고, 없을 때만 대안으로 넘어간다.
    """
    m = RE_PRICE_LOWEST.search(msg)
    if m:
        v = int(m.group(1).replace(",", ""))
        if 100 <= v <= 50_000_000:
            return v

    # 대안: 할인액이 적힌 줄을 걷어내고 남은 금액 중 최솟값.
    # 포맷이 바뀌었을 때의 보루이며, 기존 동작을 오염원만 뺀 채 유지한다.
    prices = []
    for line in msg.split("\n"):
        if RE_DISCOUNT_LINE.search(line):
            continue
        prices += [int(p.replace(",", "")) for p in RE_PRICE.findall(line)]
    prices = [p for p in prices if 100 <= p <= 50_000_000]
    return min(prices) if prices else None


# ---------------------------------------------------------------- 링크 해석

# 상품을 특정하는 데 꼭 필요한 파라미터. 나머지는 전부 버린다.
# itemId/vendorItemId 는 옵션(색상·용량 등)을 가리키므로 남겨야
# 엉뚱한 옵션의 링크가 만들어지지 않는다.
KEEP_PARAMS = ("itemId", "vendorItemId")


def clean_product_url(url):
    """방장의 트래킹 파라미터를 제거한다.

    딜방 단축링크를 따라가면 최종 URL 에 `src=1139000` 같은 유입 소스가
    붙어 온다(실측). 이건 방장의 것이다. 그대로 partners_link.py 에
    넘기면 내 링크를 만드는 입력에 남의 트래킹이 섞인다.
    """
    try:
        p = urlparse(url)
        q = parse_qs(p.query)
        keep = {k: v for k, v in q.items() if k in KEEP_PARAMS}
        return urlunparse((p.scheme, p.netloc, p.path, "",
                           urlencode(keep, doseq=True), ""))
    except Exception:
        return url


def resolve_product_url(url, session, timeout=12):
    """단축링크를 따라가 최종 쿠팡 상품 URL과 productId를 얻는다.
    반환: (product_url, product_id) / 실패 시 (None, None)

    쿠팡은 봇 트래픽에 403 을 준다(실측). 하지만 리다이렉트는 따라가지고
    최종 URL 에 productId 가 들어 있어 추출에는 지장이 없다.
    그래서 상태코드로 실패 판정을 하지 않는다. 나중에 쿠팡이 리다이렉트까지
    막으면 그때는 여기가 조용히 실패하므로 로그를 봐야 한다.
    """
    if requests is None:
        return None, None

    # 이미 상품 URL이면 그대로
    m = RE_PRODUCT_ID.search(url)
    if m:
        return clean_product_url(url), m.group(1)

    try:
        r = session.get(url, allow_redirects=True, timeout=timeout)
        final = r.url
    except Exception as e:
        print(f"  ! 해석 실패 {url} :: {e}", file=sys.stderr)
        return None, None

    m = RE_PRODUCT_ID.search(final)
    if m:
        return clean_product_url(final), m.group(1)

    # 리다이렉트가 중간 페이지에서 멈춘 경우 본문에서 찾아본다
    m = RE_PRODUCT_ID.search(r.text[:200_000])
    if m:
        pid = m.group(1)
        return f"https://www.coupang.com/vp/products/{pid}", pid

    return None, None


# ---------------------------------------------------------------- 메인 처리

def ingest(txt_path, resolve=True, sleep=1.2):
    conn = init_db()
    messages = parse_export(txt_path)
    print(f"메시지 {len(messages)}건 파싱 완료")

    session = None
    if resolve and requests is not None:
        session = requests.Session()
        session.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})

    new_count = 0
    skip_count = 0

    for msg in messages:
        urls = RE_COUPANG.findall(msg["msg"])
        if not urls:
            continue

        for url in dict.fromkeys(urls):  # 순서 유지 중복 제거
            url = url.rstrip(".,)]}\u200b")

            row = conn.execute(
                "SELECT product_id FROM seen_urls WHERE url = ?", (url,)
            ).fetchone()
            # product_id 확정에 성공한 URL만 영구 스킵.
            # 실패했던 건은 다음 실행 때 다시 시도한다.
            if row and row[0]:
                skip_count += 1
                continue

            product_url, product_id = (url, None)
            if resolve:
                product_url, product_id = resolve_product_url(url, session)
                time.sleep(sleep)  # 쿠팡 쪽 부하/차단 방지
            else:
                m = RE_PRODUCT_ID.search(url)
                product_id = m.group(1) if m else None

            conn.execute(
                "INSERT OR REPLACE INTO seen_urls VALUES (?,?,?)",
                (url, product_id, datetime.now().isoformat(timespec="seconds")),
            )

            if not product_id:
                print(f"  - productId 확정 실패, 보류: {url}")
                conn.commit()
                continue

            exists = conn.execute(
                "SELECT 1 FROM deals WHERE product_id = ?", (product_id,)
            ).fetchone()
            if exists:
                skip_count += 1
                conn.commit()
                continue

            title = guess_title(msg["msg"], url)
            price = guess_price(msg["msg"])

            conn.execute(
                "INSERT INTO deals (product_id,title,price,source_url,product_url,"
                "raw_message,chat_time,found_at,affiliate_url,posted_at) "
                "VALUES (?,?,?,?,?,?,?,?,NULL,NULL)",
                (product_id, title, price, url, product_url,
                 msg["msg"][:2000], msg["when"],
                 datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
            new_count += 1
            print(f"  + [{product_id}] {title[:50]}  {price if price else ''}")

    print(f"\n신규 {new_count}건 / 중복·기존 {skip_count}건")
    export_pending(conn)
    conn.close()


def export_pending(conn=None):
    close_after = False
    if conn is None:
        conn = init_db()
        close_after = True

    rows = conn.execute(
        "SELECT product_id,title,price,product_url,chat_time FROM deals "
        "WHERE posted_at IS NULL ORDER BY found_at DESC"
    ).fetchall()

    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["product_id", "title", "price", "product_url",
                    "chat_time", "affiliate_url(직접 채우세요)"])
        for r in rows:
            w.writerow(list(r) + [""])

    print(f"\n미발행 {len(rows)}건 -> {CSV_PATH}")
    print("파트너스에서 링크 생성 후 affiliate_url 열을 채우고 threads_post.py 를 실행하세요.\n")
    for r in rows[:20]:
        print(f"  [{r[0]}] {r[1][:45]}  {r[2] or '-'}원")
        print(f"        {r[3]}")

    if close_after:
        conn.close()


def mark_posted(product_id):
    conn = init_db()
    conn.execute("UPDATE deals SET posted_at = ? WHERE product_id = ?",
                 (datetime.now().isoformat(timespec="seconds"), product_id))
    conn.commit()
    conn.close()
    print(f"{product_id} 발행 완료 처리")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("txt", nargs="?", help="카톡 export txt 경로")
    ap.add_argument("--no-resolve", action="store_true",
                    help="네트워크 접속 없이 파싱만 수행")
    ap.add_argument("--list-pending", action="store_true")
    ap.add_argument("--mark-posted", metavar="PRODUCT_ID")
    args = ap.parse_args()

    if args.mark_posted:
        mark_posted(args.mark_posted)
    elif args.list_pending:
        export_pending()
    elif args.txt:
        ingest(args.txt, resolve=not args.no_resolve)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
