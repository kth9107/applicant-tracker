# 지원자 원문/AI JSON을 SQLite와 Notion에 저장하는 핵심 모듈.
#
# 이 파일은 프로젝트의 저장 계층이다. Discord, CLI, 하네스 테스트에서 모두
# 최종적으로 이 모듈을 호출한다.
#
# 큰 흐름:
# 1. 원문 텍스트를 rule parser로 `parsed` dict로 변환하거나,
#    AI JSON을 같은 `parsed` dict 형식으로 정규화한다.
# 2. 필수값(후보자 이름)을 검증하고 누락 정보를 진단한다.
# 3. 회사와 지원자를 SQLite에 upsert한다.
# 4. 원문과 파싱 결과를 `email_events`에 남겨 추적 가능하게 한다.
# 5. 설정된 경우 Notion DB에도 같은 정보를 생성/업데이트한다.

import argparse
import datetime
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from create_db import DB_PATH, get_connection


BASE_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = BASE_DIR / ".env"
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
NOTION_COMPANY_PROP = "기업명"
NOTION_POSITION_PROP = "포지션"
NOTION_BIRTH_YEAR_PROP = "생년"
NOTION_AGE_PROP = "나이"
NOTION_EXPECTED_SALARY_PROP = "희망연봉"
NOTION_CURRENT_SALARY_PROP = "최종연봉"
NOTION_NOTES_PROP = "기타"
_NOTION_AGE_PROPERTIES_READY = False
DEFAULT_STATUS = "대기"
VALID_STATUSES = {
    "대기",
    "서류접수",
    "서류합격",
    "서류불합격",
    "면접대기",
    "면접완료",
    "최종합격",
    "최종불합격",
    "보류",
}
STATUS_ALIASES = {
    "pending": "대기",
    "wait": "대기",
    "waiting": "대기",
    "unknown": "대기",
    "none": "대기",
    "null": "대기",
    "": "대기",
    "보류 중": "보류",
}
NOTION_REQUIRED_PROPERTIES = {
    NOTION_COMPANY_PROP: {"rich_text": {}},
    NOTION_POSITION_PROP: {"rich_text": {}},
    NOTION_BIRTH_YEAR_PROP: {"number": {}},
    NOTION_AGE_PROP: {"number": {}},
    NOTION_EXPECTED_SALARY_PROP: {"rich_text": {}},
    NOTION_CURRENT_SALARY_PROP: {"rich_text": {}},
    NOTION_NOTES_PROP: {"rich_text": {}},
}
RETIRED_NOTION_PROPERTIES = {
    "상태",
    "출생년도",
    "만나이",
    "한국나이",
    "이메일",
    "전화",
    "지원회사",
    "회사담당자",
    "지원직무",
    "스킬",
    "비고",
    "연봉",
}
FIXED_NOTION_PROPERTIES = set(NOTION_REQUIRED_PROPERTIES) | RETIRED_NOTION_PROPERTIES | {"이름"}
SUPPORTED_DYNAMIC_NOTION_TYPES = {
    "rich_text",
    "number",
    "select",
    "multi_select",
    "checkbox",
    "url",
    "email",
    "phone_number",
    "date",
}
_NOTION_DATABASE_PROPERTIES_CACHE: Optional[dict[str, Any]] = None


def load_env_file() -> None:
    # `.env` 설정을 읽어 Notion 토큰/DB ID 등 저장에 필요한 값을 로드한다.
    if not ENV_PATH.exists():
        return

    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_env_file()


def normalize_text(text: str) -> str:
    # 운영체제별 줄바꿈 차이를 정리하고 앞뒤 공백을 제거한다.
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def clean_name(value: str) -> str:
    # 이름 뒤에 붙은 호칭, 괄호 설명, 이메일 표시 등을 제거한다.
    value = re.sub(r"[<\(\[].*$", "", value).strip()
    value = re.sub(r"(님|씨|담당자|차장|과장|부장|매니저)$", "", value).strip()
    return value


def clean_company_name(value: str) -> str:
    # 회사명 후보 문자열을 한 줄 회사명으로 정리한다.
    lines = [
        line.strip()
        for line in value.splitlines()
        if line.strip() and line.strip() != "---"
    ]
    value = lines[-1] if lines else value.strip()
    value = re.sub(r"\s+", " ", value).strip()
    return value


def canonical_company_name(value: str) -> str:
    # 중복 판정을 위한 회사명 표준 키를 만든다.
    #
    # DB와 Notion에는 원래 표시명을 보존하되, `주식회사/㈜/(주)` 유무처럼
    # 실무 입력에서 흔히 흔들리는 접미사는 같은 회사로 판단한다.
    #
    value = clean_company_name(value).lower()
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"^(주식회사|㈜|\(주\))", "", value)
    value = re.sub(r"(주식회사|㈜|\(주\)|co\\.,?ltd\\.?|corp\\.?|inc\\.?)$", "", value)
    return value.strip()


