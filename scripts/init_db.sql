-- 지원자 트래커 SQLite 스키마 초기화 SQL.
-- 실행하면 기존 운영/테스트 테이블을 삭제한 뒤 현재 구조로 다시 생성한다.
-- 하네스와 `scripts/init_db.py`가 이 파일을 기준 스키마로 사용한다.

PRAGMA foreign_keys = OFF;

DROP VIEW IF EXISTS applicant_fixed_columns;
DROP TABLE IF EXISTS harness_failures;
DROP TABLE IF EXISTS harness_case_results;
DROP TABLE IF EXISTS harness_runs;
DROP TABLE IF EXISTS email_events;
DROP TABLE IF EXISTS applicants;
DROP TABLE IF EXISTS companies;

PRAGMA foreign_keys = ON;

CREATE TABLE companies (
  -- 채용 회사 마스터. name은 중복 방지를 위해 UNIQUE로 관리한다.
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  contact_person TEXT,
  contact_email TEXT,
  contact_phone TEXT,
  notion_page_id TEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE applicants (
  -- 지원자 마스터. 한 지원자는 하나의 채용 회사(company_id)에 연결된다.
  id INTEGER PRIMARY KEY,
  company_id INTEGER REFERENCES companies(id),

  name TEXT NOT NULL,
  birth_year INTEGER,
  age INTEGER,
  age_international INTEGER,
  age_korean INTEGER,

  email TEXT,
  phone TEXT,
  position TEXT,

  education TEXT,
  experience TEXT,
  skills TEXT,
  notes TEXT,
  salary_current TEXT,
  salary_expected TEXT,
  extra_properties TEXT,

  status TEXT NOT NULL DEFAULT '서류접수',

  duplicate_check_status TEXT NOT NULL DEFAULT '정상',
  duplicate_check_memo TEXT,

  notion_page_id TEXT,

  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE email_events (
  -- 원문 메시지/메일 처리 이력. 지원자 정보가 업데이트되어도 이벤트는 누적된다.
  id INTEGER PRIMARY KEY,
  applicant_id INTEGER REFERENCES applicants(id),
  company_id INTEGER REFERENCES companies(id),

  source_type TEXT DEFAULT 'manual',
  source_message_id TEXT,

  event_type TEXT,
  raw_text TEXT,
  parsed_summary TEXT,
  status TEXT,

  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE harness_runs (
  -- 하네스 1회 실행 결과 요약.
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL,
  total_count INTEGER NOT NULL DEFAULT 0,
  success_count INTEGER NOT NULL DEFAULT 0,
  fail_count INTEGER NOT NULL DEFAULT 0,
  report_path TEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE harness_case_results (
  -- 하네스 내 개별 샘플 파일 실행 결과.
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  case_file TEXT NOT NULL,
  status TEXT NOT NULL,
  error_code TEXT,
  error_message TEXT,
  parsed_json TEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE harness_failures (
  -- 반복 실패 케이스를 원문 해시 기준으로 추적하는 테이블.
  id INTEGER PRIMARY KEY,
  case_file TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  error_code TEXT NOT NULL,
  error_message TEXT NOT NULL,
  raw_text TEXT,
  fixed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(case_file, input_hash, error_code)
);

CREATE INDEX idx_applicants_name ON applicants(name);
CREATE INDEX idx_applicants_email ON applicants(email);
CREATE INDEX idx_applicants_phone ON applicants(phone);
CREATE INDEX idx_applicants_company ON applicants(company_id);
CREATE INDEX idx_applicants_name_birth_company ON applicants(name, birth_year, company_id);
CREATE INDEX idx_email_events_applicant ON email_events(applicant_id);
CREATE INDEX idx_email_events_company ON email_events(company_id);
CREATE INDEX idx_harness_case_results_run_id ON harness_case_results(run_id);
CREATE INDEX idx_harness_failures_fixed ON harness_failures(fixed);

CREATE VIEW applicant_fixed_columns AS
SELECT
  c.name AS 기업명,
  a.position AS 포지션,
  a.name AS 이름,
  a.birth_year AS 생년,
  a.age_international AS 나이,
  a.salary_expected AS 희망연봉,
  a.salary_current AS 최종연봉,
  a.notes AS 기타
FROM applicants a
LEFT JOIN companies c ON c.id = a.company_id;

CREATE TRIGGER trg_applicants_updated AFTER UPDATE ON applicants
BEGIN
  UPDATE applicants SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;

CREATE TRIGGER trg_companies_updated AFTER UPDATE ON companies
BEGIN
  UPDATE companies SET updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;
