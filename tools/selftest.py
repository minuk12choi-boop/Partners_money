#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest.py — 브라우저·네트워크·카톡 없이 돌려 보는 자체 시험

    py tools\\selftest.py

**왜 있는가**

이 프로젝트의 진짜 검증은 실제 환경에서만 된다(카톡 창, 파트너스 DOM,
토스 대시보드). 하지만 그중에는 **틀려도 조용히 넘어가는** 로직이 섞여
있다. 방장 링크를 내 링크로 착각한다든지, 할인액을 판매가로 읽는다든지,
차단당한 구독자를 지워 버린다든지.

그런 것들은 실제 환경 없이도 확인할 수 있다. 여기서 한다.

**여기서 통과했다고 실제로 돈다는 뜻이 아니다.** 브라우저를 만나는
부분(`issue_link`, `get_link`, `kakao_export`)은 가짜로 대체한다.
그쪽은 `py src\\doctor.py` 와 실제 실행으로 확인한다.

새 규칙을 만들 때마다 여기에 한 줄씩 추가할 것. 특히 **"이걸 틀리면
남의 링크를 발행한다" 류**는 반드시 넣는다.
"""

import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

# `.env` 의 실제 값이 시험에 끼어들지 않게 한다. 소유자 chat_id 가
# 들어와 있으면 권한 시험이 통째로 무의미해진다.
#
# ⚠️ 지우는 것만으로는 부족하다. `load_env()` 는 `_loaded` 로 딱 한 번만
#    `.env` 를 읽는데, 그 첫 호출이 **시험 도중**에 일어난다. src 모듈을
#    시험 함수 안에서 늦게 import 하기 때문이다. 그래서 여기서 지워도
#    나중에 import 되는 순간 되살아난다.
#
#    실측: TG_CHAT_ID 를 지운 뒤 `import telegram_deliver` 하면
#    6658586550(소유자 실제 chat_id)으로 부활했다. 그래서 전달 시험이
#    소유자 PC 에서만 깨졌다 — 로직이 아니라 격리가 샌 것이다.
#
#    '이미 읽었다' 고 표시해 `.env` 를 아예 안 읽게 만든다. 세 키만
#    막으면 다른 값이 또 새므로 통째로 막는다.
import env as _env
_env._loaded = True

# 시스템 환경변수로 직접 설정된 경우까지 막는다(`.env` 밖의 경로).
for k in ("TG_CHAT_ID", "TG_ADMIN_IDS", "TG_BOT_TOKEN"):
    os.environ.pop(k, None)

FAILED = []
PASSED = [0]


def check(name, got, want):
    if got == want:
        PASSED[0] += 1
        print(f"  OK   {name}")
    else:
        FAILED.append(name)
        print(f"  실패 {name}")
        print(f"       기대: {want!r}")
        print(f"       실제: {got!r}")


def section(title):
    print(f"\n── {title}")


def fresh_db():
    from schema import ensure_all
    conn = sqlite3.connect(":memory:")
    ensure_all(conn, verbose=False)
    return conn


# ---------------------------------------------------------------- 구독자

def test_subscribers():
    section("구독자 — 등록·재개·차단·해지")
    import subscribers as S
    conn = fresh_db()

    check("첫 /start 는 신규", S.add(conn, {"id": 111, "first_name": "가"}), True)
    check("두 번째 /start 는 신규 아님",
          S.add(conn, {"id": 111, "first_name": "가"}), False)

    S.stop(conn, 111, "차단")
    check("해지하면 안 보낸다", S.active(conn), [])
    check("해지해도 지우지 않는다", S.count(conn), (0, 1))
    check("다시 /start 하면 신규 취급",
          S.add(conn, {"id": 111, "first_name": "가"}), True)

    os.environ["TG_CHAT_ID"] = "999"
    check("TG_CHAT_ID 는 /start 안 해도 받는다",
          S.active(conn), ["999", "111"])
    check("TG_CHAT_ID 는 관리자", S.is_admin("999"), True)
    check("구독자는 관리자가 아니다", S.is_admin("111"), False)

    os.environ["TG_ADMIN_IDS"] = "111, 222"
    check("TG_ADMIN_IDS 로 관리자 추가",
          sorted(S.admin_ids()), ["111", "222", "999"])
    for k in ("TG_CHAT_ID", "TG_ADMIN_IDS"):
        os.environ.pop(k, None)

    # 이걸 틀리면 멀쩡한 구독자가 조용히 사라지거나,
    # 차단당한 사람에게 영원히 재시도하게 된다.
    check("차단은 영구 실패",
          S.is_permanent("텔레그램 오류 403: Forbidden: bot was blocked by the user"),
          True)
    check("429 는 영구 실패가 아니다",
          S.is_permanent("텔레그램 오류 429: Too Many Requests"), False)
    conn.close()


def test_broadcast():
    section("전달 — 한 명이 막아도 나머지는 간다")
    import subscribers as S
    import telegram_deliver as D
    conn = fresh_db()
    for cid in (101, 102, 103):
        S.add(conn, {"id": cid, "first_name": "x"})

    def fake_send(token, chat_id, header, body):
        if str(chat_id) == "102":
            raise RuntimeError("텔레그램 오류 403: Forbidden: bot was blocked by the user")
        if str(chat_id) == "103":
            raise RuntimeError("텔레그램 오류 429: Too Many Requests")
        return {}

    real_send, real_sleep = D.send, D.SLEEP_BETWEEN_SUBS
    D.send, D.SLEEP_BETWEEN_SUBS = fake_send, 0
    try:
        ok, fail = D.broadcast(conn, "t", S.active(conn), "헤더", "본문")
    finally:
        D.send, D.SLEEP_BETWEEN_SUBS = real_send, real_sleep

    check("성공/실패 집계", (ok, fail), (1, 2))
    check("차단한 사람만 빠진다", S.active(conn), ["101", "103"])
    conn.close()


# ---------------------------------------------------------------- 봇 분기

ROOM_TOSS = """✅ 실리콘 주방장갑 2P 내열 방수
🚨 역대 최저가 7,990원 🚨
↳ 평균가 대비 🔻 16,810원 🔻 67%
https://toss.im/_m/roomLink1
쉐어링크를 통해 수수료를 받습니다"""

ROOM_COUPANG = """✅ 대상 종가 총각김치 2.3kg
🚨 역대 최저가 17,330원 🚨
https://link.coupang.com/a/fLz3Rghg5s"""


