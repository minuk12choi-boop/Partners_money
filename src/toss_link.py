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

import toss_api
from env import load_env
from lock import profile_lock, LockBusy
from schema import ensure_deals

load_env()

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

# 링크를 만드는 API. 실측으로 확인한 경로다.
#   POST https://sharelink.toss.im/api-public/v3/shopping/sharelink/link/issue
# 버전(v3)이 바뀔 수 있어 앞뒤를 뺀 부분만으로 매칭한다.
LINK_ISSUE_PATH = "/sharelink/link/issue"

# ── 접근 빈도 제한 ────────────────────────────────────────────────
# 쿠팡(partners_link.py)과 같은 보수적인 값을 쓴다.
# 토스 운영정책도 "사용자의 의도에 반하는 방식" 을 금지한다.
# 병렬화·대기시간 단축 금지.
MAX_LINKS_PER_RUN = 4
SLEEP_BETWEEN = 6
# ─────────────────────────────────────────────────────────────────

# 이 가격을 넘는 상품은 고르지 않는다. 소유자가 정한 값이다(2026-07-29).
# 대시보드에는 118만원 정수기, 67만원 냉장고 같은 고가 가전이 섞여 있는데
# 딜방 가격 중앙값은 24,800원이다. 상한이 없으면 그런 것들이 올라간다.
# --max-price 로 실행할 때 덮어쓸 수 있다.
MAX_PRICE = 200_000

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

# 예전에는 여기 JS_HARVEST 가 있었다. 페이지에서 발급된 링크를 긁는
# 용도였는데, 실측 결과 **링크가 DOM 에 아예 나타나지 않아** 쓸모가 없었다.
# partners_link.py(쿠팡)에는 여전히 필요하다. 쿠팡은 결과를 input 에 담아
# 보여주기 때문이다. 두 사이트의 동작이 다르므로 같이 지우지 말 것.


# ---------------------------------------------------------------- 브라우저

def open_context(pw):
    os.makedirs(PROFILE_DIR, exist_ok=True)
    # CLAUDE.md 제약 5: headless 금지
    ctx = pw.chromium.launch_persistent_context(
        PROFILE_DIR, headless=False,
        viewport={"width": 1440, "height": 900}, locale="ko-KR",
        args=["--disable-blink-features=AutomationControlled"],
    )
    # 발급 API 응답을 놓쳤을 때 클립보드에서 링크를 건지기 위한 권한.
    # 토스는 발급과 동시에 링크를 클립보드에 넣는다(실측).
    try:
        ctx.grant_permissions(["clipboard-read", "clipboard-write"],
                              origin="https://sharelink.toss.im")
    except Exception as e:
        log(f"클립보드 권한 부여 실패(대비책만 못 씁니다): {e}")
    return ctx


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


def session_alive(page):
    """로그인이 **지금** 살아 있는가. `doctor.py` 가 부른다.

    `partners_link` 도 같은 이름으로 갖고 있다. 점검이 두 플랫폼에
    같은 방법으로 물어볼 수 있어야 한쪽만 빠뜨리는 일이 없다.
    """
    return goto_products(page)


# ---------------------------------------------------------------- 발급

def scan_cards(page):
    try:
        cards = page.evaluate(JS_SCAN_CARDS, BTN_ISSUE)
    except Exception as e:
        log(f"카드 스캔 실패: {e}")
        return []
    return [c for c in cards if c.get("visible") and c.get("title")]


