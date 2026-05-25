# 프로젝트 SQLite DB를 `scripts/init_db.sql` 기준으로 초기화하는 CLI.
#
# 하네스와 수동 초기화에서 사용하는 진입점이다. 실행하면 기존 주요 테이블을
# 삭제하고 현재 SQL 스키마로 다시 만든다.

from pathlib import Path
import sqlite3


BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BASE_DIR / "db" / "applicants.db"
INIT_SQL_PATH = BASE_DIR / "scripts" / "init_db.sql"


def read_init_sql() -> str:
    # 초기화 SQL 파일을 읽고, 파일 없음/빈 파일 상태를 명확한 오류로 알려준다.
    if not INIT_SQL_PATH.exists():
        raise FileNotFoundError(f"초기화 SQL 파일이 없습니다: {INIT_SQL_PATH}")

    sql = INIT_SQL_PATH.read_text(encoding="utf-8").strip()

    if not sql:
        raise ValueError(f"초기화 SQL 파일이 비어 있습니다: {INIT_SQL_PATH}")

    return sql


def ensure_db_dir() -> None:
    # `db/` 폴더가 없을 때 생성한다.
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def execute_init_sql(sql: str) -> None:
    # SQLite에 SQL 스크립트를 트랜잭션처럼 실행한다.
    #
    # 중간에 실패하면 rollback하고 예외를 다시 던져 호출자가 실패를 알 수 있게 한다.
    #
    conn = sqlite3.connect(str(DB_PATH))

    try:
        conn.executescript(sql)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def print_result() -> None:
    # 초기화 성공 후 사용자가 확인할 경로 정보를 출력한다.
    print("DB 초기화 완료")
    print(f"DB 경로: {DB_PATH}")
    print(f"SQL 경로: {INIT_SQL_PATH}")


def init_database() -> None:
    # DB 폴더 준비, SQL 읽기, 실행, 결과 출력까지 수행하는 고수준 함수.
    ensure_db_dir()
    sql = read_init_sql()
    execute_init_sql(sql)
    print_result()


def main() -> None:
    # CLI 엔트리포인트.
    init_database()


if __name__ == "__main__":
    main()