def company_keys_are_similar(left: str, right: str) -> bool:
    # 회사 표준 키가 부분 표기 차이 수준으로 비슷한지 판단한다.
    left_key = canonical_company_name(left)
    right_key = canonical_company_name(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    shorter, longer = sorted([left_key, right_key], key=len)
    return len(shorter) >= 3 and shorter in longer


def company_like_patterns(company_name: str) -> list[str]:
    # SQLite LIKE 검색에 사용할 회사명 후보 패턴을 만든다.
    key = canonical_company_name(company_name)
    if len(key) < 2:
        return []
    patterns = [f"%{key}%"]
    compact = re.sub(r"\s+", "", clean_company_name(company_name))
    if compact and compact != key and len(compact) >= 2:
        patterns.append(f"%{compact}%")
    return list(dict.fromkeys(patterns))


def is_recruiting_agency_name(value: str) -> bool:
    # 회사명 후보가 채용사가 아니라 추천사/헤드헌터인지 판단한다.
    value = value.lower()
    agency_keywords = [
        "recruitment",
        "headhunt",
        "head hunt",
        "헤드헌트",
        "헤드헌팅",
        "서치펌",
    ]
    return any(keyword in value for keyword in agency_keywords)


def is_likely_school_name(value: str) -> bool:
    # 회사명 자리에 출신 학교/학과가 들어온 것으로 보이면 회사로 쓰지 않는다.
    value = clean_company_name(to_text(value))
    school_keywords = [
        "대학교",
        "대학",
        "고등학교",
        "전문학교",
        "학점은행",
        "학과",
        "졸업",
        "편입",
    ]
    return any(keyword in value for keyword in school_keywords)


def normalize_optional_company_name(value: str) -> str:
    # 회사명이 없거나 학교/추천사로 보이면 빈 값으로 정리한다.
    company_name = clean_company_name(to_text(value))
    if not company_name or company_name == "UNKNOWN":
        return ""
    if is_recruiting_agency_name(company_name) or is_likely_school_name(company_name):
        return ""
    return company_name


def is_suspicious_candidate_name(value: str) -> bool:
    # AI가 이름 대신 직함/역할을 넣은 것으로 보이는 값인지 판단한다.
    value = clean_name(to_text(value))
    suspicious_values = {
        "수석",
        "컨설턴트",
        "담당자",
        "대리",
        "과장",
        "차장",
        "부장",
        "팀장",
        "이사",
        "상무",
        "전무",
        "후보자",
        "상담사",
        "매니저",
        "어드바이저",
        "리크루터",
    }
    suspicious_fragments = {
        "피드백",
    }
    return (
        not value
        or value in suspicious_values
        or any(fragment in value for fragment in suspicious_fragments)
    )


def choose_candidate_name(ai_name: str, raw_text: str) -> str:
    # AI 이름이 의심스럽거나 비어 있을 때만 rule로 fallback한다.
    # "원문에 있는지" 검사는 제거 — AI가 정확히 추출한 이름을 불필요하게 교체하는 원인이었다.
    cleaned_ai_name = clean_name(to_text(ai_name))
    if cleaned_ai_name and not is_suspicious_candidate_name(cleaned_ai_name):
        return cleaned_ai_name
    rule_name = extract_candidate_name(raw_text)
    return rule_name if rule_name else "UNKNOWN"


def clean_position(value: str) -> str:
    # 직무명에서 '업데이트/변경/지원직무는' 같은 문장 표현을 제거한다.
    value = to_text(value)
    value = re.sub(r"(?:로|으로)?\s*(?:업데이트|변경|수정|추가)\s*$", "", value).strip()
    value = re.sub(r"(?:지원직무|직무)\s*(?:는|은|:)?\s*", "", value).strip()
    value = re.sub(r"\s*(?:포지션|직군|직무)\s*$", "", value).strip()
    value = re.sub(r"^(.*[가-힣])직$", r"\1", value).strip()
    return value


def normalize_status(value: Any) -> str:
    # 상태값을 Notion/SQLite에서 쓰는 한국어 select 값으로 정규화한다.
    text = to_text(value)
    lowered = text.lower()
    if lowered in STATUS_ALIASES:
        return STATUS_ALIASES[lowered]
    if text in VALID_STATUSES:
        return text
    return DEFAULT_STATUS


def has_explicit_status(text: str) -> bool:
    # 원문에 상태 변경 단서가 실제로 있는지 확인한다.
    status_keywords = [
        "대기",
        "pending",
        "서류접수",
        "서류합격",
        "서류불합격",
        "면접대기",
        "면접완료",
        "최종합격",
        "최종불합격",
        "보류",
        "합격",
        "불합격",
        "사퇴",
        "거절",
    ]
    return any(keyword in text for keyword in status_keywords)


def extract_email(text: str) -> str:
    # 원문에서 첫 번째 이메일 주소를 추출한다.
    match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    return match.group(0).strip() if match else ""


def extract_phone(text: str) -> str:
    # 원문에서 한국 휴대전화 번호를 추출하고 공백을 하이픈으로 정리한다.
    match = re.search(r"01[016789][-\s]?\d{3,4}[-\s]?\d{4}", text)
    return match.group(0).replace(" ", "-") if match else ""


def extract_salary_amount(text: str) -> str:
    # 한 줄에서 연봉 금액처럼 보이는 숫자 표현을 원문 표기 그대로 추출한다.
    amount_pattern = r"((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*(?:억|만원|만\s*원|원)?)"
    match = re.search(amount_pattern, text)
    return re.sub(r"\s+", "", match.group(1)) if match else ""


def extract_expected_salary(text: str) -> str:
    # `희망연봉`은 Notion의 희망연봉 고정 컬럼으로 보낼 값이다.
    patterns = [
        r"희망\s*연봉\s*(?:은|는|:|：)?\s*([^\n;/]+)",
        r"연봉\s*희망\s*(?:은|는|:|：)?\s*([^\n;/]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            amount = extract_salary_amount(match.group(1))
            if amount:
                return amount
    return ""


def extract_current_salary(text: str) -> str:
    # 희망연봉이 아닌 연봉 표현은 최종연봉 컬럼으로 보낸다.
    explicit_patterns = [
        r"(?:최종|현재|현|직전|기존|전직장)\s*연봉\s*(?:은|는|:|：)?\s*([^\n;/]+)",
        r"연봉\s*(?:은|는|:|：)\s*([^\n;/]+)",
    ]
    for pattern in explicit_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            amount = extract_salary_amount(match.group(1))
            if amount:
                return amount

    for line in text.splitlines():
        if "연봉" not in line or "희망" in line:
            continue
        amount = extract_salary_amount(line)
        if amount:
            return amount

    return ""


def extract_candidate_name(text: str) -> str:
    # 원문에서 지원자 이름을 rule 기반 정규식으로 추출한다.
    #
    # 다양한 입력 예시를 처리한다:
    # - `서인영 지원자`
    # - `김태현 1991년 만 34세`
    # - `SM엔터 주식회사 서인영 지원자 1989년생`
    # - `케미콘 주식회사의 김태현 지원자님`
    #
    patterns = [
        r"성명\s*[:：]\s*([가-힣A-Za-z\s]+)",
        r"이름\s*[:：]\s*([가-힣A-Za-z\s]+)",
        r"(?:주식회사|㈜|\(주\)|코리아|Korea)의\s*([가-힣A-Za-z]{2,20})\s*지원자",
        r"[가-힣A-Za-z0-9&\- ]+(?:주식회사|㈜|\(주\)|코리아|Korea)의\s*([가-힣A-Za-z]{2,20})",
        r"[가-힣A-Za-z0-9&\- ]+(?:주식회사|㈜|\(주\)|코리아|Korea)\s+([가-힣A-Za-z]{2,20})\s*지원자",
        r"^\s*([가-힣A-Za-z]{2,20})\s*지원자(?:님)?\s*$",
        r"후보자\s*[:：]?\s*([가-힣A-Za-z]{2,20})\s*(?:후보자)?\s*[\(\[]",
        r"추천해주신\s+([가-힣A-Za-z]{2,20})\s*후보자",
        r"^\s*\d+\.\s*([가-힣A-Za-z]{2,20})\s*[\(\[]",
        r"^\s*([가-힣A-Za-z]{2,20})\s*[\(\[]\s*(?:19|20)\d{2}년생",
        r"^\s*([가-힣A-Za-z]{2,20})\s+(?:19|20)\d{2}년\s+만\s*\d{1,2}세",
        r"^\s*([가-힣A-Za-z]{2,20})\s+(?:19|20)\d{2}년\b",
        r"^\s*([가-힣A-Za-z]{2,20})\s+(?:서류합격|서류불합격|면접대기|면접완료|최종합격|최종불합격|보류)\b",
        r"^\s*([가-힣A-Za-z]{2,20})\s+(?:합격|불합격|사퇴|거절)\b",
        r"Candidate\s*[:：]\s*([A-Za-z\s]+)",
        r"Name\s*[:：]\s*([A-Za-z\s]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            candidate_name = clean_name(match.group(1))
            if is_suspicious_candidate_name(candidate_name):
                continue
            return candidate_name

    ignored_single_lines = {
        "안녕하세요",
        "감사합니다",
        "부탁드립니다",
        "확인부탁드립니다",
    }
    for line in text.splitlines()[:5]:
        candidate_name = clean_name(line.strip())
        if not re.fullmatch(r"[가-힣]{2,4}", candidate_name):
            continue
        if candidate_name in ignored_single_lines or is_suspicious_candidate_name(candidate_name):
            continue
        return candidate_name

    return ""


def extract_birth_year(text: str) -> Optional[int]:
    # `1988년생`, `생년: 1988` 같은 표현에서 출생년도를 추출한다.
    patterns = [
        r"생년\s*[:：]\s*((?:19|20)\d{2})",
        r"출생\s*[:：]\s*((?:19|20)\d{2})",
        r"Birth\s*Year\s*[:：]\s*((?:19|20)\d{2})",
        r"\b((?:19|20)\d{2})년생\b",
        r"\b((?:19|20)\d{2})년\s+만\s*\d{1,2}세\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))

    return None


def extract_international_age(text: str) -> Optional[int]:
    # `만 34세`처럼 원문에 명시된 만나이를 추출한다.
    patterns = [
        r"만\s*(\d{1,2})\s*세",
        r"international\s*age\s*[:：]\s*(\d{1,2})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))

    return None


def calculate_international_age(birth_year: Optional[int]) -> Optional[int]:
    # 출생년도만 있을 때 현재 연도 기준 만나이를 계산한다.
    if not birth_year:
        return None
    return datetime.date.today().year - birth_year


def calculate_korean_age(birth_year: Optional[int]) -> Optional[int]:
    # 출생년도 기준 한국나이를 계산한다. 계산식은 현재연도 - 출생년도 + 1.
    if not birth_year:
        return None
    return datetime.date.today().year - birth_year + 1


_COMPANY_SUFFIX_RE = (
    r"주식회사|유한회사|합자회사|합명회사"
    r"|㈜|\(주\)|\(유\)"
    r"|코리아|Korea"
    r"|산업|물산|통상|상사|상역"
    r"|전자|화학|소재|바이오|테크|게임즈|일렉트로닉스"
    r"|홀딩스|그룹|Holdings|Group"
    r"|제약|건설|엔지니어링|건업"
    r"|솔루션즈?|시스템즈?"
    r"|인터내셔널|International|글로벌|Global"
    r"|컴퍼니|Company"
    r"|Ltd\.?|Limited|Inc\.?|Corp\.?|Corporation|LLC|GmbH"
    r"|엔터테인먼트|엔터|Entertainment"
    r"|네트웍스?|Networks?"
    r"|파트너스|Partners"
    r"|어소시에이츠|Associates"
    r"|서비스|Services?"
)


def extract_company_name(text: str) -> str:
    # 원문에서 채용 회사명을 추출한다.
    #
    # 추천 에이전시가 아니라 지원자가 입사하려는 회사명을 우선으로 잡는다.
    # rule parser 단계에서는 `_COMPANY_SUFFIX_RE` 접미사 패턴과 구조 패턴으로 찾는다.
    #
    _sfx = _COMPANY_SUFFIX_RE
    patterns = [
        rf"([가-힣A-Za-z0-9&\- ]+(?:{_sfx}))의\s*[가-힣A-Za-z]{{2,20}}\s*지원자",
        rf"([가-힣A-Za-z0-9&\- ]+(?:{_sfx}))의\s*[가-힣A-Za-z]{{2,20}}",
        rf"([가-힣A-Za-z0-9&\- ]+(?:{_sfx}))\s+[가-힣A-Za-z]{{2,20}}\s*지원자",
        r"회사명\s*[:：]\s*([^\n]+)",
        r"기업명\s*[:：]\s*([^\n]+)",
        rf"^[^\S\n]*([가-힣A-Za-z0-9&\- ]+(?:{_sfx}))[^\S\n]*$",
        rf"제목\s*:\s*\[([^\]]*(?:{_sfx})[^\]]*)\]",
        r"제목\s*:\s*\[[^\]]+\]\s*([가-힣A-Za-z0-9&\- ]+?)\s+[가-힣A-Za-z0-9&\- ]*포지션",
        r"수신\s*[:：]\s*([가-힣A-Za-z0-9()&\- ]{2,50})",
        r"^([가-힣A-Za-z0-9()&\-\. ]{2,50})\n[가-힣A-Za-z]{2,6}\s*담당자",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            company_name = clean_company_name(match.group(1))
            if is_recruiting_agency_name(company_name):
                continue
            return company_name

    return "UNKNOWN"


def extract_contact_person(text: str) -> str:
    # 채용사 담당자 이름을 추출한다.
    patterns = [
        r"담당자\s+([가-힣A-Za-z]{2,20})",
        r"인사팀\s*([가-힣A-Za-z]{2,20})입니다",
        r"([가-힣A-Za-z]{2,20})\s*담당자님",
        r"받는사람\s*:\s*([가-힣A-Za-z\s]+)\s*<",
        r"담당자\s*[:：]\s*([가-힣A-Za-z\s]+)",
        r"Contact\s*[:：]\s*([A-Za-z\s]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return clean_name(match.group(1))

    return ""


def extract_contact_email(text: str) -> str:
    # 채용사 담당자 이메일을 추출한다.
    #
    # 합격/서류전형 회신 메일은 발신자가 채용사일 가능성이 높으므로 `보낸사람`을
    # 우선하고, 추천 메일은 `받는사람`을 우선한다.
    #
    if "인사팀" in text or "서류전형" in text or "합격" in text:
        match = re.search(r"보낸사람\s*:[^\n<]*<([^>]+)>", text)
        if match:
            return match.group(1).strip()

    match = re.search(r"받는사람\s*:[^\n<]*<([^>]+)>", text)
    if match:
        return match.group(1).strip()
    return extract_email(text)


def extract_position(text: str) -> str:
    # 지원 직무 또는 업데이트할 직무명을 추출한다.
    patterns = [
        r"(?:지원직무|직무)\s*(?:는|은|:)?\s*([^\n]+?)(?:로|으로)?\s*(?:업데이트|변경|수정)",
        r"(?:지원직무|직무)\s*(?:는|은|:)?\s*([^\n]+)",
        r"^[^\S\n]*([가-힣A-Za-z0-9 ][가-힣A-Za-z0-9 ]*지원)[^\S\n]*$",
        r"모집 중이신[^\S\n]*([가-힣A-Za-z0-9 ][가-힣A-Za-z0-9 ]*?)[^\S\n]*포지션",
        r"제목\s*:\s*\[[^\]]+\][^\S\n]*\S+[^\S\n]+([가-힣A-Za-z0-9 ][가-힣A-Za-z0-9 ]*?)[^\S\n]*포지션",
        r"([가-힣A-Za-z0-9 ][가-힣A-Za-z0-9 ]*?)[^\S\n]*포지션",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.MULTILINE)
        if match:
            value = clean_position(match.group(1))
            if value and value not in {"금번 모집 중이신"}:
                return value

    return ""


def extract_status(text: str) -> str:
    # 메일 내용에서 지원 상태를 추출한다. 없으면 기본값은 `서류접수`다.
    status_patterns = [
        ("최종합격", "최종합격"),
        ("최종 불합격", "최종불합격"),
        ("최종불합격", "최종불합격"),
        ("서류전형 합격", "서류합격"),
        ("서류합격", "서류합격"),
        ("서류 합격", "서류합격"),
        ("서류불합격", "서류불합격"),
        ("서류 불합격", "서류불합격"),
        ("면접 진행 예정", "면접대기"),
        ("면접대기", "면접대기"),
        ("면접완료", "면접완료"),
        ("보류", "보류"),
    ]

    for keyword, status in status_patterns:
        if keyword in text:
            return status

    return DEFAULT_STATUS


def extract_intent(text: str) -> str:
    # 입력이 신규 등록인지, 정보 업데이트인지, 상태 업데이트인지 판단한다.
    if any(keyword in text for keyword in ["업데이트", "변경", "수정", "추가"]):
        status_keywords = ["서류합격", "서류불합격", "면접대기", "면접완료", "최종합격", "최종불합격", "보류"]
        if any(keyword in text for keyword in status_keywords):
            return "status_update"
        return "update"
    return "create"


def is_profile_noise_line(line: str) -> bool:
    # 후보자 프로필 설명이 아니라 메일 인사/첨부/마감 문장 같은 운영 문장인지 판단한다.
    line = re.sub(r"\s+", " ", line).strip()
    if not line:
        return True
    noise_patterns = [
        r"^(안녕하세요|감사합니다|아무쪼록|잘 부탁드립니다)",
        r"(금번 모집|이번 모집|후보자 추천드립니다|추천드립니다\.$)",
        r"^[가-힣A-Za-z]{2,20}\s+(?:서류합격|서류불합격|면접대기|면접완료|최종합격|최종불합격|보류|합격|불합격|사퇴|거절)$",
        r"(이력서|첨부파일|첨부드립니다|검토부탁|검토 부탁|회신|궁금하신 사항)",
        r"(JAC Recruitment|HeadHunt|헤드헌트|컨설턴트)",
        r"^\[?후보자\s*리스트\]?$",
        r"(최종\s*연봉|현재\s*연봉|현\s*연봉|희망\s*연봉|연봉\s*[:：])",
        r"담당자님?$",
        r"^-+$",
    ]
    return any(re.search(pattern, line, re.IGNORECASE) for pattern in noise_patterns)


def collect_bullets(text: str) -> list[str]:
    # 본문에서 이력/학력/스킬 후보 줄을 수집한다.
    #
    # 하이픈 bullet이 있으면 bullet만 사용하고, 없으면 명령어/회사명/담당자 등
    # 구조 정보로 보이는 줄을 제외한 나머지를 프로필 줄로 본다.
    #
    bullets = [
        line.strip("- ").strip()
        for line in text.splitlines()
        if line.strip().startswith("-")
    ]
    if bullets:
        return bullets

    profile_lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if is_profile_noise_line(line):
            continue
        if line.startswith("!지원자") or line.startswith("!applicant"):
            continue
        if re.search(r"(담당자|보낸사람|받는사람|제목)", line):
            continue
        if re.search(r"(주식회사|㈜|\(주\)|코리아|Korea)$", line):
            continue
        if re.search(r"(주식회사|㈜|\(주\)|코리아|Korea)\s+[가-힣A-Za-z]{2,20}\s*지원자", line):
            continue
        if re.search(r"^[가-힣A-Za-z]{2,20}\s*지원자(?:님)?$", line):
            continue
        if re.search(r"^(?:19|20)\d{2}년생?$", line):
            continue
        if re.search(r"^[가-힣A-Za-z0-9\s]+지원$", line):
            continue
        profile_lines.append(line)
    return profile_lines


def split_profile_lines(text: str) -> dict[str, list[str]]:
    # 수집한 프로필 줄을 학력, 경력, 스킬, 비고로 분류한다.
    bullets = collect_bullets(text)
    education = []
    experience = []
    skills = []
    notes = []

    for line in bullets:
        if is_profile_noise_line(line):
            continue
        if any(keyword in line for keyword in ["대학교", "대학", "졸업", "학과"]):
            education.append(line)
        elif any(keyword in line for keyword in ["년", "경력", "팀", "회사", "기업"]):
            experience.append(line)
        elif any(keyword in line for keyword in ["영어", "일본어", "능력", "면허", "가능", "스킬", "개발언어", "JLPT", "jltp", "토익", "TOEIC", "자격"]):
            skills.append(line)
        else:
            notes.append(line)

    return {
        "education": education,
        "experience": experience,
        "skills": skills,
        "notes": [line for line in notes if not is_profile_noise_line(line)],
    }


def extract_notes_summary(text: str) -> str:
    # 고정 컬럼에 바로 들어가지 않는 후보자 설명을 `기타`용 문장으로 모은다.
    #
    # AI가 notes를 비워 둔 경우에도 긴 추천 사유/경력 설명이 Notion `기타`
    # 컬럼에 남도록 하는 보강 함수다. 정교한 요약 모델 대신 원문 핵심 줄을
    # 보존하는 방식이라 빠르고 예측 가능하다.
    company_name = normalize_optional_company_name(extract_company_name(text))
    note_lines = []
    for line in collect_bullets(text):
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        if company_name and company_keys_are_similar(line, company_name):
            continue
        if is_profile_noise_line(line):
            continue
        if re.search(r"(최종\s*연봉|현재\s*연봉|현\s*연봉|희망\s*연봉|연봉\s*[:：])", line):
            continue
        if re.search(r"^[가-힣A-Za-z]{2,20}\s*[\(\[]?\s*(?:19|20)\d{2}년생?", line):
            continue
        if re.search(r"^(?:ID|EXPECTED_|보낸사람|받는사람|제목)\s*[:：]", line, re.IGNORECASE):
            continue
        if line.startswith("---"):
            continue
        note_lines.append(line)

    return " ".join(dict.fromkeys(note_lines)).strip()


def parse_email_text(raw_text: str) -> dict[str, Any]:
    # 메일/Discord 원문을 저장 가능한 `parsed` dict로 변환한다.
    #
    # 이 함수가 rule parser의 중심이다. AI 없이도 빠르게 저장하기 위해 회사명,
    # 지원자명, 생년, 나이, 직무, 스킬 등을 정규식과 키워드로 추출한다.
    #
    text = normalize_text(raw_text)
    profile = split_profile_lines(text)
    birth_year = extract_birth_year(text)
    age_international = extract_international_age(text)
    if age_international is None:
        age_international = calculate_international_age(birth_year)
    age_korean = calculate_korean_age(birth_year)

    company_name = normalize_optional_company_name(extract_company_name(text))

    parsed = {
        "candidate_name": extract_candidate_name(text),
        "company_name": company_name,
        "company_canonical_name": canonical_company_name(company_name),
        "contact_person": extract_contact_person(text),
        "contact_email": extract_contact_email(text),
        "contact_phone": "",
        "birth_year": birth_year,
        "age": age_international,
        "age_international": age_international,
        "age_korean": age_korean,
        "email": extract_email(text),
        "phone": extract_phone(text),
        "position": extract_position(text),
        "education": profile["education"],
        "experience": profile["experience"],
        "skills": profile["skills"],
        "notes": extract_notes_summary(text),
        "salary_current": extract_current_salary(text),
        "salary_expected": extract_expected_salary(text),
        "extra_properties": {},
        "status": normalize_status(extract_status(text)),
        "intent": extract_intent(text),
        "raw_text": text,
    }
    return normalize_salary_fields(parsed)


def split_candidate_blocks(raw_text: str) -> list[str]:
    # 번호 목록으로 된 다중 후보자 메일을 후보자별 원문 조각으로 나눈다.
    #
    # 예를 들어 `1. 김대호 ... 2. 이영희 ...`처럼 한 메일에 여러 명이 들어오면
    # 저장 계층에서 한 명만 저장하지 않도록 공통 헤더와 각 후보자 블록을 합친
    # 원문을 여러 개 만든다. 번호 목록이 아니면 빈 리스트를 반환한다.
    #
    text = normalize_text(raw_text)
    matches = list(re.finditer(r"(?m)^\s*\d+\.\s+[가-힣A-Za-z]{2,20}\s*[\(\[]", text))
    if len(matches) < 2:
        return []

    header = text[:matches[0].start()].strip()
    blocks = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.start():end].strip()
        blocks.append("\n\n".join(part for part in [header, block] if part))
    return blocks


def candidate_records_from_extracted_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    # AI JSON에서 여러 후보자 배열을 찾아 단일 후보자 JSON 목록으로 펼친다.
    #
    # 최신 프롬프트는 `applicants`를 권장하지만, 기존 샘플/모델이 `candidates`로
    # 응답할 수도 있어 둘 다 허용한다. 각 항목은 기존 `parsed_from_extracted_data`
    # 함수가 이해하는 `company + applicant` 구조로 정규화한다.
    #
    raw_candidates = data.get("applicants")
    if not isinstance(raw_candidates, list):
        raw_candidates = data.get("candidates")
    if not isinstance(raw_candidates, list) or len(raw_candidates) < 2:
        return []

    records = []
    for candidate in raw_candidates:
        if not isinstance(candidate, dict):
            continue
        applicant = candidate.get("applicant") if isinstance(candidate.get("applicant"), dict) else candidate
        record = {
            "intent": data.get("intent") or "create",
            "company": data.get("company") or {},
            "applicant": {
                "name": applicant.get("name"),
                "birth_year": applicant.get("birth_year"),
                "age_international": applicant.get("age_international") or applicant.get("age"),
                "age_korean": applicant.get("age_korean"),
                "email": applicant.get("email"),
                "phone": applicant.get("phone"),
                "position": applicant.get("position") or data.get("position"),
                "education": applicant.get("education"),
                "experience": applicant.get("experience") or applicant.get("career"),
                "skills": (
                    to_list(applicant.get("skills"))
                    + to_list(applicant.get("languages"))
                    + to_list(applicant.get("certifications"))
                    + to_list(applicant.get("strengths"))
                ),
                "notes": applicant.get("notes"),
                "salary_current": applicant.get("salary_current"),
                "salary_expected": applicant.get("salary_expected"),
                "extra_properties": applicant.get("extra_properties") or candidate.get("extra_properties"),
            },
            "updates": data.get("updates") or {},
            "status": normalize_status(data.get("status")),
        }
        if not record["company"].get("name"):
            record["company"]["name"] = data.get("hiring_company")
        if not record["company"].get("canonical_name"):
            record["company"]["canonical_name"] = data.get("hiring_company_canonical") or data.get("hiring_company")
        records.append(record)
    return records


def to_int(value: Any) -> Optional[int]:
    # 문자열/숫자 값에서 첫 번째 정수를 꺼낸다. 없으면 None을 반환한다.
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    match = re.search(r"\d+", str(value))
    return int(match.group(0)) if match else None


def to_text(value: Any) -> str:
    # None을 빈 문자열로 바꾸고 나머지는 strip된 문자열로 변환한다.
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"null", "none", "unknown", "n/a", "na"}:
        return ""
    return text


def to_list(value: Any) -> list[str]:
    # AI가 준 문자열/배열 값을 빈 값 없는 문자열 리스트로 정규화한다.
    if value is None:
        return []
    if isinstance(value, list):
        return [to_text(item) for item in value if to_text(item)]
    text = to_text(value)
    if not text:
        return []
    return [line.strip("- ").strip() for line in text.splitlines() if line.strip()]


def parsed_from_extracted_data(data: dict[str, Any], raw_text: str) -> dict[str, Any]:
    # AI가 만든 JSON을 rule parser와 같은 `parsed` dict 형식으로 맞춘다.
    #
    # 이후 저장 로직은 AI 결과인지 rule 결과인지 구분하지 않고 같은 함수를 쓴다.
    # 이 덕분에 SQLite/Notion 저장 코드를 한 곳으로 유지할 수 있다.
    #
    company = data.get("company") or {}
    applicant = data.get("applicant") or {}
    if not applicant and isinstance(data.get("applicants"), list) and data["applicants"]:
        first_applicant = data["applicants"][0]
        if isinstance(first_applicant, dict):
            applicant = (
                first_applicant.get("applicant")
                if isinstance(first_applicant.get("applicant"), dict)
                else first_applicant
            )
    updates = data.get("updates") or {}
    raw_birth_year = extract_birth_year(raw_text)
    raw_age_international = extract_international_age(raw_text)
    birth_year = to_int(applicant.get("birth_year"))
    if raw_birth_year is not None:
        birth_year = raw_birth_year
    elif not (birth_year and 1940 <= birth_year <= 2010):
        birth_year = None

    age_international = to_int(applicant.get("age_international"))
    if raw_age_international is not None:
        age_international = raw_age_international
    elif not (age_international and 18 <= age_international <= 80):
        age_international = None
    if age_international is None:
        age_international = calculate_international_age(birth_year)

    age_korean = to_int(applicant.get("age_korean"))
    if raw_birth_year is not None:
        age_korean = calculate_korean_age(raw_birth_year)
    elif not (age_korean and 18 <= age_korean <= 81):
        age_korean = None
    if age_korean is None:
        age_korean = calculate_korean_age(birth_year)

    position = (
        to_text(applicant.get("position"))
        or to_text(updates.get("position"))
        or extract_position(raw_text)
    )
    status = normalize_status(to_text(data.get("status")) or to_text(updates.get("status")))
    candidate_name = choose_candidate_name(to_text(applicant.get("name")), raw_text)
    company_name = normalize_optional_company_name(
        to_text(company.get("name"))
        or to_text(company.get("canonical_name"))
    )
    if not company_name:
        company_name = normalize_optional_company_name(extract_company_name(raw_text))

    parsed = {
        "candidate_name": candidate_name or "UNKNOWN",
        "company_name": company_name,
        "company_canonical_name": (
            clean_company_name(to_text(company.get("canonical_name")))
            or canonical_company_name(company_name)
        ),
        "contact_person": clean_name(to_text(company.get("contact_person"))) or extract_contact_person(raw_text),
        "contact_email": to_text(company.get("contact_email")),
        "contact_phone": to_text(company.get("contact_phone")),
        "birth_year": birth_year,
        "age": age_international,
        "age_international": age_international,
        "age_korean": age_korean,
        "email": to_text(applicant.get("email")),
        "phone": to_text(applicant.get("phone")),
        "position": clean_position(position),
        "education": to_list(applicant.get("education")) or to_list(updates.get("education")),
        "experience": to_list(applicant.get("experience")) or to_list(updates.get("experience")),
        "skills": to_list(applicant.get("skills")) or to_list(updates.get("skills")),
        "notes": (
            to_text(applicant.get("notes"))
            or to_text(updates.get("notes"))
            or extract_notes_summary(raw_text)
        ),
        "salary_current": to_text(applicant.get("salary_current")) or extract_current_salary(raw_text),
        "salary_expected": to_text(applicant.get("salary_expected")) or extract_expected_salary(raw_text),
        "extra_properties": normalize_extra_properties(
            applicant.get("extra_properties")
            or data.get("extra_properties")
            or updates.get("extra_properties")
        ),
        "status": status,
        "intent": to_text(data.get("intent")) or "create",
        "raw_text": normalize_text(raw_text),
    }
    return normalize_salary_fields(parsed)


def serialize_lines(value: list[str]) -> str:
    # 문자열 리스트를 DB 저장용 줄바꿈 텍스트로 변환한다.
    return "\n".join(value or [])


def deserialize_lines(value: str) -> list[str]:
    # DB에 줄바꿈 텍스트로 저장된 목록형 값을 다시 문자열 리스트로 복원한다.
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


def ensure_applicant_runtime_columns(conn: sqlite3.Connection) -> None:
    # 기존 DB에 런타임 추가 컬럼이 없을 때 자동으로 추가한다.
    #
    # 과거 스키마로 만들어진 DB를 그대로 쓰는 경우를 위한 호환성 처리다.
    #
    rows = conn.execute("PRAGMA table_info(applicants)").fetchall()
    existing_columns = {row["name"] for row in rows}

    if "age_international" not in existing_columns:
        conn.execute("ALTER TABLE applicants ADD COLUMN age_international INTEGER")
    if "age_korean" not in existing_columns:
        conn.execute("ALTER TABLE applicants ADD COLUMN age_korean INTEGER")
    if "extra_properties" not in existing_columns:
        conn.execute("ALTER TABLE applicants ADD COLUMN extra_properties TEXT")
    if "salary_current" not in existing_columns:
        conn.execute("ALTER TABLE applicants ADD COLUMN salary_current TEXT")
    if "salary_expected" not in existing_columns:
        conn.execute("ALTER TABLE applicants ADD COLUMN salary_expected TEXT")


def get_company_by_name(conn: sqlite3.Connection, company_name: str) -> Optional[sqlite3.Row]:
    # 회사명으로 `companies` 레코드를 조회한다.
    #
    # 먼저 정확히 같은 이름을 찾고, 없으면 LIKE 검색과 표준 키 비교로 기존
    # 회사를 찾는다. `케미콘`처럼 짧은 입력도 기존 긴 회사명에 매칭될 수 있다.
    #
    cursor = conn.execute(
        """
        SELECT id, name, contact_person, contact_email, contact_phone
        FROM companies
        WHERE name = ?
        """,
        (company_name,),
    )
    exact = cursor.fetchone()
    if exact:
        return exact

    like_patterns = company_like_patterns(company_name)
    for pattern in like_patterns:
        like_row = conn.execute(
            """
            SELECT id, name, contact_person, contact_email, contact_phone
            FROM companies
            WHERE REPLACE(LOWER(name), ' ', '') LIKE LOWER(?)
            ORDER BY LENGTH(name), id
            LIMIT 1
            """,
            (pattern,),
        ).fetchone()
        if like_row:
            return like_row

    target_key = canonical_company_name(company_name)
    for row in conn.execute(
        """
        SELECT id, name, contact_person, contact_email, contact_phone
        FROM companies
        ORDER BY id
        """
    ):
        if company_keys_are_similar(row["name"], target_key):
            return row

    return None


def get_company_by_canonical_name(
    conn: sqlite3.Connection,
    canonical_name: str,
) -> Optional[sqlite3.Row]:
    # AI가 준 중복 판단용 회사 표준명으로 기존 회사를 조회한다.
    if not canonical_name:
        return None

    for pattern in company_like_patterns(canonical_name):
        like_row = conn.execute(
            """
            SELECT id, name, contact_person, contact_email, contact_phone
            FROM companies
            WHERE REPLACE(LOWER(name), ' ', '') LIKE LOWER(?)
            ORDER BY LENGTH(name), id
            LIMIT 1
            """,
            (pattern,),
        ).fetchone()
        if like_row:
            return like_row

    target_key = canonical_company_name(canonical_name)
    for row in conn.execute(
        """
        SELECT id, name, contact_person, contact_email, contact_phone
        FROM companies
        ORDER BY id
        """
    ):
        if company_keys_are_similar(row["name"], target_key):
            return row

    return None


def upsert_company(conn: sqlite3.Connection, parsed: dict[str, Any]) -> Optional[int]:
    # 회사명을 기준으로 `companies`를 생성하거나 담당자 정보를 업데이트한다.
    company_name = normalize_optional_company_name(parsed.get("company_name") or "")
    if not company_name:
        parsed["company_name"] = ""
        parsed["company_canonical_name"] = ""
        return None

    existing_company = get_company_by_name(conn, company_name)
    if not existing_company:
        existing_company = get_company_by_canonical_name(
            conn,
            parsed.get("company_canonical_name") or canonical_company_name(company_name),
        )

    if existing_company:
        parsed["company_name"] = existing_company["name"]
        parsed["company_canonical_name"] = canonical_company_name(existing_company["name"])
        conn.execute(
            """
            UPDATE companies
            SET contact_person = COALESCE(NULLIF(?, ''), contact_person),
                contact_email = COALESCE(NULLIF(?, ''), contact_email),
                contact_phone = COALESCE(NULLIF(?, ''), contact_phone),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                parsed.get("contact_person", ""),
                parsed.get("contact_email", ""),
                parsed.get("contact_phone", ""),
                int(existing_company["id"]),
            ),
        )
        return int(existing_company["id"])

    conn.execute(
        """
        INSERT INTO companies (
            name,
            contact_person,
            contact_email,
            contact_phone,
            updated_at
        )
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(name)
        DO UPDATE SET
            contact_person = COALESCE(NULLIF(excluded.contact_person, ''), companies.contact_person),
            contact_email = COALESCE(NULLIF(excluded.contact_email, ''), companies.contact_email),
            contact_phone = COALESCE(NULLIF(excluded.contact_phone, ''), companies.contact_phone),
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            company_name,
            parsed.get("contact_person", ""),
            parsed.get("contact_email", ""),
            parsed.get("contact_phone", ""),
        ),
    )

    company = get_company_by_name(conn, company_name)
    if not company:
        raise RuntimeError(f"회사 저장 실패: {company_name}")

    return int(company["id"])


def find_existing_applicant(
    conn: sqlite3.Connection,
    parsed: dict[str, Any],
    company_id: Optional[int],
) -> Optional[sqlite3.Row]:
    # 중복 생성을 막기 위해 기존 지원자를 찾는다.
    #
    # 생년이 있으면 `이름 + 생년 + 회사`로 찾고, 업데이트 메시지처럼 생년이
    # 없으면 `이름 + 회사`로 찾는다.
    #
    candidate_name = parsed.get("candidate_name") or "UNKNOWN"
    birth_year = parsed.get("birth_year")
    update_target_applicant_id = parsed.get("_update_target_applicant_id")

    if update_target_applicant_id:
        cursor = conn.execute(
            """
            SELECT id, notion_page_id
            FROM applicants
            WHERE id = ?
            """,
            (update_target_applicant_id,),
        )
        return cursor.fetchone()

    if company_id is None:
        rows = conn.execute(
            """
            SELECT id, notion_page_id
            FROM applicants
            WHERE name = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (candidate_name,),
        ).fetchall()
        return rows[0] if len(rows) == 1 else None

    if birth_year is None:
        cursor = conn.execute(
            """
            SELECT id, notion_page_id
            FROM applicants
            WHERE name = ?
              AND company_id = ?
            ORDER BY id
            LIMIT 1
            """,
            (
                candidate_name,
                company_id,
            ),
        )
        return cursor.fetchone()

    cursor = conn.execute(
        """
        SELECT id, notion_page_id
        FROM applicants
        WHERE name = ?
          AND (
            birth_year IS ?
            OR birth_year = ?
          )
          AND company_id = ?
        ORDER BY id
        LIMIT 1
        """,
        (
            candidate_name,
            birth_year,
            birth_year,
            company_id,
        ),
    )
    return cursor.fetchone()


def applicant_matches_age_or_birth(row: sqlite3.Row, parsed: dict[str, Any]) -> bool:
    # 기존 지원자와 입력값의 생년/나이 정보 중 하나라도 같은지 확인한다.
    comparisons = [
        ("birth_year", "birth_year"),
        ("age", "age"),
        ("age_international", "age_international"),
        ("age_korean", "age_korean"),
    ]
    for row_key, parsed_key in comparisons:
        parsed_value = parsed.get(parsed_key)
        row_value = row[row_key]
        if parsed_value is not None and row_value is not None and int(parsed_value) == int(row_value):
            return True
    return False


def duplicate_error_response(
    code: str,
    message: str,
    source_email_file: str,
    parsed: dict[str, Any],
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    # 중복 확인 실패는 SQLite/Notion 저장 전에 표준 오류 응답으로 반환한다.
    return build_error_response(
        error_code=code,
        error_message=message,
        source_email_file=source_email_file,
        parsed=parsed,
        diagnostics=diagnostics,
    )


def company_matches_input(row_company_name: str, input_company_name: str) -> bool:
    # 중복 게이트에서 회사명 LIKE 수준의 부분 입력도 같은 회사로 본다.
    if company_keys_are_similar(row_company_name, input_company_name):
        return True
    row_key = canonical_company_name(row_company_name)
    input_key = canonical_company_name(input_company_name)
    if not row_key or not input_key:
        return False
    return len(input_key) >= 2 and input_key in row_key


def _pin_unique_or_continue(parsed: dict[str, Any], candidates: list) -> bool:
    # 후보가 1명 이하면 대상 ID를 설정하고 True(호출자가 None 반환)를 돌려준다.
    if len(candidates) == 1:
        parsed["_update_target_applicant_id"] = int(candidates[0]["id"])
    return len(candidates) <= 1


def validate_duplicate_gate(
    conn: sqlite3.Connection,
    parsed: dict[str, Any],
    source_email_file: str,
    diagnostics: dict[str, Any],
) -> Optional[dict[str, Any]]:
    # 저장 전 중복을 검사하고, 1명으로 특정 가능한 경우 merge 대상 ID를 설정한다.
    # 각 단계에서 후보가 1명으로 좁혀지면 _update_target_applicant_id를 설정해
    # find_existing_applicant가 정확한 레코드를 merge 업데이트하도록 한다.
    candidate_name = parsed.get("candidate_name")
    if not candidate_name or candidate_name == "UNKNOWN":
        return None

    rows = conn.execute(
        """
        SELECT
            a.id,
            a.name,
            a.birth_year,
            a.age,
            a.age_international,
            a.age_korean,
            a.position,
            COALESCE(c.name, '') AS company_name
        FROM applicants a
        LEFT JOIN companies c ON c.id = a.company_id
        WHERE a.name = ?
        ORDER BY a.updated_at DESC, a.id DESC
        """,
        (candidate_name,),
    ).fetchall()
    if _pin_unique_or_continue(parsed, rows):
        return None

    company_name = parsed.get("company_name")
    if not company_name or company_name == "UNKNOWN":
        return duplicate_error_response(
            "DUPLICATE_NAME_NEEDS_COMPANY",
            f"동명이인 후보자가 {len(rows)}명 있습니다. 회사명을 추가해주세요.",
            source_email_file,
            parsed,
            diagnostics,
        )

    company_matches = [r for r in rows if company_matches_input(r["company_name"], company_name)]
    if _pin_unique_or_continue(parsed, company_matches):
        return None

    has_age_or_birth = any(
        parsed.get(key) is not None
        for key in ("birth_year", "age", "age_international", "age_korean")
    )
    if not has_age_or_birth:
        return duplicate_error_response(
            "DUPLICATE_COMPANY_NEEDS_AGE",
            "같은 이름과 회사의 지원자가 2명 이상 있습니다. 생년월일, 나이, 만나이 중 하나를 추가해주세요.",
            source_email_file,
            parsed,
            diagnostics,
        )

    age_matches = [r for r in company_matches if applicant_matches_age_or_birth(r, parsed)]
    if _pin_unique_or_continue(parsed, age_matches):
        return None

    position = clean_position(parsed.get("position") or "")
    if not position:
        return duplicate_error_response(
            "DUPLICATE_AGE_NEEDS_POSITION",
            "같은 이름, 회사, 생년/나이의 지원자가 2명 이상 있습니다. 직무를 추가해주세요.",
            source_email_file,
            parsed,
            diagnostics,
        )

    position_matches = [r for r in age_matches if clean_position(r["position"] or "") == position]
    if _pin_unique_or_continue(parsed, position_matches):
        return None
    return duplicate_error_response(
        "DUPLICATE_FULL_MATCH_BLOCKED",
        "이름, 회사, 생년/나이, 직무가 모두 같은 지원자가 2명 이상 있어 저장할 수 없습니다. 기존 데이터를 정리한 뒤 다시 시도해주세요.",
        source_email_file,
        parsed,
        diagnostics,
    )


def update_target_error_response(
    code: str,
    message: str,
    source_email_file: str,
    parsed: dict[str, Any],
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    # `!업데이트` 전용 대상 식별 실패 응답을 만든다.
    return build_error_response(
        error_code=code,
        error_message=message,
        source_email_file=source_email_file,
        parsed=parsed,
        diagnostics=diagnostics,
    )


def describe_update_candidates(rows: list[sqlite3.Row]) -> str:
    # 업데이트 대상이 여러 명일 때 사용자가 구분할 수 있는 기존 정보를 요약한다.
    descriptions = []
    for row in rows[:5]:
        parts = [row["name"]]
        if row["company_name"]:
            parts.append(f"회사 {row['company_name']}")
        if row["birth_year"]:
            parts.append(f"생년 {row['birth_year']}")
        if row["age_international"]:
            parts.append(f"만나이 {row['age_international']}")
        if row["position"]:
            parts.append(f"직무 {row['position']}")
        descriptions.append(" / ".join(parts))
    return "; ".join(descriptions)


def applicant_rows_by_name(conn: sqlite3.Connection, candidate_name: str) -> list[sqlite3.Row]:
    # 이름이 같은 기존 지원자 목록을 회사 정보와 함께 조회한다.
    return conn.execute(
        """
        SELECT
            a.id,
            a.name,
            a.birth_year,
            a.age,
            a.age_international,
            a.age_korean,
            a.position,
            COALESCE(c.name, '') AS company_name
        FROM applicants a
        LEFT JOIN companies c ON c.id = a.company_id
        WHERE a.name = ?
        ORDER BY a.updated_at DESC, a.id DESC
        """,
        (candidate_name,),
    ).fetchall()


def validate_update_target(
    conn: sqlite3.Connection,
    parsed: dict[str, Any],
    source_email_file: str,
    diagnostics: dict[str, Any],
) -> Optional[dict[str, Any]]:
    # `!업데이트`는 기존 지원자 1명을 명확히 찾을 때만 저장을 진행한다.
    candidate_name = parsed.get("candidate_name")
    if not candidate_name or candidate_name == "UNKNOWN":
        return update_target_error_response(
            "UPDATE_NEEDS_NAME",
            "업데이트할 지원자 이름을 입력해주세요.",
            source_email_file,
            parsed,
            diagnostics,
        )

    rows = applicant_rows_by_name(conn, candidate_name)
    if not rows:
        return update_target_error_response(
            "UPDATE_TARGET_NOT_FOUND",
            f"{candidate_name} 지원자를 찾지 못했습니다. 먼저 지원자를 등록해주세요.",
            source_email_file,
            parsed,
            diagnostics,
        )

    candidates = rows
    company_name = parsed.get("company_name")
    if len(candidates) > 1:
        if not company_name:
            return update_target_error_response(
                "UPDATE_DUPLICATE_NEEDS_COMPANY",
                (
                    f"{candidate_name} 지원자가 {len(candidates)}명 있습니다. "
                    "회사명, 생년, 만나이, 직무 중 구분 정보를 포함해 다시 입력해주세요. "
                    f"현재 후보: {describe_update_candidates(candidates)}"
                ),
                source_email_file,
                parsed,
                diagnostics,
            )
        candidates = [
            row
            for row in candidates
            if company_matches_input(row["company_name"], company_name)
        ]
        if not candidates:
            return update_target_error_response(
                "UPDATE_TARGET_COMPANY_NOT_FOUND",
                f"{candidate_name} 지원자 중 회사명 '{company_name}'에 해당하는 대상을 찾지 못했습니다.",
                source_email_file,
                parsed,
                diagnostics,
            )

    if len(candidates) > 1:
        has_age_or_birth = any(
            parsed.get(key) is not None
            for key in ("birth_year", "age", "age_international", "age_korean")
        )
        if not has_age_or_birth:
            return update_target_error_response(
                "UPDATE_DUPLICATE_NEEDS_AGE",
                (
                    "같은 이름과 회사의 지원자가 2명 이상 있습니다. "
                    "생년월일, 나이, 만나이, 직무 중 하나를 포함해 다시 입력해주세요. "
                    f"현재 후보: {describe_update_candidates(candidates)}"
                ),
                source_email_file,
                parsed,
                diagnostics,
            )
        candidates = [
            row
            for row in candidates
            if applicant_matches_age_or_birth(row, parsed)
        ]
        if not candidates:
            return update_target_error_response(
                "UPDATE_TARGET_AGE_NOT_FOUND",
                f"{candidate_name} 지원자 중 입력한 생년/나이에 해당하는 대상을 찾지 못했습니다.",
                source_email_file,
                parsed,
                diagnostics,
            )

    if len(candidates) > 1:
        position = clean_position(parsed.get("position") or "")
        if not position:
            return update_target_error_response(
                "UPDATE_DUPLICATE_NEEDS_POSITION",
                (
                    "같은 이름, 회사, 생년/나이의 지원자가 2명 이상 있습니다. "
                    "직무를 포함해 다시 입력해주세요. "
                    f"현재 후보: {describe_update_candidates(candidates)}"
                ),
                source_email_file,
                parsed,
                diagnostics,
            )
        candidates = [
            row
            for row in candidates
            if clean_position(row["position"] or "") == position
        ]
        if not candidates:
            return update_target_error_response(
                "UPDATE_TARGET_POSITION_NOT_FOUND",
                f"{candidate_name} 지원자 중 직무 '{position}'에 해당하는 대상을 찾지 못했습니다.",
                source_email_file,
                parsed,
                diagnostics,
            )

    if len(candidates) != 1:
        return update_target_error_response(
            "UPDATE_TARGET_AMBIGUOUS",
            (
                "업데이트 대상을 1명으로 구분할 수 없습니다. "
                "회사명, 생년월일, 만나이, 직무를 함께 적어주세요. "
                f"현재 후보: {describe_update_candidates(candidates)}"
            ),
            source_email_file,
            parsed,
            diagnostics,
        )

    parsed["_update_target_applicant_id"] = int(candidates[0]["id"])
    return None


def resolve_company_name_from_existing_context(
    conn: sqlite3.Connection,
    parsed: dict[str, Any],
) -> str:
    # 회사명이 명확하지 않으면 추론하지 않고 빈 값으로 둔다.
    current_company = parsed.get("company_name")
    return normalize_optional_company_name(current_company or "")


def upsert_applicant(
    conn: sqlite3.Connection,
    parsed: dict[str, Any],
    company_id: Optional[int],
) -> int:
    # 지원자를 생성하거나 기존 지원자 정보를 업데이트한다.
    #
    # 빈 문자열은 기존 값을 덮어쓰지 않도록 `COALESCE(NULLIF(...))`를 사용한다.
    # 예를 들어 직무 업데이트 메시지에 생년/스킬이 없어도 기존 생년/스킬은 유지된다.
    #
    applicant_name = parsed.get("candidate_name") or "UNKNOWN"
    existing = find_existing_applicant(conn, parsed, company_id)

    values = (
        parsed.get("birth_year"),
        parsed.get("age"),
        parsed.get("age_international"),
        parsed.get("age_korean"),
        parsed.get("email", ""),
        parsed.get("phone", ""),
        parsed.get("position", ""),
        serialize_lines(parsed.get("education", [])),
        serialize_lines(parsed.get("experience", [])),
        serialize_lines(parsed.get("skills", [])),
        parsed.get("notes", ""),
        parsed.get("salary_current", ""),
        parsed.get("salary_expected", ""),
        json.dumps(normalize_extra_properties(parsed.get("extra_properties")), ensure_ascii=False),
        (
            normalize_status(parsed.get("status"))
            if has_explicit_status(parsed.get("raw_text") or "")
            else ""
        ),
    )

    if existing:
        conn.execute(
            """
            UPDATE applicants
            SET company_id = COALESCE(?, company_id),
                birth_year = COALESCE(?, birth_year),
                age = COALESCE(?, age),
                age_international = COALESCE(?, age_international),
                age_korean = COALESCE(?, age_korean),
                email = COALESCE(NULLIF(?, ''), email),
                phone = COALESCE(NULLIF(?, ''), phone),
                position = COALESCE(NULLIF(?, ''), position),
                education = COALESCE(NULLIF(?, ''), education),
                experience = COALESCE(NULLIF(?, ''), experience),
                skills = COALESCE(NULLIF(?, ''), skills),
                notes = CASE
                    WHEN NULLIF(?, '') IS NULL THEN notes
                    WHEN notes IS NULL OR notes = '' THEN ?
                    WHEN instr(notes, ?) > 0 THEN notes
                    ELSE notes || CHAR(10) || ?
                END,
                salary_current = COALESCE(NULLIF(?, ''), salary_current),
                salary_expected = COALESCE(NULLIF(?, ''), salary_expected),
                extra_properties = COALESCE(NULLIF(?, '{}'), extra_properties),
                status = COALESCE(NULLIF(?, ''), status),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                company_id,
                *values[:10],
                values[10],
                values[10],
                values[10],
                values[10],
                *values[11:],
                int(existing["id"]),
            ),
        )
        return int(existing["id"])

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
            education,
            experience,
            skills,
            notes,
            salary_current,
            salary_expected,
            extra_properties,
            status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            company_id,
            applicant_name,
            parsed.get("birth_year"),
            parsed.get("age"),
            parsed.get("age_international"),
            parsed.get("age_korean"),
            parsed.get("email", ""),
            parsed.get("phone", ""),
            parsed.get("position", ""),
            serialize_lines(parsed.get("education", [])),
            serialize_lines(parsed.get("experience", [])),
            serialize_lines(parsed.get("skills", [])),
            parsed.get("notes", ""),
            parsed.get("salary_current", ""),
            parsed.get("salary_expected", ""),
            json.dumps(normalize_extra_properties(parsed.get("extra_properties")), ensure_ascii=False),
            normalize_status(parsed.get("status")),
        ),
    )
    return int(cursor.lastrowid)


def insert_email_event(
    conn: sqlite3.Connection,
    parsed: dict[str, Any],
    applicant_id: int,
    company_id: Optional[int],
    source_email_file: str,
) -> int:
    # 원문 1건의 처리 이력을 `email_events`에 남긴다.
    #
    # 지원자 레코드가 업데이트되더라도 원문 이벤트는 별도로 누적되어, 나중에
    # 어떤 메시지가 어떤 상태 변경을 만들었는지 추적할 수 있다.
    #
    cursor = conn.execute(
        """
        INSERT INTO email_events (
            applicant_id,
            company_id,
            source_type,
            source_message_id,
            event_type,
            raw_text,
            parsed_summary,
            status
        )
        VALUES (?, ?, 'file', ?, ?, ?, ?, ?)
        """,
        (
            applicant_id,
            company_id,
            source_email_file,
            "status_update" if normalize_status(parsed.get("status")) != DEFAULT_STATUS else "application",
            parsed.get("raw_text", ""),
            json.dumps(parsed, ensure_ascii=False),
            normalize_status(parsed.get("status")),
        ),
    )
    return int(cursor.lastrowid)


def notion_is_configured() -> bool:
    # Notion 동기화에 필요한 토큰과 DB ID가 모두 있는지 확인한다.
    return bool(os.getenv("NOTION_TOKEN") and os.getenv("NOTION_DB_ID"))


def notion_request(method: str, path: str, payload: Optional[dict] = None) -> dict:
    # Notion REST API를 호출하고 JSON 응답을 반환한다.
    token = os.getenv("NOTION_TOKEN")
    if not token:
        raise RuntimeError("NOTION_TOKEN이 설정되어 있지 않습니다.")

    body = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{NOTION_API_BASE}/{path.lstrip('/')}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": NOTION_VERSION,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8")
        raise RuntimeError(f"Notion API 오류: status={exc.code}, body={detail}") from exc


