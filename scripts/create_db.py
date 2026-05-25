# SQLite DB 연결과 초기화 SQL 실행을 담당하는 공통 유틸리티.
#
# 현재 프로젝트의 실제 DB 생성 기준은 `scripts/init_db.sql`이다. 이 파일은
# 다른 스크립트가 같은 DB 경로와 연결 설정을 재사용할 수 있게 해준다.

import os
import sqlite3
from pathlib import Path


# ===== 경로 설정 =====
# 모든 경로는 이 파일 위치가 아니라 프로젝트 루트를 기준으로 계산한다.
# 이렇게 해야 VS Code, 터미널, Discord 봇에서 실행 위치가 달라도 같은 DB를 본다.
BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = BASE_DIR / "db" / "applicants.db"

# SQLITE_PATH가 `.env` 또는 OS 환경변수에 있으면 그 값을 우선한다.
# 운영 DB 위치를 바꾸고 싶을 때 코드 수정 없이 설정만 바꾸기 위한 장치다.
DB_PATH = Path(os.getenv("SQLITE_PATH", str(DEFAULT_DB_PATH))).expanduser()
INIT_SQL_PATH = BASE_DIR / "scripts" / "init_db.sql"


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    # SQLite 연결을 열고 프로젝트 공통 설정을 적용한다.
    #
    # - DB 폴더가 없으면 자동 생성한다.
    # - row를 dict처럼 읽을 수 있도록 `sqlite3.Row`를 사용한다.
    # - 외래키 제약을 켜서 잘못된 applicant/company 참조를 막는다.
    #
    # DB 파일이 아직 없어도 연결 전에 폴더를 만들어 둔다.
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # sqlite3 기본 row는 tuple이라 컬럼명을 알기 어렵다.
    # Row를 쓰면 row["name"]처럼 안전하게 접근할 수 있다.
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # SQLite는 연결마다 외래키 검사를 켜야 한다.
    # 회사 삭제/지원자 참조 오류 같은 데이터 무결성 문제를 막는다.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_database(db_path: Path = DB_PATH) -> None:
    # `init_db.sql` 전체를 실행해서 DB 스키마를 생성/초기화한다.
    if not INIT_SQL_PATH.exists():
        raise FileNotFoundError(f"초기화 SQL 파일이 없습니다: {INIT_SQL_PATH}")

    # init_db.sql이 이 프로젝트의 단일 스키마 원본이다.
    sql = INIT_SQL_PATH.read_text(encoding="utf-8").strip()
    if not sql:
        raise ValueError(f"초기화 SQL 파일이 비어 있습니다: {INIT_SQL_PATH}")

    conn = get_connection(db_path)
    try:
        # executescript는 여러 SQL 문을 한 번에 실행한다.
        # DROP/CREATE/INDEX/TRIGGER 정의가 모두 init_db.sql 안에 들어 있다.
        conn.executescript(sql)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    # CLI에서 직접 실행할 때 DB를 생성하고 결과 경로를 출력한다.
    create_database()
    print(f"DB 생성 완료: {DB_PATH}")


if __name__ == "__main__":
    main()
