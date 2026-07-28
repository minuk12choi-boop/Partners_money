#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kakao_export.py

PC 카카오톡의 지정 채팅방 창을 찾아 Ctrl+S 를 눌러 대화를 txt 로 내보낸다.
창이 닫혀 있으면 메인 창의 채팅 목록에서 방을 찾아 더블클릭해 연다.

사용법:
    python kakao_export.py --room "방이름일부"
    python kakao_export.py --room "핫딜" --out export.txt

의존성 (Windows 전용):
    pip install pywinauto pyperclip
"""

import argparse
import os
import sys
import time

if sys.platform != "win32":
    sys.exit("이 스크립트는 Windows 에서만 동작합니다.")

from pywinauto import Desktop, Application
from pywinauto.keyboard import send_keys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, "export.txt")

# PC 카카오톡 윈도우 클래스
CLS_MAIN = "EVA_Window_Dblclk"
CLS_CHAT = "#32770"


def find_chat_window(room_keyword):
    """열려 있는 채팅방 창을 제목으로 찾는다."""
    for w in Desktop(backend="win32").windows():
        try:
            title = w.window_text()
            cls = w.class_name()
        except Exception:
            continue
        if not title:
            continue
        if room_keyword in title and cls in (CLS_CHAT, CLS_MAIN):
            # 메인 창(제목이 '카카오톡')은 제외
            if title.strip() in ("카카오톡", "KakaoTalk"):
                continue
            return w
    return None


def open_chat_from_main(room_keyword):
    """메인 창에서 방을 검색해 연다."""
    try:
        main = Desktop(backend="win32").window(class_name=CLS_MAIN, title_re="카카오톡|KakaoTalk")
        main.wait("exists", timeout=5)
    except Exception:
        raise RuntimeError("카카오톡 메인 창을 찾을 수 없습니다. 카톡이 실행 중인지 확인하세요.")

    main.set_focus()
    time.sleep(0.6)
    # 채팅 탭으로 이동 후 검색
    send_keys("^f")            # 카톡 검색
    time.sleep(0.8)
    send_keys(room_keyword, with_spaces=True)
    time.sleep(1.5)
    send_keys("{ENTER}")
    time.sleep(2.0)

    w = find_chat_window(room_keyword)
    if w is None:
        raise RuntimeError(f"'{room_keyword}' 채팅방 창을 열지 못했습니다.")
    return w


def handle_save_dialog(out_path, timeout=15):
    """Ctrl+S 후 뜨는 '다른 이름으로 저장' 대화상자를 처리한다."""
    deadline = time.time() + timeout
    dlg = None
    while time.time() < deadline:
        for w in Desktop(backend="win32").windows():
            try:
                t = w.window_text()
            except Exception:
                continue
            if any(k in t for k in ("다른 이름으로 저장", "Save As", "저장")):
                if w.class_name() == "#32770":
                    dlg = w
                    break
        if dlg:
            break
        time.sleep(0.4)

    if dlg is None:
        return False

    dlg.set_focus()
    time.sleep(0.5)
    # 파일명 필드에 전체 경로 입력
    send_keys("%n")                       # Alt+N = 파일 이름 필드
    time.sleep(0.3)
    send_keys("^a{BACKSPACE}")
    time.sleep(0.2)
    send_keys(out_path.replace(" ", "{SPACE}"), with_spaces=True)
    time.sleep(0.4)
    send_keys("{ENTER}")
    time.sleep(1.2)

    # 덮어쓰기 확인 대화상자 처리
    for _ in range(6):
        for w in Desktop(backend="win32").windows():
            try:
                t = w.window_text()
            except Exception:
                continue
            if "확인" in t or "Confirm" in t:
                try:
                    w.set_focus()
                    send_keys("%y")       # 예(Y)
                    time.sleep(0.5)
                except Exception:
                    pass
        time.sleep(0.4)

    return True


def export(room_keyword, out_path):
    w = find_chat_window(room_keyword)
    if w is None:
        print("채팅방 창이 닫혀 있음 → 메인 창에서 여는 중")
        w = open_chat_from_main(room_keyword)

    before = os.path.getmtime(out_path) if os.path.exists(out_path) else 0

    w.set_focus()
    time.sleep(0.8)
    send_keys("^s")
    time.sleep(1.2)

    if not handle_save_dialog(out_path):
        raise RuntimeError("저장 대화상자를 찾지 못했습니다. 카톡 창이 포커스를 받았는지 확인하세요.")

    # 파일이 실제로 갱신됐는지 확인
    for _ in range(20):
        if os.path.exists(out_path) and os.path.getmtime(out_path) > before:
            size = os.path.getsize(out_path)
            print(f"내보내기 완료: {out_path} ({size:,} bytes)")
            return out_path
        time.sleep(0.5)

    raise RuntimeError("파일이 갱신되지 않았습니다.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", required=True, help="채팅방 이름의 일부")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()
    export(args.room, os.path.abspath(args.out))


if __name__ == "__main__":
    main()
