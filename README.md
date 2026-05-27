# 지원자 트래커

Discord 채널로 들어온 지원자 추천/업데이트 메시지를 AI와 규칙 기반 파서로 정리한 뒤 SQLite에 저장하고, 선택적으로 Notion 데이터베이스에 동기화하는 프로젝트입니다.

## 주요 흐름

```text
Discord 메시지 수신
-> 메시지 정리
-> AI 파싱(Ollama, Gemini fallback)
-> 파싱 진단 및 필요 시 재파싱
-> SQLite 저장
-> Notion 동기화
-> Discord 결과 답장
-> 하네스/샘플 검증
```

## 폴더 구조

```text
applicant-tracker/
├── .env
├── db/
│   └── applicants.db
├── ollama/
│   └── skills/applicant-parser/
├── reports/
├── sample/
├── scripts/
│   ├── discord_bot.py
│   ├── extract.py
│   ├── harness.py
│   ├── init_db.py
│   ├── init_db.sql
│   ├── message_processor.py
│   ├── notion_check.py
│   ├── notion_sync.py
│   ├── run_samples_one_by_one.py
│   └── store.py
└── requirements.txt
```

## 설치 방법

프로젝트 루트에서 실행합니다.

```bash
cd /Users/ku/workspace/projects/applicant-tracker
```

가상환경을 새로 만들 경우:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

이미 `.venv`가 있으면:

```bash
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

가상환경 없이 설치할 경우:

```bash
python3 -m pip install -r requirements.txt
```

## .env 설정

프로젝트 루트에 `.env` 파일을 만들고 아래 변수명을 넣습니다. 실제 키값은 README에 적지 말고 로컬 `.env`에만 입력합니다.

```text
## SQLite DB 파일 경로
SQLITE_PATH=

## Notion API 키
NOTION_TOKEN=

## Notion 지원자 데이터베이스 ID
NOTION_DB_ID=

## Discord 봇 토큰
DISCORD_BOT_TOKEN=

## Discord에서 지원자 메시지를 받을 채널 ID
DISCORD_INTAKE_CHANNEL_ID=

## Discord 저장 결과를 Notion까지 동기화할지 여부, 1이면 사용, 0이면 미사용
DISCORD_SYNC_NOTION=

## Discord 메시지 동시 처리 개수
DISCORD_MAX_PARALLEL=

## Discord 처리에서 AI 추출을 사용할지 여부, 1이면 사용, 0이면 rule parser만 사용
DISCORD_USE_AI_EXTRACT=

## Discord AI 처리 모드
DISCORD_AI_MODE=

## AI 결과가 부족할 때 재파싱을 시도할지 여부
DISCORD_AI_RETRY_ON_INCOMPLETE=

## AI 실패 시 rule parser fallback을 사용할지 여부
DISCORD_RULE_FALLBACK_ON_AI_ERROR=

## 1차 AI provider
AI_PROVIDER=

## fallback AI provider
AI_FALLBACK_PROVIDER=

## Ollama API URL
OLLAMA_URL=

## Ollama 모델명
OLLAMA_MODEL=

## Ollama 응답 대기 시간(초)
OLLAMA_TIMEOUT=

## Ollama 동시 요청 처리 개수, Ollama 서버 실행 환경에도 별도 적용 필요
OLLAMA_NUM_PARALLEL=

## Ollama 동시 로딩 모델 수
OLLAMA_MAX_LOADED_MODELS=

## Ollama 모델 메모리 유지 시간
OLLAMA_KEEP_ALIVE=

## Ollama flash attention 사용 여부
OLLAMA_FLASH_ATTENTION=

## Gemini API 키
GEMINI_API_KEY=

## Gemini 모델명
GEMINI_MODEL=

## Gemini 응답 대기 시간(초)
GEMINI_TIMEOUT=

