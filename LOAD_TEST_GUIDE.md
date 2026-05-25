# Load Test Guide

이 문서는 `sample/sample_email_ten.txt`에 들어 있는 10개 부하 테스트 메일을 한 건씩 저장하면서 SQLite/Notion 결과를 확인하는 실행 페이지입니다.

## 데이터셋

- 원본 파일: `sample/sample_email_ten.txt`
- 자동 분리 위치: `scripts/test_data/load_test/`
- 결과 JSON 위치: `reports/one_by_one/`
- 메일 구분자: `---EMAIL_START---`, `---EMAIL_END---`
- 기대 후보자 수: 각 블록의 `EXPECTED_CANDIDATES`

## 추천 실행

Discord 봇과 같은 AI-first 흐름으로 load test 10개만 실행합니다.
`python3`로 실행해도 프로젝트의 `.venv/bin/python`이 있으면 자동으로 가상환경 Python으로 재실행됩니다.

```bash
python3 scripts/run_samples_one_by_one.py \
  --load-test-file sample/sample_email_ten.txt \
  --only-load-test \
  --use-ai \
  --sync-notion
```

## Notion 없이 먼저 검증

Notion 페이지를 만들지 않고 SQLite 저장과 파싱 결과만 확인합니다.

```bash
python3 scripts/run_samples_one_by_one.py \
  --load-test-file sample/sample_email_ten.txt \
  --only-load-test \
  --use-ai
```

## 기존 기본 샘플까지 함께 실행

기본 샘플과 기존 10개 가상 지원자, load test 10개를 모두 실행합니다.

```bash
python3 scripts/run_samples_one_by_one.py \
  --include-base \
  --load-test-file sample/sample_email_ten.txt \
  --use-ai \
  --sync-notion
```

## 현재 DB에 이어서 실행

기본 실행은 DB를 초기화합니다. 현재 DB를 유지하고 추가 테스트만 하려면 `--keep-db`를 붙입니다.

```bash
python3 scripts/run_samples_one_by_one.py \
  --load-test-file sample/sample_email_ten.txt \
  --only-load-test \
  --use-ai \
  --sync-notion \
  --keep-db
```

## 확인 포인트

- `status: ok`인지 확인합니다.
- `batch : stored=N / total=N`이 기대 후보자 수와 맞는지 확인합니다.
- `delta.email_events`가 기대 후보자 수만큼 증가하는지 확인합니다.
- Notion을 켠 경우 `notion.synced=True`인지 확인합니다.
- `reports/one_by_one/load_001.json` 같은 결과 파일에서 상세 파싱 결과를 확인합니다.

## 주의

- `--sync-notion`은 실제 Notion DB에 페이지를 생성하거나 업데이트합니다.
- `--keep-db`를 붙이지 않으면 실행 시작 시 SQLite DB가 초기화됩니다.
- AI-first 테스트는 Ollama 또는 Gemini fallback을 사용하므로 로컬 Ollama 실행 상태와 `.env`의 Gemini 키 설정에 영향을 받습니다.
