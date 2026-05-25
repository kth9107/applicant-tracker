# SQLite DB 상태를 사람이 읽기 쉬운 표 형태로 확인하는 CLI.
#
# 테이블 생성 여부, 스키마, 건수, 실제 데이터를 빠르게 확인할 때 사용한다.
# 운영 데이터 확인용이며 DB를 수정하지 않는다.

import argparse
import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BASE_DIR / "db" / "applicants.db"


def get_connection() -> sqlite3.Connection:
    # 조회 전용 SQLite 연결을 열고 row를 dict처럼 읽을 수 있게 설정한다.
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def print_rows(title: str, rows: list[sqlite3.Row]) -> None:
    # sqlite row 목록을 간단한 pipe 표로 출력한다.
    print(f"\n## {title}")
    if not rows:
        print("(no rows)")
        return

    headers = rows[0].keys()
    print(" | ".join(headers))
    print("-" * 80)
    for row in rows:
        print(" | ".join("" if row[key] is None else str(row[key]) for key in headers))


def show_tables(conn: sqlite3.Connection) -> None:
    # 현재 DB에 생성된 테이블 목록을 출력한다.
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        ORDER BY name
        """
    ).fetchall()
    print_rows("tables", rows)


def show_counts(conn: sqlite3.Connection) -> None:
    # 주요 테이블의 레코드 수를 출력한다.
    rows = []
    for table_name in ["companies", "applicants", "email_events", "harness_runs", "harness_failures"]:
        count = conn.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()["count"]
        rows.append({"table_name": table_name, "count": count})

    print("\n## counts")
    print("table_name | count")
    print("-" * 80)
    for row in rows:
        print(f"{row['table_name']} | {row['count']}")


def show_schema(conn: sqlite3.Connection) -> None:
    # 테이블, 인덱스, 트리거 생성 SQL을 출력한다.
    rows = conn.execute(
        """
        SELECT name, sql
        FROM sqlite_master
        WHERE type IN ('table', 'index', 'trigger')
        ORDER BY type, name
        """
    ).fetchall()
    print_rows("schema", rows)


def show_data(conn: sqlite3.Connection) -> None:
    # 회사, 지원자, 이메일 이벤트의 주요 컬럼 데이터를 출력한다.
    print_rows(
        "companies",
        conn.execute(
            """
            SELECT id, name, contact_person, contact_email, notion_page_id, created_at, updated_at
            FROM companies
            ORDER BY id
            """
        ).fetchall(),
    )
    print_rows(
        "applicants",
        conn.execute(
            """
            SELECT id, company_id, name, birth_year, age, age_international, age_korean,
                   email, phone, position, skills, notes, status,
                   notion_page_id, created_at, updated_at
            FROM applicants
            ORDER BY id
            """
        ).fetchall(),
    )
    print_rows(
        "email_events",
        conn.execute(
            """
            SELECT id, applicant_id, company_id, source_type, source_message_id, event_type, status, created_at
            FROM email_events
            ORDER BY id
            """
        ).fetchall(),
    )


def main() -> None:
    # CLI 옵션을 해석하고 요청한 조회 모드를 실행한다.
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=["all", "tables", "schema", "counts", "data"],
        default="all",
    )
    args = parser.parse_args()

    if not DB_PATH.exists():
        raise FileNotFoundError(f"DB 파일이 없습니다: {DB_PATH}")

    conn = get_connection()
    try:
        print(f"DB: {DB_PATH}")
        if args.mode in {"all", "tables"}:
            show_tables(conn)
        if args.mode in {"all", "schema"}:
            show_schema(conn)
        if args.mode in {"all", "counts"}:
            show_counts(conn)
        if args.mode in {"all", "data"}:
            show_data(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