def notion_rich_text(value: str) -> dict:
    # Notion rich_text 속성 payload를 만든다.
    if not value:
        return {"rich_text": []}
    return {"rich_text": [{"text": {"content": value}}]}


def get_notion_database_properties(force_refresh: bool = False) -> dict[str, Any]:
    # Notion DB 속성 스키마를 조회하고 짧게 캐시한다.
    global _NOTION_DATABASE_PROPERTIES_CACHE

    if _NOTION_DATABASE_PROPERTIES_CACHE is not None and not force_refresh:
        return _NOTION_DATABASE_PROPERTIES_CACHE

    database_id = os.getenv("NOTION_DB_ID")
    if not database_id or not notion_is_configured():
        return {}

    data = notion_request("GET", f"databases/{database_id}")
    _NOTION_DATABASE_PROPERTIES_CACHE = data.get("properties", {})
    return _NOTION_DATABASE_PROPERTIES_CACHE


def get_dynamic_notion_properties(force_refresh: bool = False) -> dict[str, str]:
    # 고정 필드를 제외한 Notion 추가 컬럼 이름과 타입을 반환한다.
    try:
        properties = get_notion_database_properties(force_refresh)
    except Exception:
        return {}
    dynamic = {}
    for name, prop in properties.items():
        prop_type = prop.get("type")
        if name in FIXED_NOTION_PROPERTIES:
            continue
        if prop_type in SUPPORTED_DYNAMIC_NOTION_TYPES:
            dynamic[name] = prop_type
    return dynamic


