# Notion 지원자 DB의 최근 페이지를 조회하는 확인용 CLI.
#
# 실제 Notion 데이터를 수정하지 않고 읽기만 한다. Discord/SQLite 저장 후 Notion에
# 제대로 동기화됐는지 사람이 확인할 때 사용한다.

import argparse
import datetime
import json
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from create_db import DB_PATH, get_connection
from notion_sync import (
    delete_applicant,
    ensure_company,
    get_sqlite_applicants_with_notion,
    notion_page_to_record,
    update_sqlite_from_notion_page,
)
from store import (
    build_notion_sync_parsed,
    extract_contact_person,
    save_applicant_notion_page_id,
    sync_notion,
)


BASE_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = BASE_DIR / ".env"
AUDIT_REPORT_DIR = BASE_DIR / "reports" / "audit"
DISCORD_LOG_DIR = BASE_DIR / "reports" / "discord_messages"
FIXED_COLUMNS = ["기업명", "포지션", "회사담당자", "이름", "생년", "나이", "희망연봉", "최종연봉", "기타"]
DEFAULT_AUDIT_REPORT_KEEP = 40


def load_env_file() -> None:
    # `.env`에서 Notion 토큰과 DB ID를 환경변수로 로드한다.
    if not ENV_PATH.exists():
        return

    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def notion_request(path: str, payload: Optional[dict] = None) -> dict:
    # Notion API를 호출한다.
    #
    # `payload`가 있으면 POST 요청, 없으면 GET 요청으로 보낸다. 현재 스크립트는
    # DB 조회만 하므로 주로 `databases/{id}/query`를 호출한다.
    #
    token = os.getenv("NOTION_TOKEN")
    if not token:
        raise RuntimeError("NOTION_TOKEN이 .env에 없습니다.")

    body = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"https://api.notion.com/v1/{path.lstrip('/')}",
        data=body,
        method="POST" if payload is not None else "GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8")
        raise RuntimeError(f"Notion API 오류: status={exc.code}, body={detail}") from exc


def plain_text(rich_text: list[dict]) -> str:
    # Notion rich_text/title 배열에서 사람이 읽는 plain text만 이어 붙인다.
    return "".join(item.get("plain_text", "") for item in rich_text or [])


def property_value(prop: dict) -> str:
    # Notion 속성 타입별 JSON 값을 출력용 문자열로 변환한다.
    prop_type = prop.get("type")
    value = prop.get(prop_type)

    if prop_type == "title":
        return plain_text(value)
    if prop_type == "rich_text":
        return plain_text(value)
    if prop_type == "select":
        return (value or {}).get("name", "")
    if prop_type == "email":
        return value or ""
    if prop_type == "phone_number":
        return value or ""
    if prop_type == "number":
        return "" if value is None else str(value)
    if prop_type == "last_edited_time":
        return value or ""

    return "" if value is None else str(value)


def fetch_all_database_pages() -> list[dict[str, Any]]:
    # Notion DB의 활성 page를 모두 가져온다.
    database_id = os.getenv("NOTION_DB_ID")
    if not database_id:
        raise RuntimeError("NOTION_DB_ID가 .env에 없습니다.")

    pages = []
    payload: dict[str, Any] = {
        "page_size": 100,
        "sorts": [
            {
                "timestamp": "last_edited_time",
                "direction": "descending",
            }
        ],
    }
    while True:
        data = notion_request(f"databases/{database_id}/query", payload)
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data.get("next_cursor")
    return pages


def notion_page_to_fixed_record(page: dict[str, Any]) -> dict[str, str]:
    # Notion page의 고정 컬럼을 비교용 dict로 변환한다.
    props = page.get("properties", {})
    record = {
        "page_id": page.get("id", ""),
        "last_edited_time": page.get("last_edited_time", ""),
    }
    for column in FIXED_COLUMNS:
        record[column] = property_value(props.get(column, {})).strip()
    return record


