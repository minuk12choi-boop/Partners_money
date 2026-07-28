# 작업 목록

**순서대로 진행할 것.** 각 작업은 앞 작업이 실제로 동작하는 것을 확인한 뒤 시작한다.
전부 한꺼번에 고치려 하면 어디서 깨졌는지 알 수 없게 된다.

완료한 작업은 `- [x]`로 바꾸고, 실제로 관측한 값(윈도우 클래스명, DOM 셀렉터 등)을
해당 작업 아래 `관측:` 항목으로 기록해 둘 것. 다음 사람이 추측을 반복하지 않게 하기 위함이다.

---

## T0. 환경 구축

- [ ] `pip install -r requirements.txt`
- [ ] `playwright install chromium`
- [ ] `.env.example`를 참고해 환경변수 설정
- [ ] `python -c "import pywinauto, playwright, requests"` 통과

---

## T1. 카톡 내보내기 동작시키기 🔴 최우선

`src/kakao_export.py` — 완전 미검증. 여기가 파이프라인의 입구다.

```
python src/kakao_export.py --room "방이름일부"
```

**예상 실패 지점**
1. 채팅방 창을 못 찾음 → `CLS_MAIN` / `CLS_CHAT` 상수가 실제와 다름
2. 메인 창에서 방 열기 실패 → `open_chat_from_main()`의 `Ctrl+F` 검색 흐름이 현재 카톡과 다름
3. 저장 대화상자를 못 찾음 → 대화상자 제목/구조가 다름
4. 덮어쓰기 확인 대화상자 처리 실패

**먼저 관측부터 할 것.** 고치기 전에 아래를 실행해 실제 창 정보를 확인한다.

```python
from pywinauto import Desktop
for w in Desktop(backend="win32").windows():
    try:
        print(repr(w.window_text()), w.class_name())
    except Exception:
        pass
```

**완료 기준**
- 채팅방 창이 열려 있을 때 → `export.txt` 생성 및 갱신
- 채팅방 창이 닫혀 있을 때 → 자동으로 열어서 `export.txt` 생성
- 두 번 연속 실행해도 덮어쓰기 대화상자에서 멈추지 않음

**관측:**
> (여기에 실제 윈도우 클래스명과 대화상자 구조를 기록)

---

## T2. 파싱 정확도 맞추기

`src/kakao_deal_extract.py` — 샘플로는 동작하나 실제 방 포맷은 미확인.

```
python src/kakao_deal_extract.py export.txt --no-resolve
```

**할 일**
- 실제 `export.txt`로 돌려 `pending.csv`의 `title`이 상품명으로 제대로 잡히는지 확인
- 안 맞으면 `guess_title()`의 필터 규칙을 실제 메시지 포맷에 맞게 조정
- 가격이 여러 개 나올 때 `guess_price()`가 최솟값을 고르는데, 그 방에서 이게 맞는지 확인
  (원가와 할인가를 같이 쓰는 방이면 맞고, 아니면 로직 변경 필요)

**완료 기준**
- 실제 딜 메시지 10건 중 8건 이상에서 상품명과 가격이 정확히 추출됨
- 딜이 아닌 잡담 메시지는 하나도 잡히지 않음 (오탐 0)

**그다음 네트워크 해석 테스트** — `--no-resolve` 없이 실행

- 쿠팡이 봇 트래픽을 차단하는지 확인. `productId 확정 실패`가 반복되면
  `resolve_product_url()`을 `partners_link.py`의 Playwright 컨텍스트를 재사용하도록 변경
- 실패한 URL은 다음 실행에서 자동 재시도되게 이미 되어 있다. 이 동작을 깨지 말 것.

**관측:**
> (실제 메시지 포맷 샘플과 쿠팡 차단 여부)

---

## T3. 파트너스 링크 생성 동작시키기 🔴

`src/partners_link.py` — 완전 미검증. 단 **폐기 예정 코드이므로 최소한으로만 손댈 것.**

```
python src/partners_link.py --login      # 최초 1회, 수동 로그인
python src/partners_link.py --limit 1
```

이 스크립트는 셀렉터를 하드코딩하지 않고 자가탐색한다.
`discover()`가 입력창·버튼 후보를 점수순으로 조합해 시도하고,
성공한 조합을 `selectors.json`에 캐시한다.

