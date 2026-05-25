---
name: applicant-parser
description: Ollama runtime skill for parsing Korean applicant intake messages into the applicant-tracker JSON schema.
---

# Applicant Parser For Ollama

이 스킬은 Discord로 들어온 자유 형식 지원자 메시지를 Ollama가 안정적으로 JSON으로 파싱하도록 돕는 런타임 전용 지침이다.

## Processing Order

1. 메시지를 읽고 `create`, `update`, `status_update` 의도를 먼저 판단한다.
2. 지원자 본명을 가장 먼저 찾는다. 저장 필수값은 지원자 이름 하나다.
3. 지원자가 입사하려는 채용사가 명확하면 추출하고, 불명확하면 null로 둔다.
4. 이름은 필수로 추출하고, 기업명, 생년, 포지션, 기타, 연봉(희망연봉, 그외 연봉)은 원문에 있으면 추출한다.
5. 후보자가 여러 명이면 한 명만 선택하지 말고 모든 후보자를 `applicants` 배열에 넣는다.
6. 결과는 반드시 지정 JSON 스키마만 출력한다.
7. 원문에 없는 값은 null로 두고 추측하지 않는다.
8. 이름이 중복될 수 있으므로 기업명, 생년, 나이, 포지션 같은 구분 단서가 있으면 함께 추출한다. 최종 중복 차단은 저장 계층이 판단한다.
9. 지원자 이름에서 `님`, `씨`, `지원자`, 직급 같은 호칭은 제거한다.
10. 기업명에서 출신 학교, 학과, 추천사, 헤드헌터 회사는 넣지 않는다.
11. 직무는 `영업 포지션` → `영업`, `DevOps 직군` → `DevOps`, `백엔드 개발자 직무` → `백엔드 개발자`처럼 저장용 핵심 직무명으로 정리한다.

## Required Fields And Optional Fields

- `applicant.name`: 지원자 본명. `님`, `씨`, `지원자`, 직급은 제거한다.
- `company.name`: 선택값이다. 채용사명이 명확할 때만 넣고, 없거나 애매하면 null로 둔다.
- `company.canonical_name`: 회사명이 있을 때만 만든다. 중복 판단용 표준 회사명이며 `주식회사`, `(주)`, `㈜` 같은 법인 표기를 제거한다.

## Normalization

- `케미콘일렉트로닉스코리아`, `케미콘일렉트로닉스코리아 주식회사`, `(주)케미콘일렉트로닉스코리아`, `㈜케미콘일렉트로닉스코리아`는 같은 회사로 본다.
- 위 예시의 `company.canonical_name`은 항상 `케미콘일렉트로닉스코리아`로 쓴다.
- `company.name`은 원문에 나온 공식 표시명을 우선하되, 같은 메시지 안에 짧은 표기와 풀네임이 함께 있으면 풀네임을 우선한다.
- 출신 학교, 학과, 졸업 예정 학교, 추천사, 헤드헌터 회사는 `company.name`에 넣지 않는다.
- 직무는 저장용 핵심 직무명으로 정리한다. `영업 포지션`은 `영업`, `DevOps 직군`은 `DevOps`, `백엔드 개발자 직무`는 `백엔드 개발자`로 쓴다.
- `만 34세`는 `age_international=34`로 넣는다.
- 생년만 있으면 `age_international`은 현재연도 - 생년, `age_korean`은 현재연도 - 생년 + 1로 계산한다.

## Multiple Candidates

- `후보자 3명`, `일괄 추천`, 번호 목록 `1.`, `2.`, `3.`이 있으면 다중 후보자 메시지로 본다.
- 번호 목록이 없어도 `첫 번째`, `두 번째`, `세 번째`, `한 분`, `두 분`, `세 분`, `A 후보`, `Candidate 1`, 이름이 반복되는 문단 구조가 있으면 후보자별로 분리한다.
- 다중 후보자 메시지는 `applicants` 배열에 모든 후보자를 넣는다.
- `applicant`에는 첫 번째 후보자를 중복으로 넣어도 된다. 저장 프로그램은 `applicants`를 우선 사용한다.
- 공통 회사명, 담당자, 직무는 모든 후보자에게 같은 값으로 적용한다.
- 제목의 `[JAC Recruitment] 케미콘일렉트로닉스코리아 영업 포지션`에서 `JAC Recruitment`만 추천사이고 `케미콘일렉트로닉스코리아`가 채용사다.
- 이 경우 `company.name`은 `케미콘일렉트로닉스코리아`, `company.canonical_name`도 `케미콘일렉트로닉스코리아`, 공통 직무는 `영업`이다.

## Update Handling

- `업데이트`, `변경`, `수정`, `추가`가 있으면 `intent="update"`로 판단한다.
- 상태만 바꾸는 문장이면 `intent="status_update"`로 판단한다.
- 변경하려는 값은 `updates`에도 넣고, 최종 지원자 값은 `applicant`에도 넣는다.
- `!업데이트`로 들어온 메시지는 새 지원자를 만들려는 메시지가 아니라 기존 지원자의 일부 정보를 바꾸려는 메시지다.
- 업데이트 대상 구분에 필요한 이름, 회사, 생년, 나이, 직무가 있으면 가능한 한 모두 추출한다.

## Salary Handling

- `희망연봉`은 반드시 `salary_expected`에 넣는다.
- `최종연봉`, `현재연봉`, `현연봉`, 단순 `연봉`은 `salary_current`에 넣는다.
- `희망연봉 5500`, `최종연봉 5,500만원`, `연봉 6000만원`처럼 단위가 일부 생략돼도 원문 표기를 최대한 보존한다.

## Quality Gate

필수값이 누락되면 저장 단계에서 실패하므로 특히 아래 값을 가장 엄격하게 확인한다.

- 지원자명

회사명은 있으면 정확히 추출하되, 출신 학교/학과/추천사를 회사명으로 넣지 않는다.

## Fixed Notion Columns

Notion 고정 컬럼은 `기업명`, `포지션`, `이름`, `생년`, `나이`, `희망연봉`, `최종연봉`, `기타`다.
각 컬럼의 JSON 매핑은 아래와 같다.

- `기업명`: `company.name`
- `포지션`: `applicant.position`
- `이름`: `applicant.name`
- `생년`: `applicant.birth_year`
- `나이`: `applicant.age_international`
- `희망연봉`: `applicant.salary_expected`
- `최종연봉`: `applicant.salary_current`
- `기타`: `applicant.notes`

이 값들은 `extra_properties`에 넣지 말고 표준 JSON 필드로 채운다.
나중에 Notion에 추가되는 컬럼만 `applicant.extra_properties`에 넣는다.

## Notes Versus Skills

- 언어, 자격증, 개발언어, 도구, 면허, 시험 점수는 `skills` 배열에 넣는다.
- 어느 고정 필드에도 맞지 않지만 저장 가치가 있는 맥락, 사유, 성향, 강점, 일정 불확실성은 `notes`에 넣는다.
- 불확실한 날짜 표현인 `다음주`, `추후`, `협의 가능`은 추측 날짜로 바꾸지 말고 `notes`에 원문 취지를 남긴다.

## Runtime Rule File

Ollama 프롬프트에 실제로 주입되는 짧은 규칙은 `ollama_prompt_rules.md`에 둔다.
