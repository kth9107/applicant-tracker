# 가상 지원자 샘플을 한 명씩 저장하며 DB 변화량을 확인하는 CLI.
#
# 하네스보다 사람이 읽기 쉬운 순차 실행 도구다. 각 샘플을 적용하기 전/후의
# 테이블 카운트를 출력해서 지원자 1명 처리 시 데이터가 삭제되거나 과도하게
# 증가하지 않는지 확인한다.

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parents[1]
VENV_PYTHON = BASE_DIR / ".venv" / "bin" / "python"


def reexec_with_project_venv() -> None:
    # 사용자가 `python3 ...`로 실행해도 프로젝트 가상환경 Python으로 재실행한다.
    #
    # AI-first 테스트는 `requests`, `discord.py` 같은 의존성이 필요하다. macOS
    # 시스템 Python으로 실행하면 의존성이 없거나 버전이 달라질 수 있으므로,
    # `.venv`가 있으면 같은 인자로 안전하게 한 번만 재실행한다.
    #
    if os.environ.get("APPLICANT_TRACKER_VENV_REEXEC") == "1":
        return
    if not VENV_PYTHON.exists():
        return
    if Path(sys.executable).resolve() == VENV_PYTHON.resolve():
        return

    os.environ["APPLICANT_TRACKER_VENV_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), *sys.argv])


reexec_with_project_venv()

from check_db import DB_PATH, get_connection
from init_db import init_database


SCRIPTS_DIR = BASE_DIR / "scripts"
TEST_DATA_DIR = SCRIPTS_DIR / "test_data"
STORE_PATH = SCRIPTS_DIR / "store.py"
RESULTS_DIR = BASE_DIR / "reports" / "one_by_one"
LOAD_TEST_SPLIT_DIR = TEST_DATA_DIR / "load_test"


def get_counts() -> dict[str, int]:
    # 회사, 지원자, 이벤트 테이블의 현재 레코드 수를 반환한다.
    conn = get_connection()
    try:
        counts = {}
        for table_name in ["companies", "applicants", "email_events"]:
            row = conn.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()
            counts[table_name] = int(row["count"])
        return counts
    finally:
        conn.close()


def split_load_test_file(load_test_file: Path) -> list[tuple[Path, int]]:
    # `---EMAIL_START---` 형식의 부하 테스트 파일을 개별 메일 파일로 분리한다.
    #
    # 반환값은 `(분리된 파일 경로, 기대 후보자 수)` 목록이다. 분리 파일은
    # `scripts/test_data/load_test/` 아래에 만들어서 기존 순차 실행 로직과 같은
    # 방식으로 저장/검증할 수 있게 한다.
    #
    text = load_test_file.read_text(encoding="utf-8")
    entries = []
    LOAD_TEST_SPLIT_DIR.mkdir(parents=True, exist_ok=True)

    for index, chunk in enumerate(text.split("---EMAIL_START---")[1:], start=1):
        body = chunk.split("---EMAIL_END---", 1)[0].strip()
        if not body:
            continue

        id_match = re.search(r"(?m)^ID:\s*([A-Za-z0-9_-]+)\s*$", body)
        if not id_match:
            continue
        expected_match = re.search(r"(?m)^EXPECTED_CANDIDATES:\s*(\d+)\s*$", body)
        case_id = id_match.group(1)
        expected_candidates = int(expected_match.group(1)) if expected_match else 1
        output_path = LOAD_TEST_SPLIT_DIR / f"{case_id}.txt"
        output_path.write_text(body, encoding="utf-8")
        entries.append((output_path, expected_candidates))

    return entries


def find_sample_files(
    include_base: bool,
    load_test_file: Optional[Path] = None,
    only_load_test: bool = False,
) -> list[tuple[Path, int]]:
    # 실행할 샘플 파일 목록을 만든다.
    #
    # `include_base`가 켜지면 기존 샘플 3개를 포함하고, 기본 가상 지원자 10명은
    # 항상 포함한다.
    #
    files: list[tuple[Path, int]] = []
    if only_load_test and not load_test_file:
        raise ValueError("--only-load-test는 --load-test-file과 함께 사용해야 합니다.")

    if include_base and not only_load_test:
        files.extend([
            (SCRIPTS_DIR / "sample_email.txt", 1),
            (SCRIPTS_DIR / "sample_email_2.txt", 1),
            (SCRIPTS_DIR / "sample_email_3.txt", 1),
        ])
    if not only_load_test:
        files.extend((path, 1) for path in sorted(TEST_DATA_DIR.glob("sample_email_10_*.txt")))
    if load_test_file:
        files.extend(split_load_test_file(load_test_file))
    return [(path, expected) for path, expected in files if path.exists()]


def run_store(sample_file: Path, output_file: Path, sync_notion: bool) -> dict:
    # 샘플 파일 1개를 `store.py` CLI로 저장하고 결과 JSON을 읽어온다.
    command = [
        sys.executable,
        str(STORE_PATH),
        "--input-file",
        str(sample_file),
        "--output-file",
        str(output_file),
    ]
    if not sync_notion:
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
        output_json = json.loads(output_file.read_text(encoding="utf-8"))

    return {
        "command": command,
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "output_json": output_json,
    }


def run_ai_store(sample_file: Path, output_file: Path, sync_notion: bool) -> dict:
    # Discord 봇과 같은 AI-first 파이프라인으로 샘플 파일 1개를 저장한다.
    from message_processor import process_text

    async def run() -> tuple[dict, str]:
        return await process_text(
            text=sample_file.read_text(encoding="utf-8"),
            source=str(sample_file),
            sync_notion=sync_notion,
            log_id=f"one_by_one_{sample_file.stem}",
        )

    command = [
        sys.executable,
        "scripts/run_samples_one_by_one.py",
        "--use-ai",
        str(sample_file),
    ]
    try:
        response, extraction_method = asyncio.run(run())
        response["extraction_method"] = extraction_method
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "command": command,
            "exit_code": 0 if response.get("status") == "ok" else 1,
            "stdout": "",
            "stderr": "",
            "output_json": response,
        }
    except Exception as exc:
        response = {
            "status": "error",
            "errors": [{"code": "AI_RUN_FAILED", "message": str(exc)}],
        }
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "command": command,
            "exit_code": 1,
            "stdout": "",
            "stderr": str(exc),
            "output_json": response,
        }


