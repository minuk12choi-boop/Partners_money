#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
toss_openapi.py — 토스 쉐어링크 **공식 API**로 링크를 발급한다

**왜 중요한가**

전에는 브라우저를 띄워 쉐어링크 대시보드를 긁고 '링크 발급' 버튼을
클릭했다. 그 방식에는 두 가지 한계가 있었다.

  1. **대시보드 큐레이션 목록(약 120개) 안의 상품만** 발급할 수 있었다.
     딜방에 올라오는 토스 딜은 대부분 그 목록 밖이라 거절됐다.
     실측(2026-08-02): 딜방에서 고른 3건이 셋 다 `dashboard_miss`.
  2. 로그인 세션이 만료되면 멈췄다. 사람이 브라우저에서 다시 로그인해야 했다.

공식 API 는 **상품 ID 를 직접 지정해서** 발급한다. 위 두 한계가 모두
사라진다. 실측(2026-08-03): 대시보드에서 거절됐던 그 상품들이 API 로는
바로 발급됐다.

    2376060177 맛팜 국내산 미니족    → https://toss.im/_m/L1ivsQXk
    2387171175 맥스앤맥스 밀폐용기   → https://toss.im/_m/XHpfLqc8

**설정** (`.env`)

    TOSS_ACCESS_KEY=...      쉐어링크 관리자에서 발급
    TOSS_SECRET_KEY=...
    TOSS_MEMBER_ID=...       회원연동 ID (= publisherId)

⚠️ **출발 IP 를 등록해야 한다.** 등록한 IP 에서 나가는 요청만 통과한다.
유동 IP 면 바뀌는 순간 조용히 막히므로 `ip_watch.py` 가 감시한다.
"""

import json
import os
import time

import requests

from env import load_env

load_env()

TOKEN_URL = "https://oauth2.cert.toss.im/token"
BASE = "https://sharelink.toss.im/openapi"
SCOPE = "sharelink:read sharelink:write"

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(HERE, "toss_token.json")

# ⚠️ 문서에는 토큰 수명이 31,535,999초(약 1년)라고 적혀 있다.
# **실측은 3599초(1시간)다**(2026-08-03). 문서를 믿고 오래 캐시하면
# 한 시간 뒤부터 조용히 401 이 난다. 그래서 응답의 expires_in 을 쓰고,
# 그마저도 만료 1분 전에 미리 새로 받는다.
REFRESH_MARGIN = 60


class TossApiError(RuntimeError):
    pass


def configured():
    return bool(os.environ.get("TOSS_ACCESS_KEY")
                and os.environ.get("TOSS_SECRET_KEY")
                and os.environ.get("TOSS_MEMBER_ID"))


def _load_token():
    try:
        with open(TOKEN_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("expires_at", 0) - REFRESH_MARGIN > time.time():
            return d["access_token"]
    except Exception:
        pass
    return None


def _save_token(tok, expires_in):
    try:
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            json.dump({"access_token": tok,
                       "expires_at": time.time() + int(expires_in)}, f)
    except Exception:
        pass        # 캐시를 못 써도 매번 받으면 되므로 치명적이지 않다


def get_token(force=False, log=print):
    """액세스 토큰. 살아 있으면 캐시를 쓴다."""
    if not force:
        tok = _load_token()
        if tok:
            return tok

    if not configured():
        raise TossApiError("TOSS_ACCESS_KEY / SECRET_KEY / MEMBER_ID 가 없습니다")

    r = requests.post(TOKEN_URL, timeout=20, data={
        "grant_type": "client_credentials",
        "client_id": os.environ["TOSS_ACCESS_KEY"],
        "client_secret": os.environ["TOSS_SECRET_KEY"],
        "scope": SCOPE,
    })
    if r.status_code != 200:
        raise TossApiError(f"토큰 발급 실패 HTTP {r.status_code}: {r.text[:200]}")
    d = r.json()
    _save_token(d["access_token"], d.get("expires_in", 3500))
    log(f"  토스 토큰 발급 (유효 {d.get('expires_in')}초)")
    return d["access_token"]


def _call(method, path, log=print, **kw):
    """토큰이 만료됐으면 한 번만 다시 받고 재시도한다."""
    for attempt in (1, 2):
        tok = get_token(force=(attempt == 2), log=log)
        r = requests.request(
            method, f"{BASE}{path}", timeout=25,
            headers={"Authorization": f"Bearer {tok}",
                     "Content-Type": "application/json"}, **kw)
        if r.status_code == 401 and attempt == 1:
            log("  토큰 만료로 보임 — 다시 받는다")
            continue
        return r
    return r


def health(log=print):
    """인증·IP 등록·라우팅이 정상인지. (정상인가, 설명)."""
    try:
        r = _call("GET", "/health", log=log)
    except Exception as e:
        return False, str(e)
    if r.status_code == 200:
        return True, "ok"
    # IP 가 안 맞으면 여기서 걸린다. 원문을 그대로 넘겨 원인을 보이게 한다.
    return False, f"HTTP {r.status_code}: {r.text[:200]}"


def issue(taca_id, log=print):
    """상품 ID 로 **내** 쉐어링크를 발급한다.

    (링크, 정보) 또는 (None, (사유코드, 설명)).
    `toss_link.issue_one()` 과 같은 모양으로 돌려준다 — 부르는 쪽이
    브라우저 방식과 API 방식을 구분하지 않아도 되게 하기 위함이다.
    """
    if not configured():
        return None, ("no_api", "공식 API 키가 설정되지 않았습니다")

    try:
        r = _call("POST", "/links", log=log,
                  json={"tacaId": int(taca_id),
                        "publisherId": os.environ["TOSS_MEMBER_ID"]})
    except Exception as e:
        return None, ("error", f"API 호출 실패: {e}")

    if r.status_code != 200:
        # 일부 상품은 발급이 제한된다(문서 명시). 그때는 브라우저로도 안 된다.
        return None, ("api_fail", f"HTTP {r.status_code}: {r.text[:200]}")

    try:
        d = r.json()["success"]
    except Exception:
        return None, ("api_fail", f"응답을 해석하지 못했습니다: {r.text[:200]}")

    link = d.get("shortUrl")
    if not link:
        return None, ("api_fail", "응답에 shortUrl 이 없습니다")

    # 발급된 링크가 요청한 상품의 것인지 확인한다. originUrl 에 상품 ID 가
    # 들어 있다(실측). 브라우저 방식과 같은 가드다 — 다르면 버린다.
    origin = d.get("originUrl") or ""
    if str(taca_id) not in origin:
        log(f"  ! 요청 {taca_id} / 발급 {origin[:60]} — 다르므로 버립니다")
        return None, ("mismatch", "발급된 링크가 요청한 상품과 다릅니다")

    return link, {"tacaItemId": d.get("tacaItemId"), "origin_url": origin}


def main():
    import argparse
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="토스 공식 API 시험")
    ap.add_argument("--health", action="store_true")
    ap.add_argument("--issue", metavar="상품ID")
    args = ap.parse_args()

    if args.health or not args.issue:
        ok, why = health()
        print(("✅ " if ok else "🔴 ") + why)
        if not ok:
            print("→ 출발 IP 가 등록된 것과 다를 수 있습니다. py src/ip_watch.py 로 확인하세요.")
    if args.issue:
        link, info = issue(args.issue)
        print(f"{link}  {info}" if link else f"🔴 {info}")


if __name__ == "__main__":
    main()
