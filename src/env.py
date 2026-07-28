#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
env.py — `.env` 파일을 환경변수로 올린다.

**왜 있는가**

`.env.example` 은 "복사해서 `.env` 로 쓰거나 시스템 환경변수로 설정" 이라고
안내하는데, 실제로 `.env` 를 읽는 코드가 저장소 어디에도 없었다(실측
2026-07-29). `python-dotenv` 도 `requirements.txt` 에 없다. 그래서 `.env` 에
값을 적어 두면 아무 일도 일어나지 않았다.

이게 조용히 위험한 이유는 `run_all.py` 의 `notify()` 다. 토큰이 없으면
그냥 return 하므로 **실패 알림이 영원히 안 온다.** 무인 운영에서 최악은
조용히 멈추는 것인데, 알림 자체가 조용히 죽어 있는 상태였다.

새 의존성을 추가하지 않는다(CLAUDE.md 코딩 규약). 표준 라이브러리로 충분하다.

**사용법**

    from env import load_env
    load_env()

**규칙**
- 이미 있는 환경변수를 덮어쓰지 않는다. 시스템 환경변수가 항상 우선이다.
- `KEY=VALUE` 만 해석한다. `export` 접두사와 따옴표는 벗겨 준다.
- `#` 로 시작하는 줄과 빈 줄은 건너뛴다.
- 파일이 없어도 조용히 넘어간다. 시스템 환경변수만 쓰는 것도 정상이다.
"""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 저장소 루트를 먼저 본다. `.env.example` 이 루트에 있으므로 거기가 기본이다.
# src/ 에 둔 경우도 받아 준다.
CANDIDATES = [os.path.join(ROOT, ".env"), os.path.join(HERE, ".env")]

_loaded = False


def parse_line(line):
    """`.env` 한 줄을 (키, 값) 으로. 해석 못 하면 None."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].strip()
    if "=" not in line:
        return None
    key, _, val = line.partition("=")
    key = key.strip()
    if not key:
        return None
    val = val.strip()
    # 따옴표로 감싼 값은 벗긴다. 토큰에 공백이 들어가는 경우가 있다.
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        val = val[1:-1]
    return key, val


def load_env(verbose=False):
    """`.env` 를 읽어 환경변수로 올린다. 올린 키 목록을 돌려준다.

    한 번만 실제로 읽는다. 여러 모듈이 호출해도 안전하다.
    """
    global _loaded
    if _loaded:
        return []

    applied = []
    for path in CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            print(f"[env] {path} 를 읽지 못했습니다: {e}")
            continue

        for raw in lines:
            kv = parse_line(raw)
            if kv is None:
                continue
            key, val = kv
            # 시스템 환경변수가 우선이다. 덮어쓰지 않는다.
            if key in os.environ and os.environ[key] != "":
                continue
            if val == "":
                # 빈 값은 설정하지 않는다. `.env.example` 을 그대로 복사해
                # 두면 빈 값들이 들어와 "설정됨" 으로 오인된다.
                continue
            os.environ[key] = val
            applied.append(key)

        if verbose:
            print(f"[env] {path} 에서 {len(applied)}개 적용: {', '.join(applied)}")
        break   # 먼저 찾은 파일 하나만 쓴다

    _loaded = True
    return applied


def require(*keys):
    """없으면 무엇이 없는지 알려주고 중단한다.

    무인 운영에서 설정 누락은 조용히 넘기면 안 된다.
    """
    load_env()
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            "환경변수가 설정되지 않았습니다: " + ", ".join(missing) + "\n"
            f"  {os.path.join(ROOT, '.env')} 에 적거나 시스템 환경변수로 설정하세요.\n"
            f"  형식은 {os.path.join(ROOT, '.env.example')} 를 참고하세요."
        )
    return [os.environ[k] for k in keys]
