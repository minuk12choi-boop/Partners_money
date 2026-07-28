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

BASE = "https://graph.threads.net/v1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "deals.db")
CSV_PATH = os.path.join(HERE, "pending.csv")
TOKEN_PATH = os.path.join(HERE, "token.json")

# ── 정책 관련 상수 ────────────────────────────────────────────────
# 쿠팡 파트너스 필수 고지. 절대 지우지 말 것.
DISCLOSURE = "이 게시물은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
MAX_CHARS = 500          # 스레드 본문 제한
DAILY_CAP = 5            # API 한도(250)가 아니라 스팸 판정 방지용 자체 캡
MIN_GAP_MINUTES = 90     # 발행 간 최소 간격
# ─────────────────────────────────────────────────────────────────

TEMPLATES = [
    "{title}\n\n{price_line}가격 확인해보세요 👉 {url}\n\n{disclosure}",
    "{title}\n{price_line}\n{url}\n\n{disclosure}",
    "이거 지금 {price_line}이네요.\n\n{title}\n{url}\n\n{disclosure}",
]


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

def build_text(title, price, url):
    price_line = f"{price:,}원 " if price else ""
    body = random.choice(TEMPLATES).format(
        title=title.strip(), price_line=price_line,
        url=url.strip(), disclosure=DISCLOSURE,
    )
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    # 500자 초과 시 제목 쪽을 줄인다. 고지 문구는 절대 자르지 않는다.
    if len(body) > MAX_CHARS:
        over = len(body) - MAX_CHARS + 1
        title = title[: max(10, len(title) - over)] + "…"
        body = random.choice(TEMPLATES).format(
            title=title, price_line=price_line,
            url=url.strip(), disclosure=DISCLOSURE,
        )
        body = re.sub(r"\n{3,}", "\n\n", body).strip()[:MAX_CHARS]

    assert DISCLOSURE in body, "고지 문구가 누락됨 — 발행 중단"
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

def load_ready_rows():
    if not os.path.exists(CSV_PATH):
        sys.exit(f"{CSV_PATH} 가 없습니다. 먼저 kakao_deal_extract.py 를 실행하세요.")
    rows = []
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            aff = (r.get("affiliate_url(직접 채우세요)") or "").strip()
            if aff.startswith("http"):
                rows.append({
                    "product_id": r["product_id"],
                    "title": r["title"],
                    "price": int(r["price"]) if r["price"] else None,
                    "affiliate_url": aff,
                })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="발행하지 않고 문구만 출력")
    ap.add_argument("--refresh-token", action="store_true")
    ap.add_argument("--cap", type=int, default=DAILY_CAP)
    args = ap.parse_args()

    if args.refresh_token:
        refresh_token()
        return

    rows = load_ready_rows()
    if not rows:
        print("affiliate_url 이 채워진 항목이 없습니다.")
        return

    conn = sqlite3.connect(DB_PATH)
    already = posted_today(conn)
    remaining = max(0, args.cap - already)
    print(f"오늘 발행 {already}건 / 캡 {args.cap}건 → 남은 슬롯 {remaining}건\n")

    if args.dry_run:
        for r in rows[: args.cap]:
            print("─" * 50)
            print(build_text(r["title"], r["price"], r["affiliate_url"]))
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
        done = conn.execute("SELECT posted_at FROM deals WHERE product_id=?",
                            (r["product_id"],)).fetchone()
        if done and done[0]:
            continue

        text = build_text(r["title"], r["price"], r["affiliate_url"])
        try:
            post_id = publish(uid, tok, text)
        except requests.HTTPError as e:
            print(f"발행 실패 [{r['product_id']}]: {e.response.text[:300]}")
            continue

        conn.execute(
            "UPDATE deals SET affiliate_url=?, posted_at=? WHERE product_id=?",
            (r["affiliate_url"], datetime.now().isoformat(timespec="seconds"),
             r["product_id"]),
        )
        conn.commit()
        print(f"발행 완료 {post_id}  [{r['product_id']}] {r['title'][:40]}")

        gap = MIN_GAP_MINUTES * 60 + random.randint(-600, 600)
        print(f"  다음 발행까지 {gap // 60}분 대기")
        time.sleep(gap)

    conn.close()


if __name__ == "__main__":
    main()
