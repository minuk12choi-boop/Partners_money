#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
partners_link.py  (자가탐색 버전)

내 PC에서 내 파트너스 세션으로 딥링크를 자동 생성한다.
셀렉터를 하드코딩하지 않는다. 페이지에서 후보를 직접 찾아 점수를 매기고,
실제로 링크가 나오는 조합을 발견하면 selectors.json 에 캐시한다.
캐시가 깨지면 자동으로 다시 탐색한다.

최초 1회:
    python partners_link.py --login

이후:
    python partners_link.py            # 전부 자동
    python partners_link.py --limit 5

의존성:
    pip install playwright
    playwright install chromium
"""

import argparse
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from env import load_env
from lock import profile_lock

load_env()

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "deals.db")
PROFILE_DIR = os.path.join(HERE, "pw_profile")
CACHE_PATH = os.path.join(HERE, "selectors.json")
SHOT_DIR = os.path.join(HERE, "shots")

# '간편 링크 만들기'. 상품 URL 을 붙여넣어 딥링크를 얻는 화면이다.
#
# 예전 값은 '#affiliate/ws/link' 였는데 그건 '상품 링크' 화면이고
# 성격이 완전히 다르다. 상품을 검색해서 고르는 3단계 마법사
# (상품 탐색 -> 마음에 드는 상품 선택 -> URL 혹은 배너 만들기)라
# 입력창이 '찾고 싶은 상품을 검색해보세요!' 검색창 하나뿐이고
# 링크 생성 버튼 자체가 없다(실측 2026-07-29). URL 을 붙여넣는
# 전제와 맞지 않는다.
#
# 상단 '링크 생성' 드롭다운은 hover 해야 DOM 에 나타난다. 그래서
# 정적 덤프에서는 하위 메뉴가 안 보였다.
#   간편 링크 만들기 -> #affiliate/ws/link-to-any-page   ← 이것
#   상품 링크        -> #affiliate/ws/link
#   검색 위젯        -> #affiliate/ws/search-bar
#   이벤트/프로모션   -> #affiliate/ws/events
#   카테고리 배너     -> #affiliate/ws/banner
#   다이나믹 배너     -> #affiliate/ws/dynamic-widgets
LINK_PAGE = "https://partners.coupang.com/#affiliate/ws/link-to-any-page"

# 승인 후 API 키를 받는 곳. T6 에서 쓴다.
API_PAGE = "https://partners.coupang.com/#affiliate/ws/tools/open-api"
RE_SHORT = re.compile(r"https?://link\.coupang\.com/[A-Za-z0-9/_\-\?\=\&\.%]+")

# 링크를 만드는 API. 실측으로 확인한 경로다.
#   GET https://partners.coupang.com/api/v1/url/any?coupangUrl=<상품URL>
LINK_API_PATH = "/api/v1/url/any"

# landingUrl 안의 상품 ID. 받은 링크가 정말 그 상품의 것인지 대조한다.
#   .../re/AFFSDP?lptag=AF4612286&pageKey=5140279812&itemId=...
RE_PAGE_KEY = re.compile(r"[?&]pageKey=(\d+)")

# 실제 화면을 관측해서 확인한 값이다(2026-07-29, tools/observe_coupang.py).
# 자가탐색보다 이걸 먼저 쓴다. 탐색은 시도 한 번이 곧 사이트 접근
# 한 번이라 되도록 안 돌리는 게 좋다.
#
#   입력창: <input type=text id="url">  (폭 1012)
#   버튼  : '링크 생성' + U+200B(폭 0 공백)
#
# ⚠️ 버튼 텍스트 끝에 보이지 않는 U+200B 이 붙어 있다. 정확일치로
# 찾으면 못 찾는다. attempt() 가 exact=False 로 부분일치를 쓰므로
# 여기에는 폭 0 공백 없이 적는다.
KNOWN_SELECTORS = {
    "input_sel": "#url",
    "button_text": "링크 생성",
}

# ── 접근 빈도 제한 (CLAUDE.md 제약 2) ────────────────────────────
# run_all.py 의 MAX_LINKS_PER_CYCLE 과 같은 값이다. 그쪽은 --limit 로
# 넘겨주지만, 이 스크립트를 직접 실행할 때도 상한이 걸려야 한다.
# toss_link.py 가 이미 같은 방식으로 자기 상한을 강제한다.
MAX_LINKS_PER_RUN = 4

# 자가탐색은 조합마다 페이지를 새로 열고 폼을 채워 제출한다.
# 입력창 5개 x 버튼 4개면 20번을 쉬지 않고 두드리게 되는데, 이건
# 사람의 행동이 아니다. 시도 횟수를 줄이고 사이에 간격을 둔다.
MAX_DISCOVER_ATTEMPTS = 8
SLEEP_BETWEEN = 6

# 이 시간보다 오래된 딜은 링크를 만들지 않는다.
#
# 이 방의 딜은 선착순이고 물량이 소진되면 끝난다. 어제 딜의 링크를
# 만들어봐야 쓰지 않는다. 그리고 **쓰지 않을 링크를 만드는 접근은
# 순수한 손해다.** 제약 2 의 취지가 파트너스 사이트 접근 최소화인데,
# DB 에 쌓인 300건 넘는 예전 딜을 주기마다 4건씩 소화하면 하루 100번 넘게
# 접근하면서 정작 그 링크는 아무도 안 쓴다.
#
# 이 필터를 넣으면 신규 딜이 없는 주기에는 접근이 0회다.
MAX_DEAL_AGE_HOURS = int(os.environ.get("MAX_DEAL_AGE_HOURS", "12"))
# ─────────────────────────────────────────────────────────────────

# 페이지 안의 모든 input/textarea 후보를 뽑고 점수를 매기는 JS
JS_SCAN_INPUTS = """() => {
  const out = [];
  document.querySelectorAll('input, textarea').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width < 80 || r.height < 10) return;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return;
    if (el.type === 'hidden' || el.type === 'password' || el.disabled || el.readOnly) return;

    const hay = ((el.placeholder||'') + ' ' + (el.name||'') + ' ' +
                 (el.id||'') + ' ' + (el.className||'')).toLowerCase();
    let score = 0;
    if (/url|주소|링크|link/.test(hay)) score += 40;
    if (el.tagName === 'TEXTAREA') score += 8;
    if (r.width > 300) score += 10;
    if (r.width > 500) score += 6;
    score += Math.max(0, 12 - Math.round(r.top / 100));

    out.push({ score, path: cssPath(el), ph: el.placeholder||'', w: Math.round(r.width) });
  });

  function cssPath(el) {
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
  }

  return out.sort((a, b) => b.score - a.score).slice(0, 8);
}"""

# 제출 버튼 후보
JS_SCAN_BUTTONS = """() => {
  const out = [];
  document.querySelectorAll('button, a, input[type=button], input[type=submit], [role=button]')
    .forEach(el => {
      const r = el.getBoundingClientRect();
      if (r.width < 20 || r.height < 10) return;
      const st = getComputedStyle(el);
      if (st.display === 'none' || st.visibility === 'hidden') return;
      const t = ((el.innerText || el.value || '') + '').trim();
      if (!t || t.length > 20) return;
      let score = 0;
      if (/링크\\s*생성|링크\\s*만들기/.test(t)) score += 50;
      else if (/생성|변환|만들기|확인/.test(t)) score += 30;
      else if (/검색/.test(t)) score += 5;
      if (score === 0) return;
      out.push({ score, text: t });
    });
  return out.sort((a, b) => b.score - a.score).slice(0, 6);
}"""

# 페이지 전역에서 딥링크를 긁는다. input 의 value 는 HTML 에 안 나오므로 JS 로 읽어야 한다.
JS_HARVEST = """() => {
  const found = [];
  document.querySelectorAll('input, textarea').forEach(el => {
    if (el.value) found.push(el.value);
  });
  found.push(document.body ? document.body.innerText : '');
  return found.join('\\n');
}"""


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def open_context(pw):
    os.makedirs(PROFILE_DIR, exist_ok=True)
    return pw.chromium.launch_persistent_context(
        PROFILE_DIR, headless=False,
        viewport={"width": 1440, "height": 900}, locale="ko-KR",
        args=["--disable-blink-features=AutomationControlled"],
    )


def load_cache():
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_cache(d):
    d["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def goto_link_page(page):
    page.goto(LINK_PAGE, wait_until="domcontentloaded")
    page.wait_for_timeout(3500)


def attempt(page, input_sel, button_text, product_url, wait=22, expect_pid=None):
    """한 조합으로 시도. 성공 시 딥링크 반환.

    링크를 만드는 API 의 응답을 먼저 본다. 실측한 응답:

        GET /api/v1/url/any?coupangUrl=<상품URL>
        {"rCode":"0","data":{
           "type":"sdp",
           "shortUrl":"https://link.coupang.com/a/fLzMJE46fI",
           "landingUrl":".../re/AFFSDP?lptag=AF4612286&pageKey=5140279812&...",
           "description":"[로켓프레시] 대상 종가 총각김치"}}

    `landingUrl` 의 `pageKey` 가 상품 ID 다. 이걸로 **받은 링크가 정말
    그 상품의 것인지 대조한다.** 화면을 긁는 방식은 그 대조를 못 한다.
    페이지에 남아 있던 이전 링크를 주워도 알 방법이 없고, 엉뚱한 상품의
    링크를 발행하면 신뢰를 잃는다.

    `lptag` 는 내 파트너스 ID 다. 이게 붙어 있어야 수익이 잡힌다.

    API 를 놓치면 화면 긁기(JS_HARVEST)로 넘어간다. 쿠팡은 토스와 달리
    결과를 '파트너스 URL' 로 화면에도 보여주므로 이 대비책이 유효하다.
    다만 그 경로에서는 상품 대조를 할 수 없다.
    """
    got = {}

    def on_response(resp):
        if LINK_API_PATH not in resp.url:
            return
        try:
            data = resp.json()
        except Exception as e:
            log(f"  링크 API 응답 파싱 실패: {e}")
            return
        d = (data or {}).get("data") or {}
        short = d.get("shortUrl")
        if not short:
            log(f"  링크 API 응답에 shortUrl 이 없음: {str(data)[:200]}")
            return
        landing = d.get("landingUrl") or ""
        m = RE_PAGE_KEY.search(landing)
        got["link"] = short
        got["pid"] = m.group(1) if m else None
        got["lptag"] = ("lptag=" in landing)

    goto_link_page(page)
    try:
        loc = page.locator(input_sel).first
        loc.wait_for(state="visible", timeout=6000)
    except PWTimeout:
        return None

    before = page.evaluate(JS_HARVEST)
    prev = set(RE_SHORT.findall(before))

    page.on("response", on_response)
    try:
        loc.click()
        loc.fill("")
        loc.type(product_url, delay=20)
        page.wait_for_timeout(700)

        if button_text:
            try:
                page.get_by_role("button", name=button_text, exact=False).first.click(timeout=4000)
            except Exception:
                try:
                    page.locator(f"text={button_text}").first.click(timeout=3000)
                except Exception:
                    loc.press("Enter")
        else:
            loc.press("Enter")

        deadline = time.time() + wait
        while time.time() < deadline:
            if got.get("link"):
                if expect_pid and got.get("pid") and got["pid"] != str(expect_pid):
                    # 요청한 상품과 다른 상품의 링크가 왔다. 저장하면 안 된다.
                    log(f"  🔴 상품이 다릅니다. 요청 {expect_pid} / 응답 {got['pid']}"
                        f" — 이 링크는 버립니다")
                    return None
                if not got.get("lptag"):
                    # 트래킹이 없으면 클릭은 되지만 수익이 0으로 찍힌다.
                    log("  🔴 landingUrl 에 lptag 가 없습니다 — 트래킹이 안 붙은 링크입니다")
                    return None
                return got["link"]

            txt = page.evaluate(JS_HARVEST)
            hits = [h for h in RE_SHORT.findall(txt) if h not in prev]
            if hits:
                log("  ! 링크 API 를 놓쳐 화면에서 링크를 건졌습니다 (상품 대조 못 함)")
                return max(hits, key=len)
            page.wait_for_timeout(900)
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
    return None


def discover(page, product_url, expect_pid=None):
    """셀렉터 조합을 탐색한다. 성공하면 (link, input_sel, button_text).

    ⚠️ 한 번 시도할 때마다 페이지를 새로 열고 폼을 채워 제출한다.
    즉 시도 횟수가 곧 파트너스 사이트 접근 횟수다. 예전에는 5x4=20 번을
    쉬지 않고 두드렸는데, 이건 CLAUDE.md 제약 2 를 정면으로 어긴다.
    시도 상한을 두고 사이에 간격을 준다. 이 값들을 올리지 말 것.
    """
    goto_link_page(page)
    inputs = page.evaluate(JS_SCAN_INPUTS)
    buttons = page.evaluate(JS_SCAN_BUTTONS)
    log(f"탐색: 입력창 후보 {len(inputs)}개, 버튼 후보 {len(buttons)}개")

    # 점수순으로 이미 정렬돼 있다. 유망한 조합부터 나오도록 짝을 만든다.
    btn_texts = [b["text"] for b in buttons] + [None]
    combos = [(inp, bt) for inp in inputs[:5] for bt in btn_texts[:4]]
    if len(combos) > MAX_DISCOVER_ATTEMPTS:
        log(f"조합 {len(combos)}개 중 상위 {MAX_DISCOVER_ATTEMPTS}개만 시도합니다 "
            f"(접근 빈도 제한). 전부 실패하면 실제 DOM 을 관측해 "
            f"selectors.json 을 직접 채우세요.")
        combos = combos[:MAX_DISCOVER_ATTEMPTS]

    for i, (inp, bt) in enumerate(combos):
        if i:
            time.sleep(SLEEP_BETWEEN)   # 사람 속도. 줄이지 마세요.
        log(f"  시도 {i+1}/{len(combos)}: "
            f"input={inp['path'][:50]!r} button={bt!r}")
        link = attempt(page, inp["path"], bt, product_url, wait=14,
                       expect_pid=expect_pid)
        if link:
            log("  → 성공. 조합을 캐시합니다.")
            return link, inp["path"], bt

    log("자가탐색 실패. 재시도 루프를 돌리지 마세요 — 계정이 위험합니다.")
    log("tools/observe_coupang.py 로 실제 DOM 을 관측한 뒤 고치세요.")
    return None, None, None


def get_link(page, product_url, cache, expect_pid=None):
    """캐시 우선, 실패하면 재탐색.

    캐시가 비어 있으면 관측으로 확인된 값을 먼저 쓴다(KNOWN_SELECTORS).
    자가탐색은 시도 한 번이 곧 사이트 접근 한 번이라 되도록 안 돌리는 게
    좋다. 실제 화면을 봐서 아는 값이 있는데 탐색부터 돌릴 이유가 없다.
    """
    if not cache.get("input_sel"):
        cache.update(KNOWN_SELECTORS)
        log(f"캐시가 비어 관측값을 씁니다: input={cache['input_sel']!r} "
            f"button={cache['button_text']!r}")

    link = attempt(page, cache["input_sel"], cache.get("button_text"),
                   product_url, expect_pid=expect_pid)
    if link:
        save_cache(cache)
        return link

    log("알려진 셀렉터로 실패 → 자가탐색으로 넘어갑니다")
    link, isel, btext = discover(page, product_url, expect_pid=expect_pid)
    if link:
        cache["input_sel"] = isel
        cache["button_text"] = btext
        save_cache(cache)
    return link


def is_logged_in(page):
    """로그인 상태인지 본다.

    로그아웃 상태의 파트너스 첫 화면에는 '로그인' 과 '회원가입' 이 있고,
    로그인하면 '마이 페이지' 와 '링크 생성' 이 나온다. 이 차이로 가른다.
    URL 만 보면 안 된다. 해시 라우트라 로그아웃 상태에서도 같은 주소에
    머무를 수 있다.
    """
    if "login.coupang.com" in page.url:
        return False
    try:
        txt = page.evaluate("() => document.body.innerText") or ""
    except Exception:
        return False
    if "회원가입" in txt and "로그인" in txt and "마이 페이지" not in txt:
        return False
    return "링크 생성" in txt or "마이 페이지" in txt


def tick_keep_login(page):
    """로그인 화면의 '자동 로그인' 을 체크한다.

    ⚠️ 이게 안 되면 로그인이 그 브라우저 창에서만 유효하다.
    실측(2026-07-29): 체크하지 않고 로그인하면 인증 쿠키가 세션 쿠키로
    발급돼 브라우저를 닫는 순간 사라진다. 프로필에는 추적용 쿠키
    (PCID, MARKETID, _ga, Akamai bm_*)만 남고 다음 실행은 로그인 화면으로
    튕긴다. 무인 운영에서는 치명적이다.
    """
    try:
        box = page.locator("#login-keep-state").first
        box.wait_for(state="attached", timeout=8000)
        if box.is_checked():
            log("'자동 로그인' 이 이미 켜져 있습니다.")
            return True
        # 체크박스가 커스텀 UI 로 가려져 있으면 일반 클릭이 안 먹는다.
        try:
            box.check(timeout=3000)
        except Exception:
            box.check(timeout=3000, force=True)
        ok = box.is_checked()
        log("'자동 로그인' 을 켰습니다." if ok else "🔴 '자동 로그인' 을 켜지 못했습니다.")
        return ok
    except Exception as e:
        log(f"🔴 '자동 로그인' 체크박스를 찾지 못했습니다: {e}")
        log("   화면에서 직접 체크하고 로그인하세요. 안 켜면 세션이 안 남습니다.")
        return False


def do_login():
    os.makedirs(SHOT_DIR, exist_ok=True)

    with sync_playwright() as pw:
        ctx = open_context(pw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        # 링크 생성 화면으로 간다. 로그인이 필요하면 알아서 로그인으로 튕긴다.
        goto_link_page(page)

        if is_logged_in(page):
            log("이미 로그인돼 있습니다.")
        else:
            tick_keep_login(page)
            print()
            print("─" * 60)
            print("브라우저에서 로그인을 완료하세요.")
            print("'자동 로그인' 은 스크립트가 켜 두었습니다. 끄지 마세요.")
            print("끄면 브라우저를 닫는 순간 세션이 사라집니다.")
            print("끝났으면 여기서 Enter 를 누르세요.")
            print("─" * 60)
            try:
                input()
            except EOFError:
                log("입력을 받을 수 없습니다. 대화형 콘솔에서 실행하세요.")
                ctx.close()
                return False

            goto_link_page(page)
            page.screenshot(path=os.path.join(SHOT_DIR, "after_login.png"))
            if not is_logged_in(page):
                log("🔴 아직 로그인 상태가 아닙니다 → shots/after_login.png")
                ctx.close()
                return False
            log("로그인 확인됨.")

        ctx.close()

    # ── 진짜 검증: 브라우저를 껐다 켜서 세션이 살아남는지 본다 ──────
    # 로그인 직후에는 당연히 로그인 상태다. 문제는 그게 디스크에
    # 남느냐다. 여기서 확인하지 않으면 다음 주기에 조용히 실패한다.
    log("세션이 재시작 후에도 남는지 확인합니다...")
    with sync_playwright() as pw:
        ctx = open_context(pw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        goto_link_page(page)
        ok = is_logged_in(page)
        page.screenshot(path=os.path.join(SHOT_DIR, "after_relaunch.png"))
        ctx.close()

    if ok:
        log(f"✅ 세션이 재시작 후에도 유지됩니다 → {PROFILE_DIR}")
    else:
        log("🔴 재시작하니 로그아웃됐습니다 → shots/after_relaunch.png")
        log("   '자동 로그인' 을 켜고 다시 로그인해야 합니다.")
        log("   켜지 않으면 인증 쿠키가 세션 쿠키로 발급돼 브라우저를")
        log("   닫는 순간 사라집니다(실측).")
    return ok


def do_run(limit):
    conn = sqlite3.connect(DB_PATH)
    # 쿠팡 딜만 가져온다. 이 스크립트는 쿠팡 파트너스 페이지를 쓰므로
    # 토스 상품 URL 을 넣으면 엉뚱한 링크가 만들어지거나 실패한다.
    # 토스는 쉐어링크 대시보드를 써야 한다(docs/toss_sharelink.md).
    cutoff = (datetime.now() - timedelta(hours=MAX_DEAL_AGE_HOURS)
              ).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT product_id, title, product_url FROM deals "
        "WHERE platform = 'coupang' AND affiliate_url IS NULL AND posted_at IS NULL "
        "AND found_at >= ? "
        "ORDER BY found_at DESC LIMIT ?", (cutoff, limit)
    ).fetchall()

    if not rows:
        # 왜 0건인지 알 수 있게 남긴다. 조용한 0건은 조용한 정지와 구분이 안 된다.
        stale = conn.execute(
            "SELECT COUNT(*) FROM deals WHERE platform='coupang' "
            "AND affiliate_url IS NULL AND found_at < ?", (cutoff,)).fetchone()[0]
        log(f"변환할 신규 항목 없음 "
            f"(최근 {MAX_DEAL_AGE_HOURS}시간 내 미처리 0건, "
            f"그보다 오래된 것 {stale}건은 건너뜀)")
        conn.close()
        return 0, 0

    cache = load_cache()
    os.makedirs(SHOT_DIR, exist_ok=True)
    ok = fail = 0

    # 텔레그램 봇도 같은 프로필로 링크를 만든다. 동시에 열면 한쪽이 죽는다.
    with profile_lock(PROFILE_DIR, log=log), sync_playwright() as pw:
        ctx = open_context(pw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        goto_link_page(page)

        # URL 만 보면 안 된다. 해시 라우트라 로그아웃 상태에서도 같은
        # 주소에 머무를 수 있다. 화면 내용으로 판정한다.
        if not is_logged_in(page):
            log("세션 만료. `py src/partners_link.py --login` 을 먼저 실행하세요.")
            ctx.close(); conn.close()
            return 0, len(rows)

        for pid, title, purl in rows:
            try:
                # 상품 ID 를 넘겨 응답의 pageKey 와 대조하게 한다.
                # 엉뚱한 상품의 링크를 저장하면 발행 뒤에야 알게 된다.
                link = get_link(page, purl, cache, expect_pid=pid)
            except Exception as e:
                log(f"  ! [{pid}] {e}")
                link = None

            if not link:
                page.screenshot(path=os.path.join(SHOT_DIR, f"err_{pid}.png"))
                log(f"  - [{pid}] 실패 → shots/err_{pid}.png")
                fail += 1
                time.sleep(SLEEP_BETWEEN)   # 실패해도 간격은 지킨다
                continue

            conn.execute(
                "UPDATE deals SET affiliate_url=? "
                "WHERE platform='coupang' AND product_id=?", (link, pid))
            conn.commit()
            ok += 1
            log(f"  + [{pid}] {title[:35]} → {link}")
            time.sleep(SLEEP_BETWEEN)   # 사람 속도. 줄이지 마세요.

        ctx.close()

    conn.close()
    log(f"변환 결과: 성공 {ok} / 실패 {fail}")
    return ok, fail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--limit", type=int, default=MAX_LINKS_PER_RUN,
                    help=f"한 번에 만들 최대 개수 (기본 {MAX_LINKS_PER_RUN})")
    args = ap.parse_args()
    if args.login:
        do_login()
        return
    limit = min(args.limit, MAX_LINKS_PER_RUN)
    if limit < args.limit:
        log(f"--limit 을 {MAX_LINKS_PER_RUN} 로 낮춥니다 (접근 빈도 제한)")
    do_run(limit)


if __name__ == "__main__":
    main()