def issue_link(page, idx, wait=20):
    """idx 번째 '링크 발급' 버튼을 눌러 (쉐어링크, 상품ID) 를 얻는다.

    ⚠️ 화면에서 링크를 읽지 않는다. 실측 결과(tools/observe_toss_issue.py,
    2026-07-29) **발급된 링크는 DOM 에 전혀 나타나지 않는다.**
    '링크를 복사했어요.' 토스트만 뜨고 링크는 클립보드로 간다.
    예전의 JS_HARVEST 방식은 원리적으로 성공할 수 없었다.

    대신 발급 API 의 응답을 가로챈다. 실측한 응답:

        POST .../shopping/sharelink/link/issue
        {"resultType":"SUCCESS","success":{
           "shortUrl":"https://toss.im/_m/vloN92Cn",
           "originUrl":"https://toss.shopping/t/599038939?k=...&referrer=affiliate",
           "affTrackKey":null}}

    `originUrl` 에 상품 ID 가 들어 있어서 내 링크를 새 탭으로 따라가
    확인하던 단계(resolve_product_id)가 통째로 없어졌다. 접근 횟수가
    줄고 실패 지점도 하나 사라진다.

    ※ 이건 링크를 손으로 조립하는 게 아니다(CLAUDE.md 제약 4).
      토스 UI 의 발급 버튼을 그대로 누르고, 토스 시스템이 돌려준
      링크를 읽을 뿐이다.
    """
    got = {}

    def on_response(resp):
        if LINK_ISSUE_PATH not in resp.url:
            return
        try:
            data = resp.json()
        except Exception as e:
            log(f"  발급 응답 파싱 실패: {e}")
            return
        s = (data or {}).get("success") or {}
        short = s.get("shortUrl")
        origin = s.get("originUrl") or ""
        if not short:
            log(f"  발급 응답에 shortUrl 이 없음: {str(data)[:200]}")
            return
        m = RE_TOSS_PRODUCT_ID.search(origin)
        got["link"] = short
        got["pid"] = m.group(1) if m else None
        if not got["pid"]:
            log(f"  발급 응답의 originUrl 에서 상품ID 를 못 찾음: {origin[:120]}")

    page.on("response", on_response)
    try:
        try:
            btn = page.get_by_role("button", name=BTN_ISSUE, exact=True).nth(idx)
            btn.scroll_into_view_if_needed(timeout=5000)
            btn.click(timeout=6000)
        except Exception as e:
            log(f"  버튼 클릭 실패: {e}")
            return None, None

        deadline = time.time() + wait
        while time.time() < deadline:
            if got.get("link"):
                return got["link"], got.get("pid")
            page.wait_for_timeout(400)
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    # 응답을 놓쳤을 때의 대비책. 토스는 발급과 동시에 클립보드에
    # '고지문구 + 상품명 + 링크' 를 넣는다(실측). 여기엔 상품 ID 가
    # 없으므로 링크만 건지고 상품 ID 는 따로 확인해야 한다.
    link = read_clipboard_link(page)
    if link:
        log("  ! 발급 API 응답을 놓쳐 클립보드에서 링크를 건졌습니다.")
        return link, None
    return None, None


def read_clipboard_link(page):
    """클립보드에서 쉐어링크를 읽는다. 대비책 경로."""
    try:
        text = page.evaluate("() => navigator.clipboard.readText()")
    except Exception as e:
        log(f"  클립보드 읽기 실패: {e}")
        return None
    m = RE_SHARELINK.search(text or "")
    return m.group(0) if m else None


def find_card(cards, title):
    """상품명으로 카드를 찾는다. **정확히 하나만 맞을 때만** 돌려준다.

    카드 DOM 에는 상품 ID 가 없다(실측). 그래서 대시보드 목록 API 가 준
    `displayName` 으로 짝을 맞춘다.

    ⚠️ 여러 개가 맞으면 포기한다. 아무거나 고르면 **다른 상품의 링크를
    그 상품이라고 발행**하게 된다. 못 찾는 것보다 훨씬 나쁘다.
    발급 뒤에 상품 ID 로 한 번 더 대조하지만, 그때는 이미 링크를 한 건
    써 버린 뒤다.
    """
    if not title:
        return None

    def norm(s):
        return re.sub(r"\s+", "", s or "")

    want = norm(title)
    exact = [c for c in cards if norm(c.get("title")) == want]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None     # 같은 이름이 여럿이면 구분할 방법이 없다

    partial = [c for c in cards
               if want and (want in norm(c.get("title"))
                            or norm(c.get("title")) in want)]
    return partial[0] if len(partial) == 1 else None


