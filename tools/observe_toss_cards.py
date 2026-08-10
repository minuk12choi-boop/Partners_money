#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
observe_toss_cards.py — 쉐어링크 '프로모션 상품' 화면의 상품 카드 관측

`toss_link.py` 가 실제로 쓰는 `JS_SCAN_CARDS` 를 그대로 import 해서 돌린다.
관측 대상과 운영 코드가 어긋나면 관측의 의미가 없기 때문이다.

이 스크립트는 읽기 전용이다. '링크 발급' 을 누르지 않는다.
단, 인덱스 정합성 검사를 위해 버튼에 `data-pm-idx` 속성을 남긴다.
DOM 속성일 뿐이고 새로고침하면 사라진다. 계정 상태는 건드리지 않는다.

확인하는 것 (전부 `toss_link.py` 의 미검증 가정이다)

1. **인덱스 정합성** — 가장 위험한 항목.
   `issue_link()` 는 `get_by_role("button", name="링크 발급").nth(idx)` 로
   누르는데, 그 `idx` 는 `JS_SCAN_CARDS` 가 매긴 번호다. 두 목록의
   순서가 다르면 **엉뚱한 상품의 링크를 발급한다.** 조용히 틀리므로
   반드시 사전에 확인해야 한다.
2. `salePct` 가 왜 전부 None 인가 — 카드 경계가 '% 특가' 배지를 품는가
3. `개당 수익` 이 가격과 어떤 관계인가 — 정렬 기준으로 쓸 값인가
4. 가격대 분포 — 무엇을 고르게 되는가

사용법:
    py tools/observe_toss_cards.py
    py tools/observe_toss_cards.py --url https://sharelink.toss.im/home