def test_routing():
    section("봇 분기 — 방장 링크와 내 링크를 구분하는가")
    import telegram_bot as B
    conn = fresh_db()

    saved = (B.handle_toss, B.handle_toss_room, B.handle_coupang)
    B.handle_toss = lambda c, t, u, s=None: (("MINE", u), None)
    B.handle_toss_room = lambda c, t, u: (("ROOM", u), None)
    B.handle_coupang = lambda c, t, u, s=None: (("COUPANG", u), None)
    try:
        def route(text):
            r, _ = B.handle_message(conn, text)
            return r[0] if r else "HELP"

        # 이 네 줄이 이 프로젝트에서 제일 비싼 실수를 막는다.
        # ROOM 으로 안 가면 방장 링크를 그대로 발행하게 된다.
        check("딜방 토스 글 → 내 링크 새로 발급", route(ROOM_TOSS), "ROOM")
        check("맨 토스 상품주소 → 내 링크 새로 발급",
              route("https://toss.shopping/t/524516537"), "ROOM")
        check("/deal 을 붙이면 강제로 새로 발급",
              route("/deal https://toss.im/_m/myLink"), "ROOM")
        check("내가 보낸 쉐어링크 → 그대로 사용",
              route("https://toss.im/_m/myLink 7990 24800"), "MINE")
        check("딜방 쿠팡 글 → 내 딥링크", route(ROOM_COUPANG), "COUPANG")
        check("링크 없으면 안내문", route("안녕하세요"), "HELP")
    finally:
        B.handle_toss, B.handle_toss_room, B.handle_coupang = saved
    conn.close()


def test_permission():
    section("권한 — 받는 것은 누구나, 시키는 것은 관리자만")
    import telegram_bot as B
    conn = fresh_db()
    os.environ["TG_CHAT_ID"] = "777"

    sent = []
    saved = (B.send_text, B.send_body, B.handle_message)
    B.send_text = lambda tok, chat, text: sent.append((str(chat), text))
    B.send_body = lambda tok, chat, h, b: sent.append((str(chat), "BODY"))
    B.handle_message = lambda c, t, s=None: (("헤더", "본문"), None)
    try:
        def msg(cid, text):
            return {"chat": {"id": cid, "first_name": "홍"}, "text": text}

        B.process(conn, "t", msg(555, "/start"))
        check("아무나 구독된다", sent[-1][0], "555")

        sent.clear()
        B.process(conn, "t", msg(555, "https://toss.im/_m/x"))
        check("구독자는 변환을 못 시킨다", sent[-1][1], B.NOT_ADMIN)

        sent.clear()
        B.process(conn, "t", msg(777, "https://toss.im/_m/x"))
        check("관리자는 변환된다", sent[-1][1], "BODY")
    finally:
        B.send_text, B.send_body, B.handle_message = saved
        os.environ.pop("TG_CHAT_ID", None)
    conn.close()


