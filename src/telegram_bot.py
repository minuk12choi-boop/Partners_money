#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
telegram_bot.py — 텔레그램으로 받은 것을 발행 문구로 만들어 돌려준다

주기 작업(`run_all.py`)이 자동으로 잡아오는 것과 별개로, 사장님이 직접
발견한 딜을 그 자리에서 문구로 만들 수 있게 한다.

두 가지를 받는다.

  1) **내 토스 쉐어링크** — `https://toss.im/_m/XXXX`
     토스 앱에서 직접 발급한 링크를 붙여넣으면 상품명을 확인해 양식을 만든다.
     가격·할인율은 같이 붙여넣은 텍스트에서 읽는다(딜방 메시지를 통째로
     붙여넣으면 그대로 잡힌다).

  2) **딜방의 쿠팡 글 통째로** — 방장 링크가 들어 있는 메시지
     방장 링크를 해석해 상품을 알아낸 뒤 **내 파트너스 딥링크를 새로 만들어**
     바꿔 끼운다. 방장 링크는 절대 그대로 내보내지 않는다.

────────────────────────────────────────────────────────────────
⚠️ 토스와 쿠팡은 처리 방식이 다르다. 헷갈리면 남의 링크를 발행하게 된다.

  쿠팡: 상품만 알면 내 링크를 **만들 수 있다** → 방장 글을 그대로 받아도 안전
  토스: 상품을 지정해 링크를 만들 **방법이 없다**(PC 웹에 검색이 없음, 실측)
        → 링크는 사장님이 앱에서 직접 만들어 주셔야 한다

  그래서 딜방의 토스 글을 붙여넣으면 **거절한다.** 그 글의 토스 링크는
  방장 것이고, 그걸로 만들면 수익이 방장에게 간다.
────────────────────────────────────────────────────────────────

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

from env import load_env
from schema import ensure_deals
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
RE_COUPANG_ANY = re.compile(
    r"https?://(?:link\.coupang\.com/\S+|www\.coupang\.com/vp/products/\S+)")

# 딜방 원문임을 알려주는 표시. 이게 있으면 글 안의 링크는 방장 것이다.
RE_ROOM_MARKER = re.compile(
    r"수수료를\s*받습니다|역대\s*최저가|평균가\s*대비|쉐어링크를\s*통해")

