# 인수인계 — 2026-07-29 기준

새 세션에서 이어서 작업할 때 이 문서부터 읽는다.
`CLAUDE.md`(아키텍처·제약)와 `TASKS.md`(작업 순서·관측 기록)가 본체이고,
이 문서는 **지금 어디까지 됐고 다음에 뭘 해야 하는지**만 담는다.

작업 브랜치: `claude/partners-money-repo-v9jgbv`
PR: https://github.com/minuk12choi-boop/Partners_money/pull/1 (draft)

---

## 한눈에 보는 현재 상태

```
카톡방 ─→ 추출 ─→ deals.db ─┬─→ partners_link.py ─→ 쿠팡 딥링크 ─┐
  ✅        ✅        ✅      │        🔴 미검증                    ├─→ threads_post.py
                             │                                    │      🔴 미검증
쉐어링크 대시보드 ───────────┴─→ toss_link.py ────→ 토스 쉐어링크 ─┘
       ✅ 관측됨                   🔴 미검증
```

| 파일 | 상태 |
|---|---|
| `src/kakao_export.py` | ✅ **실제 환경 검증 완료** (T1) |
| `src/kakao_deal_extract.py` | ✅ 실제 export.txt 6,500줄로 검증 (T2 대부분) |
| `src/partners_link.py` | 🔴 **완전 미검증** — 다음 관문 |
| `src/toss_link.py` | 🔴 **미검증 (신규 작성)** — 다음 관문 |
| `src/threads_post.py` | 🟡 문구 생성만 검증. API 호출 경로 미검증 |
| `src/run_all.py` | 🟡 구조만 |
| `src/threads_auth.py` | ❌ **아직 없음.** TASKS.md T4 에서 작성해야 함 |

---

## 완료된 것

### T1 — 카톡 내보내기 ✅

소유자 Windows PC 에서 끝까지 동작 확인. `export.txt` 264,908 bytes 생성.
실측값과 함정은 `TASKS.md` 의 T1 `관측:` 항목들에 전부 기록돼 있다.

핵심만: 채팅방 창과 메인 창이 같은 `EVA_Window_Dblclk` 클래스이고,
Ctrl+S 이후 흐름은 3단계이며, 마지막 완료 알림은 **Win32 객체가 아니라
카톡이 직접 그린 인앱 모달**이라 채팅방 창에 포커스를 주고 Enter 를
보내야 닫힌다.

### T2 — 파싱 (대부분 완료)

실제 `export.txt` 로 검증. `TASKS.md` 의 T2 `관측:` 참고.

- 인코딩 UTF-8, 정규식 그대로 맞음 → 수정 불필요
- 상품명 추출 268건 전부 성공, 잡담 오탐 0건
- **가격 버그 수정**: `역대 최저가` 대신 할인액을 쓰고 있었다. 268건 중
  176건(66%)이 틀린 가격이었다
- **방장 트래킹 제거**: 해석된 URL 에 `src=1139000` 이 딸려 온다
- **토스 추출 추가**: `platform` 컬럼 + `(platform, product_id)` 복합키
- `threads_post.py` 가 `pending.csv` 대신 `deals.db` 를 읽도록 수정
  (이전에는 발행 대상이 영원히 0건이었다)
- 고지 문구를 플랫폼별로 분리. 토스는 문구도 다르고 **본문 첫 부분**에
  와야 한다

---

## 다음에 할 일 — 우선순위 순

### 1. 토스 링크 발급 검증 (`src/toss_link.py`)

```
py src\toss_link.py --login      # 최초 1회. 토스 비즈니스 계정
py src\toss_link.py --dry-run    # 발급 없이 대상 목록만
py src\toss_link.py --limit 1    # 실제 발급
```

**확인할 것**
- 상품 카드가 스캔되는가 (`상품 카드 N개 발견`)
- 상품명·가격·개당 수익이 제대로 읽히는가
- `링크 발급` 클릭 후 `toss.im/_m/...` 링크가 잡히는가
- 발급된 링크로 상품 ID 가 확인되는가

실패하면 `shots/toss_*.png` 를 보고 고친다.

### 2. 쿠팡 파트너스 링크 생성 검증 (`src/partners_link.py`)

```
py src\partners_link.py --login
py src\partners_link.py --limit 1
```

**주의**: CLAUDE.md 가 "폐기될 코드이므로 과투자 금지" 라고 못 박았다.
동작하는 수준까지만. 실패해도 재시도 루프를 돌리지 말 것.
디버깅 중에는 `--limit 1` 만 쓴다.

### 3. 딜 추출 실행

```
py src\kakao_deal_extract.py src\export.txt
```

쿠팡 264 + 토스 115건을 해석한다. 건당 1.2초 대기가 있어 10분 이상
걸린다. 기존 `deals.db` 는 자동으로 새 스키마로 이전된다.

### 4. T4 — Threads 인증 (`src/threads_auth.py` 신규 작성)

아직 저장소에 없다. TASKS.md T4 참고.
Meta 개발자 콘솔 앱 생성 → 테스터 등록 → OAuth → 장기 토큰 → `token.json`.

### 5. T5 전에 반드시 확인할 것

**채팅방 창이 닫혀 있을 때 자동으로 여는 경로가 미검증이다.**
방을 열어두고 운영하면 안 타지만, 카톡 재시작 후 방 창이 닫힌 상태로
시작되면 여기서 막힌다. 무인 운영 전환 전에 확인할 것.

---

## 미검증·미해결 목록

| 항목 | 내용 |
|---|---|
| `partners_link.py` | DOM 전체 미검증 |
| `toss_link.py` | 카드 스캔·발급 흐름 미검증 |
| `threads_post.py` | Threads API 호출 경로 전체 미검증 |
| `threads_auth.py` | 파일 자체가 없음 |
| 카톡 방 창 자동 열기 | 미검증 (T5 전 필수) |
| 완료 알림 클래스명 | Win32 객체가 아니라 관측 불가로 확정 |
| 쿠팡 봇 차단 | 403 을 주지만 리다이렉트로 productId 추출은 성공.<br>나중에 리다이렉트까지 막히면 조용히 실패한다 |
| 토스 수수료 10% | **9월 25일까지** 프로모션. 영구 요율 아님 |

---

## 절대 완화하면 안 되는 것

`CLAUDE.md` 의 "절대 완화하면 안 되는 제약" 5가지를 그대로 따른다.
특히 이번 작업에서 추가된 것:

- **고지 문구는 플랫폼마다 다르고 위치도 다르다.** 쿠팡은 본문 끝,
  토스는 본문 첫 부분. 섞어 쓰면 양쪽 다 위반이다.
  `build_text()` 의 `assert` 3개를 제거하지 말 것.
- **`toss_link.py` 도 한 번에 4건, 사이 6초** 를 지킨다.
  쿠팡과 같은 이유다.
- `headless=False` 유지.

---

## 작업 방식

`TASKS.md` 상단에 적힌 규칙을 그대로 따른다.

- **고치기 전에 관측부터.** 추측으로 덮지 말 것.
  관측 도구가 `tools/observe_kakao.py`, `tools/observe_toss.py` 에 있다.
- 관측한 실제 값은 `TASKS.md` 의 해당 작업 아래 `관측:` 에 기록한다.
- 작업 단위로 자주 커밋하고 push 한다. 커밋 메시지는 한국어로,
  무엇을 왜 고쳤는지 적는다.
- 코드로 알아낼 수 없는 것(계정 상태, 승인 진행 상황 등)은 추측하지
  말고 소유자에게 묻는다.