## AI 추출 캐시 유지 시간(초)
AI_EXTRACT_CACHE_TTL_SECONDS=
```

권장 시작값은 아래와 같습니다. 실제 토큰과 ID는 직접 채워야 합니다.

```text
SQLITE_PATH=db/applicants.db
DISCORD_SYNC_NOTION=1
DISCORD_MAX_PARALLEL=2
DISCORD_USE_AI_EXTRACT=1
DISCORD_AI_MODE=ai_first
DISCORD_AI_RETRY_ON_INCOMPLETE=1
DISCORD_RULE_FALLBACK_ON_AI_ERROR=1
AI_PROVIDER=ollama
AI_FALLBACK_PROVIDER=gemini
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=qwen3:4b-instruct
OLLAMA_TIMEOUT=180
OLLAMA_NUM_PARALLEL=2
OLLAMA_MAX_LOADED_MODELS=1
OLLAMA_KEEP_ALIVE=-1
OLLAMA_FLASH_ATTENTION=1
GEMINI_MODEL=gemini-2.5-flash
GEMINI_TIMEOUT=45
AI_EXTRACT_CACHE_TTL_SECONDS=3600
```

## DB 초기화

아래 명령은 `scripts/init_db.sql`을 실행해서 `db/applicants.db`를 준비합니다.

```bash
python3 scripts/init_db.py
```

주의: 초기화 SQL 내용에 따라 기존 데이터가 지워질 수 있습니다. 운영 데이터가 있으면 먼저 백업합니다.

## SQLite 확인 방법

테이블 목록:

```bash
sqlite3 db/applicants.db ".tables"
```

스키마 확인:

```bash
sqlite3 db/applicants.db ".schema"
```

데이터 직접 확인:

```bash
sqlite3 db/applicants.db "SELECT * FROM companies;"
sqlite3 db/applicants.db "SELECT * FROM applicants;"
sqlite3 db/applicants.db "SELECT * FROM email_events;"
```

Python 확인 스크립트:

```bash
python3 scripts/check_db.py --mode counts
python3 scripts/check_db.py --mode data
python3 scripts/check_db.py --mode all
```

## Notion 설정

1. Notion Integration을 만들고 API 키를 발급합니다.
2. 지원자 데이터베이스 페이지에서 해당 Integration을 초대하거나 연결합니다.
3. `.env`에 `NOTION_TOKEN`, `NOTION_DB_ID`를 입력합니다.
4. 아래 명령으로 연결을 확인합니다.

```bash
python3 scripts/notion_check.py --limit 20
```

Notion 데이터베이스에 기본 컬럼이 부족하면 저장 시 자동으로 필요한 속성을 만들려고 시도합니다.
스키마를 먼저 맞추려면 아래 명령을 실행합니다.

```bash
python3 scripts/notion_schema.py --apply
```

기본으로 사용하는 Notion 컬럼:

```text
기업명
포지션
회사담당자
이름
생년
나이
희망연봉
최종연봉
기타
```

## Notion 추가 컬럼 동적 저장

Notion DB에 새 컬럼을 추가하면 AI 프롬프트가 현재 Notion 스키마를 읽고, 고정 필드 외 컬럼을 `extra_properties` JSON으로 추출합니다.

지원되는 추가 컬럼 타입:

```text
rich_text
number
select
multi_select
checkbox
url
email
phone_number
date
```

예를 들어 Notion에 `입사가능일` 컬럼을 추가하고 메시지에 해당 값이 있으면 SQLite에는 아래처럼 저장됩니다.

```json
{
  "입사가능일": "2026-07-01"
}
```

고정 컬럼인 `희망연봉`, `최종연봉`은 `extra_properties`가 아니라 각각 `salary_expected`, `salary_current`로 저장됩니다.
동적 컬럼 저장 위치는 `applicants.extra_properties`입니다. Notion 동기화 시 현재 Notion 컬럼 타입에 맞춰 입력됩니다.

## Notion 기준 SQLite 동기화

Notion에서 삭제되거나 보관된 페이지를 기준으로 SQLite 지원자 데이터를 정리할 수 있습니다.

먼저 dry-run으로 삭제 후보를 확인합니다.

```bash
python3 scripts/notion_sync.py
```

실제로 SQLite에 반영하려면:

```bash
python3 scripts/notion_sync.py --apply
```

지원자가 0명이 된 회사까지 정리하려면:

```bash
python3 scripts/notion_sync.py --apply --cleanup-companies
```

## Ollama 설정

Ollama를 1차 AI provider로 쓰려면 먼저 Ollama 앱 또는 서버가 실행 중이어야 합니다.

모델 다운로드 예시:

```bash
ollama pull qwen3:4b-instruct
```

모델 로딩 확인:

```bash
ollama ps
```

macOS에서 Ollama 병렬 설정을 영구 적용하려면:

```bash
launchctl setenv OLLAMA_NUM_PARALLEL 2
launchctl setenv OLLAMA_MAX_LOADED_MODELS 1
launchctl setenv OLLAMA_KEEP_ALIVE -1
launchctl setenv OLLAMA_FLASH_ATTENTION 1
```

그 다음 Ollama 앱을 재시작합니다.

```bash
osascript -e 'tell app "Ollama" to quit'
open -a Ollama
```

`OLLAMA_NUM_PARALLEL`은 Python `.env`에 적는 것만으로 이미 실행 중인 Ollama 앱에 자동 반영되지 않을 수 있습니다. Ollama 서버 환경에도 별도로 적용해야 합니다.

## Gemini fallback 설정

Ollama timeout, 연결 실패, JSON 오류가 발생하면 Gemini를 fallback으로 사용할 수 있습니다.

`.env` 예시:

```text
AI_PROVIDER=ollama
AI_FALLBACK_PROVIDER=gemini
GEMINI_MODEL=gemini-2.5-flash
GEMINI_API_KEY=
```

`GEMINI_API_KEY`는 Google AI Studio에서 발급한 API 키를 입력합니다.

## Discord 봇 설정

Discord Developer Portal에서 아래 설정을 확인합니다.

```text
Bot > Privileged Gateway Intents > Message Content Intent: ON
```

봇 초대 권한:

```text
View Channels
Read Message History
Send Messages
```

`.env`에 아래 값을 입력합니다.

```text
DISCORD_BOT_TOKEN=
DISCORD_INTAKE_CHANNEL_ID=
```

봇 실행:

```bash
python3 scripts/discord_bot.py
```

가상환경을 명시해서 실행:

```bash
./.venv/bin/python scripts/discord_bot.py
```

정상 실행되면 터미널에 봇 이름, 버전, 입력 채널, Notion 동기화 여부, 병렬 처리 개수가 출력됩니다.

운영 중 코드 변경을 반영할 때는 안전 재시작을 사용할 수 있습니다. 먼저 launchd 서비스로 등록합니다.

```bash
python3 scripts/install_discord_bot_service.py --load
```

이후 Discord에서 `!재시작예약`을 입력하면 새 작업을 막고, 진행 중 작업이 모두 끝난 뒤 봇이 종료됩니다. launchd가 등록되어 있으면 종료 후 자동으로 다시 시작됩니다. 수동으로 서비스를 내릴 때는 아래 명령을 사용합니다.

```bash
python3 scripts/install_discord_bot_service.py --unload
```

## Discord 사용 방법

일반 메시지는 기본적으로 `!지원자`처럼 저장/수정/상태변경 파이프라인으로 들어갑니다.
저장 최소 필수값은 지원자 이름입니다. 회사명이 없거나 출신 학교처럼 보이면 회사는 빈 값으로 둡니다.
명확히 구분하고 싶을 때는 `!지원자`로 시작하면 됩니다.
`!문제점`, `!상태`, `!대기`, `!사용법`은 저장 파이프라인으로 들어가지 않습니다.

사용법 확인:

```text
!사용법
```

지원자 등록:

```text
!지원자
케미콘일렉트로닉스코리아 주식회사
최은하 담당자님

