#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
subscribers.py — 봇에게 /start 를 보낸 사람 목록

**왜 있는가**

전에는 주기 작업이 만든 문구가 `.env` 의 `TG_CHAT_ID` **한 명**에게만
갔다. 소유자 지시(2026-08-01)로 **봇에 /start 한 사람 전원**에게 보낸다.

**권한** (2026-08-02 갱신)

| 구분 | 누구 | 할 수 있는 것 |
|---|---|---|
| 구독자 | /start 한 사람 누구나 | 문구를 **받는다** + 링크를 보내 **변환을 시킨다** |
| 관리자 | `TG_CHAT_ID` + `TG_ADMIN_IDS` | 위 전부 + 구독자 목록 조회 |

소유자 지시로 변환을 구독자 전원에게 열었다. 전에는 관리자만이었다.

**속도 제한은 기본으로 꺼져 있다** (2026-08-03 소유자 지시).
한때 총량으로 묶었지만 "사용자가 최대 2명" 이라 껐다. 몇 명이 쓰는지는
소유자가 아는 사실이다. 사람이 늘면 `.env` 로 되살린다 — 아래
`convert_allowed()` 참고.

받는 것은 누구나 되어도 안전하다. 링크는 어차피 소유자 것이고, 많이
퍼질수록 소유자 수익이 는다.
"""

import os
from datetime import datetime, timedelta

from schema import ensure_subscribers, ensure_conversions


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


def is_subscribed(conn, chat_id):
    """변환을 시킬 수 있는 사람인가.

    소유자 지시(2026-08-02)로 **구독자 전원**이 시킬 수 있다.
    관리자는 `/start` 를 안 눌렀어도 된다.
    """
    if is_admin(chat_id):
        return True
    ensure_subscribers(conn)
    r = conn.execute("SELECT active FROM subscribers WHERE chat_id=?",
                     (str(chat_id),)).fetchone()
    return bool(r and r[0])


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


# ── 변환 속도 제한 — 기본은 **꺼져 있다** ─────────────────────────
#
# 소유자 지시(2026-08-03)로 껐다. 요청이 오는 대로 바로 처리한다.
#
#   "제약 풀고 요청할때마다 확인되도록해라.
#    어차피 아는 사람끼리 쓰는거라 최대 2명이 사용자다. 그냥해라."
#
# 앞서 총량 제한을 넣었던 이유는 이랬다: 변환 한 번이 곧 이 PC 의
# 브라우저로 쿠팡·토스에 접속하는 것이라, 사람이 늘면 접근도 사람 수만큼
# 늘어난다는 것. 그 걱정은 **사용자가 2명이면 성립하지 않는다.** 사람이
# 몇인지는 소유자가 아는 사실이고, 판단은 소유자 몫이다.
#
# ⚠️ CLAUDE.md 제약 2 자체는 그대로다. 그건 **주기 작업**의 값
# (MAX_LINKS_PER_CYCLE = 4, 링크 생성 사이 sleep(6))을 말하고, 그 둘은
# 손대지 않았다. 여기서 끈 것은 사람이 직접 보낼 때의 추가 관문이다.
#
# 다시 켜려면 `.env` 에 숫자를 넣으면 된다. 0 은 '제한 없음' 이다.
# 사람이 늘면 켜는 것을 고려할 것.
CONVERT_PER_HOUR = int(os.environ.get("CONVERT_PER_HOUR", "0"))
CONVERT_PER_HOUR_EACH = int(os.environ.get("CONVERT_PER_HOUR_EACH", "0"))
CONVERT_MIN_GAP = int(os.environ.get("CONVERT_MIN_GAP", "0"))


def _since(hours=1):
    return (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")


def convert_allowed(conn, chat_id):
    """지금 변환을 시켜도 되는가. (된다, 안 되면 이유).

    **기본값에서는 항상 True 다.** 아래는 `.env` 로 제한을 켰을 때의 동작이다.

    켜면: 관리자는 1인당 제한을 받지 않지만 전체 총량과 간격은 지킨다.
    사이트가 보는 것은 누가 눌렀는지가 아니라 얼마나 자주 닿았는지다.
    """
    # 셋 다 0 이면 제한이 없다. 기본값이 이쪽이다(소유자 지시 2026-08-03).
    if not (CONVERT_PER_HOUR or CONVERT_PER_HOUR_EACH or CONVERT_MIN_GAP):
        return True, ""

    ensure_conversions(conn)
    now = datetime.now()

    last = conn.execute("SELECT MAX(at) FROM conversions").fetchone()[0]
    if CONVERT_MIN_GAP and last:
        try:
            gap = (now - datetime.fromisoformat(last)).total_seconds()
        except ValueError:
            gap = CONVERT_MIN_GAP
        if gap < CONVERT_MIN_GAP:
            return False, (f"조금만 천천히요. {int(CONVERT_MIN_GAP - gap) + 1}초 뒤에 "
                           "다시 보내 주세요.")

    if CONVERT_PER_HOUR:
        total = conn.execute("SELECT COUNT(*) FROM conversions WHERE at >= ?",
                             (_since(),)).fetchone()[0]
        if total >= CONVERT_PER_HOUR:
            return False, ("지금은 변환이 밀렸습니다. 한 시간에 "
                           f"{CONVERT_PER_HOUR}건까지만 됩니다.\n"
                           "쿠팡·토스에 너무 자주 접속하면 계정이 막혀서 둔 제한입니다.\n"
                           "잠시 뒤에 다시 보내 주세요.")

    if CONVERT_PER_HOUR_EACH and not is_admin(chat_id):
        mine = conn.execute(
            "SELECT COUNT(*) FROM conversions WHERE chat_id=? AND at >= ?",
            (str(chat_id), _since())).fetchone()[0]
        if mine >= CONVERT_PER_HOUR_EACH:
            return False, (f"한 시간에 {CONVERT_PER_HOUR_EACH}건까지만 됩니다.\n"
                           "잠시 뒤에 다시 보내 주세요.")

    return True, ""


def convert_record(conn, chat_id):
    ensure_conversions(conn)
    conn.execute("INSERT INTO conversions (chat_id, at) VALUES (?,?)",
                 (str(chat_id), datetime.now().isoformat(timespec="seconds")))
    # 오래된 기록은 지운다. 속도를 재는 데만 쓰므로 하루면 충분하다.
    conn.execute("DELETE FROM conversions WHERE at < ?", (_since(24),))
    conn.commit()
