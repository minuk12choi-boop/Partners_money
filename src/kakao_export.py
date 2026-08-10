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

# PC 카카오톡 윈도우 클래스 — 2026-07-28 실측값 (TASKS.md T1 관측 참고)
# 메인 창과 채팅방 창이 같은 클래스를 쓴다. 둘의 구분은 제목으로만 가능하다.
CLS_KAKAO = "EVA_Window_Dblclk"
# Win32 표준 대화상자 클래스. 카톡 창이 아니라 저장 대화상자가 이걸 쓴다.
CLS_DIALOG = "#32770"
# 메인 창의 제목. 이 목록에 걸리면 채팅방 창이 아니다.
MAIN_TITLES = ("카카오톡", "KakaoTalk")

# ── Ctrl+S 이후 흐름에 등장하는 창들 (2026-07-28 실측) ────────────────
# 주의: DLG_CONFIRM 은 DLG_SAVE 를 부분문자열로 포함한다.
#       그래서 제목 매칭은 반드시 정확일치여야 한다. 부분일치를 쓰면
#       덮어쓰기 확인창을 저장 대화상자로 잘못 잡는다.
DLG_SAVE = "다른 이름으로 저장"
DLG_CONFIRM = "다른 이름으로 저장 확인"
BTN_SAVE = "저장(&S)"
BTN_YES = "예(&Y)"

# 저장이 끝나면 카톡이 자체 완료 알림을 띄운다(스크린샷으로 확인).
# 버튼은 '확인' 과 '폴더열기'. 클래스명은 아직 미관측이라 제목으로만 찾는다.
# 이 창을 닫지 않으면 계속 떠 있어서 다음 주기를 방해한다.
DLG_DONE = "대화 내보내기"
BTN_DONE_OK = "확인"
# 파일이 다 쓰인 뒤에도 게이지가 100% 로 바뀌고 버튼이 살아나는 데 시간이 걸린다.
# 너무 일찍 Enter 를 보내면 먹지 않는다(실측).
SETTLE_AFTER_SAVE = 5.0


def find_chat_window(room_keyword):
    """열려 있는 채팅방 창을 제목으로 찾는다."""
    for w in Desktop(backend="win32").windows():
        try:
            title = w.window_text()
            cls = w.class_name()
            visible = w.is_visible()
        except Exception:
            continue
        if not title or cls != CLS_KAKAO:
            continue
        # 제목이 없는 숨은 EVA_Window_Dblclk 가 수십 개 떠 있다(실측).
        # 지금은 제목으로 걸러지지만, 숨은 창을 잡으면 set_focus 가 조용히 실패한다.
        if not visible:
            continue
        # 메인 창(제목이 '카카오톡')은 제외
        if title.strip() in MAIN_TITLES:
            continue
        if room_keyword in title:
            return w
    return None


def open_chat_from_main(room_keyword):
    """메인 창에서 방을 검색해 연다."""
    try:
        main = Desktop(backend="win32").window(class_name=CLS_KAKAO, title_re="카카오톡|KakaoTalk")
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