def normalized_compare_value(value: Any) -> str:
    # SQLite/Notion 타입 차이를 흡수해 비교용 문자열로 정리한다.
    if value is None:
        return ""
    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def latest_event_for_applicant(conn: sqlite3.Connection, applicant_id: int) -> dict[str, str]:
    # 지원자에 연결된 최신 원문 메시지와 파싱 요약을 가져온다.
    row = conn.execute(
        """
        SELECT raw_text, parsed_summary, status, source_message_id, created_at
        FROM email_events
        WHERE applicant_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (applicant_id,),
    ).fetchone()
    if not row:
        return {
            "raw_text": "",
            "parsed_summary": "",
            "status": "",
            "source_message_id": "",
            "created_at": "",
        }
    return {
        "raw_text": row["raw_text"] or "",
        "parsed_summary": row["parsed_summary"] or "",
        "status": row["status"] or "",
        "source_message_id": row["source_message_id"] or "",
        "created_at": row["created_at"] or "",
    }


def fetch_sqlite_audit_rows(limit: Optional[int]) -> list[dict[str, Any]]:
    # SQLite의 지원자 최종값과 최근 메시지 정보를 감사용 dict 목록으로 만든다.
    conn = get_connection(DB_PATH)
    try:
        sql = """
            SELECT
                a.id,
                a.notion_page_id,
                a.updated_at,
                c.name AS 기업명,
                a.position AS 포지션,
                c.contact_person AS 회사담당자,
                a.name AS 이름,
                a.birth_year AS 생년,
                a.age_international AS 나이,
                a.salary_expected AS 희망연봉,
                a.salary_current AS 최종연봉,
                a.notes AS 기타
            FROM applicants a
            LEFT JOIN companies c ON c.id = a.company_id
            ORDER BY a.updated_at DESC, a.id DESC
        """
        params: tuple[Any, ...] = ()
        if limit:
            sql += " LIMIT ?"
            params = (limit,)
        rows = []
        for row in conn.execute(sql, params).fetchall():
            item = dict(row)
            item["latest_event"] = latest_event_for_applicant(conn, int(row["id"]))
            rows.append(item)
        return rows
    finally:
        conn.close()


def classify_audit_issue(column: str, sqlite_value: str, notion_value: str, raw_text: str) -> str:
    # 불일치 원인을 사람이 이해할 수 있는 개선 후보 문장으로 분류한다.
    if sqlite_value and not notion_value:
        return f"{column} 값이 SQLite에는 있으나 Notion에는 비어 있습니다. Notion 동기화 payload/컬럼명을 확인하세요."
    if notion_value and not sqlite_value:
        return f"{column} 값이 Notion에는 있으나 SQLite에는 없습니다. Notion 역동기화 또는 저장 보강이 필요합니다."
    if column == "최종연봉" and "희망연봉" in raw_text and not sqlite_value:
        return "희망연봉 외 연봉 표현이 최종연봉으로 분리되지 않았습니다. salary_current 추출 규칙을 보강하세요."
    if column == "기타" and raw_text and not sqlite_value:
        return "원문에 후보자 설명/일정/메모가 있으나 기타가 비었습니다. notes 추출 규칙을 보강하세요."
    return f"{column} 값이 다릅니다. SQLite='{sqlite_value}', Notion='{notion_value}'"


def source_message_lookup_key(source_message_id: str) -> str:
    # Discord source id 또는 파일 경로에서 디버그 파일 검색에 쓸 키를 뽑는다.
    if not source_message_id:
        return ""
    if source_message_id.startswith("discord:"):
        return source_message_id.split(":")[-1]
    return Path(source_message_id).stem


def find_ai_artifacts(source_message_id: str) -> dict[str, Any]:
    # 최신 메시지와 연결된 AI JSON/진단 파일이 남아 있는지 확인한다.
    lookup_key = source_message_lookup_key(source_message_id)
    if not lookup_key or not DISCORD_LOG_DIR.exists():
        return {
            "lookup_key": lookup_key,
            "files": [],
            "missing": ["AI JSON 로그 파일을 찾을 기준값이 없습니다."],
        }

    patterns = [
        f"*{lookup_key}*ai_extract.json",
        f"*{lookup_key}*ai_reparse_extract.json",
        f"*{lookup_key}*ai_diagnostics.json",
        f"*{lookup_key}*ai_reparse_diagnostics.json",
        f"*{lookup_key}*ai_store_response.json",
        f"*{lookup_key}*ai_exception_and_fallback.json",
    ]
    files: list[Path] = []
    for pattern in patterns:
        files.extend(DISCORD_LOG_DIR.glob(pattern))

    unique_files = sorted(set(files), key=lambda path: path.stat().st_mtime, reverse=True)
    found_names = {path.name for path in unique_files}
    missing = []
    if not any(name.endswith("ai_extract.json") for name in found_names):
        missing.append("AI 1차 JSON 로그가 없습니다.")
    if not any(name.endswith("ai_store_response.json") for name in found_names):
        missing.append("AI 저장 결과 JSON 로그가 없습니다.")

    return {
        "lookup_key": lookup_key,
        "files": [str(path) for path in unique_files],
        "missing": missing,
    }


def message_json_findings(row: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    # 최근 감사에서 원문 메시지, parsed_summary, AI JSON 산출물의 누락을 점검한다.
    latest_event = row.get("latest_event") or {}
    findings = []
    if not latest_event.get("raw_text"):
        findings.append("최근 메시지 원문이 email_events에 없습니다.")

    parsed_summary = {}
    parsed_summary_text = latest_event.get("parsed_summary") or ""
    if not parsed_summary_text:
        findings.append("저장 당시 parsed_summary JSON이 없습니다.")
    else:
        try:
            parsed_summary = json.loads(parsed_summary_text)
        except json.JSONDecodeError:
            findings.append("저장 당시 parsed_summary JSON 형식이 깨져 있습니다.")

    ai_artifacts = find_ai_artifacts(latest_event.get("source_message_id") or "")
    findings.extend(ai_artifacts["missing"])
    if parsed_summary and parsed_summary.get("candidate_name") != row.get("이름"):
        findings.append("parsed_summary의 지원자 이름과 SQLite 이름이 다릅니다.")
    if parsed_summary and normalized_compare_value(parsed_summary.get("birth_year")) != normalized_compare_value(row.get("생년")):
        findings.append("parsed_summary의 생년과 SQLite 생년이 다릅니다.")

    return findings, {
        "parsed_summary": parsed_summary,
        "ai_artifacts": ai_artifacts,
    }


SYNC_CHANGE_COLUMNS = [
    ("기업명", "기업명"),
    ("회사담당자", "회사담당자"),
    ("이름", "지원자"),
    ("생년", "생년"),
    ("나이", "나이"),
    ("포지션", "포지션"),
    ("희망연봉", "희망연봉"),
    ("최종연봉", "최종연봉"),
    ("기타", "기타"),
]


def sqlite_record_by_notion_page_id(conn: sqlite3.Connection, notion_page_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            a.id,
            a.notion_page_id,
            c.name AS 기업명,
            c.contact_person AS 회사담당자,
            a.name AS 이름,
            a.birth_year AS 생년,
            a.age_international AS 나이,
            a.position AS 포지션,
            a.salary_expected AS 희망연봉,
            a.salary_current AS 최종연봉,
            a.notes AS 기타
        FROM applicants a
        LEFT JOIN companies c ON c.id = a.company_id
        WHERE a.notion_page_id = ?
        """,
        (notion_page_id,),
    ).fetchone()
    return dict(row) if row else {}


