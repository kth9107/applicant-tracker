# 지원자 트래커 확인 가이드

이 문서는 프로젝트 루트(`/Users/ku/workspace/projects/applicant-tracker`)에서 실행하는 기준입니다.

## 1. DB 초기화

```bash
python3 scripts/init_db.py
```

`scripts/init_db.sql`을 읽어서 `db/applicants.db`에 테이블을 다시 만듭니다.
기존 DB 데이터는 초기화됩니다.

## 2. sqlite3로 직접 테이블 확인

```bash
sqlite3 db/applicants.db ".tables"
```

```bash
sqlite3 db/applicants.db ".schema"
```

```bash
sqlite3 db/applicants.db "SELECT * FROM companies;"
sqlite3 db/applicants.db "SELECT * FROM applicants;"
sqlite3 db/applicants.db "SELECT * FROM email_events;"
```

## 3. Python 확인 스크립트로 보기

테이블 수만 확인:

```bash
python3 scripts/check_db.py --mode counts
```

저장 데이터 확인:

```bash
python3 scripts/check_db.py --mode data
```

전체 확인:

```bash
python3 scripts/check_db.py --mode all
```

## 4. 샘플 메일 1개씩 실행

가상 지원자 10명만 새 DB에 차례대로 적용:

```bash
python3 scripts/run_samples_one_by_one.py
```

기본값은 Notion 동기화를 건너뜁니다.
가상 지원자 10명을 실제 Notion DB에도 생성/업데이트하려면 아래처럼 명시해야 합니다.

```bash
python3 scripts/run_samples_one_by_one.py --sync-notion
```

기존 샘플 3개와 가상 지원자 10명을 함께 적용:

```bash
python3 scripts/run_samples_one_by_one.py --include-base
```

기존 샘플 3개와 가상 지원자 10명을 Notion에도 함께 적용:

```bash
python3 scripts/run_samples_one_by_one.py --include-base --sync-notion
```

현재 DB를 초기화하지 않고 이어서 적용:

```bash
python3 scripts/run_samples_one_by_one.py --keep-db
```

각 실행 결과 JSON은 아래 폴더에 저장됩니다.

```text
reports/one_by_one/
```

## 5. 전체 하네스 테스트

```bash
python3 scripts/harness.py
```

하네스는 테스트 데이터 오염 방지를 위해 Notion 동기화를 하지 않습니다.

최신 리포트:

```text
reports/latest_harness_report.md
```

## 6. Notion 데이터 조회

Notion DB의 최근 페이지를 조회합니다.

```bash
python3 scripts/notion_check.py --limit 20
```

## 6-1. Notion 기준 SQLite 동기화

Notion DB에서 삭제/보관된 페이지와 연결된 SQLite 지원자를 찾아 확인합니다.
기본은 dry-run이라 실제 DB를 수정하지 않습니다.

```bash
python3 scripts/notion_sync.py
```

실제로 SQLite에서 삭제하고, Notion에 남아 있는 페이지 값을 기준으로 SQLite 주요 필드를 갱신하려면:

```bash
python3 scripts/notion_sync.py --apply
```

지원자가 0명이 된 회사까지 함께 삭제하려면:

```bash
python3 scripts/notion_sync.py --apply --cleanup-companies
```

## 자동 감사와 reports 정리

Discord에서 확인:

```text
!명령어: Discord에서 사용할 수 있는 명령어 요약
!감사: 최근 10명 SQLite/메시지/AI JSON/Notion 비교
!감사 20: 최근 20명 SQLite/메시지/AI JSON/Notion 비교
!전체감사: 전체 SQLite/Notion 비교 및 Notion 삭제 반영
!복구: SQLite 데이터를 Notion에 다시 생성
```

launchd 스케줄 설치:

```bash
python3 scripts/install_audit_scheduler.py --load
```

스케줄:

```text
12:00 최근 10명 감사
00:00 전체 DB/Notion 감사 및 Notion 삭제 반영
00:10 reports 용도별 최근 40개 초과 파일 삭제
```

삭제 전 미리보기:

