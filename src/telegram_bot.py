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
from lock import profile_lock, LockBusy

load_env()

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "deals.db")
OFFSET_PATH = os.path.join(HERE, "tg_offset.json")

API = "https://api.telegram.org/bot{token}/{method}"

RE_TOSS_SHARE = re.compile(r"https?://toss\.im/_m/[A-Za-z0-9]+")
RE_COUPANG_ANY = re.compile(
    r"https?://(?:link\.coupang\.com/\S+|www\.coupang\.com/vp/products/\S+)")

# 딜방 원문임을 알려주는 표시. 이게 있으면 글 안의 링크는 방장 것이다.
RE_ROOM_MARKER = re.compile(
    r"수수료를\s*받습니다|역대\s*최저가|평균가\s*대비|쉐어링크를\s*통해")

HELP = """무엇을 보내면 되는지 알려드릴게요.

1) 내 토스 쉐어링크
   토스 앱에서 발급한 https://toss.im/_m/... 를 보내세요.
   딜방 글을 통째로 붙여넣고 맨 아래에 내 링크를 덧붙이면
   가격·할인율까지 같이 넣어 드립니다.

2) 딜방의 쿠팡 글
   방장 글을 통째로 복사해서 보내세요.
   방장 링크는 버리고 사장님 파트너스 링크를 새로 만들어 드립니다.

※ 딜방의 토스 글은 처리할 수 없습니다. 토스는 상품을 지정해서 링크를
   만들 방법이 없어서(PC 웹에 검색이 없음), 사장님이 앱에서 직접 발급한
   링크가 필요합니다."""


def log(msg=""):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


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
         affiliate_url, raw, pct, amt):
    """봇으로 만든 건도 DB 에 남긴다.

    `sent_at` 을 지금으로 채운다. 이미 사장님 손에 갔으므로 주기 작업이
    같은 것을 또 보내면 안 된다.
    """
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT OR REPLACE INTO deals (platform,product_id,title,price,"
        "source_url,product_url,raw_message,chat_time,found_at,affiliate_url,"
        "posted_at,sent_at,discount_pct,discount_amt) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)",
        (platform, product_id, title, price, "telegram_bot", product_url,
         (raw or "")[:2000], now, now, affiliate_url, now, pct, amt))
    conn.commit()


# ---------------------------------------------------------------- 처리

def handle_toss(conn, text, share_url):
    """내 토스 쉐어링크 → 양식."""
    session = requests.Session()
    product_url, product_id = K.resolve_toss_url(share_url, session)
    if not product_id:
        return None, ("링크를 해석하지 못했습니다.\n"
                      f"{share_url}\n"
                      "토스 앱에서 발급한 쉐어링크가 맞는지 확인해 주세요.")

    title = K.fetch_toss_title(product_url, session) or ""
    if not title:
        # og:title 이 없으면 붙여넣은 텍스트에서 뽑아 본다
        title = K.guess_title(text, share_url)
    if not title:
        return None, ("상품명을 확인하지 못했습니다.\n"
                      "상품명을 포함해 다시 보내 주세요.")

    price = K.guess_price(text)
    pct, amt = K.guess_discount(text)

    body = build_text(title, price, share_url, "toss", pct, amt)
    save(conn, "toss", product_id, title, price, product_url,
         share_url, text, pct, amt)
    header = (f"🛒 토스 · {f'{price:,}원' if price else '가격 미확인'} "
              f"· 내 링크 그대로 사용")
    return (header, body), None


def handle_coupang(conn, text, deal_url):
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
                      "coupang", pct, amt)
    save(conn, "coupang", product_id, title, price, product_url,
         my_link, text, pct, amt)
    header = (f"🛒 쿠팡 · {f'{price:,}원' if price else '가격 미확인'} "
              f"· 내 링크로 교체됨")
    return (header, body), None


def handle_message(conn, text):
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
        return handle_coupang(conn, text, coupang.group(0))

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
        return handle_toss(conn, text, toss.group(0))

    return None, HELP


# ---------------------------------------------------------------- 메인

def process(conn, token, chat_id, msg):
    chat = str((msg.get("chat") or {}).get("id", ""))
    text = msg.get("text") or msg.get("caption") or ""

    # 사장님 대화만 받는다. 봇 주소를 아는 다른 사람이 쓰면 안 된다.
    if chat_id and chat != str(chat_id):
        log(f"모르는 대화 {chat} 무시")
        return

    log(f"수신: {text[:60]!r}")
    try:
        result, err = handle_message(conn, text)
    except (AssertionError, ValueError) as e:
        # 고지 문구 문제는 절대 넘어가면 안 된다.
        result, err = None, f"본문을 만들 수 없습니다: {e}"
    except Exception as e:
        result, err = None, f"처리 중 오류: {e}"
        log(f"오류: {e}")

    if result:
        header, body = result
        send_body(token, chat, header, body)
        log(f"회신: {header}")
    else:
        send_text(token, chat, err)
        log("회신: 안내문")


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
