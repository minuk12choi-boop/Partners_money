#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
observe_coupang.py — 쿠팡 파트너스 링크 생성 화면 관측

`partners_link.py` 의 자가탐색은 조합마다 페이지를 새로 열고 폼을 제출한다.
즉 **시도 횟수가 곧 파트너스 사이트 접근 횟수**다. 아무것도 모르는 상태로
탐색을 돌리면 계정을 걸고 도박하는 셈이다. 그래서 먼저 본다.

토스에서 배운 것이 여기에도 적용될 가능성이 높다. 토스는 발급된 링크를
DOM 에 전혀 넣지 않아서 화면 긁기가 원리적으로 실패했고, 발급 API 응답을
가로채니 한 번에 풀렸다. 쿠팡도 같은 구조일 수 있다.

두 가지 모드가 있다.

  기본 (읽기 전용)
      py tools/observe_coupang.py
    화면만 덤프한다. 클릭하지 않고 링크를 만들지 않는다.

  계측 (링크를 1건 실제로 만든다)
      py tools/observe_coupang.py --issue "https://www.coupang.com/vp/products/..."
    입력창에 상품 URL 을 넣고 버튼을 눌러, 그때 오가는 네트워크와
    화면 변화를 전부 기록한다. 어디서 링크가 나오는지 확정하기 위함이다.

    ※ 실제로 딥링크가 1건 만들어진다. 한 번만 누른다.
    ※ 실패해도 다시 돌리지 마라. 계정이 위험하다.

최초 1회 로그인이 필요하다:
    py src/partners_link.py --login
"""

import argparse
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

import partners_link as P

SHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(P.__file__)), "shots")


class Tee:
    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")

    def __call__(self, msg=""):
        print(msg)
        self.f.write(str(msg) + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


# observe_toss.py 의 JS_SCAN 과 같은 방식이다.
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
  document.querySelectorAll('button, a, [role=button], [role=tab]').forEach(el => {
    if (!visible(el)) return;
    const t = (el.innerText || el.value || '').trim().replace(/\\s+/g, ' ');
    if (!t || t.length > 40) return;
    const r = el.getBoundingClientRect();
    buttons.push({ text: t, tag: el.tagName.toLowerCase(),
                   href: el.getAttribute('href') || '',
                   path: cssPath(el), top: Math.round(r.top) });
  });

  // iframe 안에 링크 생성 UI 가 들어 있는 경우가 흔하다.
  // 그러면 최상위 document 에는 입력창이 0개로 보인다. 반드시 확인한다.
  const frames = Array.from(document.querySelectorAll('iframe')).map(f => ({
    src: f.getAttribute('src') || '', id: f.id || '', name: f.name || '',
  }));

  return {
    url: location.href, title: document.title,
    inputs, buttons, frames,
    bodyText: (document.body ? document.body.innerText : '').slice(0, 3000),
  };
}"""


def needs_login(url):
    u = (url or "").lower()
    return "login" in u or "member.coupang.com" in u


def dump(w, page, label):
    w("")
    w("=" * 78)
    w(f"  {label}")
    w("=" * 78)
    try:
        d = page.evaluate(JS_SCAN)
    except Exception as e:
        w(f"  스캔 실패: {e}")
        return None

    w(f"URL   : {d['url']}")
    w(f"TITLE : {d['title']}")
    if needs_login(d["url"]):
        w("")
        w("⚠️ 로그인 화면입니다. `py src/partners_link.py --login` 을 먼저 실행하세요.")

    w("")
    w(f"── 입력창 {len(d['inputs'])}개 " + "─" * 40)
    if not d["inputs"]:
        w("  (없음)")
    for i in d["inputs"]:
        w(f"  placeholder={i['placeholder']!r} type={i['type']} "
          f"name={i['name']!r} id={i['id']!r} aria={i['aria']!r} w={i['w']}")
        w(f"    path: {i['path']}")

    w("")
    w(f"── iframe {len(d['frames'])}개 " + "─" * 42)
    if not d["frames"]:
        w("  (없음)")
    for f in d["frames"]:
        w(f"  id={f['id']!r} name={f['name']!r} src={f['src']}")

    w("")
    w(f"── 버튼·링크 {len(d['buttons'])}개 " + "─" * 38)
    for b in sorted(d["buttons"], key=lambda x: x["top"]):
        href = f"  href={b['href']}" if b["href"] else ""
        w(f"  [{b['tag']}] {b['text']!r}{href}")
        w(f"      path: {b['path']}")

    w("")
    w("── 화면 텍스트 (앞 3000자) " + "─" * 36)
    w(d["bodyText"])
    return d


def dump_frames(w, page):
    """Playwright 가 보는 프레임 목록. iframe 안에 UI 가 있으면 여기서 드러난다."""
    w("")
    w("=" * 78)
    w("  프레임 (Playwright 기준)")
    w("=" * 78)
    for fr in page.frames:
        w(f"  name={fr.name!r} url={fr.url}")
        if fr is page.main_frame:
            continue
        try:
            n_in = fr.locator("input, textarea").count()
            n_btn = fr.locator("button, [role=button]").count()
            w(f"      입력창 {n_in}개 / 버튼 {n_btn}개")
        except Exception as e:
            w(f"      조사 실패: {e}")


