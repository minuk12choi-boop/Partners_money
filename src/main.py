#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py — 명령 하나로 전부 돌린다

    py src\\main.py

이것 하나가 두 개를 띄우고 계속 살려 둔다.

    run_all.py       20분마다 카톡방 수집 → 링크 생성 → 구독자에게 전달
    telegram_bot.py  봇. /start 받고, 보내 주신 딜방 글을 내 링크로 바꿔 회신

**왜 하나로 묶었나**

전에는 사람이 창을 두 개 띄워야 했다. 하나만 띄우면 나머지 절반이 조용히
안 도는데, 그게 밖에서는 "봇이 안 돌아간다" 로 보인다. 어느 쪽이 안 도는지
구분할 방법도 없었다.

**죽으면 다시 띄운다.** 무인 운영에서 한쪽이 조용히 죽어 있는 것이 제일
나쁘다. 죽은 이유는 로그에 남기고, 연달아 죽으면 알린다.

    src/run.log   주기 작업 (run_all.py 가 직접 남긴다)
    src/bot.log   봇
"""

import os
import subprocess
import sys
import time
from datetime import datetime

import doctor
from env import load_env

load_env()

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
BOT_LOG = os.path.join(HERE, "bot.log")
MAIN_LOG = os.path.join(HERE, "main.log")

# 자식이 죽었을 때 다시 띄우기까지. 계속 즉사하는 걸 초당 수십 번
# 반복하면 로그만 터진다.
RESTART_DELAY = 10
# 이만큼 연달아 죽으면 알린다. 설정이 틀려서 영원히 못 뜨는 경우다.
CRASH_ALERT = 3
# 이 시간 이상 살아 있었으면 '잘 돌다가 죽은 것' 으로 보고 연속 카운트를
# 초기화한다.
HEALTHY_SECONDS = 120


def log(msg, level="INFO"):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {level:5s} {msg}"
    print(line, flush=True)
    try:
        with open(MAIN_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify(text):
    """실패 알림. run_all.py 와 같은 경로다."""
    token = os.environ.get("TG_BOT_TOKEN")
    chat = os.environ.get("TG_CHAT_ID")
    if not (token and chat):
        return
    try:
        import requests
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": f"[쿠팡봇] {text}"},
                      timeout=15)
    except Exception as e:
        log(f"알림 전송 실패: {e}", "WARN")


class Child:
    """자식 프로세스 하나. 죽으면 다시 띄운다."""

    def __init__(self, name, args, logfile=None):
        self.name = name
        self.args = args
        self.logfile = logfile
        self.proc = None
        self.started_at = 0.0
        self.crashes = 0
        self.retry_at = 0.0
        self._out = None

    def spawn(self):
        out = None
        if self.logfile:
            out = open(self.logfile, "a", encoding="utf-8", errors="replace")
            out.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 시작 =====\n")
            out.flush()
        # 자식이 UTF-8 로 찍게 한다. Windows 콘솔 기본 코드페이지(cp949)에서
        # 한글 로그가 깨지거나 UnicodeEncodeError 로 죽는 것을 막는다.
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            [PY] + self.args, cwd=HERE, env=env,
            stdout=out or None, stderr=subprocess.STDOUT)
        self._out = out
        self.started_at = time.time()
        log(f"{self.name} 시작 (pid {self.proc.pid})"
            + (f" → {os.path.basename(self.logfile)}" if self.logfile else ""))

    def poll(self):
        """살아 있으면 True. 죽었으면 다시 띄울 준비를 하고 False."""
        if self.proc is None:
            if time.time() >= self.retry_at:
                self.spawn()
            return True

        code = self.proc.poll()
        if code is None:
            return True

        lived = time.time() - self.started_at
        if self._out:
            try:
                self._out.close()
            except Exception:
                pass
            self._out = None

        if lived >= HEALTHY_SECONDS:
            self.crashes = 0
        self.crashes += 1
        log(f"{self.name} 종료 (코드 {code}, {int(lived)}초 살아 있었음, "
            f"연속 {self.crashes}회)", "ERROR")
        if self.logfile:
            log(f"  이유는 {os.path.basename(self.logfile)} 마지막 줄을 보세요",
                "ERROR")
        if self.crashes == CRASH_ALERT:
            notify(f"{self.name} 이 {self.crashes}회 연속으로 죽었습니다. "
                   f"{os.path.basename(self.logfile or 'run.log')} 를 확인하세요.")

        self.proc = None
        self.retry_at = time.time() + RESTART_DELAY
        return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            log(f"{self.name} 종료 중...")
            try:
                self.proc.terminate()
                self.proc.wait(timeout=15)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    import argparse
    ap = argparse.ArgumentParser(
        description="주기 수집과 텔레그램 봇을 한꺼번에 돌린다")
    ap.add_argument("--skip-check", action="store_true",
                    help="시작 전 점검을 건너뛴다")
    ap.add_argument("--no-bot", action="store_true", help="주기 작업만")
    ap.add_argument("--no-cycle", action="store_true", help="봇만")
    ap.add_argument("--cycle", type=int, help="주기(분). 기본은 20")
    args = ap.parse_args()

    if not args.skip_check:
        failed, _ = doctor.run()
        if failed:
            print()
            print("위 [실패] 를 해결한 뒤 다시 실행하세요.")
            print("점검만 다시 하려면: py src\\doctor.py")
            sys.exit(1)
        print()

    children = []
    if not args.no_cycle:
        cycle_args = ["run_all.py"]
        if args.cycle:
            cycle_args += ["--cycle", str(args.cycle)]
        # run_all.py 는 자기 로그를 run.log 에 직접 남긴다. 화면에도 보이게
        # 파일로 돌리지 않는다 — 사람이 지켜볼 때 이쪽이 본 줄기다.
        children.append(Child("주기 작업", cycle_args))
    if not args.no_bot:
        children.append(Child("텔레그램 봇", ["telegram_bot.py"], BOT_LOG))

    if not children:
        raise SystemExit("--no-bot 과 --no-cycle 을 같이 주면 할 일이 없습니다.")

    log("=" * 55)
    log(f"시작합니다. Ctrl+C 로 전부 종료됩니다.")
    for c in children:
        c.spawn()

    try:
        while True:
            for c in children:
                c.poll()
            time.sleep(3)
    except KeyboardInterrupt:
        log("사용자 중단")
    finally:
        for c in children:
            c.stop()
        log("종료했습니다.")


if __name__ == "__main__":
    main()
