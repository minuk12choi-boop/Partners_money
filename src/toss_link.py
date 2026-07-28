#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
toss_link.py

토스쇼핑 쉐어링크 대시보드에서 상품을 골라 내 쉐어링크를 발급하고
deals.db 에 저장한다.

쿠팡과 흐름이 다르다. 쿠팡은 딜방에서 상품을 알아낸 뒤 그 상품의
링크를 만들지만, 토스 대시보드에는 상품 검색이 없다(실측). 대신
대시보드가 이미 '30일 최저가 · 특가율 · 개당 수익 · 평점' 을 붙여
큐레이션해 준다. 그래서 딜방을 거치지 않고 대시보드 목록에서 바로
고른다. 상품과 링크가 같은 자리에서 나오므로 상품을 잘못 고를
위험도 없다.

최초 1회:
    python toss_link.py --login

이후:
    python toss_link.py                # 기본 개수만큼 발급
    python toss_link.py --limit 3
    python toss_link.py --dry-run      # 목록만 보고 발급하지 않음

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

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(HERE, "deals.db")
PROFILE_DIR = os.path.join(ROOT, "pw_toss_profile")
CACHE_PATH = os.path.join(HERE, "toss_selectors.json")
SHOT_DIR = os.path.join(ROOT, "shots")

HOME_URL = "https://sharelink.toss.im/home"
PRODUCTS_URL = "https://sharelink.toss.im/links/recommended-products"

# 발급된 내 쉐어링크의 형태
RE_SHARELINK = re.compile(r"https?://toss\.im/_m/[A-Za-z0-9]+")
RE_TOSS_PRODUCT_ID = re.compile(r"toss\.shopping/t/(\d+)")

# ── 접근 빈도 제한 ────────────────────────────────────────────────
# 쿠팡(partners_link.py)과 같은 보수적인 값을 쓴다.
# 토스 운영정책도 "사용자의 의도에 반하는 방식" 을 금지한다.
# 병렬화·대기시간 단축 금지.
MAX_LINKS_PER_RUN = 4
SLEEP_BETWEEN = 6
# ─────────────────────────────────────────────────────────────────

BTN_ISSUE = "링크 발급"


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- 페이지 스캔

# 상품 카드를 훑는다. 카드 구조를 하드코딩하지 않고 '링크 발급' 버튼을
# 기준으로 거슬러 올라가 그 카드의 텍스트를 모은다.
#
# 카드 경계 정의 — 실측으로 확정 (tools/observe_toss_cards.py, 2026-07-29)
#
# 예전 규칙은 "innerText 길이가 30 을 넘는 첫 조상" 이었는데, 그러면
# up=2 에서 멈춰 '68% 특가' 배지를 놓친다(배지는 up=3 에서 들어온다).
# 실제로 salePct 가 118/118 전부 None 이었다.
#
# 대신 **'링크 발급' 을 정확히 1개만 품는 가장 바깥 조상**을 카드로 본다.
# 스스로 검증되는 규칙이다. 하나라도 더 품는 순간 옆 카드가 섞이므로
# 남의 특가율·가격을 읽을 수 없다. 실측 프로파일:
#
#   up1 특가0 발급1 / up2 특가0 발급1 / up3 특가1 발급1 /
#   up4 특가1 발급1 / up5 특가118 발급118   ← 여기서 격자 전체로 터진다
#
# 경계가 up4 와 up5 사이에서 뚜렷하게 갈리므로 모호함이 없다.
JS_SCAN_CARDS = """(btnText) => {
  const cards = [];
  const btns = Array.from(document.querySelectorAll('button, [role=button]'))
    .filter(el => (el.innerText || '').trim() === btnText);

  btns.forEach((btn, idx) => {
    // '링크 발급' 을 정확히 1개 품는 가장 바깥 조상까지 올라간다
    let node = btn, card = null;
    for (let i = 0; i < 10; i++) {
      node = node.parentElement;
      if (!node) break;
      const n = ((node.innerText || '').match(/링크 발급/g) || []).length;
      if (n !== 1) break;
      card = node;
    }
    if (!card) return;

    const r = btn.getBoundingClientRect();
    const st = getComputedStyle(btn);
    const visible = r.width > 2 && r.height > 2 &&
                    st.display !== 'none' && st.visibility !== 'hidden';

    const lines = (card.innerText || '').split('\\n')
      .map(s => s.trim()).filter(Boolean);

    // 상품명: 가장 긴 줄. 특가율/가격/평점 줄은 짧다.
    let title = '';
    // 가격: '17,100원' 처럼 금액만 있는 줄.
    // 카드 전체에서 첫 금액을 잡으면 '개당 1,710원 수익'(수수료)이
    // 먼저 걸린다. 줄 단위로 정확히 일치하는 것만 가격으로 본다.
    let price = null;
    for (const L of lines) {
      if (/^\\d+% 특가$/.test(L)) continue;
      if (/^개당 [\\d,]+원 수익$/.test(L)) continue;
      // 단위당 단가 줄. '100ml당 172원', '100g당 1,340원', '1개당 685원'.
      // 가격 정규식에는 안 걸리지만 제목 후보로 남아 있으면
      // 제목이 아주 짧은 상품에서 제목 자리를 뺏을 수 있다.
      if (/^[\\d,.]+\\s*[A-Za-z가-힣]*당 [\\d,]+원$/.test(L)) continue;
      if (/^[\\d,]+원$/.test(L)) {
        if (price === null) price = parseInt(L.replace(/[,원]/g, ''), 10);
        continue;
      }
      if (L === btnText) continue;
      if (L.length > title.length) title = L;
    }

    const saleM    = (card.innerText || '').match(/(\\d+)% 특가/);
    const rewardM  = (card.innerText || '').match(/개당 ([\\d,]+)원 수익/);
    const ratingM  = (card.innerText || '').match(/(\\d(?:\\.\\d)?) \\(([\\d,]+)\\)/);

    cards.push({
      idx, visible, title, price,
      salePct: saleM   ? parseInt(saleM[1], 10)                      : null,
      reward:  rewardM ? parseInt(rewardM[1].replace(/,/g, ''), 10) : null,
      rating:  ratingM ? parseFloat(ratingM[1])                      : null,
      reviews: ratingM ? parseInt(ratingM[2].replace(/,/g, ''), 10) : null,
      lowest30: /30일 최저가/.test(card.innerText || ''),
      bestSeller: /베스트판매자/.test(card.innerText || ''),
    });
  });
  return cards;
}"""