```bash
python3 scripts/cleanup_reports.py --keep 40
```

## 6-2. Notion 추가 컬럼 동적 저장

Notion DB에 새 컬럼을 추가하면 AI 파싱 프롬프트가 현재 Notion 스키마를 읽어
고정 필드 외 컬럼을 `extra_properties` JSON으로 추출합니다.

현재 고정 컬럼:

```text
기업명, 포지션, 회사담당자, 이름, 생년, 나이, 희망연봉, 최종연봉, 기타
```

지원되는 추가 컬럼 타입:

```text
rich_text, number, select, multi_select, checkbox, url, email, phone_number, date
```

예를 들어 Notion에 `입사가능일` 컬럼을 추가하고 메시지에 해당 값이 있으면:

```json
{
  "extra_properties": {
    "입사가능일": "2026-07-01"
  }
}
```

고정 컬럼인 `희망연봉`, `최종연봉`은 각각 `salary_expected`, `salary_current`로 저장됩니다.
동적 컬럼은 SQLite의 `applicants.extra_properties` JSON으로 저장되고, Notion 동기화 때 현재 Notion 컬럼 타입에 맞춰 입력됩니다.

## 7. 메일 1개를 SQLite + Notion에 실제 저장

```bash
python3 scripts/store.py \
  --input-file scripts/sample_email_2.txt \
  --output-file scripts/store_result.json
```

Notion을 건너뛰고 SQLite만 저장하려면:

```bash
python3 scripts/store.py \
  --input-file scripts/sample_email_2.txt \
  --output-file scripts/store_result.json \
  --skip-notion
```

## 8. Discord에서 입력받아 저장

현재 Discord 입력 처리는 `scripts/discord_bot.py`가 담당합니다.
봇이 실행 중일 때 `.env`의 `DISCORD_INTAKE_CHANNEL_ID` 채널에 메일 본문을 입력하면 SQLite와 Notion에 저장합니다.

먼저 의존성을 설치합니다.

```bash
python3 -m pip install -r requirements.txt
```

또는 가상환경을 쓰는 경우:

```bash
./.venv/bin/python -m pip install -r requirements.txt
```

Discord Developer Portal에서 봇 설정도 확인해야 합니다.

- Bot > Privileged Gateway Intents > Message Content Intent: ON
- 봇 초대 권한: View Channels, Read Message History, Send Messages
- `.env`의 `DISCORD_BOT_TOKEN`: 봇 토큰
- `.env`의 `DISCORD_INTAKE_CHANNEL_ID`: 메일을 입력할 채널 ID

봇 실행:

```bash
python3 scripts/discord_bot.py
```

운영용 자동 재시작 서비스 등록:

```bash
python3 scripts/install_discord_bot_service.py --load
```

등록 후 Discord에서 `!재시작예약`을 입력하면 봇이 새 작업을 막고 진행 중 작업 완료 후 종료합니다. launchd가 자동으로 다시 시작하므로 터미널에서 별도 재시작 명령을 실행하지 않아도 됩니다.

서비스 해제:

```bash
python3 scripts/install_discord_bot_service.py --unload
```

가상환경 실행:

```bash
./.venv/bin/python scripts/discord_bot.py
```

Discord 채널에 입력 예시:

```text
!지원자
보낸사람: 박매니저 <pm@headhunt.kr>
받는사람: 최은하 <eh.choi@chemi-kor.co.kr>
제목: [추천] 케미콘일렉트로닉스코리아 영업 포지션 - 이영희

케미콘일렉트로닉스코리아 주식회사
최은하 담당자님,

이영희 (1995년생, 만 31세)
- 연세대학교 경영학과 졸업
- LG전자 영업기획팀 3년
- 영어, 일본어 가능
```

### Discord 처리 방식과 속도 설정

현재 기본값은 아래 흐름입니다.

```text
Discord 메시지 수신
→ 접수 완료 메시지 즉시 전송
→ 메시지 정리
→ Ollama에 원문 + ollama/skills/applicant-parser 스킬 전달
→ AI JSON 파싱
→ 파싱 진단 및 검토
→ 필수값 미달 시 feedback 포함 재파싱
→ SQLite 저장
→ Notion 동기화
→ Discord 완료/실패 답장
```

