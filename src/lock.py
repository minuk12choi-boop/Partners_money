#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lock.py — 브라우저 프로필을 한 번에 하나만 쓰도록 막는다

**왜 있는가**

Playwright 의 persistent context 는 프로필 디렉터리를 독점한다. 두 프로세스가
같은 프로필을 동시에 열면 뒤에 온 쪽이 이렇게 죽는다(실측 2026-07-29).

    TargetClosedError: ... Target page, context or browser has been closed
    [out] 기존 브라우저 세션에서 열고 있습니다.

지금까지는 `run_all.py` 가 순서대로만 돌려서 문제가 없었다. 그런데
텔레그램 봇이 생기면서 **사용자가 아무 때나 링크 생성을 시킬 수 있게**
됐다. 주기 작업과 봇이 같은 순간에 쿠팡 프로필을 열면 둘 중 하나가 죽는다.

프로필별로 잠근다. 쿠팡과 토스는 프로필이 다르므로 서로를 막지 않는다.

**사용법**

    from lock import profile_lock

    with profile_lock(PROFILE_DIR):
        with sync_playwright() as pw:
            ...

**설계 메모**

- `O_CREAT | O_EXCL` 로 파일을 만드는 것이 원자적 획득이다.
  Windows 에서도 동작한다.
- 프로세스가 죽어 잠금이 남는 경우를 대비해 **나이로 판단**한다.
  `STALE_SECONDS` 를 넘으면 버려진 것으로 보고 뺏는다. 브라우저 작업이
  아무리 길어도 그보다 오래 걸리지 않는다.
- 잠금 파일에 pid 와 시각을 적는다. 막혔을 때 누가 잡고 있는지 보여야
  사람이 판단할 수 있다.
"""

import os
import time
from contextlib import contextmanager
from datetime import datetime

# 이보다 오래된 잠금은 죽은 프로세스가 남긴 것으로 본다.
STALE_SECONDS = 900          # 15분
# 기다리다 포기하는 시간. 주기 작업이 봇을 영원히 굶기면 안 된다.
DEFAULT_TIMEOUT = 300        # 5분
POLL_SECONDS = 1.0


class LockBusy(RuntimeError):
    pass


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "?"


def _try_acquire(path):
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"pid={os.getpid()} "
                f"at={datetime.now().isoformat(timespec='seconds')}")
    return True


@contextmanager
def profile_lock(profile_dir, timeout=DEFAULT_TIMEOUT, log=print):
    """프로필 디렉터리 하나를 독점한다."""
    path = os.path.abspath(profile_dir).rstrip("\\/") + ".lock"
    os.makedirs(os.path.dirname(path), exist_ok=True)

    deadline = time.time() + timeout
    waited = False
    while True:
        if _try_acquire(path):
            break

        # 죽은 프로세스가 남긴 잠금이면 뺏는다.
        try:
            age = time.time() - os.path.getmtime(path)
        except FileNotFoundError:
            continue          # 방금 풀렸다. 다시 시도
        if age > STALE_SECONDS:
            log(f"[lock] {age/60:.0f}분 된 잠금을 버려진 것으로 보고 회수합니다 "
                f"({_read(path)})")
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            continue

        if time.time() > deadline:
            raise LockBusy(
                f"브라우저 프로필이 사용 중입니다: {path}\n"
                f"  잡고 있는 쪽: {_read(path)}\n"
                f"  {timeout}초를 기다렸습니다. 잠시 뒤 다시 시도하세요.")

        if not waited:
            log(f"[lock] 다른 작업이 브라우저를 쓰고 있어 기다립니다 "
                f"({_read(path)})")
            waited = True
        time.sleep(POLL_SECONDS)

    try:
        yield
    finally:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
