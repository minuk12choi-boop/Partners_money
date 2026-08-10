#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
telegram_bot.py — 텔레그램으로 받은 것을 발행 문구로 만들어 돌려준다

주기 작업(`run_all.py`)이 자동으로 잡아오는 것과 별개로, 사장님이 직접
발견한 딜을 그 자리에서 문구로 만들 수 있게 한다.

세 가지를 받는다.

  1) **딜방 글 통째로 (쿠팡)** — 방장 링크가 들어 있는 메시지
     방장 링크를 해석해 상품을 알아낸 뒤 **내 파트너스 딥링크를 새로 만들어**
     바꿔 끼운다. 방장 링크는 절대 그대로 내보내지 않는다.

  2) **딜방 글 통째로 (토스)** — 방장 쉐어링크가 들어 있는 메시지
     상품 ID 만 뽑아내고 방장 링크는 버린다. 그 상품이 쉐어링크 대시보드
     큐레이션 목록에 있으면 **내 쉐어링크를 새로 발급해** 바꿔 끼운다.
     목록에 없으면 만들 방법이 없으므로 그렇다고 말한다(아래 참고).

  3) **내 토스 쉐어링크** — `/mine https://toss.im/_m/XXXX`
     토스 앱에서 직접 발급한 링크를 **그대로** 쓰고 싶을 때다.
     ⚠️ `/mine` 을 붙여야 한다. 안 붙이면 2)로 간다 — 아래 참고.

────────────────────────────────────────────────────────────────
⚠️ 토스와 쿠팡은 처리 방식이 다르다. 헷갈리면 남의 링크를 발행하게 된다.

  쿠팡: URL 만 있으면 어떤 상품이든 내 링크를 만들 수 있다
  토스: **대시보드 큐레이션 목록(약 117개) 안에 있는 상품만** 만들 수 있다.
        PC 웹에 상품 검색이 없어서(실측) 목록 밖 상품은 방법이 없다.
        그때는 사장님이 토스 앱에서 직접 발급하셔야 한다.

  어느 경우에도 **방장 링크를 그대로 내보내지 않는다.** 그러면 수익이
  방장에게 간다. 링크를 못 만들면 만들지 못했다고 말한다.
────────────────────────────────────────────────────────────────

⚠️ **표시 없는 토스 쉐어링크는 '남의 것' 으로 본다** (2026-08-03 소유자 지시)

  "그냥 타링크가 투척될 것임. 내 링크로 변환하는 것이다라는 언급은 없을 것임."

  전에는 딜방 표시가 없으면 소유자 본인 링크로 보고 그대로 발행했다.
  이제 설명 없이 남의 링크가 던져지므로 그 기본값이면 남의 링크를
  그대로 내보내게 된다.

  두 방향의 손해가 다르다.
    남의 것 → 내 것 : 남의 링크 발행. 수익이 남에게 간다. **되돌릴 수 없다**
    내 것 → 남의 것 : 같은 상품 링크를 다시 발급할 뿐. 있으면 재사용

  그래서 애매하면 '남의 것'. 그대로 쓰려면 `/mine`. **되돌리지 마라.**

**권한** (2026-08-03 갱신)

  - `/start` 한 사람은 누구나 문구를 **받고**, 링크를 보내 **변환도 시킨다**
  - 관리자(`TG_CHAT_ID`/`TG_ADMIN_IDS`)만 구독자 목록(`/subs`)을 본다
  - 속도 제한은 **기본으로 꺼져 있다.** 사용자가 최대 2명이라는 소유자
    판단에 따른 것이다. 사람이 늘면 `.env` 의 `CONVERT_*` 로 되살린다.

사용법

    py src/telegram_bot.py            # 폴링 시작
    py src/telegram_bot.py --once     # 밀린 것만 처리하고 종료

의존성: requests (requirements.txt 에 이미 있음)
"""

import argparse
import html
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime

import requests

import subscribers
from env import load_env
from schema import ensure_all
from threads_post import build_text

import kakao_deal_extract as K
import partners_link as P
import toss_api
import toss_link as T
import vision_ocr
from lock import profile_lock, LockBusy

load_env()

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "deals.db")
OFFSET_PATH = os.path.join(HERE, "tg_offset.json")

API = "https://api.telegram.org/bot{token}/{method}"

# 가격을 못 구했을 때 헤더에 넣는 말. 이 표시를 보고 '사진을 기다릴지'
# 판단하므로 헤더 문구와 반드시 같아야 한다.
NO_PRICE = "가격 미확인"

RE_TOSS_SHARE = re.compile(r"https?://toss\.im/_m/[A-Za-z0-9]+")
# 상품 주소까지 포함한다. 딜방은 둘 다 올린다.
RE_TOSS_ANY = re.compile(
    r"https?://(?:toss\.im/_m/[A-Za-z0-9]+|toss\.shopping/t/\d+\S*)")
RE_COUPANG_ANY = re.compile(
    r"https?://(?:link\.coupang\.com/\S+|www\.coupang\.com/vp/products/\S+)")

# 딜방 원문임을 알려주는 표시. 이게 있으면 글 안의 링크는 방장 것이다.
RE_ROOM_MARKER = re.compile(
    r"수수료를\s*받습니다|역대\s*최저가|평균가\s*대비|쉐어링크를\s*통해")

HELP = """링크를 그냥 보내시면 됩니다. 설명은 안 하셔도 돼요.