**할 일**
- 자가탐색이 성공하면 → 로그에 찍힌 조합을 아래 `관측:`에 기록하고 끝
- 전부 실패하면 → 실제 DOM을 보고 `JS_SCAN_INPUTS` / `JS_SCAN_BUTTONS`의 점수 규칙 보정
- 결과 링크를 읽는 `JS_HARVEST`가 input의 `value` 속성을 JS로 읽는 이유:
  input 값은 `outerHTML`에 나타나지 않는다. 이 부분 건드리지 말 것.

**주의**
- 실패해도 재시도 루프를 돌리지 마라. 짧은 시간에 반복 접근하면 계정이 위험하다.
- 디버깅 중에는 `--limit 1`만 사용한다.

**완료 기준**
- 상품 URL 1건 → `link.coupang.com/...` 딥링크 생성 및 `deals.affiliate_url` 저장
- 생성된 링크를 브라우저에서 열어 정상 상품 페이지로 이동하는지 확인
- **파트너스 관리자 페이지에서 그 링크가 내 계정 링크로 조회되는지 확인** (트래킹 검증)

**관측:**
> (성공한 input 셀렉터와 버튼 텍스트)

---

## T4. Threads 인증 및 발행

`src/threads_post.py` — API 호출 경로 전체 미검증.

**할 일**
1. Meta 개발자 콘솔에서 앱 생성 → Threads API 제품 추가
2. 본인 Threads 계정을 테스터로 등록하고 초대 수락
3. 리디렉션 URI 등록, `threads_basic` + `threads_content_publish` 권한
4. OAuth 코드 → 단기 토큰 → 장기 토큰(60일) 교환
5. `GET /me?fields=id`로 `user_id` 조회
6. `token.json` 생성 (`.gitignore`에 있으니 커밋되지 않는다)

**만들 것**: `src/threads_auth.py`
- 인증 URL을 출력하고, 사용자가 붙여넣은 code를 받아 장기 토큰까지 교환해 `token.json` 저장
- 이 저장소에 아직 없다. 새로 작성할 것.

**검증 순서**
```
python src/threads_post.py --dry-run    # 문구만 확인
python src/threads_post.py              # 실제 발행 1건
```

**완료 기준**
- 실제로 스레드에 게시물이 올라감
- 게시물에 대가성 고지 문구가 포함되어 있음
- `deals.posted_at`이 채워지고 재실행 시 중복 발행되지 않음

**참고**: 개발 모드(development mode)에서 본인 계정만 쓰는 경우
앱 심사 없이 동작할 수 있다. 심사 신청 전에 먼저 시도해 볼 것.

---

## T5. 무인 운영 전환

- [ ] `python src/run_all.py --room "방이름" --once --dry-run` 통과
- [ ] `python src/run_all.py --room "방이름" --once` 통과
- [ ] 텔레그램 알림 설정 및 **실제로 알림이 오는지 테스트**
      (`TG_BOT_TOKEN`, `TG_CHAT_ID` 설정 후 일부러 실패시켜 확인)
- [ ] Windows 작업 스케줄러에 "시스템 시작 시 실행" 등록
- [ ] PC 재부팅 후 자동으로 살아나는지 확인
- [ ] 토큰 갱신 크론: 50일마다 `python src/threads_post.py --refresh-token`

**완료 기준**: 3일 연속 무개입으로 돌아가고, `run.log`에 치명적 오류가 없음

---

## T6. (승인 후) 공식 API로 전환

쿠팡 파트너스 최종승인 및 API 키 발급 이후.

- [ ] `src/partners_deeplink.py` 신규 작성 — HMAC 인증 + deeplink 엔드포인트 호출
- [ ] `run_all.py`에서 `partners_link.py` 호출을 이것으로 교체
- [ ] `partners_link.py`, `pw_profile/`, `selectors.json` 삭제
- [ ] `playwright` 의존성 제거

여기까지 오면 브라우저 자동화가 사라져 계정 리스크와 불안정성이 동시에 해소된다.
**이게 이 프로젝트의 최종 목표 상태다.**