def print_case_summary(
    index: int,
    total: int,
    sample_file: Path,
    before: dict[str, int],
    after: dict[str, int],
    result: dict,
    output_file: Path,
) -> None:
    # 샘플 1건의 파싱 결과, Notion 결과, DB 변화량을 콘솔에 출력한다.
    output_json = result.get("output_json", {})
    parsed = output_json.get("parsed", {})
    notion = output_json.get("notion", {})
    delta = {
        key: after[key] - before[key]
        for key in before
    }

    print("\n" + "=" * 100)
    print(f"[{index}/{total}] {sample_file}")
    print(f"status: {output_json.get('status')} exit_code={result['exit_code']}")
    if output_json.get("batch"):
        print(
            "batch : "
            f"stored={output_json.get('stored_count')} / "
            f"total={output_json.get('total_count')}"
        )
    print(
        "parsed: "
        f"회사={parsed.get('company_name')} | "
        f"지원자={parsed.get('candidate_name')} | "
        f"출생년도={parsed.get('birth_year')} | "
        f"만나이={parsed.get('age_international')} | "
        f"한국나이={parsed.get('age_korean')} | "
        f"직무={parsed.get('position')} | "
        f"상태={parsed.get('status')}"
    )
    print(
        "notion: "
        f"enabled={notion.get('enabled')} | "
        f"synced={notion.get('synced')} | "
        f"action={notion.get('action')} | "
        f"page_id={notion.get('page_id')}"
    )
    print(f"before: {before}")
    print(f"after : {after}")
    print(f"delta : {delta}")
    print(f"json  : {output_file}")

    if result["exit_code"] != 0:
        print("stderr:")
        print(result["stderr"])


def run_samples(
    include_base: bool,
    reset_db: bool,
    sync_notion: bool,
    load_test_file: Optional[Path] = None,
    use_ai: bool = False,
    only_load_test: bool = False,
) -> int:
    # 샘플들을 차례대로 실행하고 실패 여부를 exit code로 반환한다.
    if reset_db:
        init_database()

    sample_files = find_sample_files(include_base, load_test_file, only_load_test)
    if not sample_files:
        raise FileNotFoundError(f"샘플 파일을 찾지 못했습니다: {TEST_DATA_DIR}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0

    for index, (sample_file, expected_candidates) in enumerate(sample_files, start=1):
        before = get_counts()
        output_file = RESULTS_DIR / f"{sample_file.stem}.json"
        if use_ai:
            result = run_ai_store(sample_file, output_file, sync_notion)
        else:
            result = run_store(sample_file, output_file, sync_notion)
        after = get_counts()
        print_case_summary(index, len(sample_files), sample_file, before, after, result, output_file)

        if result["exit_code"] != 0:
            failures += 1
            continue

        output_json = result.get("output_json") or {}
        expected_delta = int(output_json.get("stored_count") or expected_candidates)
        if after["email_events"] - before["email_events"] != expected_delta:
            failures += 1
            print(
                "검증 실패: 성공 케이스는 기대 후보자 수만큼 email_events가 증가해야 합니다. "
                f"expected={expected_delta}"
            )

        for table_name in before:
            if after[table_name] < before[table_name]:
                failures += 1
                print(f"검증 실패: {table_name} 레코드 수가 감소했습니다.")

    print("\n" + "=" * 100)
    print(f"완료: total={len(sample_files)}, failures={failures}, db={DB_PATH}")
    return 1 if failures else 0


def main() -> None:
    # CLI 옵션을 읽고 순차 샘플 실행을 시작한다.
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--include-base",
        action="store_true",
        help="기존 sample_email.txt 3개도 함께 실행합니다.",
    )
    parser.add_argument(
        "--keep-db",
        action="store_true",
        help="DB를 초기화하지 않고 현재 상태에서 이어서 실행합니다.",
    )
    parser.add_argument(
        "--sync-notion",
        action="store_true",
        help="샘플 데이터를 실제 Notion DB에도 생성/업데이트합니다.",
    )
    parser.add_argument(
        "--load-test-file",
        type=Path,
        help="---EMAIL_START--- 형식의 부하 테스트 파일을 분리해서 함께 실행합니다.",
    )
    parser.add_argument(
        "--use-ai",
        action="store_true",
        help="store.py rule parser 대신 Discord와 같은 AI-first 파이프라인으로 실행합니다.",
    )
    parser.add_argument(
        "--only-load-test",
        action="store_true",
        help="기본 sample_email_10_* 파일은 제외하고 --load-test-file에서 분리한 케이스만 실행합니다.",
    )
    args = parser.parse_args()

    sys.exit(
        run_samples(
            include_base=args.include_base,
            reset_db=not args.keep_db,
            sync_notion=args.sync_notion,
            load_test_file=args.load_test_file,
            use_ai=args.use_ai,
            only_load_test=args.only_load_test,
        )
    )


if __name__ == "__main__":
    main()
