#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
observe_toss.py — 토스쇼핑 쉐어링크 대시보드 관측

이미 로그인된 크롬에 붙어서 sharelink.toss.im 화면 구조를 덤프한다.
목적은 하나다: **상품을 검색해서 내 쉐어링크를 만드는 UI 가 있는가?**

있으면 partners_link.py 와 같은 방식으로 자동화할 수 있고,
없으면 토스는 앱 전용으로 확정된다.

이 스크립트는 읽기 전용이다. 클릭하지 않고 아무것도 만들지 않는다.
계정 상태를 바꾸지 않기 위해서다.

────────────────────────────────────────────────────────────────
사용법 (소유자 Windows PC 에서)

1) 크롬을 완전히 종료한다. 크롬이 떠 있는 상태로 플래그를 주면
   무시되고 기존 프로세스에 창만 하나 더 열린다. 확실하게:

   taskkill /F /IM chrome.exe

2) 디버깅 포트를 열어 크롬을 다시 켠다. 명령 프롬프트에서:

   "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222

   ※ 기존 로그인이 그대로 유지된다. 새로 로그인할 필요 없다.
   ※ 확인: 주소창에 http://127.0.0.1:9222/json/version → JSON 이 나오면 성공

3) 그 크롬에서 https://sharelink.toss.im/home 을 연다. (로그인 확인)

4) 이 스크립트를 실행한다.

   py tools/observe_toss.py

크롬을 못 켜겠으면 --launch 로 별도 브라우저를 띄울 수 있다.
이때는 그 창에서 한 번 로그인해야 하고, 세션은 pw_toss_profile/ 에 남는다.

   py tools/observe_toss.py --launch
────────────────────────────────────────────────────────────────

의존성: playwright (requirements.txt 에 이미 있음)
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("playwright 가 없습니다. `py -m pip install -r requirements.txt` 를 먼저 실행하세요.")

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE_DIR = os.path.join(HERE, "pw_toss_profile")
SHOT_DIR = os.path.join(HERE, "shots")

TARGET = "https://sharelink.toss.im/home"

# 크롬 디버깅 포트는 IPv4 127.0.0.1 에만 바인딩된다.
# 'localhost' 는 윈도우에서 IPv6(::1) 로 먼저 풀려 ECONNREFUSED 가 난다(실측).
# 그래서 127.0.0.1 을 먼저 시도한다.
CDP_HOSTS = ["127.0.0.1", "localhost"]


# ---------------------------------------------------------------- 출력

