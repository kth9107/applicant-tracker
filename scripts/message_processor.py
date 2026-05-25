# Discord/CLI 입력 텍스트를 실제 저장 파이프라인으로 연결하는 처리 계층.
#
# 이 파일은 Discord 라이브러리에 의존하지 않는다. 그래서 Discord 봇뿐 아니라
# 테스트 스크립트에서도 같은 처리 로직을 재사용할 수 있다.
#
# 처리 전략:
# 1. 원문과 중간 결과를 `reports/discord_messages/`에 저장한다.
# 2. Ollama에 지원자 원문과 `ollama/skills/applicant-parser/` 스킬 지침을 전달한다.
# 3. AI JSON을 표준 parsed dict로 바꿔 파싱 진단을 수행한다.
# 4. 필수값이 부족하면 진단 feedback을 포함해 AI에 재파싱을 요청한다.
# 5. 재파싱 결과를 SQLite에 저장하고, 설정에 따라 Notion에 동기화한다.

import asyncio
import datetime
import json
import os
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union

from extract import extract_with_provider_chain as ai_extract
from extract import has_valid_cache
from store import (
    build_parse_diagnostics,
    store_email_text,
    store_extracted_data,
    parsed_from_extracted_data,
)


BASE_DIR = Path(__file__).resolve().parents[1]
DISCORD_LOG_DIR = BASE_DIR / "reports" / "discord_messages"


def make_log_id(channel_id: Union[int, str], message_id: Union[int, str]) -> str:
    # Discord 메시지 1건을 추적하기 위한 로그 ID를 만든다.
    #
    # 파일명에 시간, 채널 ID, 메시지 ID를 모두 넣어두면 Discord 답장에 표시된
    # `log_id`만 보고도 어떤 원문/AI 결과/저장 결과 파일인지 찾아갈 수 있다.
    #
    created = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{created}_{channel_id}_{message_id}"


def write_debug_file(log_id: str, name: str, data: object) -> None:
    # 처리 중간 산출물을 디버그 파일로 저장한다.
    #
    # 저장 실패 원인을 확인할 때 핵심 파일:
    # - `*_raw.txt`: 사용자가 보낸 원문
    # - `*_rule_precheck.json`: rule 파서가 잡은 값과 누락 필드
    # - `*_ai_extract.json`: Ollama가 만든 JSON
    # - `*_fallback_store_response.json`: fallback 저장 결과
    #
    DISCORD_LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = DISCORD_LOG_DIR / f"{log_id}_{name}"

    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
        return

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def build_reparse_feedback(
    diagnostics: dict,
    extracted_data: dict,
) -> str:
    # AI 1차 파싱 결과의 부족한 점을 재파싱 지시문으로 만든다.
    #
    # 이 feedback은 Ollama 프롬프트의 `[재파싱 피드백]` 섹션에 들어간다. 모델이
    # 원문 전체를 다시 읽되, 누락된 필수값을 우선 보완하도록 만드는 역할이다.
    #
    return "\n".join([
        "1차 파싱 결과가 저장 기준을 만족하지 못했습니다.",
        f"필수 누락: {', '.join(diagnostics.get('missing_required') or []) or '없음'}",
        f"선택 누락: {', '.join(diagnostics.get('missing_optional') or []) or '없음'}",
        "아래 1차 JSON에서 null/UNKNOWN/빈 값으로 남은 필드를 원문에서 다시 찾아 보완하세요.",
        "원문에 없는 정보는 추측하지 말고 null로 유지하세요.",
        "반드시 JSON만 다시 출력하세요.",
        "1차 JSON:",
        json.dumps(extracted_data, ensure_ascii=False, indent=2),
    ])


def diagnose_ai_extraction(
    extracted_data: dict,
    text: str,
) -> dict:
    # AI JSON을 저장 전 표준 parsed dict로 바꾸고 누락 필드를 진단한다.
    parsed = parsed_from_extracted_data(extracted_data, text)
    return {
        "parsed": parsed,
        "diagnostics": build_parse_diagnostics(parsed),
    }


