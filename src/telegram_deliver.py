#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
telegram_deliver.py — 발행할 문구를 텔레그램으로 보낸다

Threads 자동 발행 대신 **사람이 직접 올리는** 경로다(소유자 결정 2026-07-29).
링크와 완성된 본문을 텔레그램으로 받아서, 그대로 복사해 스레드에 붙여넣는다.

이렇게 바꾸면 Meta 앱 심사 리스크가 통째로 사라진다. 대신 무엇을 언제
올릴지는 사람이 정한다.

⚠️ **본문은 `threads_post.build_text()` 를 그대로 쓴다.** 고지 문구 규칙과
`assert` 3개(누락 / 타 플랫폼 혼입 / 토스 고지 위치)를 여기서 다시 구현하면
두 곳이 어긋난다. 사람이 복사해 붙여넣는 순간 그 문구가 곧 게시물이므로
자동 발행 때와 똑같이 엄격해야 한다.

사용법

    py src/telegram_deliver.py --whoami     # chat_id 알아내기 (최초 1회)
    py src/telegram_deliver.py --dry-run    # 보내지 않고 문구만 확인
    py src/telegram_deliver.py              # 실제 전송
    py src/telegram_deliver.py --limit 3
    py src/telegram_deliver.py --resend 5140279812   # 이미 보낸 걸 다시

설정 (.env)

    TG_BOT_TOKEN=@BotFather 에서 받은 토큰
    TG_CHAT_ID=--whoami 로 알아낸 숫자