김대호 1997년생 만 29세
레이타쿠대학교 졸업
일본어 능통
영업 포지션 지원
```

지원자 업데이트:

```text
!업데이트
케미콘 주식회사의 김태현 지원자님
지원직무는 백엔드 개발자로 업데이트
```

`!업데이트`는 기존 지원자만 수정합니다.
같은 이름이 여러 명이면 저장하지 않고 회사명, 생년월일, 만나이, 직무 같은 구분 정보를 추가로 요청합니다.
희망연봉은 Notion `희망연봉` 컬럼에 저장되고, 최종연봉/현재연봉/일반 연봉은 `최종연봉` 컬럼에 저장됩니다.

```text
!업데이트 김태현 케미콘 1991년생 백엔드 개발자 희망연봉 5500
```

상태 변경:

```text
!지원자
케미콘 주식회사의 김태현 지원자님
서류합격으로 변경
```

여러 명 추천:

```text
!지원자
SM엔터 이번 주 영업 포지션 후보자 3명 추천드립니다.

김대호 1997년생 만 29세
일본어 능통

이영희 1995년생 만 31세
영어, 일본어 가능

박철수 1990년생 만 36세
화학 산업 영업 경험
```

작업 상태 확인:

```text
!상태
```

대기/진행 작업 확인:

```text
!대기
```

`!대기`는 사용자에게 필요한 정보만 보여줍니다. 내부 `log_id`는 표시하지 않습니다.

파싱 문제점 또는 개선 요청 등록:

```text
!문제점
SM엔터라고 본문에 있으면 제목의 케미콘보다 본문 회사명을 우선해줘
```

한 줄로도 보낼 수 있습니다.

```text
!문제점 JLPT, 토익, 개발언어는 스킬에 넣어줘
```

등록된 내용은 아래 파일에 저장되고, 다음 AI 파싱 프롬프트에 사용자 피드백 규칙으로 함께 들어갑니다.

```text
ollama/skills/applicant-parser/user_feedback_rules.md
```

사용 가능한 명령어:

```text
!사용법, !도움말, !help
!지원자
!업데이트, !update
!상태, !status
!대기, !queue, !작업
!문제점, !피드백, !feedback
```

## 샘플 1개 저장

SQLite와 Notion에 저장:

```bash
python3 scripts/store.py \
  --input-file scripts/sample_email_2.txt \
  --output-file scripts/store_result.json