# 페이지 전역에서 발급된 링크를 긁는다.
# input 의 value 는 outerHTML 에 안 나오므로 JS 로 읽어야 한다.
# (partners_link.py 의 JS_HARVEST 와 같은 이유. 건드리지 말 것.)
JS_HARVEST = """() => {
  const found = [];
  document.querySelectorAll('input, textarea').forEach(el => {
    if (el.value) found.push(el.value);
  });
  found.push(document.body ? document.body.innerText : '');
  return found.join('\\n');
}"""


# ---------------------------------------------------------------- 브라우저

def open_context(pw):
    os.makedirs(PROFILE_DIR, exist_ok=True)
    # CLAUDE.md 제약 5: headless 금지
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


def needs_login(url):
    return ("business.toss.im" in url or "/sign-in" in url
            or "/login" in url or "signup" in url)


def goto_products(page):
    page.goto(PRODUCTS_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    return not needs_login(page.url)


# ---------------------------------------------------------------- 발급

def scan_cards(page):
    try:
        cards = page.evaluate(JS_SCAN_CARDS, BTN_ISSUE)
    except Exception as e:
        log(f"카드 스캔 실패: {e}")
        return []
    return [c for c in cards if c.get("visible") and c.get("title")]


def issue_link(page, idx, wait=20):
    """idx 번째 '링크 발급' 버튼을 눌러 내 쉐어링크를 얻는다."""
    before = set(RE_SHARELINK.findall(page.evaluate(JS_HARVEST)))

    try:
        btn = page.get_by_role("button", name=BTN_ISSUE, exact=True).nth(idx)
        btn.scroll_into_view_if_needed(timeout=5000)
        btn.click(timeout=6000)
    except Exception as e:
        log(f"  버튼 클릭 실패: {e}")
        return None

    deadline = time.time() + wait
    while time.time() < deadline:
        txt = page.evaluate(JS_HARVEST)
        hits = [h for h in RE_SHARELINK.findall(txt) if h not in before]
        if hits:
            return max(hits, key=len)
        page.wait_for_timeout(800)
    return None


def resolve_product_id(page, sharelink):
    """발급된 내 링크를 따라가 상품 ID 를 얻는다.

    카드에는 상품 ID 가 드러나지 않는다. 내 링크를 한 번 열어보면
    toss.shopping/t/<id> 로 이어지므로 거기서 얻는다.
    새 탭에서 열고 바로 닫아 대시보드 상태를 건드리지 않는다.
    """
    tab = None
    try:
        tab = page.context.new_page()
        tab.goto(sharelink, wait_until="domcontentloaded", timeout=20000)
        tab.wait_for_timeout(1500)
        m = RE_TOSS_PRODUCT_ID.search(tab.url)
        return m.group(1) if m else None
    except Exception as e:
        log(f"  상품ID 확인 실패: {e}")
        return None
    finally:
        if tab is not None:
            try:
                tab.close()
            except Exception:
                pass


# ---------------------------------------------------------------- DB

def ensure_schema(conn):
    """kakao_deal_extract.py 와 같은 스키마를 쓴다."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS deals (
            platform     TEXT NOT NULL DEFAULT 'coupang',
            product_id   TEXT NOT NULL,
            title        TEXT,
            price        INTEGER,
            source_url   TEXT,
            product_url  TEXT,
            raw_message  TEXT,
            chat_time    TEXT,
            found_at     TEXT,
            affiliate_url TEXT,
            posted_at    TEXT,
            PRIMARY KEY (platform, product_id)
        )
    """)
    conn.commit()