쿠팡이든 토스든, 남의 링크가 섞인 글이든, 링크만 던져 주시면
내 링크로 바꿔서 발행용 문구까지 만들어 드립니다.

  https://link.coupang.com/a/xxxx
  https://toss.im/_m/xxxx
  딜방 글 통째로 복사해서 붙여넣기

가격이 안 잡히면 링크 뒤에 숫자만 붙여 주세요.

  https://toss.im/_m/abc123 7990 24800
  (앞이 판매가, 뒤가 정가. 할인율은 알아서 계산합니다)

※ 토스는 쉐어링크 대시보드에 있는 상품만 만들 수 있습니다.
   목록에 없으면 그렇다고 알려 드립니다.

※ 이미 내 쉐어링크라서 **그대로** 쓰고 싶으시면 앞에 /mine 을 붙이세요.
   안 붙이면 남의 링크로 보고 내 것으로 새로 발급합니다.
   (표시 없이 오는 링크는 남의 것일 때가 많아서 이렇게 뒀습니다)"""

WELCOME = """구독되었습니다. 새 딜이 올라오면 바로 보내 드릴게요.

받으신 글은 그대로 복사해서 스레드에 올리시면 됩니다.
대가성 고지 문구가 이미 들어 있으니 지우지 마세요.

그만 받으시려면 /stop 을 보내 주세요."""

# 구독하지 않은 사람이 링크를 보냈을 때.
NOT_SUBSCRIBED = """먼저 /start 를 눌러 주세요.

