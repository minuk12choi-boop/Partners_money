#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
observe_toss_issue.py — '링크 발급' 을 누르는 순간 무엇이 일어나는지 계측

`toss_link.py` 의 `issue_link()` 는 클릭 후 페이지 텍스트와 input value 에서
`toss.im/_m/...` 를 찾는다. 실측 결과 **거기에 링크가 나타나지 않는다.**
버튼은 눌렸고(발급 경험 평가 설문까지 떴다) 발급도 된 것으로 보이는데
화면에서 링크를 읽을 수 없다.

그래서 세 경로를 동시에 본다.

  1. 네트워크 — 링크를 만드는 API 응답에 링크가 들어 있을 것이다.
     가장 확실한 경로다. DOM 구조가 바뀌어도 안 깨진다.
  2. 클립보드 — '발급' 이 곧 '복사' 인 UI 가 흔하다.
  3. DOM 변화 — 토스트·모달이 잠깐 떴다 사라지는 경우.
     0.3초 간격으로 훑어 사라지기 전에 잡는다.

⚠️ 이 스크립트는 **실제로 링크를 1건 발급한다.** 읽기 전용이 아니다.
   계정에 쉐어링크가 하나 생긴다. 한 번에 1건만 누른다.

사용법:
    py tools/observe_toss_issue.py            # 0번 카드로 시험
    py tools/observe_toss_issue.py --idx 3
"""

import argparse
import json
import os
import sys
import time
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
    sys.exit("playwright 가 없습니다. `py -m pip install -r requirements.txt` 를 먼저 실행하세요.")

import toss_link as T

SHOT_DIR = os.path.join(ROOT, "shots")


class Tee:
    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")

    def __call__(self, msg=""):
        print(msg)
        self.f.write(str(msg) + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


def main():
    ap = argparse.ArgumentParser(description="토스 '링크 발급' 클릭 계측 (실제로 1건 발급함)")
    ap.add_argument("--idx", type=int, default=0, help="몇 번째 '링크 발급' 버튼을 누를지")
    ap.add_argument("--watch", type=float, default=20.0, help="클릭 후 관찰 시간(초)")
    ap.add_argument("--out", default="toss_issue_observe.txt")
    args = ap.parse_args()

    w = Tee(os.path.join(ROOT, args.out))
    w("※ 계정 정보가 섞일 수 있습니다. 공유 전에 훑어보세요.")
    w(f"  {datetime.now():%Y-%m-%d %H:%M:%S}")
    w(f"  ⚠️ 실제로 쉐어링크 1건을 발급합니다 (버튼 #{args.idx})")

    calls = []          # 클릭 이후 오간 XHR/fetch

    def on_response(resp):
        try:
            req = resp.request
            if req.resource_type not in ("xhr", "fetch"):
                return
            rec = {"method": req.method, "url": resp.url, "status": resp.status,
                   "post": None, "body": None}
            try:
                rec["post"] = req.post_data
            except Exception:
                pass
            try:
                body = resp.text()
                rec["body"] = body[:4000]
            except Exception as e:
                rec["body"] = f"<본문 읽기 실패: {e}>"
            calls.append(rec)
        except Exception:
            pass

    try:
        with sync_playwright() as pw:
            ctx = T.open_context(pw)
            try:
                ctx.grant_permissions(["clipboard-read", "clipboard-write"],
                                      origin="https://sharelink.toss.im")
                w("클립보드 권한 부여됨")
            except Exception as e:
                w(f"클립보드 권한 부여 실패(계속 진행): {e}")

            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(T.PRODUCTS_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)

            if T.needs_login(page.url):
                w("⚠️ 로그인 화면입니다. `py src/toss_link.py --login` 을 먼저 실행하세요.")
                ctx.close()
                return

            cards = T.scan_cards(page)
            w(f"카드 {len(cards)}개")
            target = next((c for c in cards if c["idx"] == args.idx), None)
            if target is None:
                w(f"버튼 #{args.idx} 을 찾지 못했습니다.")
                ctx.close()
                return
            w(f"대상: {target['title']}  {target.get('price')}원 "
              f"{target.get('salePct')}% 특가")

            # 클립보드를 미리 비워 둔다. 클릭 후 값이 생기면 그게 결과다.
            try:
                page.evaluate("() => navigator.clipboard.writeText('<<빈값>>')")
                w("클립보드 초기화")
            except Exception as e:
                w(f"클립보드 초기화 실패: {e}")

            before_text = page.evaluate("() => document.body.innerText")

            page.on("response", on_response)

            btn = page.get_by_role("button", name=T.BTN_ISSUE, exact=True).nth(args.idx)
            btn.scroll_into_view_if_needed(timeout=5000)
            w("")
            w("── 클릭 " + "─" * 60)
            btn.click(timeout=6000)

            # ── DOM 변화를 짧은 간격으로 훑는다 ─────────────────
            seen_lines = set(before_text.split("\n"))
            dom_new = []
            deadline = time.time() + args.watch
            while time.time() < deadline:
                try:
                    txt = page.evaluate("() => document.body.innerText")
                except Exception:
                    break
                for line in txt.split("\n"):
                    line = line.strip()
                    if line and line not in seen_lines:
                        seen_lines.add(line)
                        dom_new.append((round(time.time() - (deadline - args.watch), 1), line))
                page.wait_for_timeout(300)

            w("")
            w("=" * 70)
            w("  1. 네트워크 (클릭 이후 XHR/fetch)")
            w("=" * 70)
            w(f"기록된 요청 {len(calls)}건")
            for c in calls:
                w("")
                w(f"  {c['method']} {c['status']}  {c['url']}")
                if c["post"]:
                    w(f"    요청 본문: {str(c['post'])[:600]}")
                if c["body"]:
                    w(f"    응답 본문: {c['body'][:1500]}")

            hits = [c for c in calls if c.get("body") and "toss.im/_m" in c["body"]]
            w("")
            if hits:
                w(f"✅ 응답 본문에 쉐어링크가 들어 있는 요청 {len(hits)}건")
                for c in hits:
                    w(f"    {c['url']}")
            else:
                w("🔴 어떤 응답에도 toss.im/_m 이 없다.")

            w("")
            w("=" * 70)
            w("  2. 클립보드")
            w("=" * 70)
            try:
                clip = page.evaluate("() => navigator.clipboard.readText()")
                w(f"  값: {clip!r}")
                if clip and "toss.im/_m" in clip:
                    w("  ✅ 클립보드에 쉐어링크가 있다. 이 경로로 읽으면 된다.")
            except Exception as e:
                w(f"  읽기 실패: {e}")

            w("")
            w("=" * 70)
            w("  3. 클릭 이후 새로 나타난 화면 텍스트")
            w("=" * 70)
            if not dom_new:
                w("  (없음) — 화면에 아무 변화가 없었다.")
            for t, line in dom_new:
                w(f"  +{t:>5.1f}s  {line}")

            os.makedirs(SHOT_DIR, exist_ok=True)
            shot = os.path.join(SHOT_DIR, "toss_issue_after.png")
            try:
                page.screenshot(path=shot)
                w("")
                w(f"스크린샷: {shot}")
            except Exception as e:
                w(f"스크린샷 실패: {e}")

            ctx.close()

        w("")
        w(f"저장: {os.path.join(ROOT, args.out)}")
    finally:
        w.close()


if __name__ == "__main__":
    main()
