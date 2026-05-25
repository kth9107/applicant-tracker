# 지원자 저장 파이프라인을 샘플 데이터로 검증하는 하네스.
#
# 하네스는 운영용 Discord/Notion 입력과 분리된 로컬 회귀 테스트 도구다.
# 샘플 메일과 가상 Discord 입력을 차례대로 `store.py`에 넣고, SQLite 데이터가
# 예상대로 증가하는지 검증한 뒤 Markdown 리포트를 만든다.
#
# 주의:
# - 기본값은 `--skip-notion`으로 실행되어 Notion 실제 DB를 오염시키지 않는다.
# - 실행 시작 시 `init_database()`로 로컬 SQLite DB를 초기화한다.
# - `reports/run_*.md`는 최신 10개만 남기고 오래된 리포트는 삭제한다.

import datetime
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from create_db import DB_PATH, get_connection
from init_db import init_database


BASE_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = BASE_DIR / "scripts"
REPORTS_DIR = BASE_DIR / "reports"
STORE_PATH = SCRIPTS_DIR / "store.py"

SAMPLE_EMAIL_FILES = [
    SCRIPTS_DIR / "sample_email.txt",
    SCRIPTS_DIR / "sample_email_2.txt",
    SCRIPTS_DIR / "sample_email_3.txt",
    BASE_DIR / "sample" / "03_rich" / "01_three_candidates.txt",
]
TEST_DATA_DIR = SCRIPTS_DIR / "test_data"
LATEST_REPORT_PATH = REPORTS_DIR / "latest_harness_report.md"
MAX_REPORT_RUNS = 10
SYNC_NOTION_IN_HARNESS = False


def now_text() -> str:
    # 리포트에 표시할 현재 시각 문자열을 만든다.
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def make_run_id() -> str:
    # 이번 하네스 실행을 식별하는 run id를 만든다.
    return datetime.datetime.now().strftime("run_%Y%m%d_%H%M%S")


def hash_text(text: str) -> str:
    # 실패 케이스 중복 관리를 위해 원문 해시를 만든다.
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def find_sample_files() -> list[Path]:
    # 하네스에 사용할 기본 샘플, 가상 지원자, Discord 샘플 파일을 모은다.
    built_in_files = [path for path in SAMPLE_EMAIL_FILES if path.exists()]
    virtual_files = sorted(TEST_DATA_DIR.glob("sample_email_10_*.txt"))
    discord_files = sorted(TEST_DATA_DIR.glob("sample_discord_*.txt"))
    return built_in_files + virtual_files + discord_files


def get_db_counts() -> dict[str, int]:
    # 주요 테이블의 현재 레코드 수를 조회한다.
    conn = get_connection(DB_PATH)
    try:
        counts = {}
        for table_name in ["companies", "applicants", "email_events"]:
            cursor = conn.execute(f"SELECT COUNT(*) AS count FROM {table_name}")
            counts[table_name] = int(cursor.fetchone()["count"])
        return counts
    finally:
        conn.close()


