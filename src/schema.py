#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
schema.py — `deals` 테이블 정의의 단일 출처

**왜 있는가**

스키마가 세 곳에 흩어져 있었다.

  kakao_deal_extract.DEALS_SCHEMA   전체 정의
  toss_link.ensure_schema()         복붙한 옛 정의
  telegram_deliver.ensure_sent_column()  자기가 쓸 컬럼만

그래서 컬럼을 하나 추가하면 나머지가 조용히 어긋났다. 실제로
`discount_pct` 를 추가했더니 `telegram_deliver.py` 가 "no such column" 으로
죽었고, `toss_link.py` 는 새 DB 를 만들 때 옛 스키마로 만들고 있었다.

어느 스크립트가 먼저 실행될지 정해져 있지 않다(사용자가 직접 돌리기도
하고 run_all.py 가 돌리기도 한다). 그러니 **모두 같은 정의를 봐야 한다.**

새 컬럼을 추가할 때는 `ADDED_COLUMNS` 에 한 줄만 넣으면 된다.
SQLite 는 `ALTER TABLE ADD COLUMN` 만 지원하므로 이 방식으로 충분하다.
"""

BASE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS deals (
        platform     TEXT NOT NULL DEFAULT 'coupang',
        product_id   TEXT NOT NULL,
        title        TEXT,
        price        INTEGER,
        source_url   TEXT,
        product_url  TEXT,
        raw_message  TEXT,
        chat_time    TEXT,
        found_at     TEXT,
        affiliate_url TEXT,
        posted_at    TEXT,
        PRIMARY KEY (platform, product_id)
    )
"""

# 처음 설계 이후에 추가된 컬럼들. 옛 DB 를 만나면 하나씩 붙인다.
ADDED_COLUMNS = [
    ("sent_at",        "TEXT"),     # 텔레그램으로 보낸 시각
    ("discount_pct",   "INTEGER"),  # 할인율 %
    ("discount_amt",   "INTEGER"),  # 평균가 대비 할인액(원) — 쿠팡 딜방 표기
    ("original_price", "INTEGER"),  # 정가 — 토스 대시보드 API 의 originalPrice
]


def ensure_deals(conn, verbose=True):
    """`deals` 테이블이 최신 스키마가 되도록 보장한다.

    이미 있으면 빠진 컬럼만 붙인다. 데이터는 건드리지 않는다.
    """
    conn.execute(BASE_SCHEMA)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(deals)")]
    for name, typ in ADDED_COLUMNS:
        if name not in cols:
            if verbose:
                print(f"deals 테이블에 {name} 컬럼을 추가합니다...")
            conn.execute(f"ALTER TABLE deals ADD COLUMN {name} {typ}")
    conn.commit()


# ── 구독자 ────────────────────────────────────────────────────────
# 봇에게 /start 를 보낸 사람들. 주기 작업이 만든 문구는 여기 있는
# 사람 전원에게 간다. 전에는 `.env` 의 TG_CHAT_ID 한 명에게만 갔다.
#
# `active` 를 두는 이유: 사람이 봇을 차단하면 텔레그램이 403 을 준다.
# 그때 지우지 않고 꺼 둔다. 지워 버리면 다음 주기에 또 보내려다 또
# 403 을 받고, 왜 실패하는지도 남지 않는다.
SUBSCRIBERS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS subscribers (
        chat_id    TEXT PRIMARY KEY,
        username   TEXT,
        name       TEXT,
        joined_at  TEXT,
        active     INTEGER NOT NULL DEFAULT 1,
        stopped_at TEXT,
        last_error TEXT,
        sent_count INTEGER NOT NULL DEFAULT 0
    )
