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
from datetime import datetime

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "deals.db")
PROFILE_DIR = os.path.join(HERE, "pw_profile")
CACHE_PATH = os.path.join(HERE, "selectors.json")
SHOT_DIR = os.path.join(HERE, "shots")

LINK_PAGE = "https://partners.coupang.com/#affiliate/ws/link"
RE_SHORT = re.compile(r"https?://link\.coupang\.com/[A-Za-z0-9/_\-\?\=\&\.%]+")

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


def attempt(page, input_sel, button_text, product_url, wait=22):
    """한 조합으로 시도. 성공 시 딥링크 반환."""
    goto_link_page(page)
    try:
        loc = page.locator(input_sel).first
        loc.wait_for(state="visible", timeout=6000)
    except PWTimeout:
        return None

    before = page.evaluate(JS_HARVEST)
    prev = set(RE_SHORT.findall(before))

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
        txt = page.evaluate(JS_HARVEST)
        hits = [h for h in RE_SHORT.findall(txt) if h not in prev]
        if hits:
            return max(hits, key=len)
        page.wait_for_timeout(900)
    return None


def discover(page, product_url):
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
        link = attempt(page, inp["path"], bt, product_url, wait=14)
        if link:
            log("  → 성공. 조합을 캐시합니다.")
            return link, inp["path"], bt

    log("자가탐색 실패. 재시도 루프를 돌리지 마세요 — 계정이 위험합니다.")
    log("tools/observe_coupang.py 로 실제 DOM 을 관측한 뒤 고치세요.")
    return None, None, None


def get_link(page, product_url, cache):
    """캐시 우선, 실패하면 재탐색."""
    if cache.get("input_sel"):
        link = attempt(page, cache["input_sel"], cache.get("button_text"), product_url)
        if link:
            return link
        log("캐시된 셀렉터 실패 → 재탐색")

    link, isel, btext = discover(page, product_url)
    if link:
        cache["input_sel"] = isel
        cache["button_text"] = btext
        save_cache(cache)
    return link


def do_login():
    with sync_playwright() as pw:
        ctx = open_context(pw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://partners.coupang.com/", wait_until="domcontentloaded")
        print("\n브라우저에서 로그인을 완료한 뒤 여기서 Enter 를 누르세요.")
        input()
        goto_link_page(page)
        os.makedirs(SHOT_DIR, exist_ok=True)
        page.screenshot(path=os.path.join(SHOT_DIR, "after_login.png"))
        log(f"세션 저장 완료 → {PROFILE_DIR}")
        ctx.close()


def do_run(limit):
    conn = sqlite3.connect(DB_PATH)
    # 쿠팡 딜만 가져온다. 이 스크립트는 쿠팡 파트너스 페이지를 쓰므로
    # 토스 상품 URL 을 넣으면 엉뚱한 링크가 만들어지거나 실패한다.
    # 토스는 쉐어링크 대시보드를 써야 한다(docs/toss_sharelink.md).
    rows = conn.execute(
        "SELECT product_id, title, product_url FROM deals "
        "WHERE platform = 'coupang' AND affiliate_url IS NULL AND posted_at IS NULL "
        "ORDER BY found_at DESC LIMIT ?", (limit,)
    ).fetchall()

    if not rows:
        log("변환할 항목 없음")
        conn.close()
        return 0, 0

    cache = load_cache()
    os.makedirs(SHOT_DIR, exist_ok=True)
    ok = fail = 0

    with sync_playwright() as pw:
        ctx = open_context(pw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        goto_link_page(page)

        if "login" in page.url.lower():
            log("세션 만료. `python partners_link.py --login` 을 먼저 실행하세요.")
            ctx.close(); conn.close()
            return 0, len(rows)

        for pid, title, purl in rows:
            try:
                link = get_link(page, purl, cache)
            except Exception as e:
                log(f"  ! [{pid}] {e}")
                link = None

            if not link:
                page.screenshot(path=os.path.join(SHOT_DIR, f"err_{pid}.png"))
                log(f"  - [{pid}] 실패 → shots/err_{pid}.png")
                fail += 1
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
