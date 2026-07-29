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
