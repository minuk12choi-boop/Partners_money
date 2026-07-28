#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
threads_auth.py — Threads API 토큰 발급

인증 URL 을 출력하고, 브라우저에서 승인한 뒤 돌아온 주소를 붙여넣으면
장기 토큰(60일)까지 교환해 `token.json` 에 저장한다.

`threads_post.py` 의 `load_token()` 이 읽는 형식에 맞춘다.

    {"user_id": "...", "access_token": "...",
     "refreshed_at": "...", "expires_in": 5183944}

────────────────────────────────────────────────────────────────
먼저 Meta 개발자 콘솔에서 해야 하는 것 (사람이 해야 한다)

  1. https://developers.facebook.com 에서 앱 생성
  2. 앱에 'Threads API' 제품 추가
  3. 본인 Threads 계정을 테스터로 등록하고, Threads 앱에서 초대 수락
     (https://www.threads.net/settings/account → 웹사이트 권한)
  4. 리디렉션 URI 를 등록한다. HTTPS 여야 한다.
     로컬 서버를 띄우지 않으므로 실제로 열리지 않아도 된다.
     주소창의 URL 만 복사해 쓸 것이므로 `https://localhost/` 로 충분하다.
  5. 앱 ID 와 앱 시크릿을 `.env` 에 적는다

        THREADS_APP_ID=...
        THREADS_APP_SECRET=...
        THREADS_REDIRECT_URI=https://localhost/

  ※ 개발 모드에서 본인 계정만 쓰면 앱 심사 없이 동작할 수 있다.
    심사 신청 전에 먼저 시도해 볼 것.
────────────────────────────────────────────────────────────────

사용법

    py src/threads_auth.py            # 인증 URL 출력 → 붙여넣기 → 저장
    py src/threads_auth.py --check    # 지금 토큰이 살아 있는지만 확인

의존성: requests (requirements.txt 에 이미 있음)
"""

import argparse
import json
import os
import sys
import urllib.parse
from datetime import datetime, timedelta

import requests

from env import load_env

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(HERE, "token.json")

# 공식 문서로 확인한 엔드포인트 (2026-07-29)
#   https://developers.facebook.com/docs/threads/get-started/get-access-tokens-and-permissions
AUTH_URL = "https://threads.net/oauth/authorize"
TOKEN_URL = "https://graph.threads.net/oauth/access_token"        # code -> 단기(1시간)
EXCHANGE_URL = "https://graph.threads.net/access_token"           # 단기 -> 장기(60일)
GRAPH = "https://graph.threads.net/v1.0"

# 글을 쓰려면 두 개가 필요하다. threads_basic 은 필수다.
SCOPES = ["threads_basic", "threads_content_publish"]


def log(msg=""):
    print(msg, flush=True)


# ---------------------------------------------------------------- 유틸

def extract_code(pasted):
    """붙여넣은 값에서 인증 코드만 뽑는다.

    사용자는 보통 주소창을 통째로 복사한다. 코드만 복사하는 경우도 받는다.

    ⚠️ Threads 는 코드 끝에 `#_` 를 붙여서 돌려준다. 공식 문서가 명시한
    동작이고 코드의 일부가 아니다. 안 떼면 교환이 실패한다.
    """
    s = (pasted or "").strip().strip('"').strip("'")
    if not s:
        return None

    if s.startswith("http://") or s.startswith("https://"):
        parsed = urllib.parse.urlparse(s)
        qs = urllib.parse.parse_qs(parsed.query)
        if "error" in qs:
            log(f"\n승인이 거부되었습니다: {qs.get('error_description', qs['error'])}")
            return None
        vals = qs.get("code")
        if not vals:
            return None
        s = vals[0]

    # parse_qs 는 프래그먼트를 떼어 주지만, 코드만 붙여넣은 경우엔 남아 있다.
    if "#" in s:
        s = s.split("#", 1)[0]
    return s.strip() or None


def save_token(user_id, access_token, expires_in):
    data = {
        "user_id": str(user_id),
        "access_token": access_token,
        "refreshed_at": datetime.now().isoformat(timespec="seconds"),
        "expires_in": expires_in,
    }
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def api_error(r):
    """응답에서 사람이 읽을 만한 오류 메시지를 뽑는다."""
    try:
        j = r.json()
    except Exception:
        return f"HTTP {r.status_code} {r.text[:300]}"
    err = j.get("error")
    if isinstance(err, dict):
        return (f"HTTP {r.status_code} {err.get('type', '')} "
                f"{err.get('code', '')}: {err.get('message', '')}")
    return f"HTTP {r.status_code} {json.dumps(j, ensure_ascii=False)[:300]}"


# ---------------------------------------------------------------- 흐름

def build_auth_url(app_id, redirect_uri):
    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "scope": ",".join(SCOPES),
        "response_type": "code",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def exchange_code(app_id, app_secret, redirect_uri, code):
    """인증 코드 → 단기 토큰(1시간). 응답에 user_id 가 함께 온다."""
    r = requests.post(TOKEN_URL, data={
        "client_id": app_id,
        "client_secret": app_secret,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "code": code,
    }, timeout=30)
    if r.status_code != 200:
        raise SystemExit(
            "단기 토큰 교환 실패: " + api_error(r) + "\n"
            "  흔한 원인:\n"
            "   - 리디렉션 URI 가 콘솔에 등록한 값과 한 글자라도 다르다\n"
            "     (끝의 슬래시까지 정확히 같아야 한다)\n"
            "   - 코드가 이미 쓰였거나 만료됐다. 인증 URL 부터 다시 하세요\n"
            "   - 앱 시크릿이 틀렸다"
        )
    j = r.json()
    return j["access_token"], j.get("user_id")


def exchange_long_lived(app_secret, short_token):
    """단기 → 장기(60일)."""
    r = requests.get(EXCHANGE_URL, params={
        "grant_type": "th_exchange_token",
        "client_secret": app_secret,
        "access_token": short_token,
    }, timeout=30)
    if r.status_code != 200:
        raise SystemExit("장기 토큰 교환 실패: " + api_error(r))
    j = r.json()
    return j["access_token"], j.get("expires_in")


def fetch_me(token):
    """토큰이 실제로 동작하는지 확인하고 user_id 를 확정한다.

    단기 토큰 응답에도 user_id 가 오지만, 실제로 Graph 를 한 번 찔러 봐야
    '토큰이 있다' 와 '토큰이 쓸모 있다' 를 구분할 수 있다.
    """
    r = requests.get(f"{GRAPH}/me",
                     params={"fields": "id,username", "access_token": token},
                     timeout=20)
    if r.status_code != 200:
        raise SystemExit(
            "토큰으로 /me 조회 실패: " + api_error(r) + "\n"
            "  본인 Threads 계정이 이 앱의 테스터로 등록되어 있고\n"
            "  Threads 앱에서 초대를 수락했는지 확인하세요."
        )
    j = r.json()
    return str(j["id"]), j.get("username")


def check_publish_limit(user_id, token):
    """발행 한도를 조회해 본다. threads_content_publish 권한 확인도 겸한다."""
    r = requests.get(f"{GRAPH}/{user_id}/threads_publishing_limit",
                     params={"fields": "quota_usage,config", "access_token": token},
                     timeout=20)
    if r.status_code != 200:
        return None, api_error(r)
    try:
        d = r.json()["data"][0]
        return (d.get("quota_usage", 0),
                d.get("config", {}).get("quota_total", 250)), None
    except Exception as e:
        return None, f"응답 해석 실패: {e}"


# ---------------------------------------------------------------- 모드

def do_check():
    if not os.path.exists(TOKEN_PATH):
        log(f"토큰 파일이 없습니다: {TOKEN_PATH}")
        log("먼저 `py src/threads_auth.py` 를 실행하세요.")
        return False

    with open(TOKEN_PATH, encoding="utf-8") as f:
        data = json.load(f)
    token = data.get("access_token")
    uid = data.get("user_id")
    if not token or not uid:
        log("token.json 에 access_token / user_id 가 없습니다. 다시 발급하세요.")
        return False

    refreshed = data.get("refreshed_at")
    expires_in = data.get("expires_in")
    if refreshed and expires_in:
        try:
            exp = datetime.fromisoformat(refreshed) + timedelta(seconds=int(expires_in))
            left = exp - datetime.now()
            log(f"토큰 만료: {exp:%Y-%m-%d %H:%M} (남은 {left.days}일)")
            if left.days <= 10:
                log("⚠️ 곧 만료됩니다. `py src/threads_post.py --refresh-token` 을 돌리세요.")
        except Exception:
            pass

    real_uid, username = fetch_me(token)
    log(f"✅ 토큰 정상. user_id={real_uid} username={username}")
    if real_uid != str(uid):
        log(f"⚠️ token.json 의 user_id({uid})가 실제({real_uid})와 다릅니다. 고쳐 둡니다.")
        save_token(real_uid, token, expires_in)

    quota, err = check_publish_limit(real_uid, token)
    if quota:
        log(f"✅ 발행 권한 확인. 사용량 {quota[0]}/{quota[1]}")
    else:
        log(f"⚠️ 발행 한도 조회 실패: {err}")
        log("   threads_content_publish 권한이 없을 수 있습니다.")
    return True


def do_auth():
    load_env()
    app_id = os.environ.get("THREADS_APP_ID")
    app_secret = os.environ.get("THREADS_APP_SECRET")
    redirect_uri = os.environ.get("THREADS_REDIRECT_URI", "https://localhost/")

    missing = [k for k, v in (("THREADS_APP_ID", app_id),
                              ("THREADS_APP_SECRET", app_secret)) if not v]
    if missing:
        log("설정이 없습니다: " + ", ".join(missing))
        log("")
        log("Meta 개발자 콘솔에서 앱을 만든 뒤 저장소 루트의 `.env` 에 적으세요:")
        log("")
        log("    THREADS_APP_ID=...")
        log("    THREADS_APP_SECRET=...")
        log("    THREADS_REDIRECT_URI=https://localhost/")
        log("")
        log("콘솔에서 할 일은 이 파일 맨 위 주석에 정리해 두었습니다.")
        return False

    url = build_auth_url(app_id, redirect_uri)
    log("=" * 70)
    log("  1단계 — 아래 주소를 브라우저에 붙여넣고 승인하세요")
    log("=" * 70)
    log("")
    log(url)
    log("")
    log(f"승인하면 {redirect_uri} 로 이동합니다.")
    log("그 페이지는 열리지 않아도 정상입니다. 로컬 서버가 없기 때문입니다.")
    log("**브라우저 주소창의 주소를 통째로 복사**해서 아래에 붙여넣으세요.")
    log("")
    log("=" * 70)
    log("  2단계 — 돌아온 주소를 붙여넣고 Enter")
    log("=" * 70)
    try:
        pasted = input("> ")
    except EOFError:
        log("대화형 콘솔에서 실행하세요.")
        return False

    code = extract_code(pasted)
    if not code:
        log("코드를 찾지 못했습니다. 주소를 통째로 붙여넣었는지 확인하세요.")
        log("(주소에 `?code=` 가 들어 있어야 합니다)")
        return False
    log(f"코드 확인: {code[:12]}… (길이 {len(code)})")

    log("")
    log("단기 토큰 교환 중...")
    short_token, uid_hint = exchange_code(app_id, app_secret, redirect_uri, code)
    log(f"  단기 토큰 확보 (user_id 힌트 {uid_hint})")

    log("장기 토큰(60일) 교환 중...")
    long_token, expires_in = exchange_long_lived(app_secret, short_token)
    log(f"  장기 토큰 확보. 만료까지 {int(expires_in or 0) // 86400}일")

    log("토큰으로 계정 조회 중...")
    user_id, username = fetch_me(long_token)
    log(f"  user_id={user_id} username={username}")

    save_token(user_id, long_token, expires_in)
    log("")
    log(f"✅ 저장 완료 → {TOKEN_PATH}")
    log("   (.gitignore 에 있으므로 커밋되지 않습니다. 남에게 주지 마세요.)")

    quota, err = check_publish_limit(user_id, long_token)
    if quota:
        log(f"✅ 발행 권한 확인. 사용량 {quota[0]}/{quota[1]}")
    else:
        log(f"⚠️ 발행 한도 조회 실패: {err}")
        log("   threads_content_publish 권한이 승인되지 않았을 수 있습니다.")

    log("")
    log("다음:")
    log("    py src/threads_post.py --dry-run    # 문구 확인")
    log("    py src/threads_post.py              # 실제 발행")
    log("")
    log("장기 토큰은 60일 만료입니다. 50일쯤에 아래를 돌리세요(T5 에서 자동화).")
    log("    py src/threads_post.py --refresh-token")
    return True


def main():
    ap = argparse.ArgumentParser(description="Threads API 토큰 발급")
    ap.add_argument("--check", action="store_true",
                    help="지금 token.json 이 살아 있는지만 확인")
    args = ap.parse_args()

    ok = do_check() if args.check else do_auth()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