def test_manual_prices():
    section("직접 입력한 가격 — 할인액을 판매가로 읽지 않는가")
    import telegram_bot as B
    L = "https://toss.im/_m/abc"
    check("두 개면 작은 쪽이 판매가",
          B.parse_manual_prices(f"{L} 7990 24800"), (7990, 24800))
    check("쉼표·'원'·'정가' 가 섞여도 된다",
          B.parse_manual_prices(f"{L} 7,990원 정가 24,800원"), (7990, 24800))
    check("하나면 판매가만", B.parse_manual_prices(f"{L} 7990"), (7990, None))
    check("/deal 을 붙여도 읽는다",
          B.parse_manual_prices(f"/deal {L} 7990 24800"), (7990, 24800))
    # 여기가 핵심이다. 딜방 글에는 금액이 둘 있는데 작은 쪽은 '할인액'이다.
    # 덥석 집으면 16,810원짜리 상품을 7,990원이라고 발행하는 게 아니라
    # 그 반대 — 할인액을 가격으로 발행하게 된다.
    check("딜방 글에는 손대지 않는다",
          B.parse_manual_prices(ROOM_TOSS), (None, None))


# ---------------------------------------------------------------- 토스 발급

def test_find_card():
    section("토스 카드 찾기 — 애매하면 포기하는가")
    import toss_link as T
    cards = [
        {"idx": 0, "title": "실리콘 주방장갑 2P 내열 방수"},
        {"idx": 1, "title": "맥스앤맥스 밀폐용기 10종"},
        {"idx": 2, "title": "맥스앤맥스 밀폐용기 10종"},
        {"idx": 3, "title": "메이빈 팰리세이드 대시보드커버"},
    ]

    def idx(want):
        c = T.find_card(cards, want)
        return c["idx"] if c else None

    check("정확히 맞으면 찾는다", idx("실리콘 주방장갑 2P 내열 방수"), 0)
    check("공백 차이는 무시한다", idx("실리콘  주방장갑 2P 내열 방수"), 0)
    check("부분 일치도 하나뿐이면 찾는다", idx("메이빈 팰리세이드"), 3)
    # 아무거나 고르면 다른 상품의 링크를 그 상품이라고 발행하게 된다.
    check("같은 이름이 둘이면 포기한다", idx("맥스앤맥스 밀폐용기 10종"), None)
    check("없으면 None", idx("없는 상품"), None)


def test_issue_guard():
    section("토스 발급 — 다른 상품이 나오면 버리는가")
    import toss_link as T

    class FakePage:
        url = "https://sharelink.toss.im/links/recommended-products"

    saved = (T.fetch_dashboard, T.scan_cards, T.issue_link, T.resolve_product_id)
    T.fetch_dashboard = lambda p, log=None: {
        "524516537": {"title": "실리콘 주방장갑", "price": 7990}}
    T.scan_cards = lambda p: [{"idx": 0, "title": "실리콘 주방장갑"}]
    T.resolve_product_id = lambda p, link: None
    quiet = lambda m: None
    try:
        T.issue_link = lambda p, i, wait=20: ("https://toss.im/_m/OK", "524516537")
        link, _ = T.issue_for_product(FakePage(), "524516537", log=quiet)
        check("맞는 상품이면 쓴다", link, "https://toss.im/_m/OK")

        T.issue_link = lambda p, i, wait=20: ("https://toss.im/_m/WRONG", "999")
        link, why = T.issue_for_product(FakePage(), "524516537", log=quiet)
        check("다른 상품이면 버린다", (link, why[0]), (None, "mismatch"))

        T.issue_link = lambda p, i, wait=20: ("https://toss.im/_m/X", None)
        link, why = T.issue_for_product(FakePage(), "524516537", log=quiet)
        check("상품 확인이 안 되면 버린다", (link, why[0]), (None, "mismatch"))

        T.issue_link = lambda p, i, wait=20: ("https://toss.im/_m/Y", "1")
        link, why = T.issue_for_product(FakePage(), "없는ID", log=quiet)
        check("목록에 없으면 발급 시도조차 안 한다",
              (link, why[0]), (None, "dashboard_miss"))
    finally:
        (T.fetch_dashboard, T.scan_cards, T.issue_link,
         T.resolve_product_id) = saved