그러면 링크를 보내 변환하실 수 있고, 새 딜도 올라오는 대로 받아 보십니다."""


def log(msg=""):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------- 사진·링크 짝 맞추기
#
# 텔레그램에서는 사진과 링크가 **한 메시지로 오지 않는 경우가 많다.**
# 앱에서 스크린샷을 찍고, 링크를 복사해 붙여넣는 동작이 따로이기 때문이다.
# 게다가 순서도 그때그때 다르다. 사진이 먼저 올 수도, 링크가 먼저 올 수도 있다.
#
# 그래서 한쪽만 오면 버리지 않고 잠깐 들고 있다가, 나머지 한쪽이 오면
# 짝을 지어 처리한다. 양방향 모두 된다.
#
# 프로세스 메모리에만 둔다. 봇을 재시작하면 기다리던 것은 사라진다.
# 15분짜리 임시 상태를 파일로 남길 만한 가치가 없다 — 다시 보내면 그만이다.
PAIR_WINDOW = 15 * 60

_pending = {}       # chat -> {"shot": {...}|None, "text": str|None, "at": float}


def pending_get(chat):
    """짝을 기다리던 것을 꺼낸다. 시간이 지났으면 버린다."""
    p = _pending.get(chat)
    if not p:
        return None
    if time.time() - p["at"] > PAIR_WINDOW:
        _pending.pop(chat, None)
        log("기다리던 짝이 시간 초과로 버려짐")
        return None
    return p


def pending_put(chat, shot=None, text=None):
    _pending[chat] = {"shot": shot, "text": text, "at": time.time()}


def pending_clear(chat):
    _pending.pop(chat, None)


def describe_shot(shot):
    """스크린샷에서 읽은 것을 사람이 확인할 수 있게 한 줄로."""
    bits = []
    if shot.get("title"):
        bits.append(shot["title"][:40])
    if shot.get("price"):
        bits.append(f"{shot['price']:,}원")
    if shot.get("original_price"):
        bits.append(f"정가 {shot['original_price']:,}원")
    if shot.get("discount_pct"):
        bits.append(f"{shot['discount_pct']}%")
    return " · ".join(bits) or "읽은 값 없음"


# ---------------------------------------------------------------- 텔레그램

def call(method, token, **params):
    r = requests.post(API.format(token=token, method=method),
                      data=params, timeout=70)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"텔레그램 오류 {j.get('error_code')}: {j.get('description')}")
    return j["result"]


def send_text(token, chat_id, text):
    return call("sendMessage", token, chat_id=chat_id, text=text,
                disable_web_page_preview="true")


def send_body(token, chat_id, header, body):
    """헤더 + 복사용 본문 블록. telegram_deliver.py 와 같은 형식."""
    msg = f"{html.escape(header)}\n<pre>{html.escape(body)}</pre>"
    return call("sendMessage", token, chat_id=chat_id, text=msg,
                parse_mode="HTML", disable_web_page_preview="true")


def download_photo(token, photos):
    """텔레그램 사진을 내려받는다. (바이트, media_type).

    `photos` 는 같은 사진의 여러 해상도다. 마지막이 가장 크다.
    작은 것을 쓰면 글씨가 뭉개져 가격을 잘못 읽는다.
    """
    biggest = photos[-1]
    info = call("getFile", token, file_id=biggest["file_id"])
    path = info["file_path"]
    url = f"https://api.telegram.org/file/bot{token}/{path}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    ext = os.path.splitext(path)[1].lower()
    return r.content, vision_ocr.MEDIA_TYPES.get(ext, "image/jpeg")


def load_offset():
    try:
        with open(OFFSET_PATH, encoding="utf-8") as f:
            return json.load(f).get("offset", 0)
    except Exception:
        return 0


def save_offset(v):
    with open(OFFSET_PATH, "w", encoding="utf-8") as f:
        json.dump({"offset": v,
                   "updated_at": datetime.now().isoformat(timespec="seconds")}, f)


# ---------------------------------------------------------------- 저장

def save(conn, platform, product_id, title, price, product_url,
         affiliate_url, raw, pct, amt, original=None):
    """봇으로 만든 건도 DB 에 남긴다.

    `sent_at` 을 지금으로 채운다. 이미 사장님 손에 갔으므로 주기 작업이
    같은 것을 또 보내면 안 된다.
    """
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT OR REPLACE INTO deals (platform,product_id,title,price,"
        "source_url,product_url,raw_message,chat_time,found_at,affiliate_url,"
        "posted_at,sent_at,discount_pct,discount_amt,original_price) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?)",
        (platform, product_id, title, price, "telegram_bot", product_url,
         (raw or "")[:2000], now, now, affiliate_url, now, pct, amt, original))
    conn.commit()


# ---------------------------------------------------------------- 처리

def lookup_db(conn, platform, product_id):
    """이미 수집해 둔 딜에서 가격을 찾는다.

    **가장 잘 맞는 출처다.** 딜방이 올린 토스 딜은 메시지에 '역대 최저가
    N원 / M% 할인' 이 적혀 있고, kakao_deal_extract 가 그걸 파싱해 DB 에
    넣어 둔다(실측: 토스 125건 전부 가격 보유).

    사장님이 앱에서 새로 발급한 쉐어링크는 코드가 딜방 것과 다르지만,
    해석하면 같은 `toss.shopping/t/<id>` 로 가므로 product_id 로 만난다.
    """
    r = conn.execute(
        "SELECT title, price, discount_pct, discount_amt, original_price "
        "FROM deals WHERE platform=? AND product_id=?",
        (platform, str(product_id))).fetchone()
    if not r or not r[1]:
        return None
    return {"title": r[0], "price": r[1], "discount_pct": r[2],
            "discount_amt": r[3], "original_price": r[4]}


def lookup_toss_price(taca_id):
    """쉐어링크 대시보드 목록에서 가격을 찾는다. 없으면 None.

    토스 앱이 클립보드에 넣어주는 텍스트에는 가격이 없고, 상품 웹페이지도
    가격을 안 준다(실측). 대시보드 목록 API 가 유일한 출처다.
    브라우저가 필요하므로 몇 초 걸린다.
    """
    try:
        with profile_lock(T.PROFILE_DIR, log=log):
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                ctx = T.open_context(pw)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(T.PRODUCTS_URL, wait_until="domcontentloaded")
                page.wait_for_timeout(3500)
                if T.needs_login(page.url):
                    ctx.close()
                    log("토스 세션 만료 — 가격 조회 건너뜀")
                    return None
                info = toss_api.lookup(page, taca_id, log=log)
                ctx.close()
                return info
    except LockBusy as e:
        log(f"가격 조회 건너뜀(브라우저 사용 중): {e}")
        return None
    except Exception as e:
        log(f"가격 조회 실패: {e}")
        return None


def handle_toss(conn, text, share_url, shot=None):
    """내 토스 쉐어링크 → 양식."""
    session = requests.Session()
    product_url, product_id = K.resolve_toss_url(share_url, session)
    if not product_id:
        return None, ("링크를 해석하지 못했습니다.\n"
                      f"{share_url}\n"
                      "토스 앱에서 발급한 쉐어링크가 맞는지 확인해 주세요.")

    # 가격 출처를 순서대로 본다.
    #
    # ⚠️ 토스 웹에는 가격이 아예 없다(실측). 상품 페이지도, 카테고리
    # 랭킹도, 토스쇼핑 홈도 '원' 붙은 숫자가 0개다. RSC 페이로드와 script
    # 전체를 훑어도 없다. 토스가 앱으로 유도하려고 웹에서 뺐다.
    # 그래서 링크를 열어 읽는 방법은 존재하지 않는다. 아래가 전부다.
    price = pct = amt = original = None
    title = ""
    source = ""

    # 1) 스크린샷 — 사장님이 앱에서 실제로 본 화면이다. 정가까지 들어 있어
    #    가장 완전하고, 딜방에 안 올라온 상품도 커버하는 유일한 출처다.
    if shot:
        price = shot.get("price")
        original = shot.get("original_price")
        pct = shot.get("discount_pct")
        title = shot.get("title") or ""
        if price:
            source = "스크린샷"

    # 2) 직접 적어 주신 숫자 — 링크 뒤에 `7990 24800` 처럼
    if not price:
        price, original = parse_manual_prices(text)
        if price:
            source = "직접 입력"

    # 3) 붙여넣은 텍스트 — 딜방 글을 같이 보내면 여기 다 있다
    if not price:
        price = K.guess_price(text)
        if price:
            source = "메시지"
    if pct is None and amt is None:
        pct, amt = K.guess_discount(text)

    # 4) 이미 수집해 둔 딜 — 딜방이 올린 것이면 여기 있다. 커버리지가 가장 넓다
    if not price:
        info = lookup_db(conn, "toss", product_id)
        if info:
            price = info["price"]
            pct = pct or info.get("discount_pct")
            amt = amt or info.get("discount_amt")
            original = info.get("original_price")
            title = title or info.get("title") or ""
            source = "딜방 수집분"

    # 5) 쉐어링크 대시보드 목록 — 큐레이션 117개에 있으면 정가까지 나온다
    if not price:
        info = lookup_toss_price(product_id)
        if info:
            price = info.get("price")
            original = original or info.get("original_price")
            pct = pct or info.get("discount_pct")
            title = title or info.get("title") or ""
            source = "대시보드"

    if not title:
        title = K.fetch_toss_title(product_url, session) or ""
    if not title:
        title = K.guess_title(text, share_url)
    if not title:
        return None, ("상품명을 확인하지 못했습니다.\n"
                      "상품명을 포함해 다시 보내 주세요.")

    body = build_text(title, price, share_url, "toss", pct, amt, original)
    save(conn, "toss", product_id, title, price, product_url,
         share_url, text, pct, amt, original)

    if price:
        header = f"🛒 토스 · {price:,}원 · 가격출처 {source}"
        return (header, body), None

    # 가격을 못 구했다. 문구는 주되 왜 비었는지 정확히 알려준다.
    #
    # 링크를 열어 읽는 방법은 없다. 토스가 웹에서 가격을 통째로 뺐다.
    # 상품 페이지·카테고리 랭킹·토스쇼핑 홈 어디에도 '원' 붙은 숫자가
    # 하나도 없고, RSC 페이로드와 script 를 전부 훑어도 없다(실측).
    header = f"🛒 토스 · {NO_PRICE}"
    hint = (
        "\n\n⚠️ 가격만 못 찾았습니다. 상품명·링크는 다 맞습니다.\n"
        "토스가 웹에서 가격을 아예 빼 놔서(상품 페이지·랭킹·홈 전부)\n"
        "링크를 열어 읽어올 방법이 없습니다.\n\n"
        "링크 뒤에 숫자만 붙여서 다시 보내 주세요.\n\n"
        f"  {share_url} 7990 24800\n\n"
        "앞이 판매가, 뒤가 정가입니다. 할인율은 알아서 계산합니다.\n"
        "정가를 모르시면 판매가 하나만 적으셔도 됩니다.")
    return (header, body + hint), None


def handle_toss_room(conn, text, deal_url):
    """딜방의 토스 글 → **내** 쉐어링크를 새로 발급해 양식.

    방장 링크는 어떤 상품인지 알아내는 데만 쓰고 버린다. 그대로
    내보내면 수익이 방장에게 간다.

    쿠팡과 달리 아무 상품이나 되는 게 아니다. 토스 PC 웹에는 상품 검색이
    없어서(실측) 대시보드 큐레이션 목록에 있는 상품만 발급할 수 있다.
    없으면 없다고 말한다 — 방장 링크로 대신하지 않는다.
    """
    session = requests.Session()
    product_url, product_id = K.resolve_toss_url(deal_url, session)
    if not product_id:
        return None, ("링크를 해석하지 못했습니다.\n"
                      f"{deal_url}\n"
                      "잠시 뒤 다시 시도해 주세요.")

    # 이미 내 링크를 만들어 둔 상품이면 다시 발급하지 않는다.
    # 발급 횟수는 아껴야 하고, 같은 상품에 링크가 여러 개 생길 이유도 없다.
    row = conn.execute(
        "SELECT affiliate_url FROM deals WHERE platform='toss' AND product_id=? "
        "AND affiliate_url IS NOT NULL AND affiliate_url != ''",
        (product_id,)).fetchone()
    my_link, info = (row[0], None) if row else (None, None)
    if my_link:
        log(f"이미 발급해 둔 링크 재사용: {my_link}")
    else:
        my_link, info = T.issue_one(product_id, log=log)

    if not my_link:
        code, why = info if isinstance(info, tuple) else ("error", str(info))
        if code == "dashboard_miss":
            return None, (
                "이 상품은 쉐어링크 대시보드 목록에 없어서 링크를 만들 수 "
                "없습니다.\n\n"
                "토스는 PC 웹에 상품 검색이 없어서, 대시보드가 골라 둔 "
                "상품(약 117개)만\n자동으로 발급할 수 있습니다.\n\n"
                "토스 앱에서 이 상품의 쉐어링크를 직접 발급하신 뒤\n"
                "그 링크와 함께 이 글을 다시 보내 주세요.\n\n"
                f"상품: https://toss.shopping/t/{product_id}")
        if code == "login":
            return None, ("토스 쉐어링크 세션이 만료되었습니다.\n"
                          "PC 에서 `py src/toss_link.py --login` 을 실행해 주세요.")
        return None, f"내 쉐어링크를 만들지 못했습니다.\n{why}"

    info = info or {}
    # 가격은 딜방 글이 가장 정확하다. 방장이 앱에서 보고 적은 값이다.
    # 글 없이 링크만 보내신 경우에는 뒤에 적어 주신 숫자, 그다음 대시보드 순.
    price = K.guess_price(text)
    original = None
    if not price:
        price, original = parse_manual_prices(text)
    price = price or info.get("price")
    pct, amt = K.guess_discount(text)
    pct = pct if pct is not None else info.get("discount_pct")
    original = original or info.get("original_price")
    title = (K.guess_title(text, deal_url) or info.get("title")
             or K.fetch_toss_title(product_url, session) or "")
    if not title:
        return None, "상품명을 확인하지 못했습니다."

    body = build_text(title, price, my_link, "toss", pct, amt, original)
    save(conn, "toss", product_id, title, price, product_url,
         my_link, text, pct, amt, original)
    header = (f"🛒 토스 · {f'{price:,}원' if price else NO_PRICE}"
              f" · 내 링크로 교체됨")
    return (header, body), None


def handle_coupang(conn, text, deal_url, shot=None):
    """딜방 쿠팡 글 → 내 딥링크로 바꿔 양식."""
    session = requests.Session()
    product_url, product_id = K.resolve_product_url(deal_url, session)
    if not product_id:
        return None, ("상품을 확정하지 못했습니다.\n"
                      f"{deal_url}\n"
                      "쿠팡이 리다이렉트를 막았을 수 있습니다. 잠시 뒤 다시 시도해 주세요.")

    title = K.guess_title(text, deal_url)
    price = K.guess_price(text)
    pct, amt = K.guess_discount(text)
    original = None

    # 링크 뒤에 숫자만 적어 주신 경우
    if not price:
        price, original = parse_manual_prices(text)

    # 스크린샷을 같이 보내셨으면 거기 값이 가장 정확하다. 빈 칸만 채운다.
    if shot:
        price = price or shot.get("price")
        original = original or shot.get("original_price")
        pct = pct if pct is not None else shot.get("discount_pct")
        title = title or shot.get("title") or ""

    # 붙여넣은 글에 가격이 없으면(링크만 보낸 경우) 수집해 둔 딜에서 찾는다.
    if not price:
        info = lookup_db(conn, "coupang", product_id)
        if info:
            price = info["price"]
            pct = pct or info.get("discount_pct")
            amt = amt or info.get("discount_amt")
            original = original or info.get("original_price")
            title = title or info.get("title") or ""

    # 내 딥링크를 새로 만든다. 방장 링크는 여기서 버려진다.
    cache = P.load_cache()
    try:
        with profile_lock(P.PROFILE_DIR, log=log):
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                ctx = P.open_context(pw)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                P.goto_link_page(page)
                if not P.is_logged_in(page):
                    ctx.close()
                    return None, ("쿠팡 파트너스 세션이 만료되었습니다.\n"
                                  "PC 에서 `py src/partners_link.py --login` 을 실행해 주세요.")
                my_link = P.get_link(page, product_url, cache, expect_pid=product_id)
                ctx.close()
    except LockBusy as e:
        return None, f"지금 다른 작업이 브라우저를 쓰고 있습니다.\n{e}"

    if not my_link:
        return None, ("내 파트너스 링크를 만들지 못했습니다.\n"
                      f"상품 {product_id}\n"
                      "shots/ 의 스크린샷을 확인해 주세요.")

    body = build_text(title or f"상품 {product_id}", price, my_link,
                      "coupang", pct, amt, original)
    save(conn, "coupang", product_id, title, price, product_url,
         my_link, text, pct, amt, original)
    header = (f"🛒 쿠팡 · {f'{price:,}원' if price else NO_PRICE} "
              f"· 내 링크로 교체됨")
    return (header, body), None


def handle_message(conn, text, shot=None):
    """(헤더, 본문) 또는 (None, 안내문)."""
    text = (text or "").strip()
    if not text:
        return None, HELP
    if text.startswith("/help"):
        return None, HELP

    toss = RE_TOSS_ANY.search(text)
    coupang = RE_COUPANG_ANY.search(text)
    is_room = bool(RE_ROOM_MARKER.search(text))

    # 쿠팡 링크가 있으면 그쪽이 우선이다. 우리가 내 링크를 만들 수 있으므로
    # 방장 글이어도 안전하다.
    if coupang:
        return handle_coupang(conn, text, coupang.group(0), shot)

    if toss:
        url = toss.group(0)
        # 이 링크가 내 것인지 남의 것인지 구분한다.
        #
        # ⚠️ **기본은 '남의 것'이다.** 2026-08-03 소유자 지시로 뒤집었다.
        #
        #   "그냥 타링크가 투척될 것임. 내 링크로 변환하는 것이다라는
        #    언급은 없을 것임."
        #
        # 전에는 딜방 표시가 없으면 '내 것' 으로 봤다. 그런데 이제 아무
        # 표시 없이 남의 링크가 그냥 던져진다. 옛 기본값이면 남의 링크를
        # 그대로 발행하게 된다.
        #
        # 두 방향의 손해가 다르다.
        #   남의 것을 내 것으로 착각 → 남의 링크를 발행. 수익이 남에게 간다
        #   내 것을 남의 것으로 착각 → 같은 상품 링크를 다시 발급할 뿐.
        #                              이미 있으면 그대로 재사용한다
        # 한쪽만 되돌릴 수 없다. 그래서 애매하면 '남의 것' 으로 본다.
        #
        # 내 링크를 그대로 쓰고 싶으면 `/mine` 을 앞에 붙인다.
        mine = (RE_TOSS_SHARE.match(url)
                and text.lstrip().startswith("/mine"))
        if mine:
            return handle_toss(conn, text, url, shot)
        return handle_toss_room(conn, text, url)

    return None, HELP


def has_link(text):
    return bool(RE_TOSS_ANY.search(text or "")
                or RE_COUPANG_ANY.search(text or ""))


RE_URL_ANY = re.compile(r"https?://\S+")
RE_BARE_NUM = re.compile(r"\d[\d,]*")
# 숫자만 적으셨는지 판별할 때, 숫자 말고 있어도 되는 것들.
# ⚠️ 순서가 중요하다. 문자클래스를 앞에 두면 '/toss' 의 '/' 만 먹고
#    'toss' 가 남아 직접 입력이 통째로 무시된다(실측).
RE_ALLOWED_LEFTOVER = re.compile(r"/deal|/toss|/mine|정가|판매가|[\s,원%/\-~+]")


def parse_manual_prices(text):
    """링크 뒤에 숫자만 적어 주셨을 때 읽는다. (판매가, 정가).

        <링크> 7990 24800      → 판매가 7,990 / 정가 24,800
        <링크> 7,990원 정가 24,800원  → 같음
        <링크> 7990            → 판매가만

    두 개면 작은 쪽이 판매가, 큰 쪽이 정가다. 할인율은 따로 안 치셔도
    정가에서 계산된다.

    ⚠️ **숫자 말고 다른 글자가 섞여 있으면 손대지 않는다.** 딜방 글에는

        🚨 역대 최저가 2,980원 🚨
        ↳ 평균가 대비 🔻 2,439원 🔻 46%

    처럼 금액이 둘 있는데, 작은 2,439원은 가격이 아니라 '할인액'이다.
    여기서 두 개라고 덥석 집으면 할인액을 판매가로 발행하게 된다.
    그래서 딜방 글은 기존 파싱(guess_price)에 그대로 맡긴다.
    """
    stripped = RE_URL_ANY.sub(" ", text or "")
    nums = []
    for m in RE_BARE_NUM.finditer(stripped):
        v = int(m.group(0).replace(",", ""))
        if 100 <= v <= 50_000_000 and v not in nums:
            nums.append(v)
    if not 1 <= len(nums) <= 2:
        return None, None
    if RE_ALLOWED_LEFTOVER.sub("", RE_BARE_NUM.sub(" ", stripped)).strip():
        return None, None       # 글이 섞여 있다
    if len(nums) == 1:
        return nums[0], None
    return min(nums), max(nums)


# ---------------------------------------------------------------- 메인

def reply(token, chat, result, err):
    if result:
        header, body = result
        send_body(token, chat, header, body)
        log(f"회신: {header}")
    else:
        send_text(token, chat, err)
        log("회신: 안내문")


def throttle_ok(conn, token, chat):
    """지금 변환을 시켜도 되는가. 안 되면 이유를 회신하고 False.

    변환은 구독자 누구나 시킬 수 있지만(2026-08-02 소유자 지시),
    **사이트에 닿는 빈도는 사람 수와 무관하게 일정해야 한다.**
    CLAUDE.md 제약 2 를 사람 손으로 깨뜨리지 않기 위한 관문이다.
    """
    ok, why = subscribers.convert_allowed(conn, chat)
    if not ok:
        log(f"속도 제한 {chat}: {why.splitlines()[0]}")
        send_text(token, chat, why)
        return False
    subscribers.convert_record(conn, chat)
    return True


def build_and_reply(conn, token, chat, text, shot):
    """문구를 만들어 보낸다. 가격을 못 채웠으면 True 를 돌려준다."""
    try:
        result, err = handle_message(conn, text, shot)
    except (AssertionError, ValueError) as e:
        # 고지 문구 문제는 절대 넘어가면 안 된다.
        result, err = None, f"본문을 만들 수 없습니다: {e}"
    except Exception as e:
        result, err = None, f"처리 중 오류: {e}"
        log(f"오류: {e}")
    reply(token, chat, result, err)
    return bool(result) and NO_PRICE in result[0]


def process(conn, token, msg):
    chat_obj = msg.get("chat") or {}
    chat = str(chat_obj.get("id", ""))
    text = msg.get("text") or msg.get("caption") or ""
    photos = msg.get("photo")
    cmd = text.strip().lower()

    # ── 구독 (누구나 된다)
    if cmd.startswith("/start"):
        new = subscribers.add(conn, chat_obj)
        a, t = subscribers.count(conn)
        log(f"구독 {'신규' if new else '재개'}: {chat} (받는 사람 {a}명)")
        send_text(token, chat, WELCOME)
        if subscribers.is_admin(chat):
            send_text(token, chat, HELP)
        return

    if cmd.startswith("/stop"):
        subscribers.stop(conn, chat, "사용자 요청")
        log(f"구독 해지: {chat}")
        send_text(token, chat, "그만 받겠습니다. 다시 받으시려면 /start 입니다.")
        return

    # ── 구독자 목록은 관리자만. 남의 이름·가입일이라 아무나 보면 안 된다.
    if cmd.startswith("/subs"):
        if not subscribers.is_admin(chat):
            send_text(token, chat, "구독자 목록은 운영자만 볼 수 있어요.")
            return
        a, t = subscribers.count(conn)
        lines = [f"구독자 {t}명 (받는 중 {a}명)", ""]
        for cid, uname, name, joined, act, cnt, err in subscribers.listing(conn):
            who = name or (f"@{uname}" if uname else "?")
            lines.append(f"{'●' if act else '○'} {who[:20]} · {joined[:10]} "
                         f"· {cnt}건")
        send_text(token, chat, "\n".join(lines))
        return

    # ── 여기부터는 변환이다. 구독자면 누구나 시킬 수 있다.
    #
    # 2026-08-02 소유자 지시로 열었다. 전에는 관리자만이었다.
    # "텔레그램에 들어온 사람은 모두 되게 하라, 어차피 아는 사람이 쓴다."
    # 누가 쓰느냐는 소유자가 정할 일이므로 그 판단을 따른다.
    #
    # ⚠️ 다만 **속도는 열지 않았다.** 변환 한 번이 곧 이 PC 의 브라우저로
    # 쿠팡 파트너스·토스에 접속하는 것이라, 사람이 늘면 접근도 사람 수만큼
    # 늘어난다. 그러면 CLAUDE.md 제약 2(접근 빈도)가 사람 손으로 깨진다.
    # 제약 2 는 완화 대상이 아니다. 그래서 총량과 간격으로 묶는다.
    if not subscribers.is_subscribed(conn, chat):
        log(f"미구독 {chat} — 변환 거절")
        send_text(token, chat, NOT_SUBSCRIBED)
        return

    if text.strip().startswith("/cancel"):
        pending_clear(chat)
        send_text(token, chat, "기다리던 사진·링크를 지웠습니다.")
        return

    # ── 사진이 왔다
    if photos:
        log(f"수신: 사진 {len(photos)}종 · 캡션 {text[:40]!r}")
        try:
            data, media_type = download_photo(token, photos)
            shot = vision_ocr.read_screenshot(data, media_type, log=log)
        except vision_ocr.OcrUnavailable as e:
            send_text(token, chat, f"스크린샷을 읽을 수 없습니다.\n{e}")
            return
        except Exception as e:
            log(f"판독 실패: {e}")
            send_text(token, chat, f"스크린샷 판독에 실패했습니다: {e}")
            return

        log(f"판독: {describe_shot(shot)}")

        # 링크는 캡션에 있을 수도, 아까 따로 보내셨을 수도 있다.
        pending = pending_get(chat)
        paired = text if has_link(text) else (
            pending["text"] if pending and pending.get("text") else None)

        if paired:
            # 캡션과 앞서 온 글을 합친다. 딜방 글을 먼저 보내고 사진을
            # 나중에 보내는 경우, 그 글의 가격·할인율도 살려야 한다.
            merged = paired if paired == text else f"{paired}\n{text}".strip()
            if not throttle_ok(conn, token, chat):
                # 사진은 그대로 들고 있는다. 잠시 뒤 링크만 다시 보내면 된다.
                return
            pending_clear(chat)
            build_and_reply(conn, token, chat, merged, shot)
            return

        # 링크가 아직 없다. 읽은 것을 들고 기다린다.
        pending_put(chat, shot=shot, text=text or None)
        send_text(token, chat,
                  f"📸 읽었습니다 — {describe_shot(shot)}\n\n"
                  "이제 이 상품의 토스 쉐어링크를 보내 주세요.\n"
                  "(15분 안에 보내시면 이 사진과 짝을 맞춰 드립니다)")
        return

    # ── 글이 왔다
    log(f"수신: {text[:60]!r}")

    if not has_link(text):
        # 링크가 없으면 기다리게 할 것도 없다. 안내만 한다.
        build_and_reply(conn, token, chat, text, None)
        return

    if not throttle_ok(conn, token, chat):
        return

    # 앞서 사진을 보내셨으면 그걸 쓴다.
    pending = pending_get(chat)
    shot = pending.get("shot") if pending else None
    extra = pending.get("text") if pending else None
    if pending:
        pending_clear(chat)
    if shot:
        log(f"앞서 온 사진과 짝지음 — {describe_shot(shot)}")
    merged = f"{text}\n{extra}".strip() if extra else text

    no_price = build_and_reply(conn, token, chat, merged, shot)

    # 가격을 못 채웠다면 사장님이 곧 스크린샷을 보내실 것이다(그렇게 안내했다).
    # 이 링크를 들고 있어야 그 사진과 짝이 맞는다. 안 들고 있으면 사진이
    # 왔을 때 다시 "링크를 보내 주세요" 가 되어 무한히 맴돈다.
    if no_price:
        pending_put(chat, shot=None, text=merged)
        log("가격 미확인 — 링크를 들고 사진을 기다린다")


def main():
    ap = argparse.ArgumentParser(description="텔레그램 입력을 발행 문구로 변환")
    ap.add_argument("--once", action="store_true",
                    help="밀린 메시지만 처리하고 종료")
    ap.add_argument("--timeout", type=int, default=50,
                    help="long polling 대기 시간(초)")
    args = ap.parse_args()

    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")
    if not token:
        raise SystemExit("TG_BOT_TOKEN 이 없습니다. `.env` 를 확인하세요.")
    if not subscribers.admin_ids():
        # 관리자가 없으면 링크 변환을 시킬 사람이 아무도 없다. 구독·전달은
        # 그대로 되므로 죽이지는 않지만, 조용히 넘어가면 "봇이 안 되네" 가 된다.
        log("⚠️ TG_CHAT_ID 가 없습니다. 링크 변환을 시킬 수 있는 사람이 "
            "아무도 없습니다.")
        log("   `py src/telegram_deliver.py --whoami` 로 확인해 `.env` 에 넣으세요.")

    conn = sqlite3.connect(DB_PATH)
    ensure_all(conn)

    me = call("getMe", token)
    a, t = subscribers.count(conn)
    log(f"봇 시작: @{me.get('username')} · 구독자 {t}명(받는 중 {a}명)")
    offset = load_offset()

    while True:
        try:
            updates = call("getUpdates", token, offset=offset,
                           timeout=0 if args.once else args.timeout)
        except Exception as e:
            log(f"getUpdates 실패: {e}")
            if args.once:
                break
            time.sleep(5)
            continue

        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or u.get("edited_message")
            if msg:
                process(conn, token, msg)
            save_offset(offset)

        if args.once:
            break

    conn.close()


if __name__ == "__main__":
    main()
