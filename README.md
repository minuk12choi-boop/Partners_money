# Partners_money

카카오톡 딜 오픈채팅방 → 쿠팡 파트너스 링크 → Threads 자동 발행 파이프라인.

Windows 전용. 24시간 켜둔 PC에서 무인 운영하는 것을 전제로 한다.

## 빠른 시작

```bash
pip install -r requirements.txt
playwright install chromium

python src/partners_link.py --login          # 최초 1회 수동 로그인
python src/run_all.py --room "방이름" --once --dry-run
```

## 구성

| 파일 | 역할 |
|---|---|
| `src/kakao_export.py` | PC 카톡 창에 Ctrl+S를 보내 대화를 txt로 내보냄 |
| `src/kakao_deal_extract.py` | txt 파싱, 단축링크 해석, 중복 제거 → `deals.db` |
| `src/partners_link.py` | 본인 파트너스 세션으로 딥링크 생성 (임시. Phase 1에서 폐기) |
| `src/threads_post.py` | 문구 생성 및 Threads 발행 |
| `src/run_all.py` | 위 4단계를 45분 주기로 반복 |

## 문서

- **[CLAUDE.md](CLAUDE.md)** — 아키텍처, 검증 상태, 절대 완화 금지 제약
- **[TASKS.md](TASKS.md)** — 순서대로 진행할 작업 목록

## 경고

이 저장소의 코드는 실제 Windows / 카카오톡 / 쿠팡 파트너스 / Threads API 환경에서
아직 검증되지 않았다. 문법과 파싱 로직만 확인된 상태다.
`CLAUDE.md`의 검증 상태 표를 먼저 읽을 것.
