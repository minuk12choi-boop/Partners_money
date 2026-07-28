#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
observe_coupang_session.py — 쿠팡 파트너스 로그인 세션이 왜 안 남는지 가린다

증상(실측 2026-07-29): 로그인은 성공한다. shots/after_login.png 가 완전히
로그인된 '상품 링크' 화면이다. 그런데 브라우저를 껐다 켜면 로그아웃이다.
'자동 로그인' 을 켜도 마찬가지다.

프로필의 쿠키를 봤더니 로그인 전후 집합이 **완전히 동일**했다. 남은 건
전부 추적용(PCID, MARKETID, _ga, Akamai bm_*)이고 회원 인증 쿠키가 없다.

그렇다면 인증 상태를 쿠키가 아닌 곳이 들고 있다는 뜻이다. 파트너스는
SPA 이므로 localStorage 나 **sessionStorage** 가 후보다. sessionStorage 라면
탭이 닫히는 순간 사라지므로 증상과 정확히 맞는다.

이 도구는 로그인 1회로 아래를 전부 확인한다.

  1. 로그인 **직후** (닫기 전) 쿠키 / localStorage / sessionStorage
  2. 브라우저 재시작 **후** 같은 항목
  3. 무엇이 사라졌는가 (차집합)
  4. storage_state 로 복원하면 로그인이 유지되는가
     → 유지되면 partners_link.py 를 그 방식으로 바꾸면 된다

사용법:
    py tools/observe_coupang_session.py

⚠️ 로그인 정보가 담길 수 있는 파일을 만든다.
   coupang_state.json 은 .gitignore 에 있다. 남에게 주지 말 것.