def already_known(conn, product_id):
    return conn.execute(
        "SELECT 1 FROM deals WHERE platform='toss' AND product_id=?",
        (product_id,)).fetchone() is not None


def save_deal(conn, product_id, card, sharelink):
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT OR REPLACE INTO deals (platform,product_id,title,price,source_url,"
        "product_url,raw_message,chat_time,found_at,affiliate_url,posted_at) "
        "VALUES ('toss',?,?,?,?,?,?,?,?,?,NULL)",
        (product_id, card["title"], card.get("price"),
         PRODUCTS_URL, f"https://toss.shopping/t/{product_id}",
         # 딜방이 아니라 대시보드에서 온 건이라는 걸 남긴다
         f"쉐어링크 대시보드 · {card.get('salePct')}% 특가 · "
         f"개당 {card.get('reward')}원 수익 · 평점 {card.get('rating')}",
         now, now, sharelink),
    )
    conn.commit()


# ---------------------------------------------------------------- 선별

def known_titles(conn):
    """이미 DB 에 있는 토스 상품의 제목 집합.

    상품 ID 로 거르는 게 정확하지만, 카드에는 상품 ID 가 없다(실측).
    ID 는 링크를 발급해 따라가 봐야만 알 수 있어서, 그때는 이미 한 건을
    써 버린 뒤다. 그래서 제목으로 먼저 거른다.

    제목이 우연히 겹치면 그 건을 건너뛸 뿐 잘못된 링크를 만들지는
    않으므로 안전한 방향의 오차다. 최종 중복 판정은 발급 후
    `already_known()` 이 상품 ID 로 다시 한다.
    """
    if conn is None:
        return set()
    return {r[0] for r in conn.execute(
        "SELECT title FROM deals WHERE platform='toss' AND title IS NOT NULL")}


def pick(cards, conn, limit, max_price=None):
    """발급할 상품을 고른다.

    ⚠️ `개당 수익` 으로 정렬하지 않는다. 실측 결과 수수료는 가격의
    정확히 10% 다(118건 전부 9.99~10.00%). 즉 개당 수익 순 정렬은
    **가격 순 정렬과 같은 것**이라 아무 정보도 주지 않고, 실제로
    118만원 정수기·67만원 냉장고만 골라내고 있었다. 딜방 성격(중앙값
    24,800원)과도 맞지 않고 고가 가전은 전환도 거의 없다.

    대신 '얼마나 싸게 사는가' 로 고른다.
      1. 30일 최저가 표시가 있는 것 우선
      2. 특가율(%) 높은 순
      3. 리뷰 수 많은 순 — 같은 특가율이면 검증된 상품으로

    ※ 이 순서는 잠정값이다. 무엇을 올릴지는 사업 판단이라
      소유자 확인이 필요하다. TASKS.md T3-b 참고.
    """
    seen = known_titles(conn)
    fresh = []
    for c in cards:
        if not c["title"]:
            continue
        if c["title"] in seen:
            continue
        if max_price is not None and (c.get("price") or 0) > max_price:
            continue
        fresh.append(c)

    fresh.sort(key=lambda c: (not c.get("lowest30"),
                              -(c.get("salePct") or 0),
                              -(c.get("reviews") or 0)))
    return fresh[:limit]