def test_room_to_mine():
    section("딜방 토스 글 → 내 링크 (전 구간, 발급만 가짜)")
    import kakao_deal_extract as K
    import telegram_bot as B
    import toss_link as T
    conn = fresh_db()

    saved = (K.resolve_toss_url, T.issue_one)
    K.resolve_toss_url = lambda u, s, timeout=12: (
        "https://toss.shopping/t/524516537", "524516537")
    T.issue_one = lambda pid, log=None: (
        "https://toss.im/_m/MYOWN", {"title": "실리콘 주방장갑 2P",
                                     "price": 7990, "original_price": 24800,
                                     "discount_pct": 67})
    try:
        r, err = B.handle_message(conn, ROOM_TOSS)
        body = r[1] if r else ""
        check("문구가 만들어진다", bool(r), True)
        check("내 링크가 들어간다", "toss.im/_m/MYOWN" in body, True)
        # 이 한 줄이 수익을 지킨다.
        check("방장 링크는 어디에도 없다", "roomLink1" in body, False)
        check("고지 문구가 맨 앞", body.startswith("이 포스팅은 토스쇼핑"), True)
        check("가격은 딜방 글에서", "7,990원" in body, True)
        check("DB 에 남는다",
              conn.execute("SELECT affiliate_url FROM deals").fetchone()[0],
              "https://toss.im/_m/MYOWN")

        # 두 번째로 같은 글이 오면 다시 발급하면 안 된다. 발급 횟수는 아껴야 한다.
        T.issue_one = lambda pid, log=None: (_ for _ in ()).throw(
            AssertionError("이미 있는 링크인데 재발급했다"))
        r2, _ = B.handle_message(conn, ROOM_TOSS)
        check("같은 상품은 재발급하지 않는다",
              "toss.im/_m/MYOWN" in (r2[1] if r2 else ""), True)

        # 대시보드에 없으면 방장 링크로 대신하지 않고 거절해야 한다.
        conn.execute("DELETE FROM deals")
        T.issue_one = lambda pid, log=None: (None, ("dashboard_miss", "없음"))
        r3, err3 = B.handle_message(conn, ROOM_TOSS)
        check("목록 밖이면 문구를 만들지 않는다", r3, None)
        check("이유를 설명한다", "대시보드 목록에 없어서" in (err3 or ""), True)
        check("방장 링크를 대신 주지 않는다", "roomLink1" in (err3 or ""), False)
    finally:
        K.resolve_toss_url, T.issue_one = saved
    conn.close()


# ---------------------------------------------------------------- 고지 문구

def test_disclosure():
    section("고지 문구 — CLAUDE.md 제약 1")
    from threads_post import build_text, DISCLOSURES

    coupang = build_text("상품", 1000, "https://link.coupang.com/a/x", "coupang")
    toss = build_text("상품", 1000, "https://toss.im/_m/x", "toss")

    check("쿠팡 고지가 맨 앞",
          coupang.startswith(DISCLOSURES["coupang"]), True)
    check("토스 고지가 맨 앞", toss.startswith(DISCLOSURES["toss"]), True)
    check("쿠팡 문구에 토스 고지가 섞이지 않는다",
          DISCLOSURES["toss"] in coupang, False)
    check("토스 문구에 쿠팡 고지가 섞이지 않는다",
          DISCLOSURES["coupang"] in toss, False)

    # 제목이 아무리 길어도 고지와 링크는 안 잘린다.
    long_body = build_text("가" * 800, 1000, "https://toss.im/_m/x", "toss")
    check("긴 제목에서도 고지 유지",
          long_body.startswith(DISCLOSURES["toss"]), True)
    check("긴 제목에서도 링크 유지", "https://toss.im/_m/x" in long_body, True)
    check("500자 제한 지킴", len(long_body) <= 500, True)

    try:
        build_text("상품", 1000, "https://x", "네이버")
        check("모르는 플랫폼은 거부", "통과함", "ValueError")
    except ValueError:
        check("모르는 플랫폼은 거부", "ValueError", "ValueError")


# ---------------------------------------------------------------- 메인

TESTS = [test_subscribers, test_broadcast, test_routing, test_permission,
         test_manual_prices, test_find_card, test_issue_guard,
         test_room_to_mine, test_disclosure]


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("=" * 60)
    print("자체 시험 — 브라우저·네트워크·카톡 없이 확인할 수 있는 것만")
    print("=" * 60)

    for t in TESTS:
        try:
            t()
        except Exception as e:
            import traceback
            FAILED.append(t.__name__)
            print(f"  실패 {t.__name__} — 예외")
            traceback.print_exc()

    print()
    print("=" * 60)
    if FAILED:
        print(f"🔴 {len(FAILED)}개 실패 / {PASSED[0]}개 통과")
        for f in FAILED:
            print(f"     - {f}")
        print()
        print("고치기 전에 왜 그렇게 돼 있었는지부터 볼 것.")
        print("여기 있는 규칙은 대부분 '틀리면 남의 링크를 발행한다' 류다.")
        sys.exit(1)
    print(f"🟢 {PASSED[0]}개 전부 통과")
    print()
    print("※ 브라우저·카톡을 만나는 부분은 여기서 확인되지 않는다.")
    print("   py src\\doctor.py 와 실제 실행으로 확인할 것.")


if __name__ == "__main__":
    main()