"""

import json
import os
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
    sys.exit("playwright 가 없습니다. `py -m pip install -r requirements.txt` 를 먼저 실행하세요.")

import partners_link as P

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(P.__file__)),
                          "coupang_state.json")


class Tee:
    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")

    def __call__(self, msg=""):
        print(msg)
        self.f.write(str(msg) + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


JS_STORAGE = """() => {
  const grab = (s) => {
    const out = {};
    try {
      for (let i = 0; i < s.length; i++) {
        const k = s.key(i);
        const v = s.getItem(k) || '';
        out[k] = v.length > 120 ? v.slice(0, 120) + `…(${v.length}자)` : v;
      }
    } catch (e) { out['<읽기실패>'] = String(e); }
    return out;
  };
  return {
    origin: location.origin,
    local: grab(window.localStorage),
    session: grab(window.sessionStorage),
  };
}"""


def snapshot(w, ctx, page, label):
    w("")
    w("=" * 74)
    w(f"  {label}")
    w("=" * 74)
    w(f"URL: {page.url}")
    logged = P.is_logged_in(page)
    w(f"로그인 상태: {'✅ 예' if logged else '🔴 아니오'}")

    cookies = [c for c in ctx.cookies() if "coupang" in c.get("domain", "")]
    w("")
    w(f"── 쿠팡 쿠키 {len(cookies)}개 " + "─" * 40)
    names = set()
    for c in sorted(cookies, key=lambda x: (x["domain"], x["name"])):
        exp = c.get("expires", -1)
        kind = "세션" if exp in (-1, 0) else "영속"
        names.add((c["domain"], c["name"]))
        w(f"  [{kind}] {c['domain']:26s} {c['name']:26s} "
          f"httpOnly={str(c.get('httpOnly')):5s} expires={exp}")

    try:
        st = page.evaluate(JS_STORAGE)
    except Exception as e:
        w(f"스토리지 읽기 실패: {e}")
        st = {"origin": "?", "local": {}, "session": {}}

    w("")
    w(f"── localStorage ({st['origin']}) {len(st['local'])}개 " + "─" * 25)
    for k, v in st["local"].items():
        w(f"  {k} = {v}")
    w("")
    w(f"── sessionStorage {len(st['session'])}개 " + "─" * 34)
    for k, v in st["session"].items():
        w(f"  {k} = {v}")

    return {"logged": logged, "cookies": names,
            "local": set(st["local"]), "session": set(st["session"])}


def main():
    w = Tee(os.path.join(ROOT, "coupang_session_observe.txt"))
    w("※ 로그인 정보가 섞일 수 있습니다. 공유 전에 반드시 훑어보세요.")
    w(f"  {datetime.now():%Y-%m-%d %H:%M:%S}")

    before = after = None
    try:
        # ── 1) 로그인하고, 닫기 전에 찍는다 ──────────────────────
        with sync_playwright() as pw:
            ctx = P.open_context(pw)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            P.goto_link_page(page)

            if not P.is_logged_in(page):
                P.tick_keep_login(page)
                print()
                print("─" * 60)
                print("브라우저에서 로그인을 완료하세요.")
                print("'자동 로그인' 은 스크립트가 켜 두었습니다.")
                print("끝났으면 여기서 Enter 를 누르세요.")
                print("─" * 60)
                try:
                    input()
                except EOFError:
                    w("대화형 콘솔에서 실행하세요.")
                    ctx.close()
                    return
                P.goto_link_page(page)

            if not P.is_logged_in(page):
                w("")
                w("🔴 로그인이 확인되지 않아 중단합니다.")
                ctx.close()
                return

            before = snapshot(w, ctx, page, "1. 로그인 직후 (브라우저 닫기 전)")

            # storage_state 에는 쿠키와 localStorage 가 들어간다.
            # sessionStorage 는 안 들어간다. 그것도 확인 항목이다.
            try:
                ctx.storage_state(path=STATE_PATH)
                w("")
                w(f"storage_state 저장: {STATE_PATH}")
            except Exception as e:
                w(f"storage_state 저장 실패: {e}")

            ctx.close()

        # ── 2) 같은 프로필로 재시작 ─────────────────────────────
        with sync_playwright() as pw:
            ctx = P.open_context(pw)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            P.goto_link_page(page)
            after = snapshot(w, ctx, page, "2. 브라우저 재시작 후 (같은 프로필)")
            ctx.close()

        # ── 3) 무엇이 사라졌나 ──────────────────────────────────
        w("")
        w("=" * 74)
        w("  3. 재시작하면서 사라진 것")
        w("=" * 74)
        for key, label in (("cookies", "쿠키"), ("local", "localStorage"),
                           ("session", "sessionStorage")):
            lost = before[key] - after[key]
            w("")
            w(f"── {label}: {len(lost)}개 사라짐 " + "─" * 30)
            for x in sorted(lost, key=str):
                w(f"  - {x}")

        if before["logged"] and not after["logged"]:
            w("")
            if before["session"] - after["session"]:
                w("👉 sessionStorage 가 사라졌다. 탭을 닫으면 없어지는 저장소라")
                w("   프로필로는 절대 보존되지 않는다. 로그인 상태를 이게 들고")
                w("   있다면 브라우저를 계속 띄워 두는 수밖에 없다.")
            if before["cookies"] - after["cookies"]:
                w("👉 쿠키가 사라졌다. 세션 쿠키라 저장되지 않은 것이다.")
            if not (before["session"] - after["session"]) and \
               not (before["cookies"] - after["cookies"]) and \
               not (before["local"] - after["local"]):
                w("👉 아무것도 안 사라졌는데 로그아웃이다. 저장된 값이 서버에서")
                w("   거부된 것이다. 봇 탐지(Akamai _abck) 나 기기 지문을")
                w("   의심해야 한다.")

        # ── 4) storage_state 로 복원하면 되는가 ─────────────────
        w("")
        w("=" * 74)
        w("  4. storage_state 로 복원하면 로그인이 유지되는가")
        w("=" * 74)
        if not os.path.exists(STATE_PATH):
            w("  storage_state 파일이 없어 건너뜁니다.")
        else:
            with sync_playwright() as pw:
                # 프로필이 아니라 깨끗한 브라우저에 상태만 주입한다.
                # CLAUDE.md 제약 5: headless 금지
                browser = pw.chromium.launch(
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled"])
                ctx = browser.new_context(
                    storage_state=STATE_PATH, locale="ko-KR",
                    viewport={"width": 1440, "height": 900})
                page = ctx.new_page()
                P.goto_link_page(page)
                ok = P.is_logged_in(page)
                w(f"  URL: {page.url}")
                w(f"  로그인 유지: {'✅ 예' if ok else '🔴 아니오'}")
                if ok:
                    w("")
                    w("  👉 storage_state 방식이면 세션을 이어갈 수 있다.")
                    w("     partners_link.py 를 이 방식으로 바꾸면 된다.")
                ctx.close()
                browser.close()

        w("")
        w(f"저장: {os.path.join(ROOT, 'coupang_session_observe.txt')}")
    finally:
        w.close()


if __name__ == "__main__":
    main()