def normalize_extra_properties(value: Any) -> dict[str, Any]:
    # AI/규칙 파서가 만든 추가 속성 dict를 저장 가능한 형태로 정리한다.
    if not isinstance(value, dict):
        return {}
    return {
        str(key).strip(): item
        for key, item in value.items()
        if str(key).strip() and item not in (None, "", [], {})
    }


def pop_first_extra_value(extra_properties: dict[str, Any], keys: list[str]) -> str:
    # extra_properties에 들어온 고정 컬럼 값을 표준 salary 필드로 승격한다.
    normalized_keys = {re.sub(r"\s+", "", key).lower(): key for key in list(extra_properties)}
    for key in keys:
        lookup_key = re.sub(r"\s+", "", key).lower()
        actual_key = normalized_keys.get(lookup_key)
        if not actual_key:
            continue
        value = to_text(extra_properties.pop(actual_key))
        if value:
            return value
    return ""


def normalize_salary_fields(parsed: dict[str, Any]) -> dict[str, Any]:
    # 희망연봉은 salary_expected, 그 외 연봉은 salary_current로 표준화한다.
    extra_properties = normalize_extra_properties(parsed.get("extra_properties"))
    raw_text = parsed.get("raw_text") or ""
    extra_expected_salary = pop_first_extra_value(
        extra_properties,
        ["희망연봉", "희망 연봉", "salary_expected", "expected_salary"],
    )
    extra_current_salary = pop_first_extra_value(
        extra_properties,
        [
            "최종연봉",
            "최종 연봉",
            "현재연봉",
            "현재 연봉",
            "현연봉",
            "연봉",
            "salary_current",
            "current_salary",
            "final_salary",
        ],
    )

    expected_salary = (
        to_text(parsed.get("salary_expected"))
        or extra_expected_salary
        or extract_expected_salary(raw_text)
    )
    current_salary = (
        to_text(parsed.get("salary_current"))
        or extra_current_salary
        or extract_current_salary(raw_text)
    )

    parsed["salary_expected"] = expected_salary
    parsed["salary_current"] = current_salary
    parsed["extra_properties"] = extra_properties
    return parsed