async def process_text(
    text: str,
    source: str,
    sync_notion: bool,
    log_id: str,
    progress: Optional[Callable[[str], Awaitable[None]]] = None,
    update_only: bool = False,
) -> tuple[dict, str]:
    # 지원자 메시지 원문 1건을 파싱하고 SQLite/Notion 저장까지 수행한다.
    #
    # 반환값은 `(저장 결과 dict, 추출 방식 설명)`이다. 기본 경로는 AI-first다.
    # AI 결과가 필수값 기준에 미달하면 같은 원문을 feedback과 함께 재파싱한다.
    #
    async def report(stage: str) -> None:
        # Discord 봇 같은 호출자에게 현재 처리 단계를 알려준다.
        if progress:
            await progress(stage)

    await report("원문 로그 저장")
    write_debug_file(log_id, "raw.txt", text)

    use_ai_extract = os.getenv("DISCORD_USE_AI_EXTRACT", "1") != "0"
    retry_on_incomplete = os.getenv("DISCORD_AI_RETRY_ON_INCOMPLETE", "1") != "0"
    rule_fallback_on_ai_error = os.getenv("DISCORD_RULE_FALLBACK_ON_AI_ERROR", "1") != "0"
    extraction_method = "rule"

    if use_ai_extract:
        try:
            # 1단계: 메시지와 Ollama 전용 스킬을 함께 전달해 1차 AI JSON을 만든다.
            await report("AI 캐시 확인")
            cache_hit = has_valid_cache(text)
            write_debug_file(
                log_id,
                "ai_cache_status.json",
                {
                    "cache_hit": cache_hit,
                },
            )
            await report("AI 1차 파싱")
            extracted_data = await asyncio.to_thread(ai_extract, text)
            write_debug_file(log_id, "ai_extract.json", extracted_data)

            # 2단계: DB에 저장하기 전에 AI JSON을 parsed dict로 바꾸고 필수값을 진단한다.
            await report("파싱 진단")
            ai_diagnosis = diagnose_ai_extraction(extracted_data, text)
            write_debug_file(log_id, "ai_diagnostics.json", ai_diagnosis)
            final_extracted_data = extracted_data
            extraction_method = "ai(skill)"

            # 3단계: 필수값 미달이면 누락 필드를 feedback으로 만들어 AI에 재파싱을 요청한다.
            if retry_on_incomplete and ai_diagnosis["diagnostics"]["missing_required"]:
                await report("재파싱 피드백 생성")
                feedback = build_reparse_feedback(
                    ai_diagnosis["diagnostics"],
                    extracted_data,
                )
                write_debug_file(log_id, "ai_reparse_feedback.txt", feedback)
                await report("AI 재파싱")
                reparsed_data = await asyncio.to_thread(ai_extract, text, feedback)
                write_debug_file(log_id, "ai_reparse_extract.json", reparsed_data)
                await report("재파싱 결과 진단")
                reparse_diagnosis = diagnose_ai_extraction(reparsed_data, text)
                write_debug_file(log_id, "ai_reparse_diagnostics.json", reparse_diagnosis)
                final_extracted_data = reparsed_data
                extraction_method = "ai(skill+재파싱)"

            # 4단계: 최종 AI JSON만 저장 계층으로 넘긴다. 저장 계층이 SQLite/Notion을 처리한다.
            await report("SQLite 저장 및 Notion 동기화")
            response = await asyncio.to_thread(
                store_extracted_data,
                final_extracted_data,
                text,
                source,
                sync_notion,
                update_only,
            )
            write_debug_file(log_id, "ai_store_response.json", response)

        except Exception as exc:
            # 운영 안정성 옵션: AI 서버 장애 때만 rule parser로 fallback할 수 있다.
            if not rule_fallback_on_ai_error:
                raise
            await report("AI 장애 fallback rule 파싱")
            response = await asyncio.to_thread(
                store_email_text,
                text,
                source,
                sync_notion,
                update_only,
            )
            extraction_method = f"rule(ai 실패 후 장애 fallback: {exc})"
            write_debug_file(log_id, "ai_exception_and_fallback.json", {
                "ai_error": str(exc),
                "fallback_response": response,
            })
    else:
        # AI를 꺼둔 운영 모드. 정규식/rule parser만 사용한다.
        await report("rule 파싱 및 저장")
        response = await asyncio.to_thread(
            store_email_text,
            text,
            source,
            sync_notion,
            update_only,
        )
        write_debug_file(log_id, "rule_store_response.json", response)

    return response, extraction_method
