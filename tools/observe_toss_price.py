#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
observe_toss_price.py — 토스 상품의 가격을 어디서 얻을 수 있는지 찾는다

**문제**

토스 앱에서 쉐어링크를 공유하면 클립보드에 이렇게 들어온다.

    ✱ 이 포스팅은 토스쇼핑 쉐어링크 활동의 ...
    맘스럽 아토클래식 물티슈, 100매입, 20팩
    https://toss.im/_m/HA8vGVWg

**가격이 없다.** 상품명과 링크뿐이다. 그래서 발행 문구에 가격·할인율을
채울 수 없다. 쿠팡은 딜방 메시지에 가격이 적혀 있어 문제가 없었다.

`toss.shopping/t/<id>` 웹 페이지도 가격을 화면에 그리지 않는다(실측).
앱 설치를 유도하려고 감춘 것으로 보인다. 그런데 **화면에 안 그리는 것과
API 가 안 주는 것은 다르다.** 두 곳을 본다.

  1. 상품 페이지가 부르는 API 에 가격이 들어 있는가
  2. 쉐어링크 대시보드의 상품 목록 API 는 무엇을 주는가
     (여기에 상품ID 가 있으면 id 로 가격을 조회할 수 있다)

읽기 전용이다. 클릭하지 않고 링크를 만들지 않는다.

사용법:
    py tools/observe_toss_price.py
    py tools/observe_toss_price.py --pid 2346957745
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("playwright 가 없습니다.")

import toss_link as T
from lock import profile_lock

# 가격으로 보이는 숫자를 담은 키 이름들
PRICE_KEYS = re.compile(
    r"price|amount|discount|sale|origin|list|cost|최저|할인", re.IGNORECASE)


class Tee:
    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")

    def __call__(self, msg=""):
        print(msg)
        self.f.write(str(msg) + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


def walk(obj, path="", out=None, depth=0):
    """중첩 JSON 에서 가격처럼 보이는 키를 찾는다."""
    if out is None:
        out = []
    if depth > 8:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if isinstance(v, (int, float)) and PRICE_KEYS.search(k):
                out.append((p, v))
            elif isinstance(v, str) and PRICE_KEYS.search(k) and len(v) < 40:
                out.append((p, v))
            else:
                walk(v, p, out, depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:5]):
            walk(v, f"{path}[{i}]", out, depth + 1)
    return out


def capture(w, page, label, url, wait=6000):
    calls = []

    def on_response(resp):
        try:
            if resp.request.resource_type not in ("xhr", "fetch"):
                return
            body = resp.text()
        except Exception:
            return
        calls.append({"url": resp.url, "status": resp.status, "body": body})

    page.on("response", on_response)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(wait)
    except Exception as e:
        w(f"  이동 실패: {e}")
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    w("")
    w("=" * 74)
    w(f"  {label}")
    w("=" * 74)
    w(f"URL: {url}")
    w(f"XHR/fetch {len(calls)}건")

    for c in calls:
        try:
            data = json.loads(c["body"])
        except Exception:
            continue
        hits = walk(data)
        if not hits:
            continue
        w("")
        w(f"  ── {c['status']} {c['url'][:140]}")
        for k, v in hits[:40]:
            w(f"       {k} = {v}")
    return calls


def main():
    ap = argparse.ArgumentParser(description="토스 가격 출처 관측 (읽기 전용)")
    ap.add_argument("--pid", default="2346957745", help="토스 상품 ID")
    ap.add_argument("--out", default="toss_price_observe.txt")
    args = ap.parse_args()

    w = Tee(os.path.join(ROOT, args.out))
    w("※ 계정 정보가 섞일 수 있습니다. 공유 전에 훑어보세요.")
    w(f"  {datetime.now():%Y-%m-%d %H:%M:%S}")

    try:
        with profile_lock(T.PROFILE_DIR, log=w), sync_playwright() as pw:
            ctx = T.open_context(pw)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            capture(w, page, "1. 상품 상세 페이지가 부르는 API",
                    f"https://toss.shopping/t/{args.pid}")

            capture(w, page, "2. 쉐어링크 대시보드 상품 목록 API",
                    T.PRODUCTS_URL, wait=7000)

            ctx.close()
        w("")
        w(f"저장: {os.path.join(ROOT, args.out)}")
    finally:
        w.close()


if __name__ == "__main__":
    main()