def wait_window(title, pid=None, cls=None, timeout=15):
    """제목이 '정확히' 일치하는 창을 기다린다.

    부분일치를 쓰면 안 된다. DLG_CONFIRM('다른 이름으로 저장 확인')이
    DLG_SAVE('다른 이름으로 저장')를 포함하고 둘 다 #32770 이라
    서로를 잘못 잡는다. 실측으로 확인한 함정이다.

    cls=None 이면 클래스를 가리지 않는다. 완료 알림창처럼 클래스를
    아직 관측하지 못한 창을 찾을 때 쓴다.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for w in Desktop(backend="win32").windows():
            try:
                if cls is not None and w.class_name() != cls:
                    continue
                if w.window_text().strip() != title:
                    continue
                # 다른 앱의 같은 이름 대화상자를 잡지 않도록 소유 프로세스 확인
                if pid is not None and w.process_id() != pid:
                    continue
            except Exception:
                continue
            return w
        time.sleep(0.3)
    return None


def click_button(win, button_title, timeout=5):
    """대화상자 안의 버튼을 누른다. 성공하면 True."""
    try:
        dlg = Desktop(backend="win32").window(handle=win.handle)
        btn = dlg.child_window(title=button_title, class_name="Button")
        btn.wait("visible enabled", timeout=timeout)
        btn.click()
        return True
    except Exception as e:
        print(f"  버튼 '{button_title}' 클릭 실패: {e}")
        return False


def dump_window_info(win, label="창"):
    """창의 정체를 전부 찍는다. 못 닫을 때 원인을 보기 위한 것이다.

    이 알림창은 클래스명조차 아직 관측되지 않았다. 추측으로 전략을
    바꾸지 말고 실제 구조를 보고 정하기 위해 남긴다.
    """
    print(f"  ── {label} 상세 " + "─" * 30)
    for name, fn in (
        ("class_name", win.class_name),
        ("window_text", win.window_text),
        ("rectangle", win.rectangle),
        ("is_visible", win.is_visible),
        ("is_enabled", win.is_enabled),
        ("handle", lambda: win.handle),
    ):
        try:
            print(f"     {name:12s}: {fn()!r}")
        except Exception as e:
            print(f"     {name:12s}: (실패 {e})")

    try:
        children = win.children()
        print(f"     자식 {len(children)}개")
        for ch in children[:25]:
            try:
                print(f"       class={ch.class_name()!r} text={ch.window_text()!r} "
                      f"rect={ch.rectangle()}")
            except Exception:
                pass
    except Exception as e:
        print(f"     자식 조회 실패: {e}")
    print("  " + "─" * 38)


def click_by_ratio(win, rx, ry, label=""):
    """창 안의 상대 좌표를 클릭한다. 컨트롤이 안 잡히는 자체 렌더링 창용.

    최후의 수단이다. 좌표는 화면 구성이 바뀌면 깨지므로,
    이 경로를 탔다는 사실을 반드시 로그에 남긴다.
    """
    try:
        r = win.rectangle()
        x = int(r.left + (r.right - r.left) * rx)
        y = int(r.top + (r.bottom - r.top) * ry)
        print(f"  좌표 클릭 시도{label}: ({x}, {y}) — 창 {r}")
        win.click_input(coords=(x - r.left, y - r.top))
        return True
    except Exception as e:
        print(f"  좌표 클릭 실패: {e}")
        return False


def is_dialog_open(pid, title=None):
    """알림창이 아직 떠 있는지 한 번만 훑어 확인한다."""
    return wait_window(title or DLG_DONE, pid=pid, cls=None, timeout=0.5) is not None


def wait_file_ready(path, before_mtime, timeout=120, settle=1.5):
    """파일이 새로 쓰이고 크기가 안정될 때까지 기다린다.

    '대화 내보내기' 알림은 진행률 0% 상태에서 이미 떠 있다.
    게이지가 차기 전에 닫으면 내보내기가 끝나지 않은 채 중단된다.
    창이 떴다는 사실만으로 완료를 판단하면 안 되므로, 실제 파일이
    쓰였는지를 완료 신호로 쓴다.
    """
    deadline = time.time() + timeout
    last_size, stable_since = -1, None
    next_report = time.time() + 5
    while time.time() < deadline:
        try:
            st = os.stat(path)
        except OSError:
            time.sleep(0.3)
            continue
        if st.st_mtime > before_mtime and st.st_size > 0:
            if st.st_size == last_size:
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since >= settle:
                    return True
            else:
                last_size, stable_since = st.st_size, None
        # 멈춘 것처럼 보이지 않게 진행 상황을 알린다.
        # 무인 운영에서 조용한 대기는 조용한 정지와 구분이 안 된다.
        if time.time() >= next_report:
            print(f"    저장 대기 중... {max(0, last_size):,} bytes")
            next_report = time.time() + 5
        time.sleep(0.3)
    return False


def dump_descendants(win, label="자식 창", limit=40):
    """창의 자식들을 훑는다. 인앱 모달은 최상위 창 목록에 안 나오므로
    채팅방 창 아래를 봐야 한다."""
    print(f"  ── {label} " + "─" * 30)
    try:
        kids = win.descendants()
    except Exception:
        try:
            kids = win.children()
        except Exception as e:
            print(f"     조회 실패: {e}")
            return
    print(f"     {len(kids)}개")
    for ch in kids[:limit]:
        try:
            t = ch.window_text()
            c = ch.class_name()
            if not t and c in ("", "Static"):
                continue
            print(f"       class={c!r} text={t!r} rect={ch.rectangle()}")
        except Exception:
            pass
    print("  " + "─" * 38)


def close_done_dialog(pid, out_path, before_mtime, chat_win=None, timeout=15):
    """저장 완료 알림('대화 내보내기')을 닫는다.

    카톡이 내보내기를 마치면 '완료되었습니다' 알림을 띄우고,
    사용자가 확인을 눌러야 사라진다. 방치하면 창이 계속 남아
    다음 주기의 Ctrl+S 를 방해한다. 무인 운영에서는 반드시 닫아야 한다.

    ⚠️ 이 창은 진행률 0% 일 때 이미 떠 있다(실측). 창을 찾자마자 닫으면
    저장이 끝나기 전에 중단된다. 반드시 파일이 다 쓰인 뒤에 닫는다.

    창의 클래스는 아직 미관측이라 클래스를 가리지 않고 제목으로 찾고,
    찾으면 실제 클래스명을 로그에 남긴다. 다음 실행 로그에서 그 값을
    확인해 TASKS.md 관측 항목에 기록할 것.
    """
    # 이 알림은 최상위 창이 아니라 카톡 창 안에 그려지는 인앱 모달로 보인다.
    # (--mode dialog 관측에서 최상위 창 17개 중 '대화 내보내기' 가 없었고,
    #  화면상 채팅방이 어둡게 깔린 위에 떠 있다)
    # 그래서 못 찾는 것이 정상이며, 못 찾았다고 실패로 처리하면 안 된다.
    done = wait_window(DLG_DONE, pid=pid, cls=None, timeout=timeout)
    if done is None:
        print(f"  '{DLG_DONE}' 이 최상위 창에 없음 → 인앱 모달로 보고 진행합니다.")
        if chat_win is not None:
            dump_descendants(chat_win, "채팅방 창의 자식(모달 탐색)")
    else:
        try:
            print(f"  완료 알림 감지 (class={done.class_name()!r})")
        except Exception:
            print("  완료 알림 감지")

    if wait_file_ready(out_path, before_mtime):
        print("  저장 완료 확인")
    else:
        # 파일이 안 쓰였어도 창은 닫아야 다음 주기가 막히지 않는다.
        print("  저장 완료를 확인하지 못했습니다. 알림만 닫습니다.")

    # 파일이 다 쓰여도 게이지가 100% 로 바뀌기까지 시간이 더 걸린다.
    time.sleep(SETTLE_AFTER_SAVE)

    if done is not None:
        dump_window_info(done, "완료 알림창")

    # 인앱 모달이라 최상위 창으로 잡히지 않는 경우가 정상이다.
    # 그래서 '카톡 채팅방 창에 포커스를 주고 Enter' 가 주 전략이다.
    # 이 방이 '관리자만 말할 수 있는 상태' 라 Enter 가 새어 나가도
    # 메시지가 전송되지 않는다. 그래도 필요 이상으로 보내지는 않는다.
    for attempt in range(1, 4):
        if done is not None and click_button(done, BTN_DONE_OK, timeout=2):
            if not is_dialog_open(pid):
                print("  ✅ 닫힘 — 버튼 클릭")
                return True

        target = chat_win if chat_win is not None else done
        if target is None:
            print("  포커스를 줄 창이 없습니다.")
            break
        try:
            target.set_focus()
            time.sleep(1.0)
            send_keys("{ENTER}")
        except Exception as e:
            print(f"  포커스/Enter 실패: {e}")
        time.sleep(1.5)

        if done is not None and not is_dialog_open(pid):
            print(f"  ✅ 닫힘 — 카톡창 포커스 + Enter (시도 {attempt}회)")
            return True
        if done is None:
            # 최상위 창으로 안 잡히니 닫힘 여부를 창 목록으로 확인할 수 없다.
            # 한 번만 보내고 넘어간다. 남아 있으면 다음 실행 로그로 드러난다.
            print(f"  카톡창 포커스 + Enter 전송 (시도 {attempt}회)")
            print("  ※ 인앱 모달이라 닫힘 여부를 창 목록으로 확인할 수 없습니다.")
            return True
        print(f"  아직 열려 있음 → 재시도 {attempt}/3")

    print("  ❌ 완료 알림을 닫지 못했습니다. 다음 주기의 Ctrl+S 가 막힙니다.")
    if done is not None:
        dump_window_info(done, "닫기 실패 후 상태")
    return False


def handle_save_dialog(out_path, pid=None, before_mtime=0, chat_win=None, timeout=20):
    """Ctrl+S 이후의 대화상자 흐름 전체를 처리한다.

    실측한 흐름:
        '다른 이름으로 저장'(#32770) → 파일명 입력 → 저장(&S)
          → 파일이 이미 있으면 '다른 이름으로 저장 확인' → 예(&Y)
          → '대화 내보내기' 완료 알림 → 확인
    """
    dlg_w = wait_window(DLG_SAVE, pid=pid, cls=CLS_DIALOG, timeout=timeout)
    if dlg_w is None:
        return False

    dlg = Desktop(backend="win32").window(handle=dlg_w.handle)

    # 파일명 입력. send_keys 로 타이핑하지 않고 컨트롤에 직접 넣는다.
    # 경로에 공백이나 (){}+^~% 가 있어도 안전하고, IME 상태에도 영향받지 않는다.
    try:
        edit = dlg.child_window(class_name="Edit")
        edit.wait("visible", timeout=5)
        edit.set_edit_text(out_path)
    except Exception as e:
        print(f"  파일명 입력 실패: {e}")
        return False
    time.sleep(0.3)

    if not click_button(dlg_w, BTN_SAVE):
        # 저장 버튼을 못 찾으면 Enter 로 대체
        try:
            edit.type_keys("{ENTER}")
        except Exception as e:
            print(f"  저장 실행 실패: {e}")
            return False

    # 덮어쓰기 확인 — 파일이 처음이면 뜨지 않는다. 짧게만 기다린다.
    conf = wait_window(DLG_CONFIRM, pid=pid, cls=CLS_DIALOG, timeout=4)
    if conf is not None:
        print("  덮어쓰기 확인 → 예")
        if not click_button(conf, BTN_YES):
            return False

    # 완료 알림을 닫는다. 실패해도 파일 자체는 저장됐을 수 있으므로
    # 여기서 False 를 돌려주지는 않되, 로그에는 반드시 남긴다.
    close_done_dialog(pid, out_path, before_mtime, chat_win=chat_win)
    return True


def export(room_keyword, out_path):
    w = find_chat_window(room_keyword)
    if w is None:
        print("채팅방 창이 닫혀 있음 → 메인 창에서 여는 중")
        w = open_chat_from_main(room_keyword)

    before = os.path.getmtime(out_path) if os.path.exists(out_path) else 0

    # 대화상자를 찾을 때 소유 프로세스로 거르기 위해 카톡 pid 를 확보한다.
    try:
        pid = w.process_id()
    except Exception:
        pid = None

    w.set_focus()
    time.sleep(0.8)
    send_keys("^s")
    time.sleep(1.2)

    if not handle_save_dialog(out_path, pid=pid, before_mtime=before, chat_win=w):
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