def notion_dynamic_value(prop_type: str, value: Any) -> Optional[dict[str, Any]]:
    # 추가 속성 값을 Notion property payload로 변환한다.
    if value in (None, "", [], {}):
        return None

    if prop_type == "rich_text":
        return notion_rich_text(serialize_lines(value) if isinstance(value, list) else str(value))
    if prop_type == "number":
        number = to_int(value)
        return None if number is None else {"number": number}
    if prop_type == "select":
        return {"select": {"name": str(value)}}
    if prop_type == "multi_select":
        values = value if isinstance(value, list) else [part.strip() for part in str(value).split(",")]
        names = [str(item).strip() for item in values if str(item).strip()]
        return {"multi_select": [{"name": name} for name in names]}
    if prop_type == "checkbox":
        if isinstance(value, bool):
            return {"checkbox": value}
        return {"checkbox": str(value).strip().lower() in {"1", "true", "yes", "y", "예", "네"}}
    if prop_type == "url":
        return {"url": str(value)}
    if prop_type == "email":
        return {"email": str(value)}
    if prop_type == "phone_number":
        return {"phone_number": str(value)}
    if prop_type == "date":
        return {"date": {"start": str(value)}}
    return None


def ensure_notion_extra_properties() -> None:
    # Notion DB에 저장에 필요한 속성이 없으면 자동으로 만든다.
    global _NOTION_AGE_PROPERTIES_READY

    if _NOTION_AGE_PROPERTIES_READY:
        return

    database_id = os.getenv("NOTION_DB_ID")
    properties = get_notion_database_properties(force_refresh=True)
    missing_properties = {
        name: schema
        for name, schema in NOTION_REQUIRED_PROPERTIES.items()
        if name not in properties
    }

    if missing_properties:
        notion_request(
            "PATCH",
            f"databases/{database_id}",
            {"properties": missing_properties},
        )
        get_notion_database_properties(force_refresh=True)

    _NOTION_AGE_PROPERTIES_READY = True


