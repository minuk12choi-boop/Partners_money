#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_all.py

카톡 내보내기 → 딜 추출 → 파트너스 링크 생성 → 스레드 발행
전체를 무인으로 반복 실행한다.

    python run_all.py --room "핫딜" --once     # 1회만 (첫 테스트용)
    python run_all.py --room "핫딜"            # 무한 루프 (평상시)

고장나면 알림을 보낸다. 무인 운영에서 제일 중요한 부분이다.
텔레그램을 쓰려면 환경변수 두 개만 설정하면 된다.
    TG_BOT_TOKEN, TG_CHAT_ID
"""

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime

from env import load_env

# `.env` 를 환경변수로 올린다. 예전에는 이걸 하는 코드가 없어서
# `.env` 에 TG_BOT_TOKEN 을 적어도 notify() 가 조용히 아무것도 안 했다.
# 무인 운영에서 알림이 죽어 있으면 조용히 멈춘 것과 구분이 안 된다.
load_env()

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
LOG_PATH = os.path.join(HERE, "run.log")
STATUS_PATH = os.path.join(HERE, "status.json")
EXPORT_PATH = os.path.join(HERE, "export.txt")

# ── 주기 ──────────────────────────────────────────────────────────
# 20분(소유자 지시 2026-08-01).
#
# 이 방의 딜은 선착순이라 늦으면 소진된다. 그렇다고 5분으로 두면 한 바퀴가
# 끝나기도 전에 다음 바퀴가 오는 경우가 생긴다 — 카톡 내보내기만 30초,
# 링크 생성이 붙으면 한 바퀴가 몇 분씩 걸린다. 20분이 그 사이다.
#
# ⚠️ 주기를 줄여도 파트너스 접근이 늘지 않는다. 오히려 줄어든다.
# 링크 생성 쪽에 MAX_DEAL_AGE_HOURS 필터가 있어서 **신규 딜이 없는 주기에는
# 접근이 0회**다. 예전에는 필터가 없어 DB 에 쌓인 300건 넘는 옛 딜을
# 주기마다 4건씩 소화하며 하루 100번 넘게 접근했다. 그 링크는 아무도 안 썼다.
# CLAUDE.md 제약 2 는 그대로 지킨다 — MAX_LINKS_PER_CYCLE 과 사이 대기 6초는
# 손대지 않았다.
CYCLE_MINUTES = int(os.environ.get("CYCLE_MINUTES", "20"))
MAX_LINKS_PER_CYCLE = 4     # 파트너스 접근 횟수. 낮게 유지할 것.
CONSECUTIVE_FAIL_ALERT = 2  # 이만큼 연속 실패하면 알림

# 토스도 쿠팡과 **함께 매 주기(20분)** 돌린다. — 2026-08-01 소유자 지시
#
# 전에는 3주기(약 1시간)에 한 번이었다. 이유는 이랬다: 토스는 딜방이 아니라
# 대시보드 핫 목록에서 고르므로 20분마다 새로 볼 것이 없고, 매 주기 4건씩
# 발급하면 117개짜리 목록을 반나절에 다 긁는다.
#
# 소유자가 쿠팡과 같이 20분마다 받기를 원한다. 그 판단을 따른다.
# 대신 알아 둘 것이 둘 있다.
#
#   · 목록을 다 긁으면 발급이 0건이 된다. **이건 고장이 아니다.**
#     pick() 이 이미 가진 제목을 건너뛰기 때문이다. 새 상품이 올라오면
#     다시 나온다. 0건이 며칠 이어져도 코드를 고치지 말 것.
#   · CLAUDE.md 제약 2 는 그대로다. toss_link.py 의 MAX_LINKS_PER_RUN = 4
#     와 발급 사이 6초 대기는 손대지 않았다. 한 주기에 4건인 것은 그대로고,
#     주기가 잦아진 만큼만 늘어난다.
#
# 되돌리려면 `.env` 에 TOSS_EVERY_N_CYCLES=3 을 넣는다. 코드를 고칠 필요 없다.
TOSS_EVERY_N_CYCLES = int(os.environ.get("TOSS_EVERY_N_CYCLES", "1"))


def log(msg, level="INFO"):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {level:5s} {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def notify(text):
    """실패 알림. 설정 안 했으면 로그만 남는다."""
    token = os.environ.get("TG_BOT_TOKEN")
    chat = os.environ.get("TG_CHAT_ID")
    if not (token and chat):
        log("알림 채널 미설정 (TG_BOT_TOKEN/TG_CHAT_ID)", "WARN")
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": f"[쿠팡봇] {text}"},
            timeout=15,
        )
    except Exception as e:
        log(f"알림 전송 실패: {e}", "WARN")


def write_status(**kw):
    kw["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with open(STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump(kw, f, ensure_ascii=False, indent=2)


# 자식 프로세스가 UTF-8 로 말하게 한다.
#
# ⚠️ 이걸 안 하면 로그가 복구 불가능하게 깨진다(2026-08-02 실측).
# 파이썬은 stdout 이 파이프일 때 로케일 인코딩(한국어 윈도우면 cp949)으로
# 쓴다. 아래에서 UTF-8 로 읽으니 글자가 errors="replace" 로 뭉개지고,
# 그 뭉개진 문자가 그대로 run.log 에 저장된다. 원문은 그 시점에 사라진다.
# 화면에서는 멀쩡해 보이는데 로그만 깨져서 알아채기 어렵다.
CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8")


def run_step(name, cmd, timeout=900):
    """하위 스크립트 실행. (성공여부, 출력) 반환."""
    log(f"── {name}")
    try:
        r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace",
                           env=CHILD_ENV)
    except subprocess.TimeoutExpired:
        log(f"{name}: 타임아웃", "ERROR")
        return False, "timeout"

    out = (r.stdout or "") + (r.stderr or "")
    for line in out.strip().splitlines()[-15:]:
        log(f"   {line}")
    if r.returncode != 0:
        log(f"{name}: 종료코드 {r.returncode}", "ERROR")
        return False, out
    return True, out


def cycle(room, dry_run=False, cycle_no=0):
    """한 바퀴. 각 단계는 앞 단계가 실패해도 가능한 만큼 진행한다."""
    results = {}

    # 1) 카톡 내보내기
    ok, _ = run_step("카톡 내보내기",
                     [PY, "kakao_export.py", "--room", room, "--out", EXPORT_PATH],
                     timeout=180)
    results["export"] = ok
    if not ok:
        # 내보내기가 실패해도 기존 export.txt 가 있으면 계속 진행
        if not os.path.exists(EXPORT_PATH):
            return results, "export 실패 & 기존 파일 없음"

    # 2) 딜 추출
    ok, _ = run_step("딜 추출",
                     [PY, "kakao_deal_extract.py", EXPORT_PATH],
                     timeout=600)
    results["extract"] = ok

    # 3) 쿠팡 파트너스 링크 생성
    ok, out = run_step("쿠팡 파트너스 링크 생성",
                       [PY, "partners_link.py", "--limit", str(MAX_LINKS_PER_CYCLE)],
                       timeout=900)
    results["link"] = ok
    if "세션 만료" in out:
        notify("쿠팡 파트너스 세션이 만료되었습니다. partners_link.py --login 을 실행하세요.")
        results["link"] = False

    # 3-2) 토스 쉐어링크 발급
    # 쿠팡과 독립이다. 한쪽이 실패해도 다른 쪽은 계속 돈다.
    #
    # 기본값(TOSS_EVERY_N_CYCLES=1)에서는 쿠팡과 **매 주기 함께** 돈다.
    # 토스는 딜방이 아니라 대시보드 목록에서 고르므로 목록을 다 긁으면
    # 발급이 0건이 된다. 고장이 아니다 — 위 상수 설명 참고.
    if cycle_no % TOSS_EVERY_N_CYCLES == 0:
        ok, out = run_step("토스 쉐어링크 발급",
                           [PY, "toss_link.py", "--limit", str(MAX_LINKS_PER_CYCLE)],
                           timeout=900)
        results["toss_link"] = ok
        if "세션 만료" in out:
            notify("토스 쉐어링크 세션이 만료되었습니다. toss_link.py --login 을 실행하세요.")
            results["toss_link"] = False
    else:
        # 건너뛴 주기를 실패로 세면 안 된다. 핵심 실패 판정에 쓰인다.
        results["toss_link"] = True
        log(f"── 토스 쉐어링크 발급 (건너뜀: {TOSS_EVERY_N_CYCLES}주기마다 실행)")

    # 4) 텔레그램으로 문구 전달
    #
    # 예전에는 threads_post.py 로 스레드에 자동 발행했다. 소유자 결정
    # (2026-07-29)으로 **사람이 직접 올리는** 방식으로 바꿨다. 파이프라인은
    # '링크 + 완성된 본문' 을 텔레그램으로 보내는 데까지만 한다.
    # Meta 앱 심사 리스크가 사라지고, 무엇을 언제 올릴지는 사람이 정한다.
    #
    # threads_post.py 는 지우지 않았다. 나중에 자동 발행을 켤 때 쓴다.
    # 그쪽의 DAILY_CAP / MIN_GAP_MINUTES 도 그대로 두었다.
    cmd = [PY, "telegram_deliver.py"]
    if dry_run:
        cmd.append("--dry-run")
    ok, out = run_step("텔레그램 전달", cmd, timeout=600)
    results["deliver"] = ok
    if "설정이 없습니다" in out:
        # 여기서 알림을 보내봐야 같은 채널이라 도착하지 않는다. 로그로 남긴다.
        log("텔레그램 설정이 없습니다. TG_BOT_TOKEN 을 `.env` 에 넣으세요. "
            "`py src/telegram_deliver.py --whoami` 참고", "ERROR")
    if "받을 사람이 없습니다" in out:
        # 문구는 다 만들어 놨는데 갈 곳이 없는 상태다. 조용히 두면
        # '오늘은 딜이 없었나 보다' 로 오인한다.
        log("만들어 둔 문구를 받을 사람이 없습니다. 텔레그램에서 봇에게 "
            "/start 를 보내세요. `py src/telegram_deliver.py --subscribers` 로 "
            "확인할 수 있습니다.", "ERROR")

    return results, None


def main():
    ap = argparse.ArgumentParser()
    # 작업 스케줄러에 등록할 때 인자를 길게 쓰면 방 이름의 공백·한글 때문에
    # 따옴표가 꼬인다. `.env` 의 KAKAO_ROOM 을 기본값으로 쓴다.
    ap.add_argument("--room", default=os.environ.get("KAKAO_ROOM"),
                    help="채팅방 이름 일부 (기본: .env 의 KAKAO_ROOM)")
    ap.add_argument("--once", action="store_true", help="1회만 실행")
    ap.add_argument("--dry-run", action="store_true",
                    help="텔레그램 전송은 하지 않고 문구만 출력")
    ap.add_argument("--cycle", type=int, default=CYCLE_MINUTES)
    args = ap.parse_args()

    if not args.room:
        raise SystemExit(
            "채팅방 이름이 없습니다.\n"
            "  `.env` 에 KAKAO_ROOM 을 적거나 --room 으로 넘기세요.\n"
            "  이모지는 넣지 마세요. 콘솔 인코딩 문제가 생깁니다(T1 관측).")

    log("=" * 55)
    log(f"시작. 방='{args.room}' 주기={args.cycle}분 dry_run={args.dry_run} "
        f"(토스는 {TOSS_EVERY_N_CYCLES}주기마다)")
    fails = 0
    cycle_no = 0

    while True:
        try:
            results, err = cycle(args.room, args.dry_run, cycle_no)
            cycle_no += 1
            write_status(last_cycle=results, error=err, consecutive_fails=fails)

            # 쿠팡과 토스 중 한쪽이라도 링크가 나오면 파이프라인은 살아 있다.
            # 둘 다 실패해야 핵심 실패로 본다.
            #
            # 전달까지 포함한다. 이 파이프라인의 목적은 이제 '텔레그램으로
            # 문구를 보내는 것' 이다. 전달이 깨지면 앞이 다 성공해도 사장님
            # 손에는 아무것도 안 들어온다. 보낼 게 없을 때는 성공으로
            # 끝나므로(종료코드 0) 이 조건이 과하게 걸리지 않는다.
            any_link = results.get("link") or results.get("toss_link")
            critical_ok = (results.get("extract") and any_link
                           and results.get("deliver"))
            if critical_ok:
                if fails:
                    notify(f"{fails}회 실패 후 복구되었습니다.")
                fails = 0
            else:
                fails += 1
                log(f"핵심 단계 실패 (연속 {fails}회)", "ERROR")
                if fails == CONSECUTIVE_FAIL_ALERT:
                    notify(f"파이프라인이 {fails}회 연속 실패했습니다. run.log 를 확인하세요.")

        except KeyboardInterrupt:
            log("사용자 중단")
            break
        except Exception:
            fails += 1
            log("예상치 못한 오류:\n" + traceback.format_exc(), "ERROR")
            write_status(error="unhandled", consecutive_fails=fails)
            if fails == CONSECUTIVE_FAIL_ALERT:
                notify("파이프라인에 예상치 못한 오류가 발생했습니다.")

        if args.once:
            break

        log(f"다음 사이클까지 {args.cycle}분 대기\n")
        time.sleep(args.cycle * 60)


if __name__ == "__main__":
    main()
