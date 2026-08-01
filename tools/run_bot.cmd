@echo off
REM ---------------------------------------------------------------
REM run_bot.cmd — 작업 스케줄러가 실행하는 진입점
REM
REM run_all.py 를 45분 주기로 무한 반복시킨다.
REM 방 이름은 .env 의 KAKAO_ROOM 에서 읽으므로 여기 인자로 주지 않는다.
REM (경로에 공백과 한글이 섞이면 따옴표가 꼬인다)
REM
REM ⚠️ 반드시 로그온한 데스크톱 세션에서 실행되어야 한다.
REM    pywinauto 는 실제 창에 키를 보내고, playwright 는 headless=False 다
REM    (CLAUDE.md 제약 5). 화면이 없는 세션에서는 조용히 계속 실패한다.
REM ---------------------------------------------------------------

chcp 65001 >nul
cd /d "%~dp0..\src"

REM cmd 는 이 파일을 ANSI 로 읽는다. echo 에 한글을 쓰면 로그가 깨진다.
REM 한글은 REM 주석에만 둔다. 단, `goto` 를 쓰는 파일에서는 REM 주석의
REM 한글도 위험하다 — cmd 가 바이트 오프셋으로 되감아 글자 중간에
REM 떨어진다. run_listener.cmd 가 그래서 죽었다(2026-08-02 실측).
REM 이 파일에는 goto 가 없어 무사하다. 추가하지 말 것.

REM 파이썬이 리다이렉트된 stdout 에 cp949 로 쓰는 것을 막는다.
set PYTHONIOENCODING=utf-8

echo [%date% %time%] run_bot.cmd start >> "%~dp0..\src\run.log"

REM py 런처가 PATH 에 없을 수 있다. 없으면 python 으로 넘어간다.
where py >nul 2>&1
if %errorlevel%==0 (
    py run_all.py
) else (
    python run_all.py
)

echo [%date% %time%] run_bot.cmd exit=%errorlevel% >> "%~dp0..\src\run.log"