def build_notion_properties(parsed: dict[str, Any]) -> dict:
    # `parsed` dict를 Notion 페이지 속성 payload로 변환한다.
    properties = {
        "이름": {"title": [{"text": {"content": parsed["candidate_name"]}}]},
        NOTION_COMPANY_PROP: notion_rich_text(parsed.get("company_name") or ""),
        NOTION_POSITION_PROP: notion_rich_text(parsed.get("position") or ""),
        NOTION_EXPECTED_SALARY_PROP: notion_rich_text(parsed.get("salary_expected") or ""),
        NOTION_CURRENT_SALARY_PROP: notion_rich_text(parsed.get("salary_current") or ""),
        NOTION_NOTES_PROP: notion_rich_text(parsed.get("notes") or ""),
    }

    if parsed.get("birth_year"):
        properties[NOTION_BIRTH_YEAR_PROP] = {"number": parsed["birth_year"]}
    if parsed.get("age_international"):
        properties[NOTION_AGE_PROP] = {"number": parsed["age_international"]}
    extra_properties = normalize_extra_properties(parsed.get("extra_properties"))
    dynamic_schema = get_dynamic_notion_properties()
    for name, value in extra_properties.items():
        if name in FIXED_NOTION_PROPERTIES:
            continue
        prop_type = dynamic_schema.get(name)
        if not prop_type:
            continue
        notion_value = notion_dynamic_value(prop_type, value)
        if notion_value is not None:
            properties[name] = notion_value

    return properties


