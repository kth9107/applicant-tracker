#!/usr/bin/env python3
# Ollama로 채용/추천 메일에서 정형화된 JSON을 추출하는 모듈.
#
# 이 파일은 "AI에게 원문을 보내고 JSON을 받는 일"만 담당한다. 받은 JSON을
# 검증하거나 DB에 저장하는 일은 `store.py`가 맡는다.
#
# 런타임 처리 순서:
# 1. `ollama/skills/applicant-parser/`의 스킬 지침을 읽는다.
# 2. 지원자 원문과 스킬 규칙을 합쳐 Ollama 프롬프트를 만든다.
# 3. Ollama가 JSON만 출력하도록 요청한다.
# 4. 1차 결과가 미달이면 `message_processor.py`가 feedback을 넣어 재파싱을 요청한다.
#
# 속도 개선 장치:
# - 같은 모델/프롬프트/입력 텍스트 조합이면 `cache/ai_extract/`의 캐시를 재사용한다.
# - `AI_EXTRACT_CACHE_TTL_SECONDS`가 지나면 캐시는 만료된 것으로 보고 다시 호출한다.
# - `OLLAMA_TIMEOUT`으로 AI 응답 대기 시간을 조절한다.
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

import requests


BASE_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = BASE_DIR / ".env"
PARSER_RULES_PATH = (
    BASE_DIR
    / "ollama"
    / "skills"
    / "applicant-parser"
    / "ollama_prompt_rules.md"
)
PARSER_SKILL_PATH = (
    BASE_DIR
    / "ollama"
    / "skills"
    / "applicant-parser"
    / "SKILL.md"
)
USER_FEEDBACK_RULES_PATH = (
    BASE_DIR
    / "ollama"
    / "skills"
    / "applicant-parser"
    / "user_feedback_rules.md"
)


def load_env_file() -> None:
    # `.env` 설정을 읽어 Ollama URL, 모델명, 타임아웃 등을 환경변수에 올린다.
    if not ENV_PATH.exists():
        return

    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_env_file()

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL = os.getenv("OLLAMA_MODEL", "gemma4:latest")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "45"))
AI_PROVIDER = os.getenv("AI_PROVIDER", "ollama")
AI_FALLBACK_PROVIDER = os.getenv("AI_FALLBACK_PROVIDER", "gemini")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_TIMEOUT = int(os.getenv("GEMINI_TIMEOUT", "45"))
AI_EXTRACT_CACHE_TTL_SECONDS = int(os.getenv("AI_EXTRACT_CACHE_TTL_SECONDS", "3600"))
AI_EXTRACT_CACHE_DIR = BASE_DIR / "cache" / "ai_extract"
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
FIXED_NOTION_PROPERTIES = {
    "이름",
    "기업명",
    "포지션",
    "생년",
    "나이",
    "희망연봉",
    "최종연봉",
    "기타",
    # 아래 컬럼은 이전 버전의 고정 컬럼이다. Notion에 남아 있어도 동적 컬럼으로
    # 취급하지 않아서 AI가 extra_properties에 중복 저장하지 않게 한다.
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
    "회사담당자",
    "연봉",
}
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


def load_parser_skill_rules() -> str:
    # Ollama 전용 파싱 스킬 파일과 짧은 규칙 파일을 읽는다.
    #
    # 사용자가 요청한 구조에 맞춰 Claude용 `.claude/skills`가 아니라
    # `ollama/skills/applicant-parser/`를 런타임 기준으로 삼는다. `SKILL.md`는
    # 처리 원칙, `ollama_prompt_rules.md`는 모델에 직접 주입할 짧은 규칙이다.
    #
    chunks = []
    if PARSER_SKILL_PATH.exists():
        chunks.append(PARSER_SKILL_PATH.read_text(encoding="utf-8").strip())
    if PARSER_RULES_PATH.exists():
        chunks.append(PARSER_RULES_PATH.read_text(encoding="utf-8").strip())
    return "\n\n".join(chunk for chunk in chunks if chunk)


