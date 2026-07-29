@echo off
REM ---------------------------------------------------------------
REM run_listener.cmd — 텔레그램 봇(수신 담당) 진입점
REM
REM telegram_bot.py 를 long polling 으로 계속 돌린다.
REM 사장님이 봇에게 보낸 링크·딜방 글을 발행 문구로 만들어 회신한다.
REM
REM 파이프라인(run_bot.cmd)과 **별개 작업**이다. 한쪽이 죽어도 다른 쪽은
REM 계속 돈다. 둘이 같은 브라우저 프로필을 쓸 때는 src/lock.py 가 막는다.
REM
REM ⚠️ 이쪽도 로그온한 데스크톱 세션이 필요하다. 쿠팡 링크를 만들 때
REM    playwright 로 브라우저를 띄우기 때문이다(headless 금지).
REM ---------------------------------------------------------------

chcp 65001 >nul
cd /d "%~dp0..\src"

REM cmd 는 이 파일을 ANSI 로 읽는다. echo 에 한글을 쓰면 로그가 깨진다.
REM 한글은 REM 주석에만 둔다(무시되므로 안전).
echo [%date% %time%] run_listener.cmd start >> "%~dp0..\src\bot.log"

:loop
where py >nul 2>&1
if %errorlevel%==0 (
    py telegram_bot.py >> "%~dp0..\src\bot.log" 2>&1
) else (
    python telegram_bot.py >> "%~dp0..\src\bot.log" 2>&1
)

REM 네트워크가 끊기거나 텔레그램이 잠시 막으면 죽을 수 있다.
REM 조용히 멈추면 사장님이 보낸 메시지가 영원히 답을 못 받는다. 되살린다.
echo [%date% %time%] telegram_bot.py exit=%errorlevel%, restarting in 30s >> "%~dp0..\src\bot.log"
timeout /t 30 /nobreak >nul
goto loop
