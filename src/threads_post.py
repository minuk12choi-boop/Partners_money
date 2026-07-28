#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
threads_post.py

pending.csv 에서 affiliate_url 이 채워진 항목을 골라 스레드에 발행한다.

사용법:
    python threads_post.py --dry-run      # 발행할 문구만 출력 (먼저 이걸로 확인)
    python threads_post.py                # 실제 발행
    python threads_post.py --refresh-token

환경변수 (.env 또는 시스템 환경변수):
    THREADS_USER_ID      = 숫자 ID
    THREADS_ACCESS_TOKEN = 장기 액세스 토큰 (60일)

의존성:
    pip install requests
"""

import argparse
import csv
import json
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, date

import requests

from env import load_env

# `.env` 를 환경변수로 올린다. 예전에는 이걸 하는 코드가 없어서
# `.env` 에 적은 값이 아무 효과가 없었다(실측 2026-07-29).
load_env()

BASE = "https://graph.threads.net/v1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "deals.db")
CSV_PATH = os.path.join(HERE, "pending.csv")
TOKEN_PATH = os.path.join(HERE, "token.json")

# ── 정책 관련 상수 ────────────────────────────────────────────────
# 쿠팡 파트너스 필수 고지. 절대 지우지 말 것.
#
# ⚠️ 이 문구는 쿠팡이 지정한 것을 **한 글자도 바꾸지 않고** 써야 한다.
# 링크 생성 화면이 직접 이렇게 안내한다(실측 2026-07-29):
#
#   "1. 게시글 작성 시, 아래 문구를 반드시 기재해 주세요.
#    "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의
#     수수료를 제공받습니다."
#    쿠팡 파트너스의 활동은 공정거래위원회의 심사지침에 따라 추천,
#    보증인인 파트너스 회원과 당사의 경제적 이해관계에 대하여
#    공개하여야 합니다."
#
# 예전 값은 '이 게시물은' 으로 시작했다. 쿠팡이 지정한 것은
# '이 포스팅은' 이다. 공정위 심사지침이 걸린 문구라 임의로 바꾸면 안 된다.
DISCLOSURE = "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."

# 토스쇼핑 쉐어링크 필수 고지. 문구도 위치 규정도 쿠팡과 다르다.
# 운영 정책상 "게시물 제목이나 첫 부분에" 노출해야 한다.
# 두 문구를 섞어 쓰면 양쪽 다 위반이다. (docs/toss_sharelink.md 참고)
#
# 이쪽은 토스가 발급 시 클립보드에 넣어주는 문구와 글자 단위로 일치함을
# 확인했다(실측 2026-07-29). 앞에 붙는 '✱ ' 는 토스 UI 의 장식이다.
TOSS_DISCLOSURE = "이 포스팅은 토스쇼핑 쉐어링크 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."

DISCLOSURES = {
    "coupang": DISCLOSURE,
    "toss": TOSS_DISCLOSURE,
}

MAX_CHARS = 500          # 스레드 본문 제한
DAILY_CAP = 5            # API 한도(250)가 아니라 스팸 판정 방지용 자체 캡
MIN_GAP_MINUTES = 90     # 발행 간 최소 간격
# ─────────────────────────────────────────────────────────────────

# 쿠팡: 고지를 본문 끝에 둔다
TEMPLATES = [
    "{title}\n\n{price_line}가격 확인해보세요 👉 {url}\n\n{disclosure}",
    "{title}\n{price_line}\n{url}\n\n{disclosure}",
    "이거 지금 {price_line}이네요.\n\n{title}\n{url}\n\n{disclosure}",
]

# 토스: 고지를 반드시 맨 앞에 둔다. 순서를 바꾸지 말 것.
TOSS_TEMPLATES = [
    "{disclosure}\n\n{title}\n\n{price_line}가격 확인해보세요 👉 {url}",
    "{disclosure}\n\n{title}\n{price_line}\n{url}",
]

TEMPLATES_BY_PLATFORM = {
    "coupang": TEMPLATES,
    "toss": TOSS_TEMPLATES,
}


# ---------------------------------------------------------------- 토큰

def load_token():
    tok = os.environ.get("THREADS_ACCESS_TOKEN")
    uid = os.environ.get("THREADS_USER_ID")
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, encoding="utf-8") as f:
            data = json.load(f)
        tok = data.get("access_token", tok)
        uid = data.get("user_id", uid)
    if not tok or not uid:
        sys.exit("THREADS_ACCESS_TOKEN / THREADS_USER_ID 가 설정되지 않았습니다.")
    return uid, tok


def refresh_token():
    """장기 토큰은 60일 만료. 50일쯤에 크론으로 이걸 돌려두면 끊기지 않는다."""
    uid, tok = load_token()
    r = requests.get(f"{BASE}/refresh_access_token",
                     params={"grant_type": "th_refresh_token", "access_token": tok},
                     timeout=20)
    r.raise_for_status()
    data = r.json()
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        json.dump({"user_id": uid,
                   "access_token": data["access_token"],
                   "refreshed_at": datetime.now().isoformat(timespec="seconds"),
                   "expires_in": data.get("expires_in")}, f, ensure_ascii=False, indent=2)
    print(f"토큰 갱신 완료. 만료까지 {data.get('expires_in', 0) // 86400}일")


# ---------------------------------------------------------------- 문구 생성

def build_text(title, price, url, platform="coupang"):
    """플랫폼에 맞는 고지 문구와 배치로 본문을 만든다.

    쿠팡은 본문 끝, 토스는 반드시 맨 앞에 고지가 와야 한다.
    두 문구를 섞어 쓰면 양쪽 정책을 다 어긴다.
    """
    disclosure = DISCLOSURES.get(platform)
    templates = TEMPLATES_BY_PLATFORM.get(platform)
    if disclosure is None or templates is None:
        raise ValueError(f"알 수 없는 플랫폼: {platform!r} — 고지 문구를 정할 수 없어 중단합니다")

    price_line = f"{price:,}원 " if price else ""
    tpl = random.choice(templates)
    body = tpl.format(title=title.strip(), price_line=price_line,
                      url=url.strip(), disclosure=disclosure)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    # 500자 초과 시 제목 쪽을 줄인다. 고지 문구는 절대 자르지 않는다.
    if len(body) > MAX_CHARS:
        over = len(body) - MAX_CHARS + 1
        title = title[: max(10, len(title) - over)] + "…"
        body = tpl.format(title=title, price_line=price_line,
                          url=url.strip(), disclosure=disclosure)
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        # 잘라낼 때도 고지가 살아남아야 한다.
        # 토스는 고지가 맨 앞이라 뒤에서 자르고, 쿠팡은 맨 뒤라 앞에서 자른다.
        if len(body) > MAX_CHARS:
            body = body[:MAX_CHARS] if platform == "toss" else body[-MAX_CHARS:]

    assert disclosure in body, f"[{platform}] 고지 문구가 누락됨 — 발행 중단"
    # 다른 플랫폼의 고지가 섞이면 양쪽 다 위반이다.
    for other, text in DISCLOSURES.items():
        if other != platform:
            assert text not in body, f"[{platform}] 다른 플랫폼({other}) 고지가 섞임 — 발행 중단"
    if platform == "toss":
        assert body.startswith(disclosure), "토스 고지는 첫 부분에 와야 함 — 발행 중단"
    return body


# ---------------------------------------------------------------- 발행

def check_quota(uid, tok):
    r = requests.get(f"{BASE}/{uid}/threads_publishing_limit",
                     params={"fields": "quota_usage,config", "access_token": tok},
                     timeout=20)
    r.raise_for_status()
    d = r.json()["data"][0]
    used = d.get("quota_usage", 0)
    total = d.get("config", {}).get("quota_total", 250)
    return used, total


def publish(uid, tok, text):
    # 1) 컨테이너 생성
    r = requests.post(f"{BASE}/{uid}/threads",
                      data={"media_type": "TEXT", "text": text, "access_token": tok},
                      timeout=30)
    r.raise_for_status()
    container_id = r.json()["id"]

    # 2) 처리 대기 — 문서 권장값은 30초, 텍스트만이면 더 짧아도 되지만 안전하게
    time.sleep(30)

    # 3) 발행
    r = requests.post(f"{BASE}/{uid}/threads_publish",
                      data={"creation_id": container_id, "access_token": tok},
                      timeout=30)
    r.raise_for_status()
    return r.json()["id"]


def posted_today(conn):
    today = date.today().isoformat()
    row = conn.execute(
        "SELECT COUNT(*) FROM deals WHERE posted_at LIKE ?", (today + "%",)
    ).fetchone()
    return row[0]


# ---------------------------------------------------------------- 메인

def load_ready_rows(conn):
    """발행 대상을 deals.db 에서 읽는다.

    예전에는 pending.csv 를 읽었는데, 그 CSV 의 affiliate_url 열은
    kakao_deal_extract.py 가 매 주기 새로 쓰면서 비워졌다. 반면 링크는
    partners_link.py 가 DB 에만 넣는다. 그래서 실제로는 발행 대상이
    영원히 0건이었고, 종료코드 0 으로 조용히 끝나 알림도 안 갔다.
    CLAUDE.md 가 "모든 상태는 deals.db 에 저장한다" 고 못 박은 이유다.
    """
    rows = conn.execute(
        "SELECT platform, product_id, title, price, affiliate_url FROM deals "
        "WHERE affiliate_url IS NOT NULL AND affiliate_url != '' "
        "AND posted_at IS NULL ORDER BY found_at DESC"
    ).fetchall()
    return [{"platform": r[0], "product_id": r[1], "title": r[2],
             "price": r[3], "affiliate_url": r[4]}
            for r in rows if str(r[4]).startswith("http")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="발행하지 않고 문구만 출력")
    ap.add_argument("--refresh-token", action="store_true")
    ap.add_argument("--cap", type=int, default=DAILY_CAP)
    args = ap.parse_args()

    if args.refresh_token:
        refresh_token()
        return

    conn = sqlite3.connect(DB_PATH)
    rows = load_ready_rows(conn)
    if not rows:
        # 무인 운영에서 조용한 0건은 조용한 정지와 구분이 안 된다.
        # 왜 0건인지 알 수 있게 상태를 함께 찍는다.
        total, linked, posted = conn.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN affiliate_url IS NOT NULL AND affiliate_url != '' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN posted_at IS NOT NULL THEN 1 ELSE 0 END) FROM deals"
        ).fetchone()
        print(f"발행 대상이 없습니다. (deals 총 {total or 0}건 / "
              f"링크 생성됨 {linked or 0} / 발행됨 {posted or 0})")
        if (total or 0) > 0 and (linked or 0) == 0:
            print("→ 링크가 하나도 생성되지 않았습니다. partners_link.py 를 확인하세요.")
        conn.close()
        return

    already = posted_today(conn)
    remaining = max(0, args.cap - already)
    print(f"오늘 발행 {already}건 / 캡 {args.cap}건 → 남은 슬롯 {remaining}건\n")

    if args.dry_run:
        for r in rows[: args.cap]:
            print("─" * 50)
            print(f"[{r['platform']}:{r['product_id']}]")
            print(build_text(r["title"], r["price"], r["affiliate_url"], r["platform"]))
        print("─" * 50)
        conn.close()
        return

    if remaining == 0:
        print("오늘 캡 도달. 종료합니다.")
        conn.close()
        return

    uid, tok = load_token()
    used, total = check_quota(uid, tok)
    print(f"Threads API 사용량 {used}/{total}\n")

    for r in rows[:remaining]:
        done = conn.execute(
            "SELECT posted_at FROM deals WHERE platform=? AND product_id=?",
            (r["platform"], r["product_id"])).fetchone()
        if done and done[0]:
            continue

        try:
            text = build_text(r["title"], r["price"], r["affiliate_url"], r["platform"])
        except (AssertionError, ValueError) as e:
            # 고지 문구 문제는 넘어가면 안 되는 사안이다. 건너뛰고 로그에 남긴다.
            print(f"본문 생성 거부 [{r['platform']}:{r['product_id']}]: {e}")
            continue

        try:
            post_id = publish(uid, tok, text)
        except requests.HTTPError as e:
            print(f"발행 실패 [{r['platform']}:{r['product_id']}]: {e.response.text[:300]}")
            continue

        conn.execute(
            "UPDATE deals SET posted_at=? WHERE platform=? AND product_id=?",
            (datetime.now().isoformat(timespec="seconds"),
             r["platform"], r["product_id"]),
        )
        conn.commit()
        print(f"발행 완료 {post_id}  [{r['platform']}:{r['product_id']}] {r['title'][:40]}")

        gap = MIN_GAP_MINUTES * 60 + random.randint(-600, 600)
        print(f"  다음 발행까지 {gap // 60}분 대기")
        time.sleep(gap)

    conn.close()


if __name__ == "__main__":
    main()