`.env` 주요 설정:

```text
DISCORD_AI_MODE=ai_first
DISCORD_USE_AI_EXTRACT=1
DISCORD_AI_RETRY_ON_INCOMPLETE=1
DISCORD_RULE_FALLBACK_ON_AI_ERROR=1
OLLAMA_TIMEOUT=90
AI_PROVIDER=ollama
AI_FALLBACK_PROVIDER=gemini
GEMINI_MODEL=gemini-2.5-flash
GEMINI_TIMEOUT=45
# GEMINI_API_KEY=
OLLAMA_NUM_PARALLEL=2
OLLAMA_MAX_LOADED_MODELS=1
OLLAMA_KEEP_ALIVE=-1
OLLAMA_FLASH_ATTENTION=1
AI_EXTRACT_CACHE_TTL_SECONDS=3600
DISCORD_MAX_PARALLEL=2
```

현재 기본 처리는 AI-first입니다.
Ollama 런타임 전용 스킬 파일은 아래 위치에 있습니다.

```text
ollama/skills/applicant-parser/SKILL.md
ollama/skills/applicant-parser/ollama_prompt_rules.md
```

AI를 끄고 rule 파싱만 쓰려면 `DISCORD_USE_AI_EXTRACT=0`으로 바꾸면 됩니다.

저장 실패 시 Discord 답장에는 아래 정보가 나옵니다.

```text
저장 실패
완료 정보: 지원자, 지원회사
부족 정보: 회사명, 출생년도, 스킬/능력
```

세부 디버그 로그는 아래 폴더에 저장됩니다. 일반 Discord 답장에는 내부 로그 ID를 표시하지 않습니다.

```text
reports/discord_messages/
```

`.txt` 파일을 첨부해도 처리합니다.

기존 지원자 업데이트 예시:

```text
!업데이트
케미콘 주식회사의 김태현 지원자님
지원직무는 백엔드 개발자로 업데이트
```

`!업데이트`는 기존 지원자만 수정합니다. 같은 이름이 여러 명이면 저장하지 않고
회사명, 생년월일, 만나이, 직무 같은 구분 정보를 추가로 요청합니다.
희망연봉은 Notion `희망연봉` 컬럼에 저장되고, 최종연봉/현재연봉/일반 연봉은 `최종연봉` 컬럼에 저장됩니다.

상태 변경 예시:

```text
!지원자
케미콘 주식회사의 김태현 지원자님
서류합격으로 변경
```

AI는 메시지 의도를 `create`, `update`, `status_update` 중 하나로 판단합니다.
AI 결과가 부족하면 규칙 기반 파서가 한 번 더 처리합니다.

Discord 입력은 기본적으로 Notion까지 동기화합니다.
Discord에서는 SQLite만 저장하고 싶으면 `.env`에 아래 값을 추가합니다.

```text
DISCORD_SYNC_NOTION=0
```

Discord 입력은 기본적으로 AI 추출을 먼저 사용합니다.
Ollama가 꺼져 있거나 모델 호출에 실패하면 운영 안정성을 위해 기존 규칙 기반 파서로 fallback할 수 있습니다.
이 fallback을 끄려면 `.env`에서 `DISCORD_RULE_FALLBACK_ON_AI_ERROR=0`으로 바꿉니다.
AI가 JSON을 만들었지만 필수값이 비어 있으면 rule fallback이 아니라 AI에 feedback을 넣어 재파싱합니다.

AI 추출을 끄고 규칙 기반 파서만 쓰려면 `.env`에 아래 값을 추가합니다.

```text
DISCORD_USE_AI_EXTRACT=0
```

Discord 사용법 확인:

```text
!사용법
```

처음 사용하는 사람에게 지원자 저장/수정/상태변경/문제점 등록 예시를 보여줍니다.
일반 메시지는 기본적으로 `!지원자`처럼 저장/수정/상태변경 파이프라인으로 들어가며, 다른 명령어는 저장 파이프라인으로 들어가지 않습니다.
저장 최소 필수값은 지원자 이름이며, 회사명이 없거나 출신 학교처럼 보이면 회사는 빈 값으로 둡니다.