```

Notion을 건너뛰고 SQLite만 저장:

```bash
python3 scripts/store.py \
  --input-file scripts/sample_email_2.txt \
  --output-file scripts/store_result.json \
  --skip-notion
```

## 샘플 순차 테스트

기본 샘플과 가상 데이터를 차례로 실행합니다.

```bash
python3 scripts/run_samples_one_by_one.py
```

Notion까지 동기화:

```bash
python3 scripts/run_samples_one_by_one.py --sync-notion
```

기존 샘플 3개와 가상 지원자 10명을 함께 적용:

```bash
python3 scripts/run_samples_one_by_one.py --include-base --sync-notion
```

부하 테스트 파일만 실행:

```bash
python3 scripts/run_samples_one_by_one.py \
  --load-test-file sample/sample_email_ten.txt \
  --only-load-test \
  --use-ai \
  --sync-notion
```

현재 DB를 초기화하지 않고 이어서 실행:

```bash
python3 scripts/run_samples_one_by_one.py --keep-db
```

결과 JSON은 아래 폴더에 저장됩니다.

```text
reports/one_by_one/
```

## 전체 하네스 테스트

```bash
python3 scripts/harness.py
```

하네스는 저장 결과, DB 변화량, 삭제/중복 위험, 실패 케이스를 검토하고 리포트를 생성합니다.

최신 리포트:

```text
reports/latest_harness_report.md
```

하네스 실행별 리포트:

```text
reports/run_YYYYMMDD_HHMMSS.md
```

오래된 리포트는 최신 10개만 남기도록 정리됩니다.

## AI 추출 단독 확인

AI가 어떤 JSON을 만드는지 먼저 확인할 수 있습니다.

```bash
python3 scripts/extract.py scripts/test_data/sample_discord_kim_taehyun.txt
```

AI 결과를 저장 프로그램에 직접 넣는 예시:

```bash
python3 scripts/extract.py scripts/test_data/sample_discord_kim_taehyun.txt > /tmp/extracted.json

python3 scripts/store.py \
  --input-file scripts/test_data/sample_discord_kim_taehyun.txt \
  --input-json /tmp/extracted.json \
  --output-file /tmp/store_from_json.json