def normalized_record_diff(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, str]]:
    changes = []
    for key, label in SYNC_CHANGE_COLUMNS:
        before_value = normalized_compare_value(before.get(key))
        after_value = normalized_compare_value(after.get(key))
        if before_value == after_value:
            continue
        changes.append({
            "column": key,
            "label": label,
            "before": before_value,
            "after": after_value,
        })
    return changes


def notion_record_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "notion_page_id": record.get("notion_page_id", ""),
        "name": record.get("candidate_name", ""),
        "company": record.get("company_name", ""),
        "contact_person": record.get("contact_person", ""),
        "position": record.get("position", ""),
        "birth_year": record.get("birth_year"),
        "age_international": record.get("age_international"),
        "salary_expected": record.get("salary_expected", ""),
        "salary_current": record.get("salary_current", ""),
        "notes": record.get("notes", ""),
    }


def sync_sqlite_from_notion_pages(notion_pages: list[dict[str, Any]], apply: bool) -> dict[str, Any]:
    # Notion 활성 page 기준으로 SQLite를 정리/갱신한다.
    #
    # 기준은 Notion이다. Notion에서 삭제/보관된 row와 연결된 SQLite 지원자만
    # 삭제하고, Notion에 살아 있는 row는 SQLite에 값을 반영한다. 회사 row는
    # 지원자가 0명이 되어도 여기서 임의 삭제하지 않는다.
    #
    active_page_ids = {page.get("id") for page in notion_pages if page.get("id")}
    records = [notion_page_to_record(page) for page in notion_pages]

    conn = get_connection(DB_PATH)
    try:
        sqlite_rows = get_sqlite_applicants_with_notion(conn)
        delete_targets = [
            row
            for row in sqlite_rows
            if row["notion_page_id"] not in active_page_ids
        ]
        existing_page_ids = {
            row["notion_page_id"]
            for row in sqlite_rows
            if row["notion_page_id"] in active_page_ids
        }

        updated_ids = []
        created_ids = []
        updated_records = []
        created_records = []
        if apply:
            for row in delete_targets:
                delete_applicant(conn, int(row["id"]))

            for record in records:
                before = sqlite_record_by_notion_page_id(conn, record["notion_page_id"])
                updated_id = update_sqlite_from_notion_page(conn, record)
                if updated_id:
                    updated_ids.append(updated_id)
                    after = sqlite_record_by_notion_page_id(conn, record["notion_page_id"])
                    changes = normalized_record_diff(before, after)
                    if changes:
                        updated_records.append({
                            "applicant_id": updated_id,
                            "name": after.get("이름", ""),
                            "company": after.get("기업명", ""),
                            "notion_page_id": record["notion_page_id"],
                            "changes": changes,
                        })
                elif record["notion_page_id"] not in existing_page_ids and record["candidate_name"]:
                    created_id = create_sqlite_applicant_from_notion_record(conn, record)
                    created_ids.append(created_id)
                    created_records.append({
                        "applicant_id": created_id,
                        **notion_record_summary(record),
                    })

            conn.commit()
        else:
            conn.rollback()

        return {
            "applied": apply,
            "notion_active_count": len(active_page_ids),
            "sqlite_notion_linked_count": len(sqlite_rows),
            "deleted_applicants": [
                {
                    "id": int(row["id"]),
                    "name": row["name"],
                    "company": row["company_name"],
                    "notion_page_id": row["notion_page_id"],
                }
                for row in delete_targets
            ],
            "updated_applicant_ids": updated_ids,
            "created_applicant_ids": created_ids,
            "updated_records": updated_records,
            "created_records": created_records,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_sqlite_applicant_from_notion_record(
    conn: sqlite3.Connection,
    record: dict[str, Any],
) -> int:
    # SQLite에 없는 Notion 활성 row를 지원자 row로 생성한다.
    company_id = ensure_company(conn, record["company_name"], record.get("contact_person") or "")
    cursor = conn.execute(
        """
        INSERT INTO applicants (
            company_id,
            name,
            birth_year,
            age,
            age_international,
            age_korean,
            email,
            phone,
            position,
            skills,
            notes,
            salary_current,
            salary_expected,
            status,
            notion_page_id,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (
            company_id,
            record["candidate_name"],
            record["birth_year"],
            record["age"],
            record["age_international"],
            record["age_korean"],
            record["email"],
            record["phone"],
            record["position"],
            record["skills"],
            record["notes"],
            record["salary_current"],
            record["salary_expected"],
            record["status"],
            record["notion_page_id"],
        ),
    )
    return int(cursor.lastrowid)


def recover_sqlite_to_notion(write_reports: bool = True) -> dict[str, Any]:
    # SQLite에는 남아 있지만 Notion에 없거나 연결되지 않은 지원자를 Notion에 복구한다.
    load_env_file()
    notion_pages = fetch_all_database_pages()
    active_page_ids = {page.get("id") for page in notion_pages if page.get("id")}

    conn = get_connection(DB_PATH)
    recovered = []
    skipped = []
    try:
        rows = conn.execute(
            """
            SELECT id, name, notion_page_id
            FROM applicants
            ORDER BY updated_at DESC, id DESC
            """
        ).fetchall()

        for row in rows:
            applicant_id = int(row["id"])
            old_page_id = row["notion_page_id"] or ""
            if old_page_id and old_page_id in active_page_ids:
                skipped.append({
                    "id": applicant_id,
                    "name": row["name"],
                    "reason": "이미 활성 Notion row가 연결되어 있습니다.",
                })
                continue

            parsed = build_notion_sync_parsed(conn, {"candidate_name": row["name"]}, applicant_id)
            notion = sync_notion(parsed, "")
            if notion.get("page_id"):
                save_applicant_notion_page_id(conn, applicant_id, notion["page_id"])
                recovered.append({
                    "id": applicant_id,
                    "name": row["name"],
                    "old_page_id": old_page_id,
                    "new_page_id": notion["page_id"],
                    "action": notion.get("action", ""),
                    "data": {
                        "company": parsed.get("company_name", ""),
                        "contact_person": parsed.get("contact_person", ""),
                        "position": parsed.get("position", ""),
                        "birth_year": parsed.get("birth_year"),
                        "age_international": parsed.get("age_international"),
                        "salary_expected": parsed.get("salary_expected", ""),
                        "salary_current": parsed.get("salary_current", ""),
                    },
                })

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    result = {
        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "recovered_count": len(recovered),
        "skipped_count": len(skipped),
        "recovered": recovered,
        "skipped": skipped,
    }
    if write_reports:
        AUDIT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = AUDIT_REPORT_DIR / "latest_recovery_report.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        result["report_path"] = str(path)
    return result


def audit_rows(
    limit: Optional[int],
    scope_name: Optional[str] = None,
    include_message_json: bool = True,
    apply_notion_cleanup: bool = False,
) -> dict[str, Any]:
    # SQLite와 Notion의 고정 컬럼을 비교하고 개선 후보를 만든다.
    sqlite_rows = fetch_sqlite_audit_rows(limit)
    notion_pages = fetch_all_database_pages()
    notion_sync_result = None
    if apply_notion_cleanup:
        notion_sync_result = sync_sqlite_from_notion_pages(notion_pages, apply=True)
        sqlite_rows = fetch_sqlite_audit_rows(limit)
    notion_by_id = {
        page.get("id", ""): notion_page_to_fixed_record(page)
        for page in notion_pages
    }

    records = []
    suggestions = []
    sqlite_page_ids = {
        row.get("notion_page_id")
        for row in sqlite_rows
        if row.get("notion_page_id")
    }
    for row in sqlite_rows:
        page_id = row.get("notion_page_id") or ""
        notion_row = notion_by_id.get(page_id)
        raw_text = (row.get("latest_event") or {}).get("raw_text", "")
        diffs = []

        if not page_id:
            diffs.append({
                "column": "notion_page_id",
                "sqlite": "",
                "notion": "",
                "suggestion": "SQLite 지원자에 notion_page_id가 없습니다. 다음 저장/동기화 시 Notion page_id 저장 여부를 확인하세요.",
            })
        elif not notion_row:
            diffs.append({
                "column": "Notion page",
                "sqlite": page_id,
                "notion": "",
                "suggestion": "SQLite의 notion_page_id에 해당하는 활성 Notion page를 찾지 못했습니다. Notion에서 삭제/보관됐을 수 있습니다.",
            })

        if notion_row:
            for column in FIXED_COLUMNS:
                sqlite_value = normalized_compare_value(row.get(column))
                notion_value = normalized_compare_value(notion_row.get(column))
                if sqlite_value == notion_value:
                    continue
                diffs.append({
                    "column": column,
                    "sqlite": sqlite_value,
                    "notion": notion_value,
                    "suggestion": classify_audit_issue(column, sqlite_value, notion_value, raw_text),
                })

        missing_from_message = []
        message_json = {}
        if include_message_json:
            message_json_findings_result, message_json = message_json_findings(row)
            missing_from_message.extend(message_json_findings_result)
        if include_message_json and raw_text:
            parsed_summary = message_json.get("parsed_summary") or {}
            if "최종연봉" in raw_text and not normalized_compare_value(row.get("최종연봉")):
                missing_from_message.append("원문에 최종연봉이 있으나 저장값이 비어 있습니다.")
            if "희망연봉" in raw_text and not normalized_compare_value(row.get("희망연봉")):
                missing_from_message.append("원문에 희망연봉이 있으나 저장값이 비어 있습니다.")
            if raw_text and not normalized_compare_value(row.get("기타")):
                missing_from_message.append("원문 메시지가 있으나 기타가 비어 있습니다.")
            if parsed_summary.get("company_name") and not normalized_compare_value(row.get("기업명")):
                missing_from_message.append("파싱 결과에는 회사명이 있으나 SQLite 기업명이 비어 있습니다.")
            if parsed_summary.get("contact_person") and not normalized_compare_value(row.get("회사담당자")):
                missing_from_message.append("파싱 결과에는 회사 담당자가 있으나 SQLite 회사담당자가 비어 있습니다.")
            if raw_text and extract_contact_person(raw_text) and not normalized_compare_value(row.get("회사담당자")):
                missing_from_message.append("원문에 회사 담당자가 있으나 저장값이 비어 있습니다.")

        record = {
            "applicant_id": row.get("id"),
            "notion_page_id": page_id,
            "name": normalized_compare_value(row.get("이름")),
            "company": normalized_compare_value(row.get("기업명")),
            "updated_at": row.get("updated_at"),
            "latest_message_at": (row.get("latest_event") or {}).get("created_at", ""),
            "diffs": diffs,
            "message_findings": missing_from_message,
            "message_json": message_json,
        }
        records.append(record)
        for diff in diffs:
            suggestions.append(diff["suggestion"])
        suggestions.extend(missing_from_message)

    for page_id, notion_row in notion_by_id.items():
        if not page_id or page_id in sqlite_page_ids:
            continue
        diff = {
            "column": "SQLite applicant",
            "sqlite": "",
            "notion": page_id,
            "suggestion": "Notion에는 활성 row가 있으나 SQLite 지원자와 연결되어 있지 않습니다. 필요하면 해당 Notion row를 다시 저장/연결하세요.",
        }
        record = {
            "applicant_id": "",
            "notion_page_id": page_id,
            "name": notion_row.get("이름", ""),
            "company": notion_row.get("기업명", ""),
            "updated_at": "",
            "latest_message_at": "",
            "diffs": [diff],
            "message_findings": [],
            "message_json": {},
        }
        records.append(record)
        suggestions.append(diff["suggestion"])

    unique_suggestions = list(dict.fromkeys(suggestions))
    return {
        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scope": scope_name or ("all" if limit is None else f"recent_{limit}"),
        "sqlite_count": len(sqlite_rows),
        "notion_count": len(notion_pages),
        "diff_count": sum(len(record["diffs"]) for record in records),
        "message_finding_count": sum(len(record["message_findings"]) for record in records),
        "records": records,
        "suggestions": unique_suggestions,
        "notion_sync": notion_sync_result,
    }


def cleanup_old_audit_reports(keep: int = DEFAULT_AUDIT_REPORT_KEEP) -> int:
    # 감사 리포트 파일을 최근 N개만 유지한다.
    removed = 0
    if not AUDIT_REPORT_DIR.exists():
        return removed
    for pattern in ["run_*.json", "run_*.md"]:
        files = sorted(AUDIT_REPORT_DIR.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in files[keep:]:
            path.unlink()
            removed += 1
    return removed


def write_audit_reports(result: dict[str, Any]) -> dict[str, Path]:
    # 감사 결과를 JSON/Markdown/latest 파일로 저장한다.
    AUDIT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = AUDIT_REPORT_DIR / f"run_{run_id}.json"
    md_path = AUDIT_REPORT_DIR / f"run_{run_id}.md"
    latest_md_path = AUDIT_REPORT_DIR / "latest_audit_report.md"
    suggestions_path = AUDIT_REPORT_DIR / "feedback_suggestions.md"

    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = render_audit_markdown(result)
    md_path.write_text(markdown, encoding="utf-8")
    latest_md_path.write_text(markdown, encoding="utf-8")
    suggestions_path.write_text(render_feedback_suggestions(result), encoding="utf-8")
    cleanup_old_audit_reports()
    return {
        "json": json_path,
        "markdown": md_path,
        "latest": latest_md_path,
        "suggestions": suggestions_path,
    }


def render_audit_markdown(result: dict[str, Any]) -> str:
    # 감사 결과를 사람이 읽는 Markdown으로 만든다.
    notion_sync = result.get("notion_sync") or {}
    lines = [
        "# Applicant Tracker Audit",
        "",
        f"- 생성 시각: {result['created_at']}",
        f"- 범위: {result['scope']}",
        f"- SQLite 대상: {result['sqlite_count']}명",
        f"- Notion 활성 row: {result['notion_count']}건",
        f"- 불일치: {result['diff_count']}건",
        f"- 메시지 기반 개선 후보: {result['message_finding_count']}건",
        f"- Notion 기준 DB 정리: {'적용' if notion_sync.get('applied') else '미적용'}",
        "",
        "## 개선 후보",
    ]
    if notion_sync:
        lines.extend([
            "",
            "## Notion 기준 SQLite 정리",
            f"- 삭제된 SQLite 지원자: {len(notion_sync.get('deleted_applicants') or [])}명",
            f"- 갱신된 SQLite 지원자: {len(notion_sync.get('updated_applicant_ids') or [])}명",
            f"- Notion 기준으로 생성된 SQLite 지원자: {len(notion_sync.get('created_applicant_ids') or [])}명",
        ])
        for deleted in (notion_sync.get("deleted_applicants") or [])[:20]:
            lines.append(
                f"- 삭제: {deleted.get('name') or ''} / {deleted.get('company') or ''} / {deleted.get('notion_page_id') or ''}"
            )
        for updated in (notion_sync.get("updated_records") or [])[:20]:
            change_text = "; ".join(
                f"{change.get('label')}: {change.get('before') or '-'} -> {change.get('after') or '-'}"
                for change in updated.get("changes", [])
            )
            lines.append(
                f"- 갱신: {updated.get('name') or ''} / {updated.get('company') or ''} / {change_text}"
            )
        for created in (notion_sync.get("created_records") or [])[:20]:
            lines.append(
                f"- 생성: {created.get('name') or ''} / {created.get('company') or ''} / 담당자 {created.get('contact_person') or '-'} / {created.get('notion_page_id') or ''}"
            )
    if result["suggestions"]:
        for suggestion in result["suggestions"][:30]:
            lines.append(f"- {suggestion}")
    else:
        lines.append("- 현재 고정 컬럼 기준 불일치/누락이 없습니다.")

    lines.extend(["", "## 상세"])
    for record in result["records"]:
        if not record["diffs"] and not record["message_findings"]:
            continue
        title = " / ".join(part for part in [record["name"], record["company"]] if part)
        lines.extend(["", f"### {title or record['applicant_id']}"])
        if record["diffs"]:
            lines.append("")
            lines.append("| 컬럼 | SQLite | Notion | 개선 후보 |")
            lines.append("| --- | --- | --- | --- |")
            for diff in record["diffs"]:
                lines.append(
                    f"| {diff['column']} | {diff['sqlite']} | {diff['notion']} | {diff['suggestion']} |"
                )
        if record["message_findings"]:
            lines.append("")
            for finding in record["message_findings"]:
                lines.append(f"- {finding}")
    return "\n".join(lines) + "\n"


def render_feedback_suggestions(result: dict[str, Any]) -> str:
    # `user_feedback_rules.md`에 수동 반영할 수 있는 후보 문장을 만든다.
    lines = [
        "# 감사 기반 파싱 개선 후보",
        "",
        "아래 항목은 자동으로 스킬에 반영하지 않습니다.",
        "`!문제점`으로 확인된 내용만 추가하세요.",
        "",
    ]
    if not result["suggestions"]:
        lines.append("- 현재 추가할 개선 후보가 없습니다.")
        return "\n".join(lines) + "\n"
    for suggestion in result["suggestions"]:
        lines.append(f"- {suggestion}")
    return "\n".join(lines) + "\n"


def run_audit(
    limit: Optional[int] = 10,
    write_reports: bool = True,
    scope_name: Optional[str] = None,
    include_message_json: bool = True,
    apply_notion_cleanup: bool = False,
) -> dict[str, Any]:
    # Discord 명령/CLI/스케줄러에서 공통으로 사용하는 감사 실행 함수.
    load_env_file()
    result = audit_rows(
        limit,
        scope_name,
        include_message_json=include_message_json,
        apply_notion_cleanup=apply_notion_cleanup,
    )
    if write_reports:
        paths = write_audit_reports(result)
        result["report_paths"] = {key: str(path) for key, path in paths.items()}
    return result


def build_discord_audit_reply(result: dict[str, Any]) -> str:
    # Discord `!감사` 답장용으로 짧은 요약을 만든다.
    scope = result.get("scope", "")
    if scope.startswith("recent_"):
        scope_text = f"최근 {scope.removeprefix('recent_')}명"
    elif scope == "all":
        scope_text = "전체 DB/Notion"
    else:
        scope_text = scope or "감사 대상"
    notion_sync = result.get("notion_sync") or {}
    lines = [
        "지원자 데이터 감사 완료",
        f"- 범위: {scope_text}",
        f"- SQLite 대상: {result['sqlite_count']}명",
        f"- Notion 활성 row: {result['notion_count']}건",
        f"- 불일치: {result['diff_count']}건",
        f"- 메시지 기반 개선 후보: {result['message_finding_count']}건",
    ]
    if notion_sync:
        lines.extend([
            f"- Notion 삭제 반영: {len(notion_sync.get('deleted_applicants') or [])}명 정리",
            f"- Notion 값 DB 갱신: {len(notion_sync.get('updated_applicant_ids') or [])}명",
            f"- Notion 기준 DB 생성: {len(notion_sync.get('created_applicant_ids') or [])}명",
        ])
        changed_records = notion_sync.get("updated_records") or []
        created_records = notion_sync.get("created_records") or []
        deleted_records = notion_sync.get("deleted_applicants") or []
        if changed_records or created_records or deleted_records:
            lines.append("")
            lines.append("변경 로그")
        for updated in changed_records[:3]:
            change_text = "; ".join(
                f"{change.get('label')}: {change.get('before') or '-'} -> {change.get('after') or '-'}"
                for change in updated.get("changes", [])[:3]
            )
            lines.append(f"- 갱신: {updated.get('name') or ''} / {change_text}")
        for created in created_records[:3]:
            lines.append(f"- 생성: {created.get('name') or ''} / {created.get('company') or ''} / 담당자 {created.get('contact_person') or '-' }")
        for deleted in deleted_records[:3]:
            lines.append(f"- 삭제: {deleted.get('name') or ''} / {deleted.get('company') or ''}")
    if result.get("report_paths"):
        lines.append(f"- 리포트: {result['report_paths'].get('latest')}")
    if result["suggestions"]:
        lines.append("")
        lines.append("개선 후보")
        for suggestion in result["suggestions"][:5]:
            lines.append(f"- {suggestion}")
    else:
        lines.append("- 현재 고정 컬럼 기준 불일치/누락이 없습니다.")
    return "\n".join(lines)


def build_discord_recovery_reply(result: dict[str, Any]) -> str:
    # Discord `!복구` 답장용 요약을 만든다.
    lines = [
        "Notion 복구 완료",
        f"- 복구: {result['recovered_count']}명",
        f"- 이미 연결됨: {result['skipped_count']}명",
    ]
    if result.get("report_path"):
        lines.append(f"- 리포트: {result['report_path']}")
    if result["recovered"]:
        lines.append("")
        lines.append("복구된 지원자")
        for item in result["recovered"][:10]:
            data = item.get("data") or {}
            details = " / ".join(
                part for part in [
                    data.get("company"),
                    f"담당자 {data.get('contact_person')}" if data.get("contact_person") else "",
                    data.get("position"),
                ] if part
            )
            suffix = f" / {details}" if details else ""
            lines.append(f"- {item['name']} ({item['action']}){suffix}")
    return "\n".join(lines)


def list_database_pages(limit: int) -> None:
    # Notion DB에서 최근 수정 순으로 페이지를 조회해 표 형태로 출력한다.
    database_id = os.getenv("NOTION_DB_ID")
    if not database_id:
        raise RuntimeError("NOTION_DB_ID가 .env에 없습니다.")

    data = notion_request(
        f"databases/{database_id}/query",
        {
            "page_size": limit,
            "sorts": [
                {
                    "timestamp": "last_edited_time",
                    "direction": "descending",
                }
            ],
        },
    )

    print(f"Notion DB: {database_id}")
    print(f"조회 결과: {len(data.get('results', []))}건")
    print("page_id | 이름 | 기업명 | 포지션 | 회사담당자 | 생년 | 나이 | 희망연봉 | 최종연봉 | 기타 | 최근수정")
    print("-" * 100)

    for page in data.get("results", []):
        props = page.get("properties", {})
        print(
            " | ".join(
                [
                    page.get("id", ""),
                    property_value(props.get("이름", {})),
                    property_value(props.get("기업명", {})),
                    property_value(props.get("포지션", {})),
                    property_value(props.get("회사담당자", {})),
                    property_value(props.get("생년", {})),
                    property_value(props.get("나이", {})),
                    property_value(props.get("희망연봉", {})),
                    property_value(props.get("최종연봉", {})),
                    property_value(props.get("기타", {})),
                    page.get("last_edited_time", ""),
                ]
            )
        )


def main() -> None:
    # CLI 옵션을 읽고 Notion DB 조회를 실행한다.
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--audit", action="store_true", help="SQLite/Notion 감사 리포트를 생성합니다.")
    parser.add_argument("--all", action="store_true", help="전체감사를 실행하고 Notion 기준으로 SQLite를 정리/갱신합니다.")
    parser.add_argument("--no-report", action="store_true", help="감사 결과 파일을 쓰지 않습니다.")
    args = parser.parse_args()

    load_env_file()
    if args.audit:
        audit_limit = None if args.all else args.limit
        scope_name = "all" if args.all else f"recent_{args.limit}"
        result = run_audit(
            limit=audit_limit,
            write_reports=not args.no_report,
            scope_name=scope_name,
            include_message_json=not args.all,
            apply_notion_cleanup=args.all,
        )
        print(build_discord_audit_reply(result))
        return

    list_database_pages(args.limit)


if __name__ == "__main__":
    main()