def issue_for_product(page, taca_id, log=log):
    """상품 ID 하나를 지정해 **내** 쉐어링크를 발급한다.

    (링크, 상품정보) 또는 (None, 사유) 를 돌려준다.

    딜방에 올라온 토스 딜을 봇으로 넘겼을 때 쓴다. 방장 링크는 어떤
    상품인지 알아내는 데만 쓰고 버린다. 그대로 내보내면 수익이 방장에게
    간다.

    ⚠️ **발급된 링크가 요청한 상품의 것인지 반드시 대조한다.** 카드를
    제목으로 찾기 때문에 잘못 짚을 여지가 있고, 잘못 짚으면 엉뚱한 상품
    링크를 그 상품이라고 발행하게 된다. 대조에 실패하면 링크를 버린다
    (이미 발급된 링크는 로그에 남긴다 — 조용히 잃지 않기 위함).

    **한계**: 대시보드 큐레이션 목록(약 117개)에 있는 상품만 된다.
    토스 PC 웹에는 상품 검색이 없어서(실측) 목록 밖 상품은 발급할 방법이
    없다. 그때는 사장님이 토스 앱에서 직접 발급하셔야 한다.
    """
    taca_id = str(taca_id)
    products = fetch_dashboard(page, log=log)
    info = products.get(taca_id)
    if not info:
        return None, ("dashboard_miss", "대시보드 큐레이션 목록에 없는 상품")

    cards = scan_cards(page)
    if not cards:
        return None, ("no_cards", "대시보드에서 상품 카드를 찾지 못했습니다")

    card = find_card(cards, info.get("title"))
    if card is None:
        return None, ("card_miss",
                      f"목록에는 있는데 화면에서 카드를 특정하지 못했습니다"
                      f" ({(info.get('title') or '')[:30]})")

    link, pid = issue_link(page, card["idx"])
    if not link:
        return None, ("issue_fail", "링크 발급 버튼이 응답하지 않았습니다")

    if not pid:
        pid = resolve_product_id(page, link)

    if str(pid) != taca_id:
        # 여기서 그냥 쓰면 다른 상품의 링크를 발행하게 된다. 버린다.
        log(f"  ! 발급된 링크가 요청한 상품과 다릅니다. 버립니다.")
        log(f"    요청 {taca_id} / 발급 {pid} / 링크 {link}")
        return None, ("mismatch",
                      "발급된 링크가 요청한 상품과 달라 사용하지 않았습니다")

    return link, info


def fetch_dashboard(page, log=log):
    """대시보드 목록 API. 로그인 상태가 아니면 빈 dict."""
    if needs_login(page.url):
        return {}
    return toss_api.fetch_products(page, log=log)


