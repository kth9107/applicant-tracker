# Notion DB를 기준으로 로컬 SQLite 지원자 DB를 맞추는 동기화 CLI.
#
# 기본 실행은 dry-run이다. Notion에서 삭제/보관되어 더 이상 조회되지 않는
# 페이지와 연결된 SQLite 지원자를 찾아 보여주고, `--apply`를 붙인 경우에만
# 실제로 SQLite에서 삭제한다. 또한 Notion에 남아 있는 페이지는 page_id 기준으로
# SQLite의 주요 필드를 최신 Notion 값으로 갱신한다.

import argparse
import json
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from create_db import DB_PATH, get_connection
from store import (
    calculate_international_age,
    calculate_korean_age,
    canonical_company_name,
    get_company_by_name,
    notion_request,
    normalize_status,
    to_int,
)


BASE_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = BASE_DIR / ".env"


def load_env_file() -> None:
    # `.env` 값을 환경변수에 올린다. 이미 설정된 OS 환경변수는 덮어쓰지 않는다.
    if not ENV_PATH.exists():
        return

    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def plain_text(items: list[dict[str, Any]]) -> str:
    # Notion title/rich_text 배열에서 plain_text만 이어 붙인다.
    return "".join(item.get("plain_text", "") for item in items or []).strip()


def property_text(properties: dict[str, Any], name: str) -> str:
    # Notion 속성을 문자열로 꺼낸다. 없는 속성은 빈 문자열로 반환한다.
    prop = properties.get(name) or {}
    prop_type = prop.get("type")
    value = prop.get(prop_type)

    if prop_type == "title":
        return plain_text(value)
    if prop_type == "rich_text":
        return plain_text(value)
    if prop_type == "select":
        return (value or {}).get("name", "").strip()
    if prop_type == "email":
        return value or ""
    if prop_type == "phone_number":
        return value or ""
    if prop_type == "number":
        return "" if value is None else str(value)
    return ""


def fetch_active_notion_pages() -> list[dict[str, Any]]:
    # Notion DB의 현재 활성 페이지를 모두 조회한다.
    #
    # Notion database query는 일반적으로 휴지통/보관된 페이지를 결과에서 제외한다.
    # 따라서 SQLite에 저장된 notion_page_id가 이 목록에 없으면 Notion에서 삭제
    # 또는 보관된 것으로 보고 로컬 삭제 후보로 판단한다.
    #
    database_id = os.getenv("NOTION_DB_ID")
    if not database_id:
        raise RuntimeError("NOTION_DB_ID가 .env에 없습니다.")

    pages = []
    payload: dict[str, Any] = {"page_size": 100}
    while True:
        data = notion_request("POST", f"databases/{database_id}/query", payload)
        pages.extend(data.get("results") or [])
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data.get("next_cursor")
    return pages


def notion_page_to_record(page: dict[str, Any]) -> dict[str, Any]:
    # Notion 페이지 속성을 SQLite 갱신용 dict로 변환한다.
    props = page.get("properties") or {}
    birth_year = to_int(property_text(props, "생년"))
    age_international = to_int(property_text(props, "나이")) or calculate_international_age(birth_year)
    age_korean = to_int(property_text(props, "한국나이")) or calculate_korean_age(birth_year)
    company_name = property_text(props, "기업명")

    return {
        "notion_page_id": page.get("id", ""),
        "candidate_name": property_text(props, "이름"),
        "company_name": company_name,
        "company_canonical_name": canonical_company_name(company_name),
        "birth_year": birth_year,
        "age": age_international,
        "age_international": age_international,
        "age_korean": age_korean,
        "email": property_text(props, "이메일"),
        "phone": property_text(props, "전화"),
        "position": property_text(props, "포지션"),
        "skills": property_text(props, "스킬"),
        "notes": property_text(props, "기타"),
        "salary_current": property_text(props, "최종연봉"),
        "salary_expected": property_text(props, "희망연봉"),
        "status": normalize_status(property_text(props, "상태")),
    }