def get_db_snapshot() -> dict:
    # 리포트에 넣을 회사/지원자/event 전체 스냅샷을 만든다.
    conn = get_connection(DB_PATH)
    try:
        companies = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, name, contact_person, contact_email, notion_page_id
                FROM companies
                ORDER BY id
                """
            )
        ]
        applicants = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, company_id, name, birth_year, age, age_international,
                       age_korean, position, status, notion_page_id
                FROM applicants
                ORDER BY id
                """
            )
        ]
        events = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, applicant_id, company_id, event_type, status
                FROM email_events
                ORDER BY id
                """
            )
        ]
        notion_page_count = len([
            row for row in applicants
            if row.get("notion_page_id")
        ])

        return {
            "counts": {
                "companies": len(companies),
                "applicants": len(applicants),
                "email_events": len(events),
                "notion_page_ids": notion_page_count,
            },
            "companies": companies,
            "applicants": applicants,
            "email_events": events,
            "notion_status": {
                "enabled_in_current_store_py": True,
                "enabled_in_harness": SYNC_NOTION_IN_HARNESS,
                "message": "하네스는 테스트 데이터 오염 방지를 위해 기본적으로 --skip-notion으로 실행합니다.",
            },
        }
    finally:
        conn.close()


def validate_mutation_safety(
    before_counts: dict[str, int],
    after_counts: dict[str, int],
    store_result: dict,
) -> list[dict]:
    # 저장 과정에서 데이터가 삭제되거나 event가 비정상 증가했는지 검사한다.
    errors = []

    for table_name, before_count in before_counts.items():
        after_count = after_counts.get(table_name, 0)
        if after_count < before_count:
            errors.append({
                "code": "TABLE_COUNT_DECREASED",
                "message": (
                    f"{table_name} 레코드 수가 감소했습니다. "
                    f"before={before_count}, after={after_count}"
                ),
            })

    output_json = store_result.get("output_json") or {}
    if output_json.get("status") == "ok":
        event_delta = after_counts["email_events"] - before_counts["email_events"]
        expected_delta = int(output_json.get("stored_count") or 1)
        if event_delta != expected_delta:
            errors.append({
                "code": "EMAIL_EVENT_DELTA_INVALID",
                "message": (
                    "성공 케이스는 저장된 지원자 수만큼 email_events가 증가해야 합니다. "
                    f"expected={expected_delta}, delta={event_delta}"
                ),
            })

    return errors


def run_store_for_file(sample_file: Path, output_file: Path) -> dict:
    # 샘플 파일 1개를 `store.py` CLI로 실행하고 결과 JSON을 읽어온다.
    command = [
        sys.executable,
        str(STORE_PATH),
        "--input-file",
        str(sample_file),
        "--output-file",
        str(output_file),
    ]
    if not SYNC_NOTION_IN_HARNESS:
        command.append("--skip-notion")

    proc = subprocess.run(
        command,
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
        timeout=60,
    )

    output_json = {}
    if output_file.exists():
        try:
            output_json = json.loads(output_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            output_json = {}

    return {
        "command": command,
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "output_json": output_json,
    }


def validate_store_result(result: dict) -> list[dict]:
    # `store.py` 실행 결과가 저장 성공으로 볼 수 있는지 검증한다.
    errors = []
    output_json = result.get("output_json") or {}

    if result.get("exit_code") != 0:
        errors.append({
            "code": "NON_ZERO_EXIT",
            "message": f"store.py가 비정상 종료되었습니다. exit_code={result.get('exit_code')}",
        })

    if not output_json:
        errors.append({
            "code": "NO_JSON_OUTPUT",
            "message": "store.py 출력 JSON을 읽지 못했습니다.",
        })
        return errors

    if output_json.get("status") != "ok":
        errors.append({
            "code": "STATUS_NOT_OK",
            "message": f"status가 ok가 아닙니다. status={output_json.get('status')}",
        })

    if output_json.get("batch"):
        results = output_json.get("results") or []
        if not results:
            errors.append({
                "code": "EMPTY_BATCH_RESULTS",
                "message": "batch 응답인데 results가 비어 있습니다.",
            })
            return errors

        for index, item in enumerate(results, start=1):
            if item.get("status") != "ok":
                errors.append({
                    "code": "BATCH_ITEM_NOT_OK",
                    "message": f"batch {index}번째 저장 결과가 ok가 아닙니다.",
                })
                continue
            item_parsed = item.get("parsed") or {}
            if item_parsed.get("candidate_name") == "UNKNOWN":
                errors.append({
                    "code": "UNKNOWN_CANDIDATE_NAME",
                    "message": f"batch {index}번째 후보자 이름을 추출하지 못했습니다.",
                })
        return errors

    parsed = output_json.get("parsed")
    if not isinstance(parsed, dict):
        errors.append({
            "code": "PARSED_NOT_DICT",
            "message": "parsed 필드가 dict 형식이 아닙니다.",
        })
        return errors

    required_keys = [
        "candidate_name",
        "company_name",
        "birth_year",
        "age_international",
        "age_korean",
        "status",
    ]

    for key in required_keys:
        if key not in parsed:
            errors.append({
                "code": "MISSING_PARSED_KEY",
                "message": f"parsed.{key} 필드가 없습니다.",
            })

    if parsed.get("candidate_name") == "UNKNOWN":
        errors.append({
            "code": "UNKNOWN_CANDIDATE_NAME",
            "message": "후보자 이름을 추출하지 못했습니다.",
        })

    return errors


def save_case_result_to_db(
    run_id: str,
    sample_file: Path,
    result: dict,
    validation_errors: list[dict],
) -> None:
    # 샘플 1건의 하네스 결과를 DB에 저장하고 실패 이력을 누적한다.
    conn = get_connection(DB_PATH)

    try:
        status = "success" if not validation_errors else "fail"
        first_error = validation_errors[0] if validation_errors else {}

        conn.execute(
            """
            INSERT INTO harness_case_results (
                run_id,
                case_file,
                status,
                error_code,
                error_message,
                parsed_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                str(sample_file),
                status,
                first_error.get("code"),
                first_error.get("message"),
                json.dumps(result.get("output_json", {}), ensure_ascii=False),
            ),
        )

        if validation_errors:
            raw_text = sample_file.read_text(encoding="utf-8")
            input_hash = hash_text(raw_text)

            for error in validation_errors:
                conn.execute(
                    """
                    INSERT INTO harness_failures (
                        case_file,
                        input_hash,
                        error_code,
                        error_message,
                        raw_text,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(case_file, input_hash, error_code)
                    DO UPDATE SET
                        error_message = excluded.error_message,
                        raw_text = excluded.raw_text,
                        fixed = 0,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        str(sample_file),
                        input_hash,
                        error.get("code", "UNKNOWN_ERROR"),
                        error.get("message", ""),
                        raw_text,
                    ),
                )

        conn.commit()
    finally:
        conn.close()


def build_ai_review_prompt(results: list[dict]) -> str:
    # 실패 케이스를 사람이 AI에게 다시 검토시킬 수 있는 프롬프트로 만든다.
    failed = [item for item in results if item["validation_errors"]]

    return f"""
너는 Python 후보자 관리 에이전트의 코드 검토자다.

검토 대상:
- scripts/init_db.sql
- scripts/init_db.py
- scripts/store.py
- scripts/harness.py
- scripts/sample_email*.txt

목표:
- 실패 케이스의 원인을 분석한다.
- 수정 파일 경로를 먼저 쓴다.
- 수정 함수는 함수별 전체 코드로 작성한다.
- 자동 패치는 하지 않는다.
- 추정은 반드시 '가설/추정'으로 표시한다.
- 한국어로만 작성한다.

실패 결과:
{json.dumps(failed, ensure_ascii=False, indent=2)}

출력 형식:
1. 핵심 원인
2. 수정 대상 파일 경로
3. 함수별 전체 수정 코드
4. 추가해야 할 sample_email 테스트
5. 다음 CLI 실행 순서
""".strip()


def save_report(
    run_id: str,
    results: list[dict],
    ai_prompt: str,
    db_snapshot: dict,
) -> Path:
    # 하네스 실행 결과와 DB 스냅샷을 Markdown 리포트로 저장한다.
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    total = len(results)
    fail_count = len([item for item in results if item["validation_errors"]])
    success_count = total - fail_count

    report_path = REPORTS_DIR / f"{run_id}.md"
    report = f"""# Harness Report

생성 시각: {now_text()}

## 요약

- 전체: {total}
- 성공: {success_count}
- 실패: {fail_count}
- 회사 수: {db_snapshot["counts"]["companies"]}
- 지원자 수: {db_snapshot["counts"]["applicants"]}
- 이메일 이벤트 수: {db_snapshot["counts"]["email_events"]}
- Notion page id 수: {db_snapshot["counts"]["notion_page_ids"]}

## Notion 확인

- 현재 store.py Notion API 호출 가능: 예
- 현재 harness Notion 동기화: {"예" if SYNC_NOTION_IN_HARNESS else "아니오"}
- 현재 DB notion_page_id 기록: {db_snapshot["counts"]["notion_page_ids"]}건
- 판단: 현재 하네스 테스트는 로컬 SQLite만 변경하며 Notion은 변경하지 않습니다.

## 케이스 결과

```json
{json.dumps(results, ensure_ascii=False, indent=2)}
```

## 최종 DB 스냅샷

```json
{json.dumps(db_snapshot, ensure_ascii=False, indent=2)}
```

## AI 리뷰 프롬프트

```text
{ai_prompt}
```
"""
    report_path.write_text(report, encoding="utf-8")
    LATEST_REPORT_PATH.write_text(report, encoding="utf-8")
    return report_path


def save_run_to_db(run_id: str, results: list[dict], report_path: Path) -> None:
    # 하네스 실행 1회의 요약 정보를 `harness_runs` 테이블에 저장한다.
    total = len(results)
    fail_count = len([item for item in results if item["validation_errors"]])
    success_count = total - fail_count
    status = "success" if fail_count == 0 else "fail"

    conn = get_connection(DB_PATH)
    try:
        conn.execute(
            """
            INSERT INTO harness_runs (
                run_id,
                status,
                total_count,
                success_count,
                fail_count,
                report_path
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                status,
                total,
                success_count,
                fail_count,
                str(report_path),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def cleanup_old_reports(max_runs: int = MAX_REPORT_RUNS) -> list[Path]:
    # 최신 리포트만 남기고 오래된 `run_*.md`와 상세 폴더를 삭제한다.
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    report_files = sorted(
        REPORTS_DIR.glob("run_*.md"),
        key=lambda path: path.name,
        reverse=True,
    )
    old_report_files = report_files[max_runs:]
    removed_paths = []

    for report_file in old_report_files:
        report_dir = REPORTS_DIR / report_file.stem

        if report_file.exists():
            report_file.unlink()
            removed_paths.append(report_file)

        if report_dir.exists() and report_dir.is_dir():
            shutil.rmtree(report_dir)
            removed_paths.append(report_dir)

    return removed_paths


def run_harness() -> int:
    # DB 초기화부터 샘플 실행, 검증, 리포트 저장까지 전체 하네스를 실행한다.
    sample_files = find_sample_files()
    if not sample_files:
        raise FileNotFoundError(f"샘플 메일 파일을 찾지 못했습니다: {SCRIPTS_DIR}")

    init_database()

    run_id = make_run_id()
    run_dir = REPORTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for sample_file in sample_files:
        output_file = run_dir / f"{sample_file.stem}.json"
        before_counts = get_db_counts()
        store_result = run_store_for_file(sample_file, output_file)
        after_counts = get_db_counts()
        validation_errors = validate_store_result(store_result)
        validation_errors.extend(
            validate_mutation_safety(before_counts, after_counts, store_result)
        )

        case_result = {
            "case_file": str(sample_file),
            "output_file": str(output_file),
            "exit_code": store_result["exit_code"],
            "db_counts_before": before_counts,
            "db_counts_after": after_counts,
            "db_count_delta": {
                table_name: after_counts[table_name] - before_counts[table_name]
                for table_name in before_counts
            },
            "validation_errors": validation_errors,
            "output_json": store_result["output_json"],
        }
        results.append(case_result)
        save_case_result_to_db(run_id, sample_file, store_result, validation_errors)

    ai_prompt = build_ai_review_prompt(results)
    db_snapshot = get_db_snapshot()
    report_path = save_report(run_id, results, ai_prompt, db_snapshot)
    save_run_to_db(run_id, results, report_path)
    removed_reports = cleanup_old_reports()

    fail_count = len([item for item in results if item["validation_errors"]])
    print(f"하네스 완료: total={len(results)}, fail={fail_count}")
    print(f"DB: {DB_PATH}")
    print(f"report: {report_path}")
    print(f"removed_old_reports: {len(removed_reports)}")

    return 1 if fail_count else 0


def main() -> None:
    # CLI 엔트리포인트. 실패 케이스가 있으면 exit code 1을 반환한다.
    sys.exit(run_harness())


if __name__ == "__main__":
    main()
