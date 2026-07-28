#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
observe_kakao.py — T1 관측 전용 도구

카카오톡의 실제 창 구조를 "있는 그대로" 덤프한다.
kakao_export.py 의 상수를 추측으로 고치지 않기 위해 먼저 돌리는 스크립트다.

이 스크립트는 카카오톡에 키 입력을 보내지 않는다. 읽기 전용이다.
(Ctrl+S 는 --mode dialog 에서 사용자가 직접 누르고, 스크립트는 그 전후로
 창 목록을 관찰해 차이만 기록한다.)

사용법 (Windows PC 에서, 딜방 창을 열어둔 상태로):

    # 1) 창 목록 — 채팅방 창의 실제 클래스명을 여기서 확인한다
    python tools/observe_kakao.py --mode windows --room "핫딜방"

    # 2) 컨트롤 트리 — 메인 창/방 창의 내부 구조
    python tools/observe_kakao.py --mode tree --room "핫딜방"

    # 3) 저장 대화상자 — 실행 후 방 창에서 직접 Ctrl+S 를 누른다
    python tools/observe_kakao.py --mode dialog

    # 4) 내보낸 txt 의 인코딩과 포맷
    python tools/observe_kakao.py --mode encoding --file export.txt

결과는 화면과 --out 파일(기본 kakao_observe.txt)에 함께 기록된다.

주의: 출력에는 채팅방 이름·친구 이름 등 개인정보가 포함될 수 있다.
      kakao_observe.txt 는 .gitignore 에 등록되어 있다. 공유 전에 훑어볼 것.