def main():
    ap = argparse.ArgumentParser(description="쿠팡 파트너스 링크 생성 화면 관측")
    ap.add_argument("--issue", metavar="상품URL",
                    help="이 URL 로 링크를 1건 실제로 만들며 네트워크를 계측한다")
    ap.add_argument("--input-sel", help="--issue 에서 쓸 입력창 셀렉터 (생략하면 자동 선택)")
    ap.add_argument("--button", help="--issue 에서 누를 버튼 텍스트 (생략하면 자동 선택)")
    ap.add_argument("--watch", type=float, default=25.0, help="클릭 후 관찰 시간(초)")
    ap.add_argument("--out", default="coupang_observe.txt")
    args = ap.parse_args()

    w = Tee(os.path.join(ROOT, args.out))
    w("※ 이 출력에는 계정 정보가 포함될 수 있습니다. 공유 전에 훑어보세요.")
    w(f"  {datetime.now():%Y-%m-%d %H:%M:%S}")

    calls = []

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
                rec["body"] = resp.text()[:4000]
            except Exception as e:
                rec["body"] = f"<본문 읽기 실패: {e}>"
            calls.append(rec)
        except Exception:
            pass

    try:
        with sync_playwright() as pw:
            ctx = P.open_context(pw)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            P.goto_link_page(page)

            d = dump(w, page, "파트너스 링크 생성 화면")
            dump_frames(w, page)

            os.makedirs(SHOT_DIR, exist_ok=True)
            shot = os.path.join(SHOT_DIR, "coupang_link_page.png")
            try:
                page.screenshot(path=shot)
                w("")
                w(f"스크린샷: {shot}")
            except Exception as e:
                w(f"스크린샷 실패: {e}")

            if not args.issue:
                w("")
                w("읽기 전용 모드로 끝났습니다. 링크를 만들지 않았습니다.")
                w("실제 생성 흐름까지 보려면:")
                w('  py tools/observe_coupang.py --issue "https://www.coupang.com/vp/products/..."')
                ctx.close()
                return

            if d is None or needs_login(d["url"]):
                w("")
                w("로그인이 안 된 상태라 생성 계측을 건너뜁니다.")
                ctx.close()
                return

            # ── 계측 모드 ───────────────────────────────────────
            w("")
            w("=" * 78)
            w("  링크 생성 계측 — 실제로 1건 만듭니다")
            w("=" * 78)

            sel = args.input_sel
            if not sel:
                cands = page.evaluate(P.JS_SCAN_INPUTS)
                if not cands:
                    w("🔴 입력창 후보가 없습니다. 위 덤프의 iframe 항목을 보세요.")
                    ctx.close()
                    return
                sel = cands[0]["path"]
                w(f"입력창 자동 선택: {sel}  (placeholder={cands[0].get('ph')!r})")

            btn_text = args.button
            if btn_text is None:
                bcands = page.evaluate(P.JS_SCAN_BUTTONS)
                btn_text = bcands[0]["text"] if bcands else None
                w(f"버튼 자동 선택: {btn_text!r}")

            before_text = page.evaluate("() => document.body.innerText")
            page.on("response", on_response)

            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=8000)
            loc.click()
            loc.fill("")
            loc.type(args.issue, delay=20)
            page.wait_for_timeout(700)

            w("")
            w("── 클릭 " + "─" * 68)
            clicked = False
            if btn_text:
                for how in (
                    lambda: page.get_by_role("button", name=btn_text, exact=False)
                                .first.click(timeout=4000),
                    lambda: page.locator(f"text={btn_text}").first.click(timeout=3000),
                ):
                    try:
                        how()
                        clicked = True
                        break
                    except Exception:
                        continue
            if not clicked:
                loc.press("Enter")
                w("  버튼을 못 눌러 Enter 로 제출했습니다.")

            seen = set(before_text.split("\n"))
            dom_new = []
            t0 = time.time()
            deadline = t0 + args.watch
            while time.time() < deadline:
                try:
                    txt = page.evaluate("() => document.body.innerText")
                except Exception:
                    break
                for line in txt.split("\n"):
                    line = line.strip()
                    if line and line not in seen:
                        seen.add(line)
                        dom_new.append((round(time.time() - t0, 1), line))
                page.wait_for_timeout(400)

            w("")
            w("=" * 78)
            w("  1. 네트워크 (클릭 이후 XHR/fetch)")
            w("=" * 78)
            w(f"기록된 요청 {len(calls)}건")
            for c in calls:
                w("")
                w(f"  {c['method']} {c['status']}  {c['url']}")
                if c["post"]:
                    w(f"    요청 본문: {str(c['post'])[:600]}")
                if c["body"]:
                    w(f"    응답 본문: {c['body'][:1500]}")

            hits = [c for c in calls if c.get("body") and "link.coupang.com" in c["body"]]
            w("")
            if hits:
                w(f"✅ 응답 본문에 딥링크가 들어 있는 요청 {len(hits)}건")
                for c in hits:
                    w(f"    {c['url']}")
                w("  → 토스처럼 이 응답을 가로채는 방식이 가장 안정적입니다.")
            else:
                w("🔴 어떤 응답에도 link.coupang.com 이 없습니다.")

            w("")
            w("=" * 78)
            w("  2. 클릭 이후 새로 나타난 화면 텍스트")
            w("=" * 78)
            if not dom_new:
                w("  (없음)")
            for t, line in dom_new:
                w(f"  +{t:>5.1f}s  {line}")

            w("")
            w("=" * 78)
            w("  3. 화면에서 긁은 딥링크 (JS_HARVEST)")
            w("=" * 78)
            try:
                harvested = P.RE_SHORT.findall(page.evaluate(P.JS_HARVEST))
                w(f"  {len(harvested)}건: {harvested}")
            except Exception as e:
                w(f"  실패: {e}")

            shot2 = os.path.join(SHOT_DIR, "coupang_after_issue.png")
            try:
                page.screenshot(path=shot2)
                w("")
                w(f"스크린샷: {shot2}")
            except Exception as e:
                w(f"스크린샷 실패: {e}")

            ctx.close()

        w("")
        w(f"저장: {os.path.join(ROOT, args.out)}")
    finally:
        w.close()


if __name__ == "__main__":
    main()