```

## 병렬 처리 확인

AI를 끄고 Discord 처리 병렬 구조만 빠르게 확인:

```bash
DISCORD_USE_AI_EXTRACT=0 python3 scripts/test_parallel_processing.py
```

운영 병렬 처리에 관련된 값:

```text
DISCORD_MAX_PARALLEL
OLLAMA_NUM_PARALLEL
OLLAMA_MAX_LOADED_MODELS
```

`DISCORD_MAX_PARALLEL`은 봇이 동시에 처리할 Discord 메시지 수입니다.
`OLLAMA_NUM_PARALLEL`은 Ollama 서버가 같은 모델 요청을 동시에 처리하는 수입니다.

Mac Mini 16GB 환경에서는 처음에는 2부터 시작하는 것을 권장합니다.

## 로그와 캐시

Discord 메시지 처리 로그:

```text
reports/discord_messages/
```

샘플 실행 결과:

```text
reports/one_by_one/
```

AI 추출 캐시:

```text
cache/ai_extract/
```

같은 원문, 같은 모델, 같은 프롬프트는 캐시 유지 시간 동안 AI를 다시 호출하지 않고 이전 결과를 재사용합니다.

## 자주 생기는 문제

### requests 모듈이 없다는 오류

```text
ModuleNotFoundError: No module named 'requests'
```

의존성을 설치합니다.

```bash
python3 -m pip install -r requirements.txt
```

가상환경을 쓰면:

```bash
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

### Discord 봇이 반응하지 않음

확인할 것:

```text
DISCORD_BOT_TOKEN 값이 현재 초대한 봇의 토큰인지
DISCORD_INTAKE_CHANNEL_ID가 실제 입력 채널 ID인지
Message Content Intent가 ON인지
봇이 서버와 채널에 초대되어 있는지
봇 권한에 Send Messages가 있는지
봇 프로세스를 재시작했는지
```

### Notion 동기화 실패

확인할 것:

```text
NOTION_TOKEN 값이 맞는지
NOTION_DB_ID 값이 맞는지
Notion DB에 Integration이 초대되어 있는지
python3 scripts/notion_check.py --limit 20 명령이 성공하는지
```

### Ollama timeout

확인할 것:

```text
Ollama 앱 또는 서버가 실행 중인지
ollama ps에서 모델이 로딩되어 있는지
OLLAMA_MODEL 값이 실제 모델명과 같은지
OLLAMA_TIMEOUT 값을 충분히 크게 잡았는지
DISCORD_MAX_PARALLEL과 OLLAMA_NUM_PARALLEL이 너무 크지 않은지
```

### sudo로 실행 후 DB 권한 오류

`sudo`로 실행하면 `db/applicants.db`가 root 소유가 될 수 있습니다.

권한 확인:

```bash
ls -al db
```

권한 복구:

```bash
sudo chown -R $(whoami):staff db
sudo chown -R $(whoami):staff scripts
```

## 운영 순서 요약

처음 설치:

```bash
cd /Users/ku/workspace/projects/applicant-tracker
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 scripts/init_db.py
python3 scripts/notion_check.py --limit 20
python3 scripts/discord_bot.py
```

테스트:

```bash
python3 scripts/run_samples_one_by_one.py --include-base
python3 scripts/harness.py
```

Notion까지 실제 테스트:

```bash
python3 scripts/run_samples_one_by_one.py --include-base --sync-notion
```

운영 중 확인:

```text
Discord: !상태
Discord: !대기
Discord: !명령어
Discord: !감사
Discord: !전체감사
Discord: !복구
```

## 자동 감사와 리포트 정리

`!감사`는 최근 10명의 SQLite/최근 메시지/AI JSON/Notion 값을 비교합니다.
`!전체감사`는 전체 SQLite/Notion 값을 비교하고, Notion에서 삭제된 row와 연결된 SQLite 지원자를 정리한 뒤 Notion 값을 DB에 갱신합니다.
`!복구`는 SQLite에는 남아 있지만 Notion에서 삭제되었거나 연결이 없는 지원자를 Notion에 다시 생성합니다.
리포트 정리는 용도별 최근 40개만 남기는 기준으로 동작합니다.

Discord 명령어 요약:

```text
!명령어
```

최근 감사 개수 지정:

```text
!감사
!감사 20
```

launchd 스케줄 설치:

```bash
python3 scripts/install_audit_scheduler.py --load
```

등록되는 작업:

```text
매일 12:00: 최근 10명 감사
매일 00:00: 전체 DB/Notion 감사 및 Notion 삭제 반영
매일 00:10: reports 폴더를 용도별 최근 40개만 남기고 정리
```

리포트 정리 미리보기:

```bash
python3 scripts/cleanup_reports.py --keep 40
```

리포트 정리 즉시 실행:

```bash
python3 scripts/cleanup_reports.py --apply --keep 40
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