class Tee:
    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")

    def __call__(self, msg=""):
        print(msg)
        self.f.write(str(msg) + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


# ---------------------------------------------------------------- 페이지 스캔

# partners_link.py 의 자가탐색과 같은 방식이다.
# 셀렉터를 추측하지 않고 페이지에서 후보를 직접 뽑는다.
JS_SCAN = """() => {
  const cssPath = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    while (el && el.nodeType === 1 && parts.length < 6) {
      let s = el.tagName.toLowerCase();
      if (el.id) { parts.unshift('#' + CSS.escape(el.id)); break; }
      const p = el.parentNode;
      if (p) {
        const sibs = Array.from(p.children).filter(c => c.tagName === el.tagName);
        if (sibs.length > 1) s += ':nth-of-type(' + (sibs.indexOf(el) + 1) + ')';
      }
      parts.unshift(s);
      el = el.parentNode;
    }
    return parts.join(' > ');
  };
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(el);
    return st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0';
  };

  const inputs = [];
  document.querySelectorAll('input, textarea').forEach(el => {
    if (!visible(el) || el.type === 'hidden' || el.type === 'password') return;
    const r = el.getBoundingClientRect();
    inputs.push({
      path: cssPath(el),
      type: el.type || el.tagName.toLowerCase(),
      placeholder: el.placeholder || '',
      name: el.name || '', id: el.id || '',
      aria: el.getAttribute('aria-label') || '',
      w: Math.round(r.width), top: Math.round(r.top),
    });
  });

  const buttons = [];
  document.querySelectorAll('button, a, [role=button], [role=tab], [role=link]').forEach(el => {
    if (!visible(el)) return;
    const t = (el.innerText || el.value || '').trim().replace(/\\s+/g, ' ');
    if (!t || t.length > 40) return;
    const r = el.getBoundingClientRect();
    buttons.push({
      text: t, tag: el.tagName.toLowerCase(),
      href: el.getAttribute('href') || '',
      path: cssPath(el),
      top: Math.round(r.top),
    });
  });

  // 관심 키워드가 들어간 텍스트 노드 — 링크 생성 UI 의 단서
  const KEY = /검색|링크\\s*만들|링크\\s*생성|쉐어링크|상품|공유|만들기/;
  const hits = [];
  document.querySelectorAll('*').forEach(el => {
    if (el.children.length > 0) return;      // 잎 노드만
    if (!visible(el)) return;
    const t = (el.innerText || '').trim().replace(/\\s+/g, ' ');
    if (!t || t.length > 60) return;
    if (KEY.test(t)) hits.push({ text: t, tag: el.tagName.toLowerCase(), path: cssPath(el) });
  });

  return {
    url: location.href,
    title: document.title,
    inputs, buttons,
    keywordHits: hits.slice(0, 40),
    bodyText: (document.body ? document.body.innerText : '').slice(0, 3000),
  };
}"""


def page_state(url):
    """URL 로 현재 상태를 판정한다.

    실측한 리다이렉트:
      https://business.toss.im/account/sign-in?...&redirect_uri=
        https%3A%2F%2Fsharelink.toss.im%2Fsignup-start

    쉐어링크는 개인 토스 앱 계정이 아니라 '토스 비즈니스' 계정으로
    로그인한다. 그리고 미가입 계정은 /home 이 아니라 /signup-start 로
    보내진다. 이 둘은 다른 상태이므로 구분해서 안내해야 한다.
    """
    if "business.toss.im" in url or "/sign-in" in url or "/login" in url:
        return "login"
    if "signup" in url:
        return "signup"
    if "sharelink.toss.im" in url:
        return "ready"
    return "unknown"


def dump(w, page, label):
    w("")
    w("=" * 78)
    w(f"  {label}")
    w("=" * 78)
    try:
        d = page.evaluate(JS_SCAN)
    except Exception as e:
        w(f"  스캔 실패: {e}")
        return

    w(f"URL   : {d['url']}")
    w(f"TITLE : {d['title']}")

    state = page_state(d["url"])
    if state == "login":
        w("")
        w("⚠️ 토스 비즈니스 로그인 화면입니다. 대시보드 내용이 아닙니다.")
    elif state == "signup":
        w("")
        w("⚠️ 쉐어링크 가입 화면입니다. 이 계정은 아직 쉐어링크에 가입되어")
        w("   있지 않습니다. 가입을 마쳐야 대시보드를 볼 수 있습니다.")

    w("")
    w(f"── 입력창 {len(d['inputs'])}개 " + "─" * 40)
    if not d["inputs"]:
        w("  (없음) — 검색창이 없다는 뜻일 수 있습니다.")
    for i in d["inputs"]:
        w(f"  placeholder={i['placeholder']!r} type={i['type']} "
          f"name={i['name']!r} aria={i['aria']!r}")
        w(f"    path: {i['path']}")

    w("")
    w(f"── 버튼·링크 {len(d['buttons'])}개 " + "─" * 38)
    for b in sorted(d["buttons"], key=lambda x: x["top"]):
        href = f"  href={b['href']}" if b["href"] else ""
        w(f"  [{b['tag']}] {b['text']!r}{href}")

    w("")
    w(f"── 키워드 매칭 텍스트 {len(d['keywordHits'])}개 " + "─" * 30)
    for h in d["keywordHits"]:
        w(f"  {h['text']!r}  ({h['tag']})")

    w("")
    w("── 화면 텍스트 (앞 3000자) " + "─" * 36)
    w(d["bodyText"])


# ---------------------------------------------------------------- 접속

def probe_cdp(w, port):
    """어느 호스트로 디버깅 포트가 열려 있는지 먼저 확인한다.

    Playwright 에 바로 넘기면 IPv4/IPv6 문제인지 크롬이 안 켜진 건지
    구분이 안 된다. 표준 라이브러리로 미리 찔러보고 원인을 갈라낸다.
    """
    for host in CDP_HOSTS:
        endpoint = f"http://{host}:{port}"
        try:
            with urllib.request.urlopen(f"{endpoint}/json/version", timeout=3) as r:
                info = json.load(r)
            w(f"  {host}:{port} 응답함 — {info.get('Browser', '?')}")
            return endpoint
        except urllib.error.URLError as e:
            w(f"  {host}:{port} 실패 ({e.reason})")
        except Exception as e:
            w(f"  {host}:{port} 실패 ({e})")
    return None


def via_cdp(w, pw, url, port):
    """이미 열려 있는 크롬에 붙는다. 기존 로그인이 그대로 살아 있다."""
    w(f"디버깅 포트 탐색 (포트 {port})")
    endpoint = probe_cdp(w, port)
    if endpoint is None:
        raise RuntimeError("디버깅 포트가 열려 있지 않습니다")

    w(f"크롬에 연결: {endpoint}")
    browser = pw.chromium.connect_over_cdp(endpoint)
    ctxs = browser.contexts
    if not ctxs:
        raise RuntimeError("크롬 컨텍스트가 없습니다.")
    ctx = ctxs[0]

    # 이미 토스 탭이 열려 있으면 그걸 쓴다
    page = None
    for p in ctx.pages:
        try:
            if "toss.im" in p.url:
                page = p
                w(f"  기존 탭 사용: {p.url}")
                break
        except Exception:
            pass
    if page is None:
        page = ctx.new_page()
        w(f"  새 탭에서 {url} 여는 중")
        page.goto(url, wait_until="domcontentloaded")

    page.wait_for_timeout(4000)   # SPA 렌더링 대기
    return browser, page


def via_launch(w, pw, url):
    """별도 브라우저를 띄운다. 이 창에서 한 번 로그인해야 한다."""
    os.makedirs(PROFILE_DIR, exist_ok=True)
    # CLAUDE.md 제약 5: headless 금지
    ctx = pw.chromium.launch_persistent_context(
        PROFILE_DIR, headless=False,
        viewport={"width": 1440, "height": 900}, locale="ko-KR",
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    return ctx, page


def ensure_ready(w, page, url, tries=3):
    """대시보드에 도달할 때까지 사용자에게 안내하고 기다린다.

    로그인 화면과 가입 화면은 원인이 다르므로 안내를 따로 준다.
    자동으로 진행하지 않는다. 가입은 계정 정보가 걸린 결정이라
    사람이 직접 해야 한다.
    """
    for _ in range(tries):
        state = page_state(page.url)
        if state == "ready":
            return True

        w("")
        if state == "login":
            w("─" * 60)
            w("토스 비즈니스 로그인이 필요합니다.")
            w("쉐어링크는 개인 토스 앱 계정이 아니라 '토스 비즈니스' 계정을 씁니다.")
            w("브라우저 창에서 이메일/ID 또는 QR코드로 로그인하세요.")
        elif state == "signup":
            w("─" * 60)
            w("이 계정은 아직 쉐어링크에 가입되어 있지 않습니다.")
            w("(로그인은 됐지만 /signup-start 로 이동했습니다)")
            w("가입을 진행하시려면 브라우저에서 마친 뒤 이어가세요.")
            w("가입하지 않을 거라면 그냥 Enter 를 눌러 현재 화면을 덤프합니다.")
        else:
            w("─" * 60)
            w(f"예상 밖의 화면입니다: {page.url}")
        w("끝났으면 이 콘솔에서 Enter 를 누르세요.")
        w("─" * 60)
        try:
            input()
        except EOFError:
            return False

        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
        except Exception as e:
            w(f"페이지 이동 실패: {e}")
            return False

    return page_state(page.url) == "ready"


def main():
    ap = argparse.ArgumentParser(description="토스 쉐어링크 대시보드 관측 (읽기 전용)")
    ap.add_argument("--launch", action="store_true",
                    help="기존 크롬 대신 별도 브라우저를 띄운다 (로그인 필요)")
    ap.add_argument("--url", default=TARGET)
    ap.add_argument("--port", type=int, default=9222, help="크롬 디버깅 포트")
    ap.add_argument("--out", default="toss_observe.txt")
    args = ap.parse_args()

    w = Tee(args.out)
    w("※ 이 출력에는 계정 정보가 포함될 수 있습니다. 공유 전에 훑어보세요.")
    w(f"  {datetime.now():%Y-%m-%d %H:%M:%S}")

    handle = page = None
    try:
        with sync_playwright() as pw:
            if args.launch:
                handle, page = via_launch(w, pw, args.url)
            else:
                try:
                    handle, page = via_cdp(w, pw, args.url, args.port)
                except Exception as e:
                    w("")
                    w(f"크롬 연결 실패: {e}")
                    w("")
                    w("가장 흔한 원인은 크롬이 이미 떠 있는 상태에서 플래그를 준 것입니다.")
                    w("크롬이 실행 중이면 새로 준 --remote-debugging-port 는 무시되고")
                    w("기존 프로세스에 창만 하나 더 열립니다. 트레이 아이콘까지 꺼야 합니다.")
                    w("")
                    w("명령 프롬프트에서 순서대로:")
                    w("  taskkill /F /IM chrome.exe")
                    w('  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"'
                      f' --remote-debugging-port={args.port}')
                    w("")
                    w("제대로 켜졌는지 확인하는 법 — 그 크롬 주소창에 아래를 넣어")
                    w("JSON 이 나오면 성공입니다:")
                    w(f"  http://127.0.0.1:{args.port}/json/version")
                    w("")
                    w("그래도 안 되면 --launch 로 별도 브라우저를 쓰세요 (로그인 1회 필요):")
                    w("  py tools/observe_toss.py --launch")
                    return

            if page_state(page.url) != "ready":
                ensure_ready(w, page, args.url)

            dump(w, page, "쉐어링크 대시보드")

            os.makedirs(SHOT_DIR, exist_ok=True)
            shot = os.path.join(SHOT_DIR, "toss_sharelink.png")
            try:
                page.screenshot(path=shot, full_page=True)
                w("")
                w(f"스크린샷 저장: {shot}")
            except Exception as e:
                w(f"스크린샷 실패: {e}")

            # connect_over_cdp 로 붙은 경우 브라우저를 닫으면 안 된다.
            # 사용자의 크롬이 통째로 닫힌다.
            if args.launch and handle is not None:
                handle.close()

        w("")
        w(f"결과 저장 완료: {os.path.abspath(args.out)}")
    finally:
        w.close()


if __name__ == "__main__":
    main()