def notion_blocks(parsed: dict[str, Any]) -> list[dict]:
    # Notion 페이지 본문에 들어갈 학력/경력/스킬/비고 블록을 만든다.
    blocks = []
    sections = [
        ("학력", parsed.get("education") or []),
        ("경력", parsed.get("experience") or []),
        ("스킬", parsed.get("skills") or []),
    ]

    for label, items in sections:
        if not items:
            continue
        blocks.append({
            "object": "block",
            "type": "toggle",
            "toggle": {
                "rich_text": [{"text": {"content": label}}],
                "children": [
                    {
                        "object": "block",
                        "type": "bulleted_list_item",
                        "bulleted_list_item": {
                            "rich_text": [{"text": {"content": item}}],
                        },
                    }
                    for item in items
                ],
            },
        })

    if parsed.get("notes"):
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"text": {"content": parsed["notes"]}}],
            },
        })

    return blocks


def find_notion_page(parsed: dict[str, Any]) -> Optional[str]:
    # 이름, 회사, 가능하면 출생년도로 기존 Notion 페이지를 찾는다.
    database_id = os.getenv("NOTION_DB_ID")
    filters = [
        {
            "property": "이름",
            "title": {"equals": parsed["candidate_name"]},
        }
    ]

    if parsed.get("company_name"):
        filters.append({
            "property": NOTION_COMPANY_PROP,
            "rich_text": {"equals": parsed["company_name"]},
        })

    if parsed.get("birth_year"):
        filters.append({
            "property": NOTION_BIRTH_YEAR_PROP,
            "number": {"equals": parsed["birth_year"]},
        })

    data = notion_request(
        "POST",
        f"databases/{database_id}/query",
        {
            "page_size": 1,
            "filter": {
                "and": filters,
            },
        },
    )
    results = data.get("results") or []
    if not results:
        return None
    return results[0].get("id")


def create_notion_page(parsed: dict[str, Any]) -> str:
    # 지원자 정보를 담은 새 Notion 페이지를 생성한다.
    database_id = os.getenv("NOTION_DB_ID")
    ensure_notion_extra_properties()
    data = notion_request(
        "POST",
        "pages",
        {
            "parent": {"database_id": database_id},
            "properties": build_notion_properties(parsed),
            "children": notion_blocks(parsed),
        },
    )
    return data["id"]


def update_notion_page(page_id: str, parsed: dict[str, Any]) -> str:
    # 기존 Notion 페이지의 속성을 최신 지원자 정보로 업데이트한다.
    ensure_notion_extra_properties()
    notion_request(
        "PATCH",
        f"pages/{page_id}",
        {
            "properties": build_notion_properties(parsed),
        },
    )
    return page_id


def is_archived_notion_error(exc: Exception) -> bool:
    # Notion에서 삭제/보관된 기존 page_id를 업데이트하려다 난 오류인지 판단한다.
    message = str(exc).lower()
    return "archived" in message or "can't edit block that is archived" in message


def sync_notion(parsed: dict[str, Any], existing_page_id: str = "") -> dict[str, Any]:
    # SQLite 저장 결과를 Notion DB에 반영한다.
    #
    # SQLite에 저장된 `notion_page_id`가 있으면 그 페이지를 우선 업데이트하고,
    # 없으면 Notion DB에서 같은 지원자를 검색한 뒤 없을 때만 새 페이지를 만든다.
    #
    if not notion_is_configured():
        return {
            "enabled": False,
            "synced": False,
            "action": "skipped",
            "page_id": "",
            "message": "NOTION_TOKEN 또는 NOTION_DB_ID가 없어 Notion 동기화를 건너뜁니다.",
        }

    ensure_notion_extra_properties()
    page_id = existing_page_id or find_notion_page(parsed)
    if page_id:
        try:
            page_id = update_notion_page(page_id, parsed)
            return {
                "enabled": True,
                "synced": True,
                "action": "updated",
                "page_id": page_id,
                "message": "기존 Notion 페이지를 업데이트했습니다.",
            }
        except Exception as exc:
            if not is_archived_notion_error(exc):
                raise

    page_id = create_notion_page(parsed)
    return {
        "enabled": True,
        "synced": True,
        "action": "created_after_archived" if existing_page_id else "created",
        "page_id": page_id,
        "message": (
            "기존 Notion 페이지가 보관/삭제되어 새 페이지를 생성했습니다."
            if existing_page_id
            else "새 Notion 페이지를 생성했습니다."
        ),
    }


def get_applicant_notion_page_id(conn: sqlite3.Connection, applicant_id: int) -> str:
    # SQLite 지원자 레코드에 저장된 Notion page id를 조회한다.
    cursor = conn.execute(
        "SELECT notion_page_id FROM applicants WHERE id = ?",
        (applicant_id,),
    )
    row = cursor.fetchone()
    return row["notion_page_id"] if row and row["notion_page_id"] else ""


def build_notion_sync_parsed(
    conn: sqlite3.Connection,
    parsed: dict[str, Any],
    applicant_id: int,
) -> dict[str, Any]:
    # Notion과 Discord 응답에는 방금 SQLite에 저장된 최종값을 항상 보낸다.
    #
    # sparse 업데이트 메시지에는 회사/생년/포지션 같은 기존 고정 컬럼이 없을 수 있다.
    # 원문 parsed만 Notion에 보내면 빈 rich_text로 기존 컬럼을 지울 수 있으므로,
    # 저장 완료 후 DB의 최종 상태를 다시 읽어 payload와 응답을 만든다.
    #
    row = conn.execute(
        """
        SELECT
            a.name,
            a.birth_year,
            a.age,
            a.age_international,
            a.age_korean,
            a.email,
            a.phone,
            a.position,
            a.education,
            a.experience,
            a.skills,
            a.notes,
            a.salary_current,
            a.salary_expected,
            a.extra_properties,
            a.status,
            c.name AS company_name,
            c.contact_person,
            c.contact_email,
            c.contact_phone
        FROM applicants a
        LEFT JOIN companies c ON c.id = a.company_id
        WHERE a.id = ?
        """,
        (applicant_id,),
    ).fetchone()
    if not row:
        return parsed

    notion_parsed = dict(parsed)
    notion_parsed.update({
        "candidate_name": row["name"] or parsed.get("candidate_name"),
        "company_name": row["company_name"] or "",
        "company_canonical_name": canonical_company_name(row["company_name"] or ""),
        "contact_person": row["contact_person"] or parsed.get("contact_person") or "",
        "contact_email": row["contact_email"] or parsed.get("contact_email") or "",
        "contact_phone": row["contact_phone"] or parsed.get("contact_phone") or "",
        "birth_year": row["birth_year"],
        "age": row["age"],
        "age_international": row["age_international"],
        "age_korean": row["age_korean"],
        "email": row["email"] or "",
        "phone": row["phone"] or "",
        "position": row["position"] or "",
        "education": deserialize_lines(row["education"] or ""),
        "experience": deserialize_lines(row["experience"] or ""),
        "skills": deserialize_lines(row["skills"] or ""),
        "notes": row["notes"] or "",
        "salary_current": row["salary_current"] or "",
        "salary_expected": row["salary_expected"] or "",
        "extra_properties": normalize_extra_properties(
            json.loads(row["extra_properties"] or "{}")
        ),
        "status": normalize_status(row["status"]),
    })
    return notion_parsed


