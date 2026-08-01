#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
subscribers.py — 봇에게 /start 를 보낸 사람 목록

**왜 있는가**

전에는 주기 작업이 만든 문구가 `.env` 의 `TG_CHAT_ID` **한 명**에게만
갔다. 소유자 지시(2026-08-01)로 **봇에 /start 한 사람 전원**에게 보낸다.

**권한은 두 종류다. 섞으면 안 된다.**

| 구분 | 누구 | 할 수 있는 것 |
|---|---|---|
| 구독자 | /start 한 사람 누구나 | 주기 작업이 만든 문구를 **받는다** |
| 관리자 | `TG_CHAT_ID` + `TG_ADMIN_IDS` | 링크를 보내 **변환을 시킨다** |

변환을 아무나 시킬 수 있으면 안 된다. 변환 한 번이 곧 소유자 PC 의
브라우저로 쿠팡 파트너스 / 토스에 접근하는 것이라, 모르는 사람이 연달아
보내면 CLAUDE.md 제약 2(접근 빈도)를 그대로 깨뜨린다. 계정이 정지되면
구독자가 몇 명이든 의미가 없다.

받는 것은 누구나 되어도 안전하다. 링크는 어차피 소유자 것이고, 많이
퍼질수록 소유자 수익이 는다.
"""

import os
from datetime import datetime

from schema import ensure_subscribers


def admin_ids():
    """변환을 시킬 수 있는 chat_id 집합.

    `TG_CHAT_ID` 는 소유자 본인이다. 추가로 `TG_ADMIN_IDS` 에 쉼표로
    나열할 수 있다. 비어 있으면 **아무도 관리자가 아니다** — 그 편이
    모르는 사람이 브라우저를 돌리게 두는 것보다 낫다.
    """
    out = set()
    for key in ("TG_CHAT_ID", "TG_ADMIN_IDS"):
        for part in (os.environ.get(key) or "").replace(";", ",").split(","):
            part = part.strip()
            if part:
                out.add(part)
    return out


def is_admin(chat_id):
    return str(chat_id) in admin_ids()


def add(conn, chat):
    """/start 를 받았다. (신규인가?) 를 돌려준다.

    이미 있던 사람이 다시 /start 하면 다시 켠다. 차단했다가 푼 경우다.
    """
    ensure_subscribers(conn)
    cid = str(chat.get("id"))
    name = " ".join(filter(None, [chat.get("first_name"),
                                  chat.get("last_name")])) or chat.get("title")
    row = conn.execute("SELECT active FROM subscribers WHERE chat_id=?",
                       (cid,)).fetchone()
    now = datetime.now().isoformat(timespec="seconds")
    if row is None:
        conn.execute(
            "INSERT INTO subscribers (chat_id,username,name,joined_at,active) "
            "VALUES (?,?,?,?,1)",
            (cid, chat.get("username"), name, now))
        conn.commit()
        return True
    conn.execute(
        "UPDATE subscribers SET active=1, stopped_at=NULL, last_error=NULL, "
        "username=?, name=? WHERE chat_id=?",
        (chat.get("username"), name, cid))
    conn.commit()
    return not row[0]


def stop(conn, chat_id, reason=None):
    """구독을 끈다. /stop 을 받았거나 텔레그램이 차단을 알려줬을 때."""
    ensure_subscribers(conn)
    conn.execute(
        "UPDATE subscribers SET active=0, stopped_at=?, last_error=? "
        "WHERE chat_id=?",
        (datetime.now().isoformat(timespec="seconds"), reason, str(chat_id)))
    conn.commit()


def bump(conn, chat_id):
    conn.execute(
        "UPDATE subscribers SET sent_count=sent_count+1 WHERE chat_id=?",
        (str(chat_id),))
    conn.commit()


def active(conn):
    """보낼 대상 목록. `.env` 의 TG_CHAT_ID 는 항상 포함한다.

    소유자가 자기 봇에 /start 를 안 했더라도 자기 것은 받아야 한다.
    무인 운영에서 '아무한테도 안 갔는데 성공으로 끝남' 이 제일 나쁘다.
    """
    ensure_subscribers(conn)
    ids = [r[0] for r in conn.execute(
        "SELECT chat_id FROM subscribers WHERE active=1 ORDER BY joined_at")]
    owner = (os.environ.get("TG_CHAT_ID") or "").strip()
    if owner and owner not in ids:
        ids.insert(0, owner)
    return ids


def count(conn):
    ensure_subscribers(conn)
    a, t = conn.execute(
        "SELECT SUM(CASE WHEN active=1 THEN 1 ELSE 0 END), COUNT(*) "
        "FROM subscribers").fetchone()
    return (a or 0), (t or 0)


def listing(conn):
    ensure_subscribers(conn)
    return conn.execute(
        "SELECT chat_id, username, name, joined_at, active, sent_count, "
        "last_error FROM subscribers ORDER BY joined_at").fetchall()


# 텔레그램이 '이 사람은 더 못 보낸다' 고 알려주는 문구들.
# 일시적인 오류(네트워크, 429)와 구분해야 한다. 일시적인 것까지 꺼 버리면
# 멀쩡한 구독자가 조용히 사라진다.
PERMANENT = ("bot was blocked by the user", "user is deactivated",
             "chat not found", "bot was kicked", "peer_id_invalid",
             "have no rights to send")


def is_permanent(msg):
    m = (msg or "").lower()
    return any(p in m for p in PERMANENT)