"""


def ensure_subscribers(conn, verbose=True):
    conn.execute(SUBSCRIBERS_SCHEMA)
    conn.commit()


# ── 누가 무엇을 받았는가 ──────────────────────────────────────────
# **왜 필요한가** (2026-08-02 실측)
#
# 전에는 `deals.sent_at` 하나로 판정했다. 딜 하나가 **누구에게든** 나가면
# 그 딜은 '보냄' 으로 잠긴다. 그래서 나중에 /start 한 사람은 그 딜을
# 영원히 못 받는다. 실제로 그렇게 됐다: 구독자 1명이 08:58 에 가입했는데
# 링크 44건이 전부 08:55 이전에 '보냄' 으로 잠겨 있어 0건을 받았다.
# 소유자에게만 가고 구독자에게는 아무것도 안 갔다.
#
# 그래서 (딜, 사람) 단위로 기록한다. 이러면
#   · 늦게 가입한 사람도 자기가 못 받은 것을 받는다
#   · 이미 받은 사람에게 두 번 가지 않는다
#
# `deals.sent_at` 은 남겨 둔다. '한 명에게라도 나갔다' 는 뜻으로 계속
# 쓰이고(리포트·오늘 N번째), 없애면 옛 기록의 뜻이 사라진다.
DELIVERIES_SCHEMA = """
    CREATE TABLE IF NOT EXISTS deliveries (
        platform   TEXT NOT NULL,
        product_id TEXT NOT NULL,
        chat_id    TEXT NOT NULL,
        sent_at    TEXT,
        PRIMARY KEY (platform, product_id, chat_id)
    )
"""


def ensure_deliveries(conn, verbose=True):
    conn.execute(DELIVERIES_SCHEMA)

    # 옛 DB 를 위한 한 번짜리 이관.
    #
    # 이미 보낸 딜(sent_at 있음)은 소유자에게 간 것이다. 그 기록이 없으면
    # 이 코드로 바꾼 직후 소유자가 지난 딜을 통째로 다시 받는다.
    n = conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
    if n:
        return
    import os
    owner = (os.environ.get("TG_CHAT_ID") or "").strip()
    if not owner:
        conn.commit()
        return
    cur = conn.execute(
        "INSERT OR IGNORE INTO deliveries (platform, product_id, chat_id, sent_at) "
        "SELECT platform, product_id, ?, sent_at FROM deals "
        "WHERE sent_at IS NOT NULL", (owner,))
    if verbose and cur.rowcount:
        print(f"이미 보낸 {cur.rowcount}건을 소유자({owner}) 수신 기록으로 옮깁니다...")
    conn.commit()


# ── 변환 요청 기록 ────────────────────────────────────────────────
# 봇으로 링크 변환을 시킨 기록. 접근 속도를 재는 데만 쓴다.
#
# **왜 필요한가** (2026-08-02 소유자 지시)
#
# 전에는 변환을 관리자만 할 수 있었다. 소유자가 "텔레그램에 들어온 사람은
# 모두 되게 하라, 어차피 아는 사람이 쓴다" 고 정했다. 그 판단은 따른다.
#
# 다만 변환 한 번이 곧 이 PC 의 브라우저로 쿠팡 파트너스·토스에 접속하는
# 것이다. 사람이 늘면 접근 빈도가 사람 수만큼 늘고, 그러면 CLAUDE.md
# 제약 2(접근 빈도를 올리지 마라)가 사람 손으로 깨진다. 제약 2 는 완화
# 대상이 아니다.
#
# 그래서 **권한은 열되 속도는 총량으로 묶는다.** 누가 시키든 사이트에
# 닿는 빈도는 그대로다. 그 판정에 이 표를 쓴다.
CONVERSIONS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS conversions (
        chat_id TEXT NOT NULL,
        at      TEXT NOT NULL
    )
"""


def ensure_conversions(conn, verbose=True):
    conn.execute(CONVERSIONS_SCHEMA)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_conversions_at "
                 "ON conversions (at)")
    conn.commit()


def ensure_all(conn, verbose=True):
    ensure_deals(conn, verbose)
    ensure_subscribers(conn, verbose)
    ensure_deliveries(conn, verbose)
    ensure_conversions(conn, verbose)
