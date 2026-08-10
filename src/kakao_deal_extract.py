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
import html
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from schema import BASE_SCHEMA, ensure_deals

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

# 토스쇼핑 링크. 이 방은 쿠팡과 토스를 섞어 올린다(실측 고유 264 : 115).
#   toss.im/_m/XXXX      단축링크
#   toss.shopping/t/123  상품 페이지
RE_TOSS = re.compile(
    r"https?://(?:"
    r"toss\.im/_m/[^\s\)\]\}<>\"']+"
    r"|toss\.shopping/t/[^\s\)\]\}<>\"']+"
    r")",
    re.IGNORECASE,
)

# 두 플랫폼을 한 번에 훑을 때 쓴다
RE_DEAL_LINK = re.compile(f"(?:{RE_COUPANG.pattern})|(?:{RE_TOSS.pattern})", re.IGNORECASE)

RE_PRICE = re.compile(r"([0-9][0-9,]{2,})\s*원")
RE_PRODUCT_ID = re.compile(r"/(?:vp|vm)/products/(\d+)")
# 쿠팡 /vp/products/(\d+) 에 대응하는 토스의 상품 식별자
RE_TOSS_PRODUCT_ID = re.compile(r"toss\.shopping/t/(\d+)")
RE_OG_TITLE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)["\']', re.I)

PLATFORM_COUPANG = "coupang"
PLATFORM_TOSS = "toss"


def detect_platform(url):
    if RE_TOSS.match(url):
        return PLATFORM_TOSS
    return PLATFORM_COUPANG


# ---------------------------------------------------------------- DB

# 스키마 정의는 schema.py 가 단일 출처다. 여기서 다시 적지 않는다.
DEALS_SCHEMA = BASE_SCHEMA


def migrate_deals(conn):
    """platform 컬럼이 없는 옛 테이블을 새 스키마로 옮긴다.

    쿠팡과 토스의 상품 ID 가 둘 다 숫자라 product_id 단독 PK 로는
    충돌할 수 있다(쿠팡 2279371037, 토스 524516537). 복합 키로 바꾼다.
    SQLite 는 PK 변경을 지원하지 않아 테이블을 새로 만들어 옮긴다.
    기존 행은 전부 쿠팡이므로 platform='coupang' 으로 채운다.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(deals)")]
    if not cols or "platform" in cols:
        return

    print("deals 테이블을 platform 포함 스키마로 이전합니다...")
    old = ["product_id", "title", "price", "source_url", "product_url",
           "raw_message", "chat_time", "found_at", "affiliate_url", "posted_at"]
    conn.execute("ALTER TABLE deals RENAME TO deals_old")
    conn.execute(DEALS_SCHEMA)
    conn.execute(
        f"INSERT INTO deals (platform, {','.join(old)}) "
        f"SELECT 'coupang', {','.join(old)} FROM deals_old"
    )
    moved = conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
    conn.execute("DROP TABLE deals_old")
    conn.commit()
    print(f"  {moved}건 이전 완료 (전부 platform='coupang')")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(DEALS_SCHEMA)
    migrate_deals(conn)
    ensure_deals(conn)
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


# 상품명 후보에서 걷어낼 주소.
#
# ⚠️ **쿠팡 주소만 지우면 안 된다.** 실측(2026-08-03): 설명 없이 토스
# 링크만 보냈더니 그 링크가 상품명으로 뽑혔다. 발행문의 상품명 자리에
# **남의 쉐어링크가 그대로 박혔다.** 구매 링크는 내 것으로 바뀌었는데
# 본문에는 남의 링크가 남는 꼴이다.
#
# URL 은 어떤 경우에도 상품명이 아니다. 종류를 가리지 말고 전부 지운다.
RE_ANY_URL = re.compile(r"https?://\S+")


def guess_title(msg, url):
    """메시지에서 상품명으로 쓸 만한 줄을 고른다.
    딜방 메시지는 보통 첫 줄이 상품명, 그 아래에 가격/링크가 붙는다."""
    candidates = []
    for raw in msg.split("\n"):
        line = RE_ANY_URL.sub(" ", raw).strip()
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

# 할인 정보. 이 방은 플랫폼마다 다르게 적는다(실측 348건).
#
#   쿠팡: ↳ 평균가 대비 🔻 15,902원 🔻 38%    할인액 + 할인율
#   토스: ↳ 🔻 60% 할인                      할인율만
#
# **평균가를 따로 구할 필요가 없다.** 평균가 = 최저가 + 할인액 이다.
# 검산: 25,810 + 15,902 = 41,712 이고 15,902/41,712 = 38.1% 로 표기된 38% 와
# 일치한다. 방장이 평균가를 어디서 얻는지는 알 필요가 없다. 이미 계산해
# 적어 둔 값을 읽기만 하면 된다.
#
# 커버리지(348건): 역대 최저가 91% / 할인율 89% / 평균가 대비 59%
RE_DISCOUNT_AMT = re.compile(r"평균가\s*대비[^\d]{0,12}([0-9][0-9,]{2,})\s*원")
RE_DISCOUNT_PCT = re.compile(r"([0-9]{1,3})\s*%")


def guess_discount(msg):
    """(할인율 %, 할인액 원). 못 찾으면 각각 None.

    할인 관련 줄에서만 찾는다. 상품명에 '20%' 같은 게 들어 있어도
    엉뚱한 값을 집지 않게 하기 위함이다.
    """
    pct = amt = None
    for line in msg.split("\n"):
        if not ("평균가" in line or "할인" in line or "🔻" in line):
            continue
        if amt is None:
            m = RE_DISCOUNT_AMT.search(line)
            if m:
                v = int(m.group(1).replace(",", ""))
                if 100 <= v <= 50_000_000:
                    amt = v
        if pct is None:
            m = RE_DISCOUNT_PCT.search(line)
            if m:
                v = int(m.group(1))
                if 1 <= v <= 99:
                    pct = v
    return pct, amt


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


# ---------------------------------------------------------------- 토스 해석

def resolve_toss_url(url, session, timeout=12):
    """토스 단축링크를 따라가 상품 URL 과 상품 ID 를 얻는다.

    실측:
      https://toss.im/_m/9X1XXhQf
        -> https://toss.shopping/t/524516537?k=<uuid>&referrer=affiliate

    `k=` 는 방장의 쉐어링크 트래킹 키다. 반드시 버린다.
    파라미터를 뗀 주소가 정상 동작하고, 토스도 og:url 로 그것을
    정식 URL 이라고 선언한다.
    """
    if requests is None:
        return None, None

    m = RE_TOSS_PRODUCT_ID.search(url)
    if m:
        return f"https://toss.shopping/t/{m.group(1)}", m.group(1)

    try:
        r = session.get(url, allow_redirects=True, timeout=timeout)
        final = r.url
    except Exception as e:
        print(f"  ! 토스 해석 실패 {url} :: {e}", file=sys.stderr)
        return None, None

    m = RE_TOSS_PRODUCT_ID.search(final)
    if m:
        return f"https://toss.shopping/t/{m.group(1)}", m.group(1)

    m = RE_TOSS_PRODUCT_ID.search(r.text[:200_000])
    if m:
        return f"https://toss.shopping/t/{m.group(1)}", m.group(1)

    return None, None


def fetch_toss_title(product_url, session, timeout=12):
    """토스 상품 페이지의 og:title 에서 정확한 상품명을 얻는다.

    채팅 메시지를 추측 파싱하는 guess_title() 보다 정확하다.
    실패하면 None 을 돌려주고 호출부가 메시지 파싱으로 되돌아간다.
    """
    if requests is None:
        return None
    try:
        r = session.get(product_url, timeout=timeout)
        m = RE_OG_TITLE.search(r.text[:200_000])
    except Exception as e:
        print(f"  ! 토스 상품명 조회 실패 {product_url} :: {e}", file=sys.stderr)
        return None
    if not m:
        return None
    title = html.unescape(m.group(1)).strip()
    # '상품명 | 토스쇼핑' 형태라 접미사를 뗀다
    title = re.sub(r"\s*\|\s*토스쇼핑\s*$", "", title).strip()
    return title or None


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

    new_by_platform = {PLATFORM_COUPANG: 0, PLATFORM_TOSS: 0}

    for msg in messages:
        urls = RE_DEAL_LINK.findall(msg["msg"])
        if not urls:
            continue

        for url in dict.fromkeys(urls):  # 순서 유지 중복 제거
            url = url.rstrip(".,)]}\u200b")

            platform = detect_platform(url)

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
                if platform == PLATFORM_TOSS:
                    product_url, product_id = resolve_toss_url(url, session)
                else:
                    product_url, product_id = resolve_product_url(url, session)
                time.sleep(sleep)  # 상대 서버 부하/차단 방지
            else:
                pat = RE_TOSS_PRODUCT_ID if platform == PLATFORM_TOSS else RE_PRODUCT_ID
                m = pat.search(url)
                product_id = m.group(1) if m else None

            conn.execute(
                "INSERT OR REPLACE INTO seen_urls VALUES (?,?,?)",
                (url, product_id, datetime.now().isoformat(timespec="seconds")),
            )

            if not product_id:
                print(f"  - [{platform}] 상품ID 확정 실패, 보류: {url}")
                conn.commit()
                continue

            exists = conn.execute(
                "SELECT 1 FROM deals WHERE platform = ? AND product_id = ?",
                (platform, product_id),
            ).fetchone()
            if exists:
                skip_count += 1
                conn.commit()
                continue

            title = guess_title(msg["msg"], url)
            # 토스는 상품 페이지의 og:title 이 채팅 메시지 파싱보다 정확하다.
            # 실패하면 메시지에서 뽑은 값을 그대로 쓴다.
            if platform == PLATFORM_TOSS and resolve:
                og = fetch_toss_title(product_url, session)
                if og:
                    title = og
                time.sleep(sleep)
            price = guess_price(msg["msg"])
            disc_pct, disc_amt = guess_discount(msg["msg"])

            conn.execute(
                "INSERT INTO deals (platform,product_id,title,price,source_url,"
                "product_url,raw_message,chat_time,found_at,affiliate_url,posted_at,"
                "discount_pct,discount_amt) "
                "VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,?,?)",
                (platform, product_id, title, price, url, product_url,
                 msg["msg"][:2000], msg["when"],
                 datetime.now().isoformat(timespec="seconds"),
                 disc_pct, disc_amt),
            )
            conn.commit()
            new_count += 1
            new_by_platform[platform] = new_by_platform.get(platform, 0) + 1
            print(f"  + [{platform}:{product_id}] {title[:50]}  {price if price else ''}")

    print(f"\n신규 {new_count}건 "
          f"(쿠팡 {new_by_platform[PLATFORM_COUPANG]} / "
          f"토스 {new_by_platform[PLATFORM_TOSS]}) "
          f"/ 중복·기존 {skip_count}건")
    export_pending(conn)
    conn.close()


def export_pending(conn=None):
    close_after = False
    if conn is None:
        conn = init_db()
        close_after = True

    rows = conn.execute(
        "SELECT platform,product_id,title,price,product_url,chat_time,affiliate_url "
        "FROM deals WHERE posted_at IS NULL ORDER BY found_at DESC"
    ).fetchall()

    # 이 CSV 는 사람이 눈으로 보는 용도다.
    # 파이프라인의 진실 공급원은 deals.db 이고 threads_post.py 도 DB 를 읽는다.
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["platform", "product_id", "title", "price", "product_url",
                    "chat_time", "affiliate_url"])
        for r in rows:
            w.writerow(list(r))

    n_link = sum(1 for r in rows if r[6])
    print(f"\n미발행 {len(rows)}건 (링크 생성됨 {n_link} / 대기 {len(rows) - n_link})"
          f" -> {CSV_PATH}")
    for r in rows[:20]:
        mark = "링크O" if r[6] else "링크X"
        print(f"  [{r[0]}:{r[1]}] {mark} {r[2][:40]}  {r[3] or '-'}원")
        print(f"        {r[4]}")

    if close_after:
        conn.close()


def mark_posted(product_id, platform=None):
    """발행 완료 처리. platform 을 주면 그 플랫폼만, 없으면 전부 훑는다."""
    conn = init_db()
    now = datetime.now().isoformat(timespec="seconds")
    if platform:
        cur = conn.execute(
            "UPDATE deals SET posted_at = ? WHERE platform = ? AND product_id = ?",
            (now, platform, product_id))
    else:
        cur = conn.execute(
            "UPDATE deals SET posted_at = ? WHERE product_id = ?", (now, product_id))
    conn.commit()
    n = cur.rowcount
    conn.close()
    print(f"{product_id} 발행 완료 처리 ({n}건)" if n else
          f"{product_id} 에 해당하는 행이 없습니다.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("txt", nargs="?", help="카톡 export txt 경로")
    ap.add_argument("--no-resolve", action="store_true",
                    help="네트워크 접속 없이 파싱만 수행")
    ap.add_argument("--list-pending", action="store_true")
    ap.add_argument("--mark-posted", metavar="PRODUCT_ID")
    ap.add_argument("--platform", choices=[PLATFORM_COUPANG, PLATFORM_TOSS],
                    help="--mark-posted 와 함께 쓴다")
    args = ap.parse_args()

    if args.mark_posted:
        mark_posted(args.mark_posted, args.platform)
    elif args.list_pending:
        export_pending()
    elif args.txt:
        ingest(args.txt, resolve=not args.no_resolve)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