def save_applicant_notion_page_id(
    conn: sqlite3.Connection,
    applicant_id: int,
    page_id: str,
) -> None:
    # Notion 동기화 후 page id를 SQLite 지원자 레코드에 저장한다.
    conn.execute(
        """
        UPDATE applicants
        SET notion_page_id = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (page_id, applicant_id),
    )


def build_success_response(
    parsed: dict[str, Any],
    company_id: int,
    applicant_id: int,
    event_id: int,
    source_email_file: str,
    notion: dict[str, Any],
    diagnostics: Optional[dict[str, Any]] = None,
    errors: Optional[list[dict[str, str]]] = None,
) -> dict[str, Any]:
    # CLI/Discord/하네스가 공통으로 사용하는 성공 응답 JSON을 만든다.
    return {
        "status": "ok",
        "source_email_file": source_email_file,
        "company_id": company_id,
        "applicant_id": applicant_id,
        "candidate_id": applicant_id,
        "event_id": event_id,
        "notion": notion,
        "parsed": parsed,
        "diagnostics": diagnostics or build_parse_diagnostics(parsed),
        "errors": errors or [],
    }


def build_parse_diagnostics(parsed: dict[str, Any]) -> dict[str, Any]:
    # 파싱 결과에서 실제 추출된 값과 누락 필드를 진단한다.
    #
    # Discord 실패 답장에서 `필수 누락`과 `선택 누락`을 분리해서 보여주기 위한
    # 데이터다. 필수값은 저장 가능 여부를 결정하고, 선택값은 보완 안내용이다.
    #
    raw_text = parsed.get("raw_text") or ""
    fields = {
        "candidate_name": parsed.get("candidate_name"),
        "company_name": parsed.get("company_name"),
        "contact_person": parsed.get("contact_person"),
        "birth_year": parsed.get("birth_year"),
        "age_international": parsed.get("age_international"),
        "age_korean": parsed.get("age_korean"),
        "position": parsed.get("position"),
        "skills": parsed.get("skills") or [],
        "notes": parsed.get("notes") or "",
        "salary_current": parsed.get("salary_current") or "",
        "salary_expected": parsed.get("salary_expected") or "",
        "status": parsed.get("status"),
        "intent": parsed.get("intent"),
    }
    required = {
        "candidate_name": "후보자 이름",
    }
    optional = {
        "company_name": "기업명",
        "birth_year": "생년",
        "age_international": "나이",
        "position": "포지션",
        "notes": "기타",
        "salary_current": "최종연봉",
        "salary_expected": "희망연봉",
    }
    missing_required = [
        label
        for key, label in required.items()
        if not fields.get(key) or fields.get(key) == "UNKNOWN"
    ]
    optional_hints = {
        "company_name": bool(extract_company_name(raw_text) != "UNKNOWN"),
        "birth_year": bool(re.search(r"(?:19|20)\d{2}년생|생년|출생", raw_text)),
        "age_international": bool(re.search(r"만\s*\d{1,2}\s*세|나이", raw_text)),
        "position": bool(extract_position(raw_text)),
        "notes": bool(extract_notes_summary(raw_text)),
        "salary_current": bool(extract_current_salary(raw_text)),
        "salary_expected": bool(extract_expected_salary(raw_text)),
    }
    missing_optional = [
        label
        for key, label in optional.items()
        if optional_hints.get(key) and not fields.get(key)
    ]

    return {
        "fields": fields,
        "missing_required": missing_required,
        "missing_optional": missing_optional,
    }


def build_error_response(
    error_code: str,
    error_message: str,
    source_email_file: str = "",
    parsed: Optional[dict[str, Any]] = None,
    diagnostics: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    # 저장 실패를 호출자에게 돌려주기 위한 표준 오류 응답 JSON을 만든다.
    return {
        "status": "error",
        "source_email_file": source_email_file,
        "company_id": None,
        "applicant_id": None,
        "candidate_id": None,
        "event_id": None,
        "notion": {
            "enabled": notion_is_configured(),
            "synced": False,
            "action": "error",
            "page_id": "",
        },
        "parsed": parsed or {},
        "diagnostics": diagnostics or {},
        "errors": [
            {
                "code": error_code,
                "message": error_message,
            }
        ],
    }


def validate_parsed(parsed: dict[str, Any]) -> list[str]:
    # 저장 전에 반드시 필요한 필드가 빠졌는지 확인한다.
    diagnostics = build_parse_diagnostics(parsed)
    return diagnostics["missing_required"]


def store_email_text(
    raw_text: str,
    source_email_file: str = "",
    sync_to_notion: bool = True,
    update_only: bool = False,
) -> dict[str, Any]:
    # 원문 텍스트를 rule parser로 파싱한 뒤 저장한다.
    candidate_blocks = split_candidate_blocks(raw_text)
    if candidate_blocks:
        return store_batch_parsed_data(
            parsed_items=[
                parse_email_text(candidate_block)
                for candidate_block in candidate_blocks
            ],
            source_email_file=source_email_file,
            sync_to_notion=sync_to_notion,
            update_only=update_only,
        )

    return store_parsed_data(
        parsed=parse_email_text(raw_text),
        source_email_file=source_email_file,
        sync_to_notion=sync_to_notion,
        update_only=update_only,
    )


def store_extracted_data(
    data: dict[str, Any],
    raw_text: str,
    source_email_file: str = "",
    sync_to_notion: bool = True,
    update_only: bool = False,
) -> dict[str, Any]:
    # AI 추출 JSON을 표준 parsed 형식으로 변환한 뒤 저장한다.
    candidate_records = candidate_records_from_extracted_data(data)
    if candidate_records:
        return store_batch_parsed_data(
            parsed_items=[
                parsed_from_extracted_data(candidate_record, raw_text)
                for candidate_record in candidate_records
            ],
            source_email_file=source_email_file,
            sync_to_notion=sync_to_notion,
            update_only=update_only,
        )

    candidate_blocks = split_candidate_blocks(raw_text)
    if candidate_blocks:
        return store_batch_parsed_data(
            parsed_items=[
                parse_email_text(candidate_block)
                for candidate_block in candidate_blocks
            ],
            source_email_file=source_email_file,
            sync_to_notion=sync_to_notion,
            update_only=update_only,
        )

    return store_parsed_data(
        parsed=parsed_from_extracted_data(data, raw_text),
        source_email_file=source_email_file,
        sync_to_notion=sync_to_notion,
        update_only=update_only,
    )


def store_batch_parsed_data(
    parsed_items: list[dict[str, Any]],
    source_email_file: str = "",
    sync_to_notion: bool = True,
    update_only: bool = False,
) -> dict[str, Any]:
    # 여러 후보자를 각각 저장하고 배치 응답으로 묶어 반환한다.
    results = [
        store_parsed_data(
            parsed=parsed,
            source_email_file=source_email_file,
            sync_to_notion=sync_to_notion,
            update_only=update_only,
        )
        for parsed in parsed_items
    ]
    ok_results = [result for result in results if result.get("status") == "ok"]
    first_result = ok_results[0] if ok_results else (results[0] if results else {})
    parsed = first_result.get("parsed") or {}
    errors = [
        error
        for result in results
        for error in result.get("errors", [])
    ]

    return {
        "status": "ok" if ok_results else "error",
        "batch": True,
        "source_email_file": source_email_file,
        "stored_count": len(ok_results),
        "total_count": len(results),
        "results": results,
        "company_id": first_result.get("company_id"),
        "applicant_id": first_result.get("applicant_id"),
        "candidate_id": first_result.get("candidate_id"),
        "event_id": first_result.get("event_id"),
        "notion": first_result.get("notion", {}),
        "parsed": parsed,
        "diagnostics": first_result.get("diagnostics", build_parse_diagnostics(parsed)),
        "errors": errors,
    }


def store_parsed_data(
    parsed: dict[str, Any],
    source_email_file: str = "",
    sync_to_notion: bool = True,
    update_only: bool = False,
) -> dict[str, Any]:
    # 표준 `parsed` dict를 SQLite와 선택적으로 Notion에 저장한다.
    #
    # 이 함수가 실제 저장 트랜잭션의 중심이다. 회사/지원자/event를 한 트랜잭션으로
    # 처리하고, 중간에 실패하면 rollback해서 일부만 저장되는 상태를 피한다.
    #
    conn = get_connection(DB_PATH)
    try:
        ensure_applicant_runtime_columns(conn)
        parsed["company_name"] = resolve_company_name_from_existing_context(conn, parsed)
        diagnostics = build_parse_diagnostics(parsed)
        missing_required = validate_parsed(parsed)
        if missing_required:
            return build_error_response(
                error_code="PARSE_VALIDATION_FAILED",
                error_message=f"필수 정보 누락: {', '.join(missing_required)}",
                source_email_file=source_email_file,
                parsed=parsed,
                diagnostics=diagnostics,
            )
        if update_only:
            update_target_error = validate_update_target(
                conn,
                parsed,
                source_email_file,
                diagnostics,
            )
            if update_target_error:
                return update_target_error
        else:
            duplicate_error = validate_duplicate_gate(conn, parsed, source_email_file, diagnostics)
            if duplicate_error:
                return duplicate_error
        company_id = upsert_company(conn, parsed)
        applicant_id = upsert_applicant(conn, parsed, company_id)
        event_id = insert_email_event(conn, parsed, applicant_id, company_id, source_email_file)
        conn.commit()

        notion = {
            "enabled": notion_is_configured(),
            "synced": False,
            "action": "skipped",
            "page_id": "",
            "message": "CLI 옵션 또는 호출 설정으로 Notion 동기화를 건너뜁니다.",
        }
        errors: list[dict[str, str]] = []
        final_parsed = build_notion_sync_parsed(conn, parsed, applicant_id)

        if sync_to_notion:
            try:
                existing_page_id = get_applicant_notion_page_id(conn, applicant_id)
                notion = sync_notion(final_parsed, existing_page_id)
                if notion.get("page_id"):
                    save_applicant_notion_page_id(conn, applicant_id, notion["page_id"])
                    conn.commit()
            except Exception as exc:
                notion = {
                    "enabled": notion_is_configured(),
                    "synced": False,
                    "action": "error",
                    "page_id": "",
                    "message": f"Notion 동기화 실패: {exc}",
                }
                errors.append({
                    "code": "NOTION_SYNC_FAILED",
                    "message": str(exc),
                })

        final_diagnostics = build_parse_diagnostics(final_parsed)

        return build_success_response(
            parsed=final_parsed,
            company_id=company_id,
            applicant_id=applicant_id,
            event_id=event_id,
            notion=notion,
            source_email_file=source_email_file,
            diagnostics=final_diagnostics,
            errors=errors,
        )
    except Exception as exc:
        conn.rollback()
        diagnostics = build_parse_diagnostics(parsed)
        return build_error_response(
            error_code="STORE_FAILED",
            error_message=str(exc),
            source_email_file=source_email_file,
            parsed=parsed,
            diagnostics=diagnostics,
        )
    finally:
        conn.close()


def read_input_file(input_file: str) -> str:
    # CLI 입력 파일을 UTF-8 텍스트로 읽는다.
    return Path(input_file).read_text(encoding="utf-8")


def write_json_response(response: dict[str, Any], output_file: Optional[str] = None) -> None:
    # 응답 JSON을 파일 또는 stdout으로 출력한다.
    text = json.dumps(response, ensure_ascii=False, indent=2)

    if output_file:
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        Path(output_file).write_text(text, encoding="utf-8")
        return

    print(text)


def main() -> None:
    # `store.py` CLI 엔트리포인트.
    #
    # 원문 파일 또는 AI 추출 JSON을 받아 SQLite/Notion 저장을 실행하고 표준 응답
    # JSON을 출력한다.
    #
    parser = argparse.ArgumentParser()
    parser.add_argument("input_path", nargs="?")
    parser.add_argument("--input-file", required=False)
    parser.add_argument("--input-json", required=False)
    parser.add_argument("--output-file", required=False)
    parser.add_argument("--skip-notion", action="store_true")

    args = parser.parse_args()
    input_file = args.input_file or args.input_path

    if not input_file and not args.input_json:
        response = build_error_response(
            error_code="MISSING_INPUT_FILE",
            error_message="--input-file, --input-json 또는 위치 인자로 입력 파일을 지정해야 합니다.",
        )
        write_json_response(response, args.output_file)
        sys.exit(1)

    try:
        if args.input_json:
            raw_text = read_input_file(input_file) if input_file else ""
            extracted_data = json.loads(read_input_file(args.input_json))
            response = store_extracted_data(
                data=extracted_data,
                raw_text=raw_text,
                source_email_file=args.input_json,
                sync_to_notion=not args.skip_notion,
            )
        else:
            raw_text = read_input_file(input_file)
            response = store_email_text(
                raw_text=raw_text,
                source_email_file=input_file,
                sync_to_notion=not args.skip_notion,
            )
    except Exception as exc:
        response = build_error_response(
            error_code="UNHANDLED_EXCEPTION",
            error_message=str(exc),
            source_email_file=input_file,
        )

    write_json_response(response, args.output_file)

    if response.get("status") == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
