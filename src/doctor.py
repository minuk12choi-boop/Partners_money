#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
doctor.py — 돌기 전에 무엇이 빠졌는지 한 번에 알려준다

**왜 있는가**

"봇이 안 돌아간다" 는 신고가 왔을 때, 원인이 될 수 있는 곳이 여덟 군데쯤
있다. `.env` 가 없거나, 토큰이 틀렸거나, /start 한 사람이 없거나,
playwright 브라우저를 안 깔았거나, 로그인 세션이 없거나, 카톡이 꺼져
있거나… 각각을 따로 확인하려면 명령을 여러 번 쳐야 하고, 대부분은
**조용히 실패해서** 어디가 문제인지 안 보인다.

여기서 한 번에 다 본다.

    py src/doctor.py

출력은 세 가지다.

    [OK]   된다
    [경고]  없어도 돌아가지만 반쪽이다
    [실패]  이것 때문에 안 돈다

`[실패]` 가 하나라도 있으면 종료코드가 1 이다. `main.py` 가 시작 전에
이걸 부르고, 실패가 있으면 무엇을 해야 하는지 찍고 멈춘다.
"""

import os
import sqlite3
import sys

from env import load_env

load_env()

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(HERE, "deals.db")

IS_WINDOWS = sys.platform == "win32"

OK, WARN, FAIL = "OK", "경고", "실패"


class Report:
    def __init__(self):
        self.rows = []

    def add(self, level, what, detail="", fix=""):
        self.rows.append((level, what, detail, fix))
        mark = {OK: "[OK]  ", WARN: "[경고]", FAIL: "[실패]"}[level]
        print(f"  {mark} {what}" + (f" — {detail}" if detail else ""),
              flush=True)
        if fix and level != OK:
            for line in fix.splitlines():
                print(f"         → {line}", flush=True)

    @property
    def failed(self):
        return [r for r in self.rows if r[0] == FAIL]

    @property
    def warned(self):
        return [r for r in self.rows if r[0] == WARN]


# ---------------------------------------------------------------- 검사

def check_env(r):
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        r.add(FAIL, ".env 파일", "저장소 루트에 없습니다",
              "copy .env.example .env\n"
              "그런 다음 TG_BOT_TOKEN / TG_CHAT_ID / KAKAO_ROOM 을 채우세요.")
        return
    r.add(OK, ".env 파일", path)


def check_packages(r):
    for mod, why, fix in (
        ("requests", "텔레그램 전송", "py -m pip install -r requirements.txt"),
        ("playwright", "쿠팡·토스 링크 생성",
         "py -m pip install -r requirements.txt\npy -m playwright install chromium"),
    ):
        try:
            __import__(mod)
        except ImportError:
            r.add(FAIL, f"{mod} 패키지", f"없음 ({why} 불가)", fix)
        else:
            r.add(OK, f"{mod} 패키지")

    if IS_WINDOWS:
        try:
            __import__("pywinauto")
        except ImportError:
            r.add(FAIL, "pywinauto 패키지", "없음 (카톡 내보내기 불가)",
                  "py -m pip install -r requirements.txt")
        else:
            r.add(OK, "pywinauto 패키지")
    else:
        r.add(WARN, "운영체제", f"{sys.platform} — 카톡 내보내기는 Windows 전용",
              "이 파이프라인은 PC 카카오톡이 있는 Windows 에서 돌립니다.")


# 크로미움이 깔렸는지 보려면 playwright 드라이버를 띄워야 하는데, 이 과정이
# 끝날 때 관계없는 경고를 화면에 뱉는다("Task was destroyed but it is
# pending!"). 점검 결과 사이에 섞이면 사람이 그걸 오류로 읽는다.
# 별도 프로세스에서 돌려 출력을 통째로 가둔다.
_BROWSER_PROBE = (
    "from playwright.sync_api import sync_playwright\n"
    "import os\n"
    "with sync_playwright() as pw:\n"
    "    p = pw.chromium.executable_path\n"
    "print('YES' if p and os.path.exists(p) else 'NO')\n")


def check_browser(r):
    try:
        import playwright  # noqa: F401
    except ImportError:
        return      # 위에서 이미 실패로 잡혔다

    import subprocess
    try:
        out = subprocess.run([sys.executable, "-c", _BROWSER_PROBE],
                             capture_output=True, text=True, timeout=60)
    except Exception as e:
        r.add(WARN, "playwright 크로미움", f"확인 실패: {str(e)[:60]}")
        return

    if "YES" in (out.stdout or ""):
        r.add(OK, "playwright 크로미움")
    else:
        why = (out.stderr or "").strip().splitlines()
        r.add(FAIL, "playwright 크로미움",
              why[-1][:80] if why else "설치되지 않았습니다",
              "py -m playwright install chromium")


def check_telegram(r):
    token = os.environ.get("TG_BOT_TOKEN")
    if not token:
        r.add(FAIL, "TG_BOT_TOKEN", "없습니다",
              "텔레그램에서 @BotFather 에게 /newbot 을 보내 봇을 만들고\n"
              "받은 토큰을 `.env` 의 TG_BOT_TOKEN 에 적으세요.")
        return None
    try:
        import requests
        j = requests.get(
            f"https://api.telegram.org/bot{token}/getMe", timeout=15).json()
    except Exception as e:
        r.add(WARN, "텔레그램 연결", str(e)[:80],
              "인터넷 연결을 확인하세요. 토큰 자체는 있습니다.")
        return None
    if not j.get("ok"):
        r.add(FAIL, "TG_BOT_TOKEN", j.get("description", "토큰이 거부되었습니다"),
              "@BotFather 에서 토큰을 다시 확인하세요.")
        return None
    name = (j.get("result") or {}).get("username")
    r.add(OK, "텔레그램 봇", f"@{name}")
    return name


def check_recipients(r, bot_name):
    import subscribers
    try:
        conn = sqlite3.connect(DB_PATH)
        a, t = subscribers.count(conn)
        conn.close()
    except Exception as e:
        r.add(FAIL, "구독자 목록", str(e)[:80])
        return

    admins = subscribers.admin_ids()
    if not admins:
        r.add(WARN, "TG_CHAT_ID", "없습니다 — 링크 변환을 시킬 사람이 없습니다",
              "py src/telegram_deliver.py --whoami 로 확인해 `.env` 에 넣으세요.\n"
              "(먼저 텔레그램에서 봇에게 아무 말이나 한 번 걸어야 합니다)")
    else:
        r.add(OK, "관리자", f"{len(admins)}명")

    if a or admins:
        r.add(OK, "받는 사람", f"{a}명 구독 (등록 {t}명)")
    else:
        r.add(FAIL, "받는 사람", "아무도 없습니다",
              f"텔레그램에서 @{bot_name or '봇'} 을 열고 '시작'(/start) 을 "
              "눌러 주세요.")


def check_room(r):
    room = os.environ.get("KAKAO_ROOM")
    if not room:
        r.add(FAIL, "KAKAO_ROOM", "없습니다",
              "`.env` 에 채팅방 이름 일부를 적으세요. 이모지는 빼고요.\n"
              "  KAKAO_ROOM=쿠팡 실시간 핫딜방")
        return
    r.add(OK, "KAKAO_ROOM", room)


def check_sessions(r):
    """로그인 세션(브라우저 프로필)이 있는지.

    프로필 디렉터리가 있다고 로그인이 살아 있다는 뜻은 아니다. 실제
    확인은 브라우저를 띄워야 하는데, 그건 시작할 때마다 하기엔 무겁다.
    여기서는 '한 번이라도 로그인한 적이 있는가' 만 본다.
    """
    for name, path, fix in (
        ("쿠팡 파트너스 세션", os.path.join(HERE, "pw_profile"),
         "py src/partners_link.py --login\n"
         "※ 로그인할 때 '자동 로그인' 을 반드시 켜세요(실측). 안 켜면 "
         "세션이 몇 시간 만에 끊깁니다."),
        ("토스 쉐어링크 세션", os.path.join(ROOT, "pw_toss_profile"),
         "py src/toss_link.py --login"),
    ):
        if os.path.isdir(path) and os.listdir(path):
            r.add(OK, name)
        else:
            r.add(WARN, name, "로그인한 적이 없습니다", fix)


def check_locks(r):
    """죽은 프로세스가 남긴 잠금 파일.

    lock.py 가 15분 지나면 알아서 뺏지만, 남아 있으면 그 사이 작업이
    막힌다. 왜 멈춰 보이는지 알 수 있게 보여준다.
    """
    import time
    found = []
    for d in (HERE, ROOT):
        for f in os.listdir(d):
            if f.endswith(".lock"):
                p = os.path.join(d, f)
                age = time.time() - os.path.getmtime(p)
                found.append((f, int(age)))
    if not found:
        r.add(OK, "브라우저 잠금", "없음")
        return
    for f, age in found:
        if age > 900:
            r.add(WARN, "브라우저 잠금", f"{f} ({age//60}분 전 — 버려진 것)",
                  "지워도 됩니다. 15분이 지나면 자동으로 무시됩니다.")
        else:
            r.add(OK, "브라우저 잠금", f"{f} 사용 중 ({age}초)")


def check_kakao(r):
    """PC 카카오톡이 켜져 있는지. Windows 에서만."""
    if not IS_WINDOWS:
        return
    try:
        from pywinauto import Desktop
    except ImportError:
        return
    room = os.environ.get("KAKAO_ROOM") or ""
    try:
        wins = Desktop(backend="win32").windows()
    except Exception as e:
        r.add(WARN, "카카오톡 창", f"확인 실패: {str(e)[:60]}")
        return

    titles = []
    for w in wins:
        try:
            if w.class_name() == "EVA_Window_Dblclk":
                titles.append(w.window_text())
        except Exception:
            continue

    if not titles:
        r.add(FAIL, "카카오톡", "실행 중이 아닙니다",
              "PC 카카오톡을 켜고 로그인해 두세요. 창을 닫으면 안 됩니다.")
        return
    r.add(OK, "카카오톡", f"창 {len(titles)}개")

    if room and not any(room in t for t in titles):
        r.add(WARN, "채팅방 창", f"'{room}' 창이 열려 있지 않습니다",
              "그 채팅방을 더블클릭해 별도 창으로 열어 두세요.\n"
              f"현재 열린 창: {', '.join(t for t in titles if t)[:100]}")
    elif room:
        r.add(OK, "채팅방 창", room)


def check_db(r):
    from schema import ensure_all
    try:
        conn = sqlite3.connect(DB_PATH)
        ensure_all(conn, verbose=False)
        n = conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
        linked = conn.execute(
            "SELECT COUNT(*) FROM deals WHERE affiliate_url IS NOT NULL "
            "AND affiliate_url != ''").fetchone()[0]
        conn.close()
    except Exception as e:
        r.add(FAIL, "deals.db", str(e)[:80])
        return
    r.add(OK, "deals.db", f"{n}건 (내 링크 생성됨 {linked}건)")


# ---------------------------------------------------------------- 메인

def run(quiet=False):
    """(실패 수, 경고 수) 를 돌려준다."""
    if not quiet:
        print("─" * 60)
        print("점검을 시작합니다.")
        print("─" * 60)
    r = Report()
    check_env(r)
    check_packages(r)
    check_browser(r)
    bot = check_telegram(r)
    check_recipients(r, bot)
    check_room(r)
    check_sessions(r)
    check_kakao(r)
    check_locks(r)
    check_db(r)

    print("─" * 60)
    if r.failed:
        print(f"🔴 {len(r.failed)}가지가 막고 있습니다. 위의 → 를 먼저 해결하세요.")
    elif r.warned:
        print(f"🟡 돌아갑니다. 다만 {len(r.warned)}가지는 반쪽입니다.")
    else:
        print("🟢 전부 정상입니다.")
    print("─" * 60)
    return len(r.failed), len(r.warned)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    failed, _ = run()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