현재 Discord 봇 상태 확인:

```text
!상태
```

정상 최신 버전이면 `bot_version: 2026-05-23-ai-json-v7`가 보여야 합니다.

현재 대기/진행 작업 확인:

```text
!대기
```

`!대기`는 아래 내용을 보여줍니다.

```text
진행 중 작업 수
대기 중 작업 수
사용 중인 병렬 슬롯
각 작업의 현재 단계, 경과시간, 지원자/회사/나이/출생년도
최근 완료 작업
```

작업 단계는 예를 들어 `AI 캐시 확인`, `AI 1차 파싱`, `파싱 진단`, `AI 재파싱`, `SQLite 저장 및 Notion 동기화`처럼 표시됩니다.

파싱 문제점 또는 개선 요청 등록:

```text
!문제점
SM엔터라고 본문에 있으면 제목의 케미콘보다 본문 회사명을 우선해줘
```

등록된 개선 요청은 아래 파일에 누적되고, 다음 AI 파싱부터 사용자 피드백 규칙으로 프롬프트에 포함됩니다.

```text
ollama/skills/applicant-parser/user_feedback_rules.md
```

Discord 메시지 처리 방식:

1. 메시지를 받으면 즉시 "지원자 메시지 접수 완료" 답장을 보냅니다.
2. AI 추출과 저장을 백그라운드에서 처리합니다.
3. 처리가 끝나면 접수 답장에 다시 댓글로 완료/실패 결과를 보냅니다.

동시 처리 개수는 `.env`에서 설정할 수 있습니다.

```text
DISCORD_MAX_PARALLEL=3
```

AI 호출 타임아웃과 캐시도 `.env`에서 설정합니다.

```text
OLLAMA_TIMEOUT=90
AI_EXTRACT_CACHE_TTL_SECONDS=3600
```

- `OLLAMA_TIMEOUT`: Ollama AI 추출을 최대 몇 초 기다릴지 정합니다.
- `AI_EXTRACT_CACHE_TTL_SECONDS`: 같은 원문+같은 모델+같은 프롬프트의 AI 추출 결과를 몇 초 동안 재사용할지 정합니다.
- 캐시 위치: `cache/ai_extract/`

같은 메시지를 다시 처리하면 캐시가 살아 있는 동안 Ollama를 다시 호출하지 않아 훨씬 빠릅니다.
단, 내용이 조금이라도 달라지면 다른 캐시로 봅니다.

### Ollama 병렬 설정

### AI fallback 설정

현재 권장 흐름:

```text
Ollama 시도
→ Ollama timeout/연결 오류/JSON 오류
→ GEMINI_API_KEY가 있으면 Gemini fallback 시도
→ Gemini도 실패하거나 키가 없으면 rule parser fallback
```

`.env` 예시:

```text
AI_PROVIDER=ollama
AI_FALLBACK_PROVIDER=gemini
OLLAMA_TIMEOUT=90
GEMINI_MODEL=gemini-2.5-flash
GEMINI_TIMEOUT=45
GEMINI_API_KEY=Google AI Studio에서 발급한 키
```

Gemini API는 Google AI Studio에서 무료 API key를 만들 수 있지만, 무료 tier는 RPM/TPM/RPD 제한이 있고 실제 한도는 프로젝트/모델/계정 상태에 따라 바뀔 수 있습니다.
그래서 운영 기본값은 Ollama를 1차로 두고, Gemini는 장애 fallback으로만 쓰는 것을 권장합니다.

`DISCORD_MAX_PARALLEL`은 봇이 동시에 몇 개의 메시지를 처리할지 정합니다.
`OLLAMA_NUM_PARALLEL`은 Ollama 서버가 한 모델에 대해 동시에 몇 개 요청을 처리할지 정합니다.

Mac Mini M4 16GB, `qwen3.5:4b` 기준 권장 시작값:

```text
DISCORD_MAX_PARALLEL=2
OLLAMA_NUM_PARALLEL=2
OLLAMA_MAX_LOADED_MODELS=1
OLLAMA_KEEP_ALIVE=-1
OLLAMA_FLASH_ATTENTION=1
```

중요: `.env`에 적은 `OLLAMA_NUM_PARALLEL` 값은 이 Python 봇 프로세스에는 보이지만,
이미 실행 중인 Ollama 앱/서버에는 자동 반영되지 않을 수 있습니다.
macOS에서 Ollama 앱에 영구 적용하려면 터미널에서 아래처럼 설정하고 Ollama를 재시작합니다.

```bash
launchctl setenv OLLAMA_NUM_PARALLEL 2
launchctl setenv OLLAMA_MAX_LOADED_MODELS 1
launchctl setenv OLLAMA_KEEP_ALIVE -1
launchctl setenv OLLAMA_FLASH_ATTENTION 1
osascript -e 'tell app "Ollama" to quit'
open -a Ollama
```

설정 후 봇도 재시작해야 `!상태`에서 `max_parallel` 값이 갱신됩니다.

병렬 처리 로컬 검증:

```bash
DISCORD_USE_AI_EXTRACT=0 python3 scripts/test_parallel_processing.py
```

Discord 메시지 처리 디버그 로그:

```text
reports/discord_messages/
```

각 메시지마다 원문, AI 추출 JSON, 파싱 진단, 재파싱 feedback, 저장 결과가 파일로 남습니다.
AI 캐시 사용 여부는 `*_ai_cache_status.json` 파일에서 확인합니다.

## 9. 하네스와 속도

하네스는 모델을 학습시키지 않습니다.
즉, 하네스를 많이 돌린다고 AI 모델 자체가 점점 똑똑해지거나 영구적으로 빨라지지는 않습니다.

대신 하네스는 아래 방식으로 전체 속도와 안정성에 간접적으로 도움을 줍니다.

- 실패 케이스를 테스트 데이터로 축적해 fallback 파서를 빠르게 개선합니다.
- 같은 입력은 AI 추출 캐시를 통해 재사용할 수 있습니다.
- Ollama 모델이 이미 메모리에 올라와 있으면 첫 호출보다 다음 호출이 빨라질 수 있습니다.
- 실패 후 재시도 시간을 줄여 전체 처리 시간을 줄입니다.

AI 추출만 단독 확인:

```bash
python3 scripts/extract.py scripts/test_data/sample_discord_kim_taehyun.txt
```

AI가 만든 JSON을 저장 프로그램에 직접 넣기:

```bash
python3 scripts/extract.py scripts/test_data/sample_discord_kim_taehyun.txt > /tmp/extracted.json

python3 scripts/store.py \
  --input-file scripts/test_data/sample_discord_kim_taehyun.txt \
  --input-json /tmp/extracted.json \
  --output-file /tmp/store_from_json.json
```

## Microsoft Teams 봇 병행 운영

Discord 봇은 그대로 유지하고, Teams 봇은 별도 프로세스로 실행합니다. 두 봇 모두 같은 `message_processor.py`와 SQLite/Notion 저장 계층을 사용합니다.

필수 `.env` 값:

```env
TEAMS_APP_ID=
TEAMS_APP_PASSWORD=
TEAMS_TENANT_ID=
TEAMS_BOT_PORT=5678
TEAMS_PUBLIC_ENDPOINT=https://your-ngrok-or-domain/api/messages
```

로컬 실행:

```bash
./.venv/bin/python scripts/teams_bot.py
```

헬스체크:

```bash
curl http://127.0.0.1:5678/health
```

ngrok 테스트 예시:

```bash
ngrok http 5678
```

ngrok 주소가 바뀌면 `.env`의 `TEAMS_PUBLIC_ENDPOINT`와 Azure Bot의 Messaging endpoint를 모두 `https://.../api/messages` 형식으로 갱신해야 합니다.

launchd 서비스 등록:

```bash
python3 scripts/install_teams_bot_service.py --load
```

서비스 해제:

```bash
python3 scripts/install_teams_bot_service.py --unload
```