def get_sqlite_applicants_with_notion(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    # Notion page_id가 연결된 SQLite 지원자를 조회한다.
    return conn.execute(
        """
        SELECT a.id, a.name, a.birth_year, a.company_id, a.notion_page_id, c.name AS company_name
        FROM applicants a
        LEFT JOIN companies c ON c.id = a.company_id
        WHERE a.notion_page_id IS NOT NULL
          AND a.notion_page_id != ''
        ORDER BY a.id
        """
    ).fetchall()


def ensure_company(conn: sqlite3.Connection, company_name: str) -> Optional[int]:
    # Notion의 지원회사 이름을 SQLite 회사 id로 맞춘다.
    if not company_name:
        return None

    company = get_company_by_name(conn, company_name)
    if company:
        return int(company["id"])

    cursor = conn.execute(
        """
        INSERT INTO companies (name, updated_at)
        VALUES (?, CURRENT_TIMESTAMP)
        """,
        (company_name,),
    )
    return int(cursor.lastrowid)


def update_sqlite_from_notion_page(
    conn: sqlite3.Connection,
    record: dict[str, Any],
) -> Optional[int]:
    # Notion page_id가 연결된 SQLite 지원자를 Notion 값으로 갱신한다.
    if not record["notion_page_id"]:
        return None

    row = conn.execute(
        """
        SELECT id, company_id
        FROM applicants
        WHERE notion_page_id = ?
        """,
        (record["notion_page_id"],),
    ).fetchone()
    if not row:
        return None

    company_id = ensure_company(conn, record["company_name"])
    if company_id is None and row["company_id"] is not None:
        company_id = int(row["company_id"])
    conn.execute(
        """
        UPDATE applicants
        SET company_id = ?,
            name = COALESCE(NULLIF(?, ''), name),
            birth_year = COALESCE(?, birth_year),
            age = COALESCE(?, age),
            age_international = COALESCE(?, age_international),
            age_korean = COALESCE(?, age_korean),
            email = COALESCE(NULLIF(?, ''), email),
            phone = COALESCE(NULLIF(?, ''), phone),
            position = COALESCE(NULLIF(?, ''), position),
            skills = COALESCE(NULLIF(?, ''), skills),
            notes = COALESCE(NULLIF(?, ''), notes),
            salary_current = COALESCE(NULLIF(?, ''), salary_current),
            salary_expected = COALESCE(NULLIF(?, ''), salary_expected),
            status = COALESCE(NULLIF(?, ''), status),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
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
            int(row["id"]),
        ),
    )
    return int(row["id"])


def delete_applicant(conn: sqlite3.Connection, applicant_id: int) -> None:
    # 지원자와 관련 email_events를 SQLite에서 삭제한다.
    conn.execute("DELETE FROM email_events WHERE applicant_id = ?", (applicant_id,))
    conn.execute("DELETE FROM applicants WHERE id = ?", (applicant_id,))


def cleanup_empty_companies(conn: sqlite3.Connection) -> int:
    # 지원자가 0명인 회사를 삭제하고 삭제 수를 반환한다.
    cursor = conn.execute(
        """
        DELETE FROM companies
        WHERE id NOT IN (
            SELECT DISTINCT company_id
            FROM applicants
        )
        """
    )
    return int(cursor.rowcount or 0)


def sync_from_notion(apply: bool, cleanup_companies: bool) -> int:
    # Notion 현재 상태를 기준으로 SQLite 갱신/삭제 후보를 계산하고 실행한다.
    load_env_file()
    pages = fetch_active_notion_pages()
    active_page_ids = {page.get("id") for page in pages if page.get("id")}
    records = [notion_page_to_record(page) for page in pages]

    conn = get_connection(DB_PATH)
    try:
        sqlite_rows = get_sqlite_applicants_with_notion(conn)
        delete_targets = [
            row
            for row in sqlite_rows
            if row["notion_page_id"] not in active_page_ids
        ]

        print(f"Notion 활성 페이지: {len(active_page_ids)}건")
        print(f"SQLite notion 연결 지원자: {len(sqlite_rows)}건")
        print(f"SQLite 삭제 후보: {len(delete_targets)}건")

        for row in delete_targets:
            print(
                "DELETE_CANDIDATE | "
                f"applicant_id={row['id']} | "
                f"name={row['name']} | "
                f"company={row['company_name']} | "
                f"notion_page_id={row['notion_page_id']}"
            )

        updated_ids = []
        if apply:
            for row in delete_targets:
                delete_applicant(conn, int(row["id"]))

            for record in records:
                updated_id = update_sqlite_from_notion_page(conn, record)
                if updated_id:
                    updated_ids.append(updated_id)

            deleted_companies = cleanup_empty_companies(conn) if cleanup_companies else 0
            conn.commit()
            print(f"적용 완료: deleted_applicants={len(delete_targets)}, updated_applicants={len(updated_ids)}, deleted_companies={deleted_companies}")
        else:
            print("dry-run 완료: 실제 SQLite 변경 없음. 적용하려면 --apply를 붙이세요.")

        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    # CLI 옵션을 읽고 Notion 기준 SQLite 동기화를 실행한다.
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="dry-run이 아니라 실제 SQLite에 적용합니다.")
    parser.add_argument(
        "--cleanup-companies",
        action="store_true",
        help="지원자가 0명인 회사도 함께 삭제합니다.",
    )
    args = parser.parse_args()
    raise SystemExit(sync_from_notion(apply=args.apply, cleanup_companies=args.cleanup_companies))


if __name__ == "__main__":
    main()