def resolve_product_id(page, sharelink):
    """내 링크를 따라가 상품 ID 를 얻는다. 대비책 경로.

    평소에는 발급 API 응답의 originUrl 에서 바로 얻으므로 쓰이지 않는다.
    응답을 놓쳐 클립보드로 링크만 건졌을 때만 여기까지 온다.
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
    """스키마는 schema.py 가 단일 출처다.

    예전에는 여기에 CREATE TABLE 을 복붙해 두었는데, 컬럼이 추가될 때마다
    조용히 어긋났다. 어느 스크립트가 먼저 실행될지 정해져 있지 않으므로
    이 파일이 옛 스키마로 테이블을 만들어 버리면 다른 스크립트가 죽는다.
    """
    ensure_deals(conn)


def already_known(conn, product_id):
    return conn.execute(
        "SELECT 1 FROM deals WHERE platform='toss' AND product_id=?",
        (product_id,)).fetchone() is not None


def save_deal(conn, product_id, card, sharelink, api=None):
    """`api` 는 toss_api.lookup() 결과. 정가처럼 카드에 없는 값을 준다.

    카드(DOM)에서 얻는 값보다 API 값이 정확하다. 카드에는 정가가 아예
    표시되지 않고, 특가율도 배지 텍스트를 긁은 것이다. API 가 있으면
    그쪽을 우선한다. 없는 값은 지어내지 않고 비워 둔다.
    """
    now = datetime.now().isoformat(timespec="seconds")
    api = api or {}
    price = api.get("price") or card.get("price")
    pct = api.get("discount_pct") or card.get("salePct")
    original = api.get("original_price")

    note = (f"쉐어링크 대시보드 · {pct}% 특가 · "
            f"개당 {card.get('reward')}원 수익 · 평점 {card.get('rating')}")
    if original:
        note += f" · 정가 {original:,}원"

    conn.execute(
        "INSERT OR REPLACE INTO deals (platform,product_id,title,price,source_url,"
        "product_url,raw_message,chat_time,found_at,affiliate_url,posted_at,"
        "discount_pct,discount_amt,original_price) "
        "VALUES ('toss',?,?,?,?,?,?,?,?,?,NULL,?,NULL,?)",
        (product_id, api.get("title") or card["title"], price,
         PRODUCTS_URL, f"https://toss.shopping/t/{product_id}",
         # 딜방이 아니라 대시보드에서 온 건이라는 걸 남긴다
         note, now, now, sharelink, pct, original),
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

    대신 '얼마나 싸게 사는가' 로 고른다. 소유자가 확정한 기준이다
    (2026-07-29). 임의로 바꾸지 말 것.
      0. 20만원 초과 제외 (MAX_PRICE)
      1. 30일 최저가 표시가 있는 것 우선
      2. 특가율(%) 높은 순
      3. 리뷰 수 많은 순 — 같은 특가율이면 검증된 상품으로
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


def do_run(limit, dry_run, max_price=MAX_PRICE):
    conn = sqlite3.connect(DB_PATH)
    ensure_schema(conn)
    os.makedirs(SHOT_DIR, exist_ok=True)
    ok = fail = skip = 0

    with profile_lock(PROFILE_DIR, log=log), sync_playwright() as pw:
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

        api_cache = {}
        for c in targets:
            link, pid = issue_link(page, c["idx"])
            if not link:
                page.screenshot(path=os.path.join(SHOT_DIR, f"toss_err_{c['idx']}.png"))
                log(f"  - 발급 실패: {c['title'][:35]} → shots/toss_err_{c['idx']}.png")
                fail += 1
                time.sleep(SLEEP_BETWEEN)
                continue

            # 평소에는 발급 응답에서 이미 상품ID 가 나온다.
            # 여기까지 오는 건 클립보드 대비책으로 링크만 건진 경우다.
            if not pid:
                pid = resolve_product_id(page, link)

            if not pid:
                # 링크는 이미 발급됐다. 조용히 버리면 발급만 하고
                # 기록이 없는 상태가 되므로 반드시 남긴다.
                log(f"  ! 상품ID 미확인. 발급된 링크를 잃지 않도록 기록해 둡니다: {link}")
                log(f"    상품명: {c['title']}")
                fail += 1
                time.sleep(SLEEP_BETWEEN)
                continue

            if already_known(conn, pid):
                log(f"  = 이미 있는 상품이라 저장하지 않음: [{pid}] {c['title'][:35]}")
                skip += 1
                time.sleep(SLEEP_BETWEEN)
                continue

            # 목록 API 에서 정가 등 카드에 없는 값을 보탠다.
            # 한 번 실행에 한 번만 부르도록 api_cache 를 재사용한다.
            save_deal(conn, pid, c, link,
                      toss_api.lookup(page, pid, log=log, cache=api_cache))
            ok += 1
            log(f"  + [{pid}] {c['title'][:35]} → {link}")
            time.sleep(SLEEP_BETWEEN)   # 사람 속도. 줄이지 마세요.

        ctx.close()

    conn.close()
    log(f"발급 결과: 성공 {ok} / 실패 {fail} / 기존 {skip}")
    return ok, fail


def issue_one(taca_id, log=log):
    """상품 ID 하나에 대해 브라우저를 열고 내 쉐어링크를 발급한다.

    봇이 쓴다. 브라우저 열기·잠금·로그인 확인까지 여기서 처리하므로
    부르는 쪽은 결과만 보면 된다.
    (링크, 상품정보) 또는 (None, (사유코드, 설명)).
    """
    try:
        with profile_lock(PROFILE_DIR, log=log), sync_playwright() as pw:
            ctx = open_context(pw)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                if not goto_products(page):
                    return None, ("login", "토스 쉐어링크 세션이 만료되었습니다")
                return issue_for_product(page, taca_id, log=log)
            finally:
                ctx.close()
    except LockBusy as e:
        return None, ("busy", f"지금 다른 작업이 브라우저를 쓰고 있습니다: {e}")
    except Exception as e:
        log(f"발급 중 오류: {e}")
        return None, ("error", f"발급 중 오류가 났습니다: {e}")


def main():
    ap = argparse.ArgumentParser(
        description="토스 쉐어링크 대시보드에서 링크 발급")
    ap.add_argument("--login", action="store_true", help="최초 1회 수동 로그인")
    ap.add_argument("--limit", type=int, default=MAX_LINKS_PER_RUN,
                    help=f"한 번에 발급할 최대 개수 (기본 {MAX_LINKS_PER_RUN})")
    ap.add_argument("--dry-run", action="store_true",
                    help="발급하지 않고 대상 목록만 출력")
    ap.add_argument("--max-price", type=int, default=MAX_PRICE,
                    help=f"이 가격을 넘는 상품은 고르지 않는다 (원). "
                         f"기본 {MAX_PRICE:,}원. 0 이면 제한 없음")
    args = ap.parse_args()

    if args.login:
        do_login()
        return

    limit = min(args.limit, MAX_LINKS_PER_RUN)
    if limit < args.limit:
        log(f"--limit 을 {MAX_LINKS_PER_RUN} 로 낮춥니다 (접근 빈도 제한)")
    # 0 을 '제한 없음' 으로 쓴다. None 을 넘기려면 명시적인 값이 필요하다.
    max_price = args.max_price if args.max_price and args.max_price > 0 else None
    do_run(limit, args.dry_run, max_price)


if __name__ == "__main__":
    main()