# ---------------------------------------------------------------- 메인

def do_login():
    with sync_playwright() as pw:
        ctx = open_context(pw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(HOME_URL, wait_until="domcontentloaded")
        print("\n브라우저에서 토스 비즈니스 계정으로 로그인한 뒤")
        print("여기서 Enter 를 누르세요.")
        input()
        ok = goto_products(page)
        os.makedirs(SHOT_DIR, exist_ok=True)
        page.screenshot(path=os.path.join(SHOT_DIR, "toss_after_login.png"))
        log("로그인 확인" if ok else "아직 로그인 화면입니다. 다시 시도하세요.")
        log(f"세션 저장 → {PROFILE_DIR}")
        ctx.close()


def do_run(limit, dry_run, max_price=None):
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)
    os.makedirs(SHOT_DIR, exist_ok=True)
    ok = fail = skip = 0

    with sync_playwright() as pw:
        ctx = open_context(pw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if not goto_products(page):
            log("세션 만료. `python toss_link.py --login` 을 먼저 실행하세요.")
            ctx.close(); conn.close()
            return 0, 0

        cards = scan_cards(page)
        log(f"상품 카드 {len(cards)}개 발견")
        if not cards:
            page.screenshot(path=os.path.join(SHOT_DIR, "toss_no_cards.png"))
            log("카드를 찾지 못했습니다 → shots/toss_no_cards.png")
            ctx.close(); conn.close()
            return 0, 0

        targets = pick(cards, conn, limit, max_price)
        log(f"발급 대상 {len(targets)}개")
        for c in targets:
            price = f"{c['price']:,}원" if c.get("price") else "가격?"
            log(f"  · {c['title'][:45]}  {price}")
            log(f"      특가 {c.get('salePct')}% · 평점 {c.get('rating')} "
                f"({c.get('reviews')})"
                f"{' · 30일최저' if c.get('lowest30') else ''}")

        if dry_run:
            log("dry-run: 발급하지 않고 종료합니다.")
            ctx.close(); conn.close()
            return 0, 0

        for c in targets:
            link = issue_link(page, c["idx"])
            if not link:
                page.screenshot(path=os.path.join(SHOT_DIR, f"toss_err_{c['idx']}.png"))
                log(f"  - 발급 실패: {c['title'][:35]} → shots/toss_err_{c['idx']}.png")
                fail += 1
                time.sleep(SLEEP_BETWEEN)
                continue

            pid = resolve_product_id(page, link)
            if not pid:
                log(f"  ! 상품ID 미확인이라 저장하지 않음: {link}")
                fail += 1
                time.sleep(SLEEP_BETWEEN)
                continue

            if already_known(conn, pid):
                log(f"  = 이미 있는 상품: [{pid}] {c['title'][:35]}")
                skip += 1
                time.sleep(SLEEP_BETWEEN)
                continue

            save_deal(conn, pid, c, link)
            ok += 1
            log(f"  + [{pid}] {c['title'][:35]} → {link}")
            time.sleep(SLEEP_BETWEEN)   # 사람 속도. 줄이지 마세요.

        ctx.close()

    conn.close()
    log(f"발급 결과: 성공 {ok} / 실패 {fail} / 기존 {skip}")
    return ok, fail


def main():
    ap = argparse.ArgumentParser(
        description="토스 쉐어링크 대시보드에서 링크 발급")
    ap.add_argument("--login", action="store_true", help="최초 1회 수동 로그인")
    ap.add_argument("--limit", type=int, default=MAX_LINKS_PER_RUN,
                    help=f"한 번에 발급할 최대 개수 (기본 {MAX_LINKS_PER_RUN})")
    ap.add_argument("--dry-run", action="store_true",
                    help="발급하지 않고 대상 목록만 출력")
    ap.add_argument("--max-price", type=int, default=None,
                    help="이 가격을 넘는 상품은 고르지 않는다 (원). 기본: 제한 없음")
    args = ap.parse_args()

    if args.login:
        do_login()
        return

    limit = min(args.limit, MAX_LINKS_PER_RUN)
    if limit < args.limit:
        log(f"--limit 을 {MAX_LINKS_PER_RUN} 로 낮춥니다 (접근 빈도 제한)")
    do_run(limit, args.dry_run, args.max_price)


if __name__ == "__main__":
    main()
