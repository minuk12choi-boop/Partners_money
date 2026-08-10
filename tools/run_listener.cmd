@echo off
REM ---------------------------------------------------------------
REM run_listener.cmd - Telegram bot entry point (Task Scheduler)
REM
REM   *** ASCII ONLY. DO NOT PUT KOREAN TEXT IN THIS FILE. ***
REM
REM This file uses `goto`. When cmd.exe jumps, it re-seeks the batch
REM file by BYTE OFFSET. Multi-byte UTF-8 characters make it land in
REM the middle of a character, after which it parses garbage and
REM runs fragments of these comments as commands.
REM
REM Measured 2026-08-02 on the owner's PC: the task exited 255 and
REM never even created bot.log.
REM   '<garbage>' is not recognized as an internal or external command
REM The same file with Korean stripped runs fine. run_bot.cmd has no
REM `goto`, which is why only this one died.
REM
REM REM comments are NOT safe here just because cmd ignores them.
REM They are still bytes, and `goto` counts bytes.
REM
REM Korean rationale and the measurement: TASKS.md, T5 section.
REM
REM Needs a logged-on interactive desktop: telegram_bot opens a
REM visible browser to build Coupang links (headless is forbidden,
REM CLAUDE.md constraint 5).
REM ---------------------------------------------------------------

chcp 65001 >nul
cd /d "%~dp0..\src"

REM Without this, Python writes cp949 when stdout is redirected to a
REM file, so bot.log ends up in a different encoding than every other
REM file in the project. Measured 2026-08-02.
set PYTHONIOENCODING=utf-8

echo [%date% %time%] run_listener.cmd start >> "%~dp0..\src\bot.log"

:loop
where py >nul 2>&1
if %errorlevel%==0 (
    py telegram_bot.py >> "%~dp0..\src\bot.log" 2>&1
) else (
    python telegram_bot.py >> "%~dp0..\src\bot.log" 2>&1
)

REM A network drop or a Telegram outage can kill it. If it stays
REM dead, messages the owner sends are never answered. Bring it back.
echo [%date% %time%] telegram_bot.py exit=%errorlevel%, restarting in 30s >> "%~dp0..\src\bot.log"
timeout /t 30 /nobreak >nul
goto loop