"""

import argparse
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

# 운영 코드의 스캐너를 그대로 쓴다
import toss_link as T


class Tee:
    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")

    def __call__(self, msg=""):
        print(msg)
        self.f.write(str(msg) + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


# '링크 발급' 버튼에 DOM 순서대로 번호를 찍고, 각 버튼이 속한 카드의
# 원문 텍스트를 함께 돌려준다. JS 쪽 순서를 Python 쪽에서 대조하기 위함이다.
JS_MARK = """(btnText) => {
  const btns = Array.from(document.querySelectorAll('button, [role=button]'))
    .filter(el => (el.innerText || '').trim() === btnText);
  const out = [];
  btns.forEach((btn, i) => {
    btn.setAttribute('data-pm-idx', String(i));
    let node = btn, card = null;
    for (let k = 0; k < 8 && node; k++) {
      node = node.parentElement;
      if (!node) break;
      if ((node.innerText || '').trim().length > 30) { card = node; break; }
    }
    out.push({
      i,
      tag: btn.tagName.toLowerCase(),
      cardText: card ? (card.innerText || '').trim() : '',
      // 카드 경계를 몇 단계 위에서 잡았는지, 그리고 한 단계 더 위로 가면
      // '% 특가' 배지가 들어오는지 본다 (salePct 가 None 인 원인 추적)
      parentHasSale: card && card.parentElement
        ? /\\d+% 특가/.test(card.parentElement.innerText || '') : false,
      cardHasSale: card ? /\\d+% 특가/.test(card.innerText || '') : false,
    });
  });
  return out;
}"""


# 버튼에서 조상을 하나씩 거슬러 올라가며 각 단계가 '% 특가' 와 '링크 발급' 을
# 각각 몇 개 품고 있는지 센다. 카드 하나만 감싸는 단계는 둘 다 1이어야 한다.
JS_CLIMB = """(btnText) => {
  const btns = Array.from(document.querySelectorAll('button, [role=button]'))
    .filter(el => (el.innerText || '').trim() === btnText);
  return btns.map((btn, i) => {
    const levels = [];
    let node = btn;
    for (let up = 1; up <= 10; up++) {
      node = node.parentElement;
      if (!node) break;
      const t = node.innerText || '';
      levels.push({
        up,
        tag: node.tagName.toLowerCase(),
        len: t.length,
        sale: (t.match(/\\d+% 특가/g) || []).length,
        issue: (t.match(/링크 발급/g) || []).length,
      });
    }
    return { i, levels };
  });
}"""


# 카드 하나(= '링크 발급' 을 정확히 1개 품는 가장 바깥 조상) 안에서
# 상품 ID 로 보이는 긴 숫자를 href / img src / data-* 속성에서 찾는다.
JS_IDS = """(btnText) => {
  const btns = Array.from(document.querySelectorAll('button, [role=button]'))
    .filter(el => (el.innerText || '').trim() === btnText);
  const cardOf = (btn) => {
    let node = btn, card = null;
    for (let up = 1; up <= 10; up++) {
      node = node.parentElement;
      if (!node) break;
      const n = ((node.innerText || '').match(/링크 발급/g) || []).length;
      if (n !== 1) break;
      card = node;
    }
    return card;
  };
  return btns.map((btn, i) => {
    const card = cardOf(btn);
    if (!card) return { i, ids: [], srcs: [], hrefs: [] };
    const srcs = Array.from(card.querySelectorAll('img'))
      .map(e => e.getAttribute('src') || '').filter(Boolean);
    const hrefs = Array.from(card.querySelectorAll('a'))
      .map(e => e.getAttribute('href') || '').filter(Boolean);
    const attrs = [];
    card.querySelectorAll('*').forEach(el => {
      for (const a of el.attributes) {
        if (a.name.startsWith('data-') || a.name === 'id')
          attrs.push(a.name + '=' + a.value);
      }
    });
    const hay = srcs.join(' ') + ' ' + hrefs.join(' ') + ' ' + attrs.join(' ');
    const ids = Array.from(new Set((hay.match(/\\d{6,12}/g) || [])));
    return { i, ids: ids.slice(0, 6), srcs: srcs.slice(0, 3), hrefs: hrefs.slice(0, 3) };
  });
}"""


def main():
    ap = argparse.ArgumentParser(description="토스 쉐어링크 상품 카드 관측 (읽기 전용)")
    ap.add_argument("--url", default=T.PRODUCTS_URL)
    ap.add_argument("--out", default="toss_cards_observe.txt")
    ap.add_argument("--full", action="store_true", help="카드 원문 전체를 덤프")
    args = ap.parse_args()

    w = Tee(os.path.join(ROOT, args.out))
    w("※ 계정 정보가 섞일 수 있습니다. 공유 전에 훑어보세요.")
    w(f"  {datetime.now():%Y-%m-%d %H:%M:%S}")
    w(f"  대상: {args.url}")

    try:
        with sync_playwright() as pw:
            ctx = T.open_context(pw)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(args.url, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)

            w("")
            w(f"URL   : {page.url}")
            if T.needs_login(page.url):
                w("⚠️ 로그인 화면입니다. `py src/toss_link.py --login` 을 먼저 실행하세요.")
                ctx.close()
                return

            cards = page.evaluate(T.JS_SCAN_CARDS, T.BTN_ISSUE)
            marks = page.evaluate(JS_MARK, T.BTN_ISSUE)

            w(f"JS_SCAN_CARDS 원본 {len(cards)}개 / 버튼 {len(marks)}개")
            visible = [c for c in cards if c.get("visible") and c.get("title")]
            w(f"visible+title 필터 후 {len(visible)}개  ← scan_cards() 가 돌려주는 것")

            # ── 1. 인덱스 정합성 ────────────────────────────────────
            w("")
            w("=" * 70)
            w("  1. 인덱스 정합성 — nth(idx) 가 정말 그 카드의 버튼인가")
            w("=" * 70)
            loc = page.get_by_role("button", name=T.BTN_ISSUE, exact=True)
            n_pw = loc.count()
            w(f"Playwright get_by_role 개수 : {n_pw}")
            w(f"JS querySelectorAll 개수    : {len(marks)}")
            if n_pw != len(marks):
                w("🔴 개수부터 다르다. nth(idx) 로는 대상을 특정할 수 없다.")

            mismatch = []
            checked = 0
            for i in range(min(n_pw, len(marks))):
                try:
                    got = loc.nth(i).get_attribute("data-pm-idx")
                except Exception as e:
                    got = f"<오류 {e}>"
                checked += 1
                if got != str(i):
                    mismatch.append((i, got))
            w(f"대조한 버튼 {checked}개 / 어긋난 것 {len(mismatch)}개")
            if mismatch:
                w("🔴 인덱스가 어긋난다. 엉뚱한 상품의 링크가 발급된다.")
                for i, got in mismatch[:20]:
                    w(f"    nth({i}) → data-pm-idx={got}")
            else:
                w("✅ 전부 일치. nth(idx) 로 특정해도 안전하다.")

            # ── 2. salePct 가 None 인 원인 ──────────────────────────
            w("")
            w("=" * 70)
            w("  2. '% 특가' 배지가 카드 경계 안에 있는가")
            w("=" * 70)
            in_card = sum(1 for m in marks if m["cardHasSale"])
            in_parent = sum(1 for m in marks if m["parentHasSale"])
            w(f"카드 안에 '% 특가' 가 있는 것        : {in_card}/{len(marks)}")
            w(f"한 단계 위 조상에 '% 특가' 가 있는 것 : {in_parent}/{len(marks)}")
            got_pct = sum(1 for c in cards if c.get("salePct") is not None)
            w(f"실제로 salePct 가 잡힌 카드          : {got_pct}/{len(cards)}")

            # ── 3. 개당 수익 vs 가격 ────────────────────────────────
            w("")
            w("=" * 70)
            w("  3. '개당 N원 수익' 은 가격의 몇 %인가 (정렬 기준으로 쓸 값인가)")
            w("=" * 70)
            ratios = []
            for c in visible:
                p, r = c.get("price"), c.get("reward")
                if p and r:
                    ratios.append(r / p)
            if ratios:
                ratios.sort()
                w(f"표본 {len(ratios)}건")
                w(f"  최소 {ratios[0]*100:.2f}%  중앙 {ratios[len(ratios)//2]*100:.2f}%  "
                  f"최대 {ratios[-1]*100:.2f}%")
                near10 = sum(1 for x in ratios if 0.095 <= x <= 0.105)
                w(f"  10% 근처(9.5~10.5%) : {near10}/{len(ratios)}")
                if near10 == len(ratios):
                    w("  → 수수료는 가격의 정확히 10% 다.")
                    w("    따라서 reward 로 정렬하는 것은 **가격으로 정렬하는 것과 같다.**")

            # ── 4. 가격대 분포 ──────────────────────────────────────
            w("")
            w("=" * 70)
            w("  4. 가격대 분포와 현재 pick() 이 고르는 것")
            w("=" * 70)
            prices = sorted(c["price"] for c in visible if c.get("price"))
            if prices:
                w(f"가격 있는 카드 {len(prices)}건 "
                  f"(전체 {len(visible)}건 중 가격 미확보 {len(visible)-len(prices)}건)")
                w(f"  최저 {prices[0]:,}원 / 중앙 {prices[len(prices)//2]:,}원 / "
                  f"최고 {prices[-1]:,}원")
                for lo, hi in ((0, 10000), (10000, 30000), (30000, 100000),
                               (100000, 500000), (500000, 10**9)):
                    n = sum(1 for p in prices if lo <= p < hi)
                    w(f"  {lo:>7,} ~ {hi:>9,}원 : {n:3d}건")

            w("")
            w("현재 pick() 이 고르는 상위 8건 (30일최저 우선 → 개당수익 큰 순):")
            for c in T.pick(visible, None, 8):
                w(f"  {c.get('price'):>9,}원  개당{c.get('reward'):>8,}원  "
                  f"{'[30일최저]' if c.get('lowest30') else '          '}  "
                  f"{c['title'][:40]}")

            w("")
            w("참고 — 가격이 낮은 순 상위 8건:")
            for c in sorted([c for c in visible if c.get("price")],
                            key=lambda c: c["price"])[:8]:
                w(f"  {c.get('price'):>9,}원  개당{c.get('reward'):>8,}원  "
                  f"{'[30일최저]' if c.get('lowest30') else '          '}  "
                  f"{c['title'][:40]}")

            # ── 5. 카드 원문 ────────────────────────────────────────
            w("")
            w("=" * 70)
            w("  5. 카드 원문 (파싱 근거)")
            w("=" * 70)
            for m in marks[: (len(marks) if args.full else 3)]:
                w("")
                w(f"── 버튼 #{m['i']} ({m['tag']}) cardHasSale={m['cardHasSale']} "
                  f"parentHasSale={m['parentHasSale']} " + "─" * 15)
                for line in m["cardText"].split("\n"):
                    w(f"    {line}")

            # ── 6. 조상 단계별 프로파일 ─────────────────────────────
            # '% 특가' 를 어느 조상에서 읽어야 하는지 정한다.
            # 조상이 카드 여러 개를 감싸면 남의 특가율을 읽게 되므로
            # "'% 특가' 가 정확히 1개인 가장 가까운 조상" 을 찾아야 한다.
            w("")
            w("=" * 70)
            w("  6. 조상 단계별 '% 특가' / '링크 발급' 개수")
            w("=" * 70)
            climb = page.evaluate(JS_CLIMB, T.BTN_ISSUE)

            w("버튼에서 위로 올라가며 각 단계의 매칭 개수 (앞 3개 버튼)")
            for c in climb[:3]:
                w("")
                w(f"── 버튼 #{c['i']} " + "─" * 50)
                for lv in c["levels"]:
                    w(f"    up{lv['up']:>2}  <{lv['tag']}>  len={lv['len']:>5}  "
                      f"특가={lv['sale']}  발급버튼={lv['issue']}")

            w("")
            w("전체 118개 버튼에 대해, '특가=1 이고 발급버튼=1' 인 가장 가까운 단계:")
            hist = {}
            bad = 0
            for c in climb:
                lv = next((l for l in c["levels"]
                           if l["sale"] == 1 and l["issue"] == 1), None)
                if lv is None:
                    bad += 1
                else:
                    hist[lv["up"]] = hist.get(lv["up"], 0) + 1
            for up in sorted(hist):
                w(f"    up={up} : {hist[up]}개")
            if bad:
                w(f"    🔴 그런 단계가 없는 버튼 {bad}개")
            else:
                w("    ✅ 모든 버튼에 '특가 1개 + 발급버튼 1개' 인 조상이 존재한다.")

            # ── 7. 카드에서 상품 ID 를 미리 읽을 수 있는가 ──────────
            # 읽을 수 있으면 발급 전에 중복을 걸러낼 수 있다.
            # 지금은 발급한 뒤에야 ID 를 알 수 있어서, 이미 가진 상품에
            # 한 번에 4건뿐인 발급 한도를 낭비한다.
            w("")
            w("=" * 70)
            w("  7. 카드 안에 상품 ID 단서가 있는가 (발급 전 중복 제거용)")
            w("=" * 70)
            ids = page.evaluate(JS_IDS, T.BTN_ISSUE)
            with_id = [x for x in ids if x["ids"]]
            w(f"상품ID 후보를 찾은 카드 : {len(with_id)}/{len(ids)}")
            w("")
            w("앞 5개 카드의 단서:")
            for x in ids[:5]:
                w(f"  #{x['i']} ids={x['ids']}")
                for s in x["srcs"][:3]:
                    w(f"      src : {s}")
                for h in x["hrefs"][:3]:
                    w(f"      href: {h}")

            ctx.close()

        w("")
        w(f"저장: {os.path.join(ROOT, args.out)}")
    finally:
        w.close()


if __name__ == "__main__":
    main()
