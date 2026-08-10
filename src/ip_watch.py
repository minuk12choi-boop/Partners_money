#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ip_watch.py — 공인 IP 가 바뀌면 텔레그램으로 알린다

**왜 있는가**

토스 공식 API 는 **등록한 출발 IP 에서 나가는 요청만** 통과시킨다.
가정용 회선은 대개 유동 IP 라 공유기를 재부팅하거나 통신사가 갱신하면
주소가 바뀐다. 그 순간부터 API 가 막히는데, 무인 운영 중이라 **아무도
모른 채 며칠이 지날 수 있다.** 무인 운영에서 최악은 조용히 멈추는 것이다.

그래서 주기마다 지금 IP 를 확인하고, 마지막으로 본 값과 다르면 알린다.
API 가 실제로 살아 있는지(health)도 함께 본다 — IP 는 그대로인데 막히는
경우(키 만료, 등록 해제)도 있기 때문이다.

    py src/ip_watch.py            # 확인하고 바뀌었으면 알린다
    py src/ip_watch.py --show     # 지금 IP 만 보여준다
    py src/ip_watch.py --force    # 안 바뀌었어도 알림을 보낸다(경로 시험)
"""

import argparse
import json
import os
import sys
from datetime import datetime

import requests

from env import load_env

load_env()

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "ip_state.json")

# 한 곳만 믿지 않는다. 서비스가 죽거나 엉뚱한 값을 줄 수 있는데,
# 그걸로 "IP 가 바뀌었다" 고 잘못 알리면 사람이 헛걸음한다.
SOURCES = [
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://ipv4.icanhazip.com",
]


def log(msg=""):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def current_ip():
    """지금 공인 IP. 두 곳 이상이 같은 값을 줘야 인정한다.

    과반이 안 나오면 None 을 돌려준다. 확신이 없으면 알리지 않는다 —
    거짓 경보가 반복되면 진짜 경보도 무시하게 된다.
    """
    votes = {}
    for url in SOURCES:
        try:
            ip = requests.get(url, timeout=10).text.strip()
        except Exception:
            continue
        if ip:
            votes[ip] = votes.get(ip, 0) + 1
    if not votes:
        return None
    best = max(votes, key=votes.get)
    return best if votes[best] >= 2 else None


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(ip):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump({"ip": ip,
                   "checked_at": datetime.now().isoformat(timespec="seconds")},
                  f)


def notify(text):
    """텔레그램으로 알린다. 관리자에게만 — 남이 알 일이 아니다."""
    token = os.environ.get("TG_BOT_TOKEN")
    chat = os.environ.get("TG_CHAT_ID")
    if not (token and chat):
        log("TG_BOT_TOKEN/TG_CHAT_ID 가 없어 알리지 못했습니다")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": text,
                  "disable_web_page_preview": "true"}, timeout=25)
        return r.json().get("ok", False)
    except Exception as e:
        log(f"알림 전송 실패: {e}")
        return False


def check(force=False):
    """(바뀌었나, 지금 IP). 바뀌었으면 알린다."""
    ip = current_ip()
    if not ip:
        log("공인 IP 를 확인하지 못했습니다 (네트워크 문제로 보임)")
        return False, None

    old = load_state().get("ip")
    changed = bool(old and old != ip)
    save_state(ip)

    if not old:
        log(f"현재 IP {ip} — 처음 기록합니다")
        if force:
            notify(f"📍 출발 IP 를 기록했습니다: {ip}")
        return False, ip

    if not changed and not force:
        log(f"현재 IP {ip} — 그대로입니다")
        return False, ip

    # API 가 실제로 막혔는지 함께 본다. 사람이 다음에 뭘 해야 하는지가
    # 달라지기 때문이다.
    api_line = ""
    try:
        import toss_openapi
        if toss_openapi.configured():
            ok, why = toss_openapi.health(log=lambda *a: None)
            api_line = ("\n\n토스 API: ✅ 아직 정상입니다"
                        if ok else
                        f"\n\n토스 API: 🔴 막혔습니다\n{why[:150]}")
    except Exception as e:
        api_line = f"\n\n토스 API 확인 실패: {str(e)[:100]}"

    if changed:
        msg = ("⚠️ 공인 IP 가 바뀌었습니다.\n\n"
               f"이전: {old}\n"
               f"현재: {ip}\n\n"
               "토스 쉐어링크 관리자에서 출발 IP 를 새 값으로 다시 등록해 주세요.\n"
               "등록 전까지 토스 링크 발급이 막힙니다."
               + api_line)
        log(f"IP 변경 {old} → {ip} — 알립니다")
    else:
        msg = f"📍 현재 출발 IP: {ip}{api_line}"
        log("강제 알림")

    # 보냈는지 반드시 남긴다. 알림이 조용히 실패하면 IP 가 바뀐 것도
    # 모르고, 알림이 안 온 것도 모른다. 두 번 놓치는 셈이다.
    if notify(msg):
        log("텔레그램 알림 보냄")
    else:
        log("🔴 텔레그램 알림을 보내지 못했습니다")
    return changed, ip


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="공인 IP 변경 감시")
    ap.add_argument("--show", action="store_true", help="지금 IP 만 출력")
    ap.add_argument("--force", action="store_true",
                    help="안 바뀌었어도 알림을 보낸다(경로 시험)")
    args = ap.parse_args()

    if args.show:
        ip = current_ip()
        print(ip or "확인 실패")
        return

    changed, ip = check(force=args.force)
    # 바뀐 것은 '실패' 가 아니다. 알렸으면 할 일을 한 것이다.
    sys.exit(0 if ip else 1)


if __name__ == "__main__":
    main()