의존성: pywinauto (Windows 전용). 그 외에는 표준 라이브러리만 쓴다.
"""

import argparse
import contextlib
import io
import os
import sys
import time
from datetime import datetime

# 한글·이모지가 콘솔 인코딩(cp949)에서 깨지지 않도록 먼저 고정한다.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

if sys.platform != "win32":
    sys.exit(
        "이 스크립트는 Windows 에서만 동작합니다. (pywinauto 는 Win32/UIA 래퍼)\n"
        "카카오톡이 실행 중인 소유자 PC 에서 실행하세요."
    )

import ctypes
from ctypes import wintypes

try:
    from pywinauto import Desktop
except ImportError:
    sys.exit("pywinauto 가 없습니다. 먼저 `pip install -r requirements.txt` 를 실행하세요.")


# ---------------------------------------------------------------- 출력

class Tee:
    """화면과 파일에 동시에 쓴다."""

    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")
        self.path = path

    def __call__(self, msg=""):
        print(msg)
        self.f.write(str(msg) + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


def header(w, title):
    w()
    w("=" * 78)
    w(f"  {title}")
    w(f"  {datetime.now():%Y-%m-%d %H:%M:%S}")
    w("=" * 78)


# ---------------------------------------------------------------- 프로세스명

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
_k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def process_name(pid):
    """pid -> 실행파일 이름. 실패해도 예외를 던지지 않는다."""
    if not pid:
        return "?"
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return "?(접근불가)"
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return "?"
    finally:
        _k32.CloseHandle(h)


# ---------------------------------------------------------------- 창 정보

def win_info(w):
    """창 하나에서 뽑을 수 있는 건 다 뽑는다. 실패한 항목은 None."""
    info = {"handle": None, "title": None, "cls": None, "pid": None,
            "proc": None, "rect": None, "visible": None}
    for key, fn in (
        ("handle", lambda: w.handle),
        ("title", lambda: w.window_text()),
        ("cls", lambda: w.class_name()),
        ("pid", lambda: w.process_id()),
        ("rect", lambda: str(w.rectangle())),
        ("visible", lambda: w.is_visible()),
    ):
        try:
            info[key] = fn()
        except Exception:
            pass
    info["proc"] = process_name(info["pid"])
    return info


def codepoints(s):
    """이모지가 섞인 제목의 정확한 코드포인트를 보여준다.
    --room 부분일치 문자열을 정하려면 이게 필요하다."""
    parts = []
    for ch in s:
        if ch == " ":
            parts.append("U+0020(공백)")
        else:
            parts.append(f"U+{ord(ch):04X}({ch})")
    return " ".join(parts)


def list_windows(backend):
    """top-level 창 전부. 숨은 창도 포함해서 본다."""
    try:
        return Desktop(backend=backend).windows(visible_only=False)
    except Exception:
        # 일부 pywinauto 버전은 visible_only 를 안 받는다
        return Desktop(backend=backend).windows()


# ---------------------------------------------------------------- mode: windows

def mode_windows(w, room, show_all):
    header(w, "MODE: windows — top-level 창 목록")
    w("")
    w("확인할 것:")
    w("  1) 카카오톡 채팅방 창의 class_name  (현재 코드 가정: '#32770' ← 의심스러움)")
    w("  2) 메인 창의 class_name             (현재 코드 가정: 'EVA_Window_Dblclk')")
    w("  3) 방 창 제목에 붙은 이모지의 정확한 코드포인트")
    w("")

    for backend in ("win32", "uia"):
        w("")
        w("-" * 78)
        w(f"[backend = {backend}]")
        w("-" * 78)
        try:
            wins = list_windows(backend)
        except Exception as e:
            w(f"  창 목록 조회 실패: {e}")
            continue

        rows = []
        for win in wins:
            info = win_info(win)
            title = info["title"] or ""
            proc = (info["proc"] or "").lower()
            is_kakao = "kakao" in proc
            # 기본값은 카카오 프로세스만. --all 이면 전부.
            if not show_all and not is_kakao:
                continue
            if not show_all and not title and not is_kakao:
                continue
            rows.append(info)

        if not rows:
            w("  해당하는 창이 없습니다.")
            if not show_all:
                w("  → 카카오톡이 실행 중인지 확인하고, --all 로 전체 창을 보세요.")
            continue

        w(f"  {len(rows)}개")
        for info in rows:
            w("")
            w(f"  class_name : {info['cls']!r}")
            w(f"  title      : {info['title']!r}")
            w(f"  proc / pid : {info['proc']} / {info['pid']}")
            w(f"  visible    : {info['visible']}    rect: {info['rect']}")
            w(f"  handle     : {info['handle']}")
            title = info["title"] or ""
            if room and room in title:
                w(f"  ★ --room '{room}' 과 부분일치하는 창입니다.")
                w(f"  제목 코드포인트:")
                w(f"    {codepoints(title)}")

    w("")
    w("-" * 78)
    w("판정 방법:")
    w("  · 딜방 창이 목록에 있다면 그 class_name 이 CLS_CHAT 의 실제 값이다.")
    w("  · 딜방 창이 win32 에는 없고 uia 에만 있다면, kakao_export.py 는")
    w("    backend='win32' 를 쓰므로 창을 영원히 못 찾는다. 이 경우 backend 변경이 필요하다.")
    w("  · 제목 코드포인트를 보고 이모지를 뺀 안전한 --room 문자열을 정한다.")


# ---------------------------------------------------------------- mode: tree

def mode_tree(w, room, depth):
    header(w, "MODE: tree — 카카오톡 창의 컨트롤 트리")
    w("")
    w("확인할 것: 메인 창의 검색창/채팅목록이 컨트롤로 노출되는지.")
    w("노출된다면 Ctrl+F 키 입력 대신 컨트롤을 직접 조작할 수 있어 훨씬 안정적이다.")
    w("")

    for backend in ("win32", "uia"):
        w("")
        w("-" * 78)
        w(f"[backend = {backend}]")
        w("-" * 78)
        try:
            wins = list_windows(backend)
        except Exception as e:
            w(f"  창 목록 조회 실패: {e}")
            continue

        for win in wins:
            info = win_info(win)
            proc = (info["proc"] or "").lower()
            if "kakao" not in proc:
                continue
            if not info["visible"]:
                continue
            title = info["title"] or ""
            if room and room not in title and title.strip() not in ("카카오톡", "KakaoTalk"):
                continue

            w("")
            w(f"  ▼ {info['cls']!r} / {title!r}")
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    win.print_control_identifiers(depth=depth)
            except Exception as e:
                w(f"    트리 덤프 실패: {e}")
                continue
            for line in buf.getvalue().splitlines():
                w("    " + line)


# ---------------------------------------------------------------- mode: dialog

def snapshot(backend):
    """{handle: info} 형태의 현재 창 스냅샷."""
    snap = {}
    try:
        for win in list_windows(backend):
            info = win_info(win)
            if info["handle"] is not None:
                snap[info["handle"]] = info
    except Exception:
        pass
    return snap


def mode_dialog(w, wait, depth):
    header(w, "MODE: dialog — Ctrl+S 후 나타나는 창 추적")
    w("")
    w("확인할 것 (가장 중요):")
    w("  · Ctrl+S 를 누르면 '다른 이름으로 저장' 이 바로 뜨는가?")
    w("  · 아니면 그 전에 옵션 대화상자(텍스트만 저장 등)가 한 번 더 뜨는가?")
    w("    ← 현재 kakao_export.py 에는 이 중간 단계가 아예 없다.")
    w("  · 저장 대화상자와 덮어쓰기 확인 대화상자의 정확한 제목/클래스/버튼")
    w("")

    base = snapshot("win32")
    w(f"기준 스냅샷: 창 {len(base)}개")
    w("")
    w("*" * 78)
    w("  지금부터 직접 하세요:")
    w("    1. 카카오톡 딜방 창을 클릭해 포커스를 준다")
    w("    2. Ctrl+S 를 누른다")
    w("    3. 대화상자가 뜨면 '그대로 두고' 기다린다 (저장하지 말 것)")
    w(f"    4. {wait}초 동안 관찰합니다")
    w("*" * 78)
    w("")

    seen = {}          # handle -> info
    order = []         # 나타난 순서
    start = time.time()
    while time.time() - start < wait:
        cur = snapshot("win32")
        for h, info in cur.items():
            if h not in base and h not in seen:
                elapsed = time.time() - start
                seen[h] = info
                order.append((elapsed, info))
                w(f"[+{elapsed:5.1f}s] 새 창  class={info['cls']!r}  title={info['title']!r}"
                  f"  proc={info['proc']}")
        for h in list(seen):
            if h not in cur:
                elapsed = time.time() - start
                w(f"[+{elapsed:5.1f}s] 사라짐 class={seen[h]['cls']!r}"
                  f"  title={seen[h]['title']!r}")
                del seen[h]
        time.sleep(0.4)

    w("")
    w("-" * 78)
    if not order:
        w("새로 나타난 창이 없습니다.")
        w("→ Ctrl+S 가 저장 대화상자를 띄우지 않는다는 뜻일 수 있습니다.")
        w("  카톡 메뉴에서 '대화 내용 저장' 이 어디에 있는지, 단축키가 정말 Ctrl+S 인지")
        w("  수동으로 확인해 주세요. 이게 T1 의 핵심 전제입니다.")
        return

    w(f"나타난 창 {len(order)}개 (순서대로):")
    for elapsed, info in order:
        w("")
        w(f"  +{elapsed:.1f}s")
        w(f"    class_name : {info['cls']!r}")
        w(f"    title      : {info['title']!r}")
        w(f"    proc / pid : {info['proc']} / {info['pid']}")
        if info["title"]:
            w(f"    제목 코드포인트: {codepoints(info['title'])}")

    w("")
    w("-" * 78)
    w("남아 있는 대화상자의 컨트롤 트리 (버튼 이름·입력 필드 확인용):")
    for h, info in seen.items():
        w("")
        w(f"  ▼ {info['cls']!r} / {info['title']!r}")
        try:
            win = Desktop(backend="win32").window(handle=h).wrapper_object()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                win.print_control_identifiers(depth=depth)
            for line in buf.getvalue().splitlines():
                w("    " + line)
        except Exception as e:
            w(f"    트리 덤프 실패: {e}")


# ---------------------------------------------------------------- mode: encoding

def mode_encoding(w, path, lines):
    header(w, "MODE: encoding — 내보낸 txt 의 인코딩과 포맷")
    w("")
    w("확인할 것: kakao_deal_extract.py 는 encoding='utf-8' 로 읽는다.")
    w("카톡이 CP949 나 UTF-16 으로 저장하면 파싱 이전에 깨진다.")
    w("")

    if not os.path.exists(path):
        w(f"파일이 없습니다: {path}")
        w("→ 먼저 카톡에서 대화 내용을 저장한 뒤 그 경로를 --file 로 주세요.")
        return

    raw = open(path, "rb").read()
    w(f"경로   : {path}")
    w(f"크기   : {len(raw):,} bytes")
    w(f"앞 16B : {raw[:16]!r}")

    boms = [
        (b"\xef\xbb\xbf", "UTF-8 BOM"),
        (b"\xff\xfe\x00\x00", "UTF-32 LE BOM"),
        (b"\xff\xfe", "UTF-16 LE BOM"),
        (b"\xfe\xff", "UTF-16 BE BOM"),
    ]
    found_bom = next((name for sig, name in boms if raw.startswith(sig)), "없음")
    w(f"BOM    : {found_bom}")
    w("")

    w("인코딩별 디코드 시도:")
    best = None
    for enc in ("utf-8-sig", "utf-8", "cp949", "utf-16", "utf-16-le"):
        try:
            text = raw.decode(enc)
        except Exception as e:
            w(f"  {enc:12s} 실패: {type(e).__name__}")
            continue
        # 한글이 깨지면 대개 치환문자나 이상한 문자가 섞인다
        bad = text.count("�")
        w(f"  {enc:12s} 성공  (치환문자 {bad}개)")
        if best is None and bad == 0:
            best = (enc, text)

    if best is None:
        w("")
        w("→ 어떤 인코딩으로도 깨끗하게 읽히지 않습니다. 앞 16바이트를 보고 판단이 필요합니다.")
        return

    enc, text = best
    w("")
    w(f"→ 가장 적합해 보이는 인코딩: {enc}")
    if enc != "utf-8":
        w(f"  현재 kakao_deal_extract.py 는 'utf-8' 로 읽으므로 수정이 필요합니다.")
    w("")
    w("-" * 78)
    w(f"앞부분 {lines}줄 (※ 개인정보 주의 — 공유 전에 확인할 것):")
    w("-" * 78)
    for i, line in enumerate(text.splitlines()[:lines], 1):
        w(f"{i:3d}| {line}")
    w("-" * 78)
    w("이 줄들이 kakao_deal_extract.py 의 RE_PC_LINE / RE_MO_LINE 과 맞는지 T2 에서 본다.")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(
        description="카카오톡 창 구조 관측 (읽기 전용)")
    ap.add_argument("--mode", required=True,
                    choices=["windows", "tree", "dialog", "encoding"])
    ap.add_argument("--room", default="", help="방 이름 일부 (이모지는 빼고)")
    ap.add_argument("--out", default="kakao_observe.txt", help="결과 저장 경로")
    ap.add_argument("--all", action="store_true",
                    help="windows 모드에서 카카오 외 프로세스 창도 전부 출력")
    ap.add_argument("--depth", type=int, default=4, help="컨트롤 트리 깊이")
    ap.add_argument("--wait", type=int, default=25,
                    help="dialog 모드 관찰 시간(초)")
    ap.add_argument("--file", default="export.txt",
                    help="encoding 모드에서 검사할 txt 경로")
    args = ap.parse_args()

    w = Tee(args.out)
    try:
        w("※ 이 출력에는 채팅방 이름 등 개인정보가 포함될 수 있습니다.")
        w("   공유 전에 내용을 훑어보세요.")
        if args.mode == "windows":
            mode_windows(w, args.room, args.all)
        elif args.mode == "tree":
            mode_tree(w, args.room, args.depth)
        elif args.mode == "dialog":
            mode_dialog(w, args.wait, args.depth)
        elif args.mode == "encoding":
            mode_encoding(w, args.file, 40)
        w("")
        w(f"결과 저장 완료: {os.path.abspath(args.out)}")
    finally:
        w.close()


if __name__ == "__main__":
    main()