HELP = """무엇을 보내면 되는지 알려드릴게요.

1) 내 토스 쉐어링크
   토스 앱에서 발급한 https://toss.im/_m/... 를 보내세요.

2) 상품 화면 스크린샷
   토스 앱 상품 화면을 찍어 보내시면 상품명·판매가·정가·할인율을 읽습니다.
   토스가 웹에서 가격을 빼 놔서, 딜방에 안 올라온 상품은 이게 유일한 방법입니다.

   ※ 링크와 사진은 따로 보내셔도 됩니다. 순서도 상관없습니다.
      15분 안에 둘 다 오면 알아서 짝을 맞춥니다.

3) 딜방의 쿠팡 글
   방장 글을 통째로 복사해서 보내세요.
   방장 링크는 버리고 사장님 파트너스 링크를 새로 만들어 드립니다.

※ 딜방의 토스 글은 처리할 수 없습니다. 토스는 상품을 지정해서 링크를
   만들 방법이 없어서(PC 웹에 검색이 없음), 사장님이 앱에서 직접 발급한
   링크가 필요합니다.

/cancel — 기다리고 있는 사진·링크를 지웁니다."""


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

    # 2) 붙여넣은 텍스트 — 딜방 글을 같이 보내면 여기 다 있다
    if not price:
        price = K.guess_price(text)
        if price:
            source = "메시지"
    if pct is None and amt is None:
        pct, amt = K.guess_discount(text)

    # 3) 이미 수집해 둔 딜 — 딜방이 올린 것이면 여기 있다. 커버리지가 가장 넓다
    if not price:
        info = lookup_db(conn, "toss", product_id)
        if info:
            price = info["price"]
            pct = pct or info.get("discount_pct")
            amt = amt or info.get("discount_amt")
            original = info.get("original_price")
            title = title or info.get("title") or ""
            source = "딜방 수집분"

    # 4) 쉐어링크 대시보드 목록 — 큐레이션 117개에 있으면 정가까지 나온다
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
        "\n\n⚠️ 가격을 못 찾았습니다.\n"
        "이 상품은 딜방에도 안 올라왔고 쉐어링크 대시보드 목록에도 없습니다.\n"
        "토스는 웹에서 가격을 아예 빼 놨습니다(상품 페이지·랭킹·홈 전부).\n"
        "그래서 링크를 열어 읽어올 방법이 없습니다.\n\n"
        "📸 토스 앱 상품 화면을 캡처해서 보내 주세요. 그 사진에서\n"
        "   상품명·판매가·정가·할인율을 읽어 채워 드립니다.")
    return (header, body + hint), None


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
    if text.startswith("/start") or text.startswith("/help"):
        return None, HELP

    toss = RE_TOSS_SHARE.search(text)
    coupang = RE_COUPANG_ANY.search(text)
    is_room = bool(RE_ROOM_MARKER.search(text))

    # 쿠팡 링크가 있으면 그쪽이 우선이다. 우리가 내 링크를 만들 수 있으므로
    # 방장 글이어도 안전하다.
    if coupang:
        return handle_coupang(conn, text, coupang.group(0), shot)

    if toss:
        # 딜방 원문에 들어 있는 토스 링크는 방장 것이다. 그대로 쓰면
        # 수익이 방장에게 간다. 명령으로 명시하지 않으면 거절한다.
        if is_room and not text.lstrip().startswith("/toss"):
            return None, (
                "이건 딜방 원문으로 보입니다. 안에 있는 토스 링크는 방장 것이라\n"
                "그대로 쓰면 수익이 방장에게 갑니다.\n\n"
                "토스는 상품을 지정해 링크를 만들 방법이 없어서(PC 웹에 검색이\n"
                "없음), 사장님이 토스 앱에서 직접 발급하셔야 합니다.\n\n"
                "발급하신 뒤 이렇게 보내 주세요. 가격·할인율은 아래 붙여넣은\n"
                "글에서 읽습니다.\n\n"
                "/toss https://toss.im/_m/내링크\n"
                "(그 아래에 딜방 글을 붙여넣기)")
        return handle_toss(conn, text, toss.group(0), shot)

    return None, HELP


def has_link(text):
    return bool(RE_TOSS_SHARE.search(text or "")
                or RE_COUPANG_ANY.search(text or ""))


# ---------------------------------------------------------------- 메인

def reply(token, chat, result, err):
    if result:
        header, body = result
        send_body(token, chat, header, body)
        log(f"회신: {header}")
    else:
        send_text(token, chat, err)
        log("회신: 안내문")


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


def process(conn, token, chat_id, msg):
    chat = str((msg.get("chat") or {}).get("id", ""))
    text = msg.get("text") or msg.get("caption") or ""
    photos = msg.get("photo")

    # 사장님 대화만 받는다. 봇 주소를 아는 다른 사람이 쓰면 안 된다.
    if chat_id and chat != str(chat_id):
        log(f"모르는 대화 {chat} 무시")
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
            pending_clear(chat)
            # 캡션과 앞서 온 글을 합친다. 딜방 글을 먼저 보내고 사진을
            # 나중에 보내는 경우, 그 글의 가격·할인율도 살려야 한다.
            merged = paired if paired == text else f"{paired}\n{text}".strip()
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
    if not chat_id:
        log("⚠️ TG_CHAT_ID 가 없습니다. 아무 대화나 받게 됩니다.")

    conn = sqlite3.connect(DB_PATH)
    ensure_deals(conn)

    me = call("getMe", token)
    log(f"봇 시작: @{me.get('username')}")
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
                process(conn, token, chat_id, msg)
            save_offset(offset)

        if args.once:
            break

    conn.close()


if __name__ == "__main__":
    main()