의존성: requests (requirements.txt 에 이미 있음)
"""

import argparse
import html
import os
import sqlite3
import sys
import time
from datetime import datetime, date, timedelta

import requests

from env import load_env
from schema import ensure_deals
from threads_post import build_text   # 고지 문구 규칙의 단일 출처

load_env()

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "deals.db")

API = "https://api.telegram.org/bot{token}/{method}"

# 한 번에 너무 많이 보내면 텔레그램이 막는다. 초당 여러 건은 피한다.
SLEEP_BETWEEN = 1.5

# 이 시간보다 오래된 딜은 보내지 않는다.
#
# 이 방의 딜은 **선착순이고 물량이 소진되면 끝난다.** 하루 지난 딜을
# 보내는 것은 도움이 안 되고, 이미 품절된 링크를 올리면 신뢰만 잃는다.
#
# 실용적인 효과가 하나 더 있다. 이 필터가 없으면 DB 에 쌓인 300건 넘는
# 예전 딜이 주기마다 8건씩 링크로 만들어져 텔레그램을 덮는다. 쓰지도 않을
# 링크를 만드느라 파트너스 사이트에 접근하는 것은 CLAUDE.md 제약 2 의
# 취지에도 어긋난다. 신선한 것만 처리하면 접근 횟수가 오히려 줄어든다.
MAX_DEAL_AGE_HOURS = int(os.environ.get("MAX_DEAL_AGE_HOURS", "12"))

PLATFORM_LABEL = {"coupang": "쿠팡", "toss": "토스"}


def log(msg=""):
    print(msg, flush=True)


# ---------------------------------------------------------------- 텔레그램

def call(method, token, **params):
    r = requests.post(API.format(token=token, method=method),
                      data=params, timeout=25)
    try:
        j = r.json()
    except Exception:
        raise RuntimeError(f"HTTP {r.status_code} {r.text[:300]}")
    if not j.get("ok"):
        raise RuntimeError(
            f"텔레그램 오류 {j.get('error_code')}: {j.get('description')}")
    return j["result"]


def do_whoami(token):
    """chat_id 를 알아낸다.

    getUpdates 는 봇이 받은 메시지를 돌려준다. 그래서 **먼저 봇에게 아무
    말이나 한 번 걸어야** 한다. 말을 건 적이 없으면 결과가 비어 있다.
    이게 chat_id 를 못 찾는 가장 흔한 이유다.
    """
    me = call("getMe", token)
    log(f"봇 확인: @{me.get('username')} ({me.get('first_name')})")
    log("")

    updates = call("getUpdates", token, timeout=0)
    chats = {}
    for u in updates:
        msg = (u.get("message") or u.get("edited_message")
               or u.get("channel_post") or {})
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat

    if not chats:
        log("🔴 아직 아무 대화도 없습니다.")
        log("")
        log(f"  1. 텔레그램에서 @{me.get('username')} 를 검색해 대화를 엽니다")
        log("  2. '시작' 을 누르거나 아무 메시지나 보냅니다")
        log("  3. 이 명령을 다시 실행합니다")
        log("")
        log("  ※ 봇은 자기에게 말을 건 적이 있는 상대만 알 수 있습니다.")
        return False

    log(f"찾은 대화 {len(chats)}개:")
    log("")
    for cid, chat in chats.items():
        who = (chat.get("title")
               or " ".join(filter(None, [chat.get("first_name"),
                                         chat.get("last_name")]))
               or chat.get("username") or "?")
        log(f"    TG_CHAT_ID={cid}      ({chat.get('type')}: {who})")
    log("")
    log("위 줄을 저장소 루트의 `.env` 에 붙여넣으세요.")
    if len(chats) > 1:
        log("여러 개면 사장님 본인과의 1:1 대화(type=private)를 쓰세요.")
    return True


def send(token, chat_id, header, body):
    """헤더 한 줄 + 복사용 본문 블록.

    본문을 <pre> 로 감싼다. 텔레그램에서 코드 블록은 탭 한 번으로 통째
    복사되므로, 사장님이 그대로 스레드에 붙여넣을 수 있다.
    헤더는 블록 밖에 두어 복사본에 섞이지 않게 한다.
    """
    text = f"{html.escape(header)}\n<pre>{html.escape(body)}</pre>"
    return call("sendMessage", token,
                chat_id=chat_id, text=text, parse_mode="HTML",
                disable_web_page_preview="true")


# ---------------------------------------------------------------- DB

def load_pending(conn, resend=None):
    cols = ("platform, product_id, title, price, affiliate_url, "
            "discount_pct, discount_amt, original_price")
    if resend:
        rows = conn.execute(
            f"SELECT {cols} FROM deals "
            "WHERE product_id = ? AND affiliate_url IS NOT NULL "
            "AND affiliate_url != ''", (resend,)).fetchall()
    else:
        # 오래된 딜은 보내지 않는다. 핫딜은 선착순이라 이미 소진됐을 가능성이
        # 높고, 늦게 올리면 신뢰만 잃는다. found_at 기준이다.
        rows = conn.execute(
            f"SELECT {cols} FROM deals "
            "WHERE affiliate_url IS NOT NULL AND affiliate_url != '' "
            "AND sent_at IS NULL AND posted_at IS NULL "
            "AND found_at >= ? "
            "ORDER BY found_at DESC",
            ((datetime.now() - timedelta(hours=MAX_DEAL_AGE_HOURS))
             .isoformat(timespec="seconds"),)).fetchall()
    return [{"platform": r[0], "product_id": r[1], "title": r[2],
             "price": r[3], "affiliate_url": r[4],
             "discount_pct": r[5], "discount_amt": r[6],
             "original_price": r[7]}
            for r in rows if str(r[4]).startswith("http")]


def sent_today(conn):
    today = date.today().isoformat()
    return conn.execute(
        "SELECT COUNT(*) FROM deals WHERE sent_at LIKE ?", (today + "%",)
    ).fetchone()[0]


def mark_sent(conn, platform, product_id):
    conn.execute("UPDATE deals SET sent_at=? WHERE platform=? AND product_id=?",
                 (datetime.now().isoformat(timespec="seconds"),
                  platform, product_id))
    conn.commit()


# ---------------------------------------------------------------- 메인

def make_header(row, n_today):
    label = PLATFORM_LABEL.get(row["platform"], row["platform"])
    price = f"{row['price']:,}원" if row.get("price") else "가격?"
    return f"🛒 {label} · {price} · 오늘 {n_today}번째"


def main():
    ap = argparse.ArgumentParser(
        description="발행할 문구를 텔레그램으로 보낸다 (사람이 직접 올리는 경로)")
    ap.add_argument("--whoami", action="store_true",
                    help="chat_id 를 알아낸다 (최초 1회)")
    ap.add_argument("--dry-run", action="store_true",
                    help="보내지 않고 문구만 출력")
    ap.add_argument("--limit", type=int, default=0,
                    help="한 번에 보낼 최대 개수 (0 = 제한 없음)")
    ap.add_argument("--resend", metavar="상품ID",
                    help="이미 보낸 건을 다시 보낸다")
    args = ap.parse_args()

    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")

    if args.whoami:
        if not token:
            raise SystemExit(
                "TG_BOT_TOKEN 이 없습니다.\n"
                "  텔레그램에서 @BotFather 에게 /newbot 을 보내 봇을 만들고,\n"
                "  받은 토큰을 저장소 루트 `.env` 에 적으세요.\n"
                "    TG_BOT_TOKEN=123456:AA...")
        sys.exit(0 if do_whoami(token) else 1)

    conn = sqlite3.connect(DB_PATH)
    ensure_deals(conn)
    rows = load_pending(conn, args.resend)

    if not rows:
        # 무인 운영에서 조용한 0건은 조용한 정지와 구분이 안 된다.
        # 왜 0건인지 알 수 있게 상태를 함께 찍는다.
        total, linked, sent = conn.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN affiliate_url IS NOT NULL AND affiliate_url != '' "
            "         THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN sent_at IS NOT NULL THEN 1 ELSE 0 END) FROM deals"
        ).fetchone()
        log(f"보낼 항목이 없습니다. (deals 총 {total or 0}건 / "
            f"링크 생성됨 {linked or 0} / 이미 보냄 {sent or 0})")
        if (total or 0) > 0 and (linked or 0) == 0:
            log("→ 링크가 하나도 없습니다. partners_link.py / toss_link.py 를 확인하세요.")
        conn.close()
        return

    if args.limit > 0:
        rows = rows[: args.limit]

    n_today = sent_today(conn)
    # 하루 개수 제한은 두지 않는다(소유자 결정 2026-07-29). 사람이 골라서
    # 올리므로 발행량은 사람이 조절한다. 다만 오늘 몇 건째인지는 알려 준다.
    # 몰아서 올리면 도달이 떨어지는 것은 자동이든 수동이든 같다.
    log(f"보낼 항목 {len(rows)}건 (오늘 이미 보낸 것 {n_today}건)\n")

    if args.dry_run:
        for i, r in enumerate(rows, 1):
            try:
                body = build_text(r["title"], r["price"], r["affiliate_url"],
                                  r["platform"], r.get("discount_pct"),
                                  r.get("discount_amt"), r.get("original_price"))
            except (AssertionError, ValueError) as e:
                log(f"본문 생성 거부 [{r['platform']}:{r['product_id']}]: {e}")
                continue
            log("─" * 50)
            log(make_header(r, n_today + i))
            log(body)
        log("─" * 50)
        log("dry-run: 보내지 않았습니다.")
        conn.close()
        return

    if not (token and chat_id):
        missing = [k for k, v in (("TG_BOT_TOKEN", token),
                                  ("TG_CHAT_ID", chat_id)) if not v]
        raise SystemExit(
            "설정이 없습니다: " + ", ".join(missing) + "\n"
            "  `py src/telegram_deliver.py --whoami` 로 chat_id 를 알아낸 뒤\n"
            "  저장소 루트 `.env` 에 적으세요.")

    ok = fail = 0
    for i, r in enumerate(rows, 1):
        tag = f"{r['platform']}:{r['product_id']}"
        try:
            body = build_text(r["title"], r["price"], r["affiliate_url"],
                              r["platform"], r.get("discount_pct"),
                              r.get("discount_amt"), r.get("original_price"))
        except (AssertionError, ValueError) as e:
            # 고지 문구 문제는 넘어가면 안 되는 사안이다. 보내지 않는다.
            log(f"본문 생성 거부 [{tag}]: {e}")
            fail += 1
            continue

        try:
            send(token, chat_id, make_header(r, n_today + ok + 1), body)
        except Exception as e:
            log(f"전송 실패 [{tag}]: {e}")
            fail += 1
            continue

        mark_sent(conn, r["platform"], r["product_id"])
        ok += 1
        log(f"보냄 [{tag}] {r['title'][:40]}")
        if i < len(rows):
            time.sleep(SLEEP_BETWEEN)

    conn.close()
    log(f"\n전송 결과: 성공 {ok} / 실패 {fail}")
    if fail:
        # 무인 운영에서 실패를 조용히 넘기면 안 된다.
        sys.exit(1)


if __name__ == "__main__":
    main()