def load_user_feedback_rules() -> str:
    # Discord `!문제점`으로 접수한 운영 피드백을 AI 프롬프트용 규칙으로 읽는다.
    if not USER_FEEDBACK_RULES_PATH.exists():
        return ""
    content = USER_FEEDBACK_RULES_PATH.read_text(encoding="utf-8").strip()
    if not content:
        return ""
    return "\n".join([
        "아래는 운영 중 Discord에서 접수된 사용자 피드백입니다.",
        "최신 피드백을 우선 참고하되, 원문에 없는 정보는 추측하지 마세요.",
        content,
    ])


def load_dynamic_notion_prompt_rules() -> str:
    # Notion DB의 추가 컬럼을 읽어 AI가 extra_properties로 채우도록 안내한다.
    token = os.getenv("NOTION_TOKEN")
    database_id = os.getenv("NOTION_DB_ID")
    if not token or not database_id:
        return ""

    request = urllib.request.Request(
        f"{NOTION_API_BASE}/databases/{database_id}",
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": NOTION_VERSION,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return ""

    dynamic = []
    for name, prop in (data.get("properties") or {}).items():
        prop_type = prop.get("type")
        if name in FIXED_NOTION_PROPERTIES:
            continue
        if prop_type in SUPPORTED_DYNAMIC_NOTION_TYPES:
            dynamic.append(f"- {name}: {prop_type}")

    if not dynamic:
        return ""

    return "\n".join([
        "Notion DB에 아래 추가 컬럼이 있습니다.",
        "원문에서 해당 값이 보이면 applicant.extra_properties 객체에 컬럼명 그대로 넣으세요.",
        "기업명/포지션/회사담당자/이름/생년/나이/희망연봉/최종연봉/기타는 고정 컬럼이므로 extra_properties에 넣지 마세요.",
        "원문에 없으면 추측하지 말고 넣지 마세요.",
        *dynamic,
        '예: {"applicant": {"extra_properties": {"입사가능일": "2026-07-01"}}}',
    ])

PROMPT = """당신은 채용 관련 이메일에서 후보자 정보를 추출하는 시스템입니다.

★ "회사(company)"의 정의:
  - 후보자가 입사하려는 **채용사**(직무를 모집하는 곳)를 의미합니다.
  - 추천 에이전시(예: JAC Recruitment, 헤드헌터)는 회사가 아닙니다.
  - 출신 학교/학과/졸업 예정 학교도 회사가 아닙니다.
  - 메일이 에이전시→채용사 추천 메일이면 회사는 "채용사" (메일 수신자 측).
  - 메일이 채용사→에이전시 회신 메일이면 회사는 "채용사" (메일 발신자 측).
  - 회사명이 본문에 명시되지 않았으면 발신/수신자 이메일 도메인 회사명을 추론하지 말고 null.

★ "회사 담당자(contact_person)"의 정의:
  - 채용사 측의 HR/인사 담당자.
  - 추천인(에이전시 직원)은 담당자가 아닙니다.

다음 JSON 스키마에 정확히 맞춰 출력하세요. JSON 외 어떤 텍스트도 출력 금지.

스키마:
{
  "intent": "create|update|status_update",
  "company": {
    "name": "채용사명(풀네임). 명시 안 됐으면 null.",
    "canonical_name": "중복 판단용 표준 회사명. 주식회사/(주)/㈜ 같은 법인 표기는 제거.",
    "contact_person": "채용사 담당자 이름(호칭 제외)",
    "contact_email": "담당자 이메일",
    "contact_phone": "담당자 전화"
  },
  "applicant": {
    "name": "후보자 이름(호칭 제외)",
    "birth_year": 1990,
    "age_international": 34,
    "age_korean": 36,
    "email": "후보자 이메일",
    "phone": "후보자 전화",
    "position": "지원 직무명",
    "education": ["학력을 한 줄씩"],
    "experience": ["경력을 한 줄씩"],
    "skills": ["보유 능력/자격을 한 줄씩"],
    "salary_current": "최종연봉/현재연봉 (예: '5,500만원'). 없으면 null.",
    "salary_expected": "희망연봉 (예: '6,000만원'). 없으면 null.",
    "notes": "성향/강점 등 자유 메모",
    "extra_properties": {"Notion 추가 컬럼명": "원문에서 찾은 값"}
  },
  "applicants": [
    {
      "name": "여러 후보자가 있을 때 후보자 이름",
      "birth_year": 1990,
      "age_international": 34,
      "age_korean": 36,
      "email": "후보자 이메일",
      "phone": "후보자 전화",
      "position": "지원 직무명",
      "education": ["학력을 한 줄씩"],
      "experience": ["경력을 한 줄씩"],
      "skills": ["보유 능력/자격을 한 줄씩"],
      "salary_current": "최종연봉/현재연봉. 없으면 null.",
      "salary_expected": "희망연봉. 없으면 null.",
      "notes": "성향/강점 등 자유 메모",
      "extra_properties": {"Notion 추가 컬럼명": "원문에서 찾은 값"}
    }
  ],
  "updates": {
    "position": "변경할 지원 직무",
    "status": "변경할 상태",
    "education": ["추가/변경할 학력"],
    "experience": ["추가/변경할 경력"],
    "skills": ["추가/변경할 스킬"],
    "notes": "추가/변경할 메모",
    "extra_properties": {"Notion 추가 컬럼명": "추가/변경할 값"}
  },
  "status": "서류접수|서류합격|서류불합격|면접대기|면접완료|최종합격|최종불합격|보류 중 하나"
}

규칙:
- 메일에 없는 정보는 null. 절대 추측 금지.
- 필수 저장값은 후보자 이름뿐이며, 회사명이 명확하지 않으면 company.name은 null.
- 한 메시지에 후보자가 2명 이상 있으면 절대 한 명만 고르지 말고 모든 후보자를 applicants 배열에 넣기.
- 여러 후보자가 있으면 applicant에는 첫 번째 후보자도 넣고, applicants에는 전체 후보자를 모두 넣기.
- `1. 김대호`, `2. 이영희`, `3. 박철수`처럼 번호 목록이면 번호별 후보자 전원을 분리하기.
- 번호가 없어도 첫 번째/두 번째/세 번째, 한 분/두 분/세 분, 이름별 문단, Candidate 1/2 같은 구조는 후보자별로 분리하기.
- `[JAC Recruitment] 케미콘일렉트로닉스코리아 영업 포지션`에서 JAC Recruitment는 추천사이고 company.name은 케미콘일렉트로닉스코리아.
- 회사명 표기가 `케미콘일렉트로닉스코리아`, `케미콘일렉트로닉스코리아 주식회사`, `(주)케미콘일렉트로닉스코리아`처럼 다르면 같은 회사로 보고 canonical_name은 `케미콘일렉트로닉스코리아`로 통일하기.
- company.name은 원문에 나온 가장 공식적인 표시명을 쓰되, canonical_name은 중복 방지용으로 법인 표기를 제거한 값을 넣기.
- 지원 직무는 저장용 핵심 직무명으로 정규화한다. 예: `영업 포지션` -> `영업`, `백엔드 개발자 직무` -> `백엔드 개발자`.
- 모든 값은 한국어로 (이메일/전화/숫자 제외).
- 이름은 호칭(님/씨/담당자) 제거 후 본명만.
- birth_year는 숫자(정수)로.
- 이메일에 "만 34세"처럼 만 나이가 있으면 age_international에 숫자로 넣기.
- birth_year가 있으면 age_korean은 현재연도 - birth_year + 1로 계산하기.
- 나이가 없고 생년만 있으면 age_international은 현재연도 - birth_year로 계산하기.
- "김태현 1991년 만 34세"처럼 자유 형식이어도 후보자 이름은 김태현, birth_year는 1991, age_international은 34로 추출하기.
- "지원자 봉하선 연봉 3000"처럼 짧은 업데이트 문장은 applicant.name="봉하선", salary_current="3000", intent="update"로 추출하기.
- 희망연봉은 반드시 salary_expected에 저장.
- 최종연봉·현재연봉·연봉처럼 희망연봉이 아닌 모든 연봉 표현은 salary_current에 저장.
- 연봉 금액은 원문 표기를 그대로 유지 (예: '5,500만원', '6000만원').
- Notion 고정 컬럼은 기업명/포지션/회사담당자/이름/생년/나이/희망연봉/최종연봉/기타이며, 이 값은 extra_properties에 넣지 않기.
- 고정 컬럼 매핑은 기업명=company.name, 포지션=applicant.position, 회사담당자=company.contact_person, 이름=applicant.name, 생년=applicant.birth_year, 나이=applicant.age_international, 희망연봉=applicant.salary_expected, 최종연봉=applicant.salary_current, 기타=applicant.notes.
- 상태, 이메일, 전화, 스킬은 표준 JSON 필드에는 넣되 현재 Notion 고정 컬럼이 아니므로 extra_properties에 중복으로 넣지 않기. 담당자는 회사담당자 고정 컬럼으로 쓰는 company.contact_person에 넣기.
- 신규 추천/등록이면 intent는 create.
- "업데이트", "변경", "수정", "추가"처럼 기존 지원자 정보를 바꾸는 문장이면 intent는 update.
- "서류합격으로 변경", "면접대기로 변경"처럼 상태만 바꾸는 문장이면 intent는 status_update.
- 업데이트 문장에서는 바꾸려는 필드를 updates에도 넣고 applicant에도 최종 값으로 넣기.
- 예: "케미콘 주식회사의 김태현 지원자님 지원직무는 백엔드 개발자로 업데이트"는 company.name="케미콘 주식회사", applicant.name="김태현", applicant.position="백엔드 개발자", updates.position="백엔드 개발자", intent="update".

[이메일]
"""


PARSER_SKILL_RULES = load_parser_skill_rules()


def build_prompt(email_text: str, feedback: Optional[str] = None) -> str:
    # 기본 추출 프롬프트, Ollama 스킬 규칙, 재파싱 feedback, 원문을 결합한다.
    #
    # feedback은 1차 AI 결과가 필수 조건을 만족하지 못했을 때만 들어간다.
    # 이때 모델은 누락된 필드를 보완하는 데 집중해서 다시 JSON을 만든다.
    #
    prompt = PROMPT
    if PARSER_SKILL_RULES:
        prompt = PROMPT.replace(
            "[이메일]\n",
            f"[Ollama 파싱 스킬]\n{PARSER_SKILL_RULES}\n\n[이메일]\n",
        )
    user_feedback_rules = load_user_feedback_rules()
    if user_feedback_rules:
        prompt = prompt.replace(
            "[이메일]\n",
            f"[사용자 피드백 규칙]\n{user_feedback_rules}\n\n[이메일]\n",
        )
    dynamic_rules = load_dynamic_notion_prompt_rules()
    if dynamic_rules:
        prompt = prompt.replace(
            "[이메일]\n",
            f"[Notion 동적 추가 컬럼]\n{dynamic_rules}\n\n[이메일]\n",
        )
    if feedback:
        prompt = prompt.replace(
            "[이메일]\n",
            f"[재파싱 피드백]\n{feedback.strip()}\n\n[이메일]\n",
        )
    return prompt + email_text


def cache_key(
    email_text: str,
    feedback: Optional[str] = None,
    provider: Optional[str] = None,
) -> str:
    # AI 캐시 파일명을 만들기 위한 고유 해시를 생성한다.
    #
    # 모델명과 프롬프트가 바뀌면 같은 입력이라도 결과 의미가 달라질 수 있으므로
    # 세 값을 모두 해시에 포함한다.
    #
    provider_name = provider or AI_PROVIDER
    model_name = model_for_provider(provider_name)
    source = "\n".join([provider_name, model_name, build_prompt(email_text, feedback)])
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def cache_path(
    email_text: str,
    feedback: Optional[str] = None,
    provider: Optional[str] = None,
) -> Path:
    # 입력 텍스트에 대응하는 AI 캐시 JSON 파일 경로를 반환한다.
    return AI_EXTRACT_CACHE_DIR / f"{cache_key(email_text, feedback, provider)}.json"


def read_cache(
    email_text: str,
    feedback: Optional[str] = None,
    provider: Optional[str] = None,
) -> Optional[dict]:
    # 유효한 AI 캐시가 있으면 추출 결과 dict를 반환한다.
    #
    # 캐시가 없거나, JSON이 깨졌거나, TTL이 지났으면 `None`을 반환해서
    # 호출자가 Ollama를 다시 호출하게 한다.
    #
    if AI_EXTRACT_CACHE_TTL_SECONDS <= 0:
        return None

    path = cache_path(email_text, feedback, provider)
    if not path.exists():
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None

    created_at = payload.get("created_at", 0)
    if time.time() - created_at > AI_EXTRACT_CACHE_TTL_SECONDS:
        return None

    return payload.get("result")


def has_valid_cache(
    email_text: str,
    feedback: Optional[str] = None,
    provider: Optional[str] = None,
) -> bool:
    # 현재 입력에 재사용 가능한 AI 캐시가 있는지 여부만 빠르게 확인한다.
    return read_cache(email_text, feedback, provider) is not None


def write_cache(
    email_text: str,
    result: dict,
    feedback: Optional[str] = None,
    provider: Optional[str] = None,
) -> None:
    # Ollama에서 받은 JSON 결과를 디스크 캐시에 저장한다.
    if AI_EXTRACT_CACHE_TTL_SECONDS <= 0:
        return

    provider_name = provider or AI_PROVIDER
    AI_EXTRACT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = cache_path(email_text, feedback, provider_name)
    path.write_text(
        json.dumps(
            {
                "created_at": time.time(),
                "provider": provider_name,
                "model": model_for_provider(provider_name),
                "result": result,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def extract(email_text: str) -> dict:
    # 원문 텍스트를 Ollama에 보내 지원자 JSON을 추출한다.
    #
    # 먼저 캐시를 확인하고, 캐시가 없을 때만 `/api/chat`을 호출한다. Ollama는
    # `format=json` 옵션을 사용하므로 모델 응답이 JSON 형태로 제한된다.
    #
    return extract_with_feedback(email_text)


def model_for_provider(provider: str) -> str:
    # AI provider 이름에 맞는 모델명을 반환한다.
    if provider == "gemini":
        return GEMINI_MODEL
    return MODEL


def provider_chain() -> list[str]:
    # 기본 provider와 fallback provider를 중복 없이 순서대로 반환한다.
    raw = [AI_PROVIDER, *AI_FALLBACK_PROVIDER.split(",")]
    providers = []
    for provider in raw:
        provider = provider.strip().lower()
        if provider and provider not in providers and provider != "rule":
            providers.append(provider)
    return providers or ["ollama"]


def extract_with_feedback(
    email_text: str,
    feedback: Optional[str] = None,
    provider: Optional[str] = None,
) -> dict:
    # 선택적 feedback을 포함해 AI provider에 파싱을 요청한다.
    #
    # 1차 파싱은 feedback 없이 호출하고, 파싱 진단에서 필수값 누락이 발견되면
    # `message_processor.py`가 누락 필드와 1차 결과 요약을 feedback으로 넣어
    # 이 함수를 다시 호출한다.
    #
    provider_name = (provider or AI_PROVIDER).lower()
    cached_result = read_cache(email_text, feedback, provider_name)
    if cached_result is not None:
        return cached_result

    prompt = build_prompt(email_text, feedback)
    if provider_name == "gemini":
        result = extract_with_gemini(prompt)
        write_cache(email_text, result, feedback, provider_name)
        return result

    result = extract_with_ollama(prompt)
    write_cache(email_text, result, feedback, provider_name)
    return result


def extract_json_from_response(full: str) -> dict:
    # Ollama 응답에서 JSON 객체를 추출한다.
    #
    # qwen3.5는 format:json 지시에도 불구하고 한국어 설명문을 앞에 붙이거나,
    # ```json ... ``` 마크다운 블록으로 감싸는 경우가 있다. 세 가지 케이스를 처리한다:
    #   1. ```json ... ``` 블록: 첫 줄과 마지막 ``` 제거 후 파싱
    #   2. 순수 JSON: 그대로 파싱
    #   3. 설명문 + JSON: { 가 처음 나오는 위치부터 마지막 } 까지 추출
    #
    stripped = full.strip()

    # 케이스 1: 마크다운 코드 블록
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]  # 첫 줄(```json) 제거
        stripped = stripped.rsplit("```", 1)[0]  # 마지막 ``` 제거
        stripped = stripped.strip()

    # 케이스 2: 순수 JSON (가장 흔한 정상 케이스)
    if stripped.startswith("{") or stripped.startswith("["):
        return json.loads(stripped)

    # 케이스 3: 설명문 앞뒤로 붙은 경우 — { ... } 블록만 추출
    # 중첩 JSON을 올바르게 처리하기 위해 bracket 카운터 방식 사용
    start = stripped.find("{")
    if start == -1:
        raise ValueError(f"JSON 객체를 찾을 수 없습니다. 응답 앞부분: {repr(stripped[:200])}")
    depth = 0
    end = -1
    in_string = False
    escape_next = False
    for i, ch in enumerate(stripped[start:], start=start):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        raise ValueError(f"JSON 객체가 닫히지 않았습니다. 응답 앞부분: {repr(stripped[:200])}")
    return json.loads(stripped[start : end + 1])


def extract_with_ollama(prompt: str) -> dict:
    # Ollama `/api/chat`로 JSON 추출을 수행한다.
    #
    # stream=True: 스트리밍으로 수신하여 타임아웃을 방지한다.
    #   - stream=False 는 전체 응답이 올 때까지 block되어 qwen3.5 thinking 모드에서 타임아웃 발생.
    #   - stream=True 는 첫 토큰부터 수신하므로 타임아웃 없이 동작한다.
    # /no_think + think=False: qwen3/qwen3.5 계열의 thinking 모드를 비활성화해 속도를 높인다.
    r = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": f"/no_think\n{prompt}"}],
            "format": "json",
            "stream": True,
            "think": False,
            "options": {"temperature": 0.1},
        },
        stream=True,
        timeout=OLLAMA_TIMEOUT,
    )
    r.raise_for_status()
    full = ""
    for line in r.iter_lines():
        if not line:
            continue
        chunk = json.loads(line)
        full += chunk.get("message", {}).get("content", "")
        if chunk.get("done"):
            break
    return extract_json_from_response(full)


def extract_with_gemini(prompt: str) -> dict:
    # Gemini generateContent API로 JSON 추출을 수행한다.
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY가 없어 Gemini fallback을 사용할 수 없습니다.")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    response = requests.post(
        url,
        headers={
            "x-goog-api-key": GEMINI_API_KEY,
            "Content-Type": "application/json",
        },
        json={
            "contents": [
                {
                    "parts": [
                        {
                            "text": prompt,
                        }
                    ]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.1,
            },
        },
        timeout=GEMINI_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def extract_with_provider_chain(email_text: str, feedback: Optional[str] = None) -> dict:
    # 설정된 provider chain을 순서대로 시도하고 마지막 오류를 보고한다.
    errors = []
    for provider in provider_chain():
        try:
            return extract_with_feedback(email_text, feedback, provider)
        except Exception as exc:
            errors.append(f"{provider}: {exc}")
    raise RuntimeError("AI provider가 모두 실패했습니다. " + " | ".join(errors))

if __name__ == "__main__":
    text = open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1 else sys.stdin.read()
    print("⏳ gemma4 추출 중...", file=sys.stderr)
    result = extract(text)
    print(json.dumps(result, ensure_ascii=False, indent=2))
