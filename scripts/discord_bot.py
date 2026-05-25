# Discord 채널에서 지원자 메시지를 받아 저장 파이프라인으로 넘기는 봇.
#
# 이 파일은 Discord 입출력만 담당한다. 실제 지원자 정보 추출, SQLite 저장,
# Notion 동기화는 `message_processor.process_text()` 아래 단계에서 처리한다.
#
# 전체 흐름:
# 1. `.env`에서 봇 토큰, 입력 채널 ID, 병렬 처리 개수 등을 읽는다.
# 2. 지정된 Discord 채널의 메시지만 처리한다.
# 3. `!지원자` 또는 `!applicant` 명령어를 제거하고 본문만 추출한다.
# 4. 접수 완료 메시지를 먼저 보낸 뒤 백그라운드 task로 저장을 진행한다.
# 5. 저장이 끝나면 성공/실패 상세 내용을 Discord 답장으로 보낸다.

import asyncio
import datetime
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import discord

from message_processor import make_log_id, process_text
from notion_check import (
    build_discord_audit_reply,
    build_discord_recovery_reply,
    recover_sqlite_to_notion,
    run_audit,
)
from store import parse_email_text


BASE_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = BASE_DIR / ".env"
BOT_VERSION = "2026-05-23-ai-json-v7"
USER_FEEDBACK_RULES_PATH = (
    BASE_DIR
    / "ollama"
    / "skills"
    / "applicant-parser"
    / "user_feedback_rules.md"
)
USAGE_COMMANDS = {"!사용법", "!help", "!도움말"}
COMMAND_HELP_COMMANDS = {"!명령어", "!commands"}
APPLICANT_COMMANDS = {"!지원자", "!applicant"}
UPDATE_COMMANDS = {"!업데이트", "!update"}
STATUS_COMMANDS = {"!상태", "!status"}
QUEUE_COMMANDS = {"!대기", "!queue", "!작업"}
FEEDBACK_COMMANDS = {"!문제점", "!feedback", "!피드백"}
AUDIT_COMMANDS = {"!감사", "!audit"}
FULL_AUDIT_COMMANDS = {"!전체감사", "!fullaudit", "!audit_all"}
RECOVERY_COMMANDS = {"!복구", "!recover"}


@dataclass
class JobRecord:
    # Discord 메시지 1건의 대기/진행 상태를 저장하는 작업 레코드.

    log_id: str
    message_id: int
    author_name: str
    candidate_name: str
    company_name: str
    birth_year: Optional[int]
    age_international: Optional[int]
    age_korean: Optional[int]
    status: str
    stage: str
    created_at: float
    started_at: Optional[float] = None
    finished_at: Optional[float] = None


class JobTracker:
    # 봇이 처리 중인 작업을 메모리에 기록하고 `!대기`에 보여주는 상태 관리자.
    #
    # 이 정보는 봇 프로세스 메모리에만 존재한다. 봇을 재시작하면 진행 목록은
    # 초기화되지만, 실제 저장 로그는 `reports/discord_messages/`에 남는다.
    #

    def __init__(self, max_history: int = 10) -> None:
        # 진행 중 작업과 최근 완료 작업을 저장할 컨테이너를 초기화한다.
        self.max_history = max_history
        self.active: dict[str, JobRecord] = {}
        self.history: list[JobRecord] = []
        self.lock = asyncio.Lock()

    async def add(self, job: JobRecord) -> None:
        # 새 작업을 대기 상태로 등록한다.
        async with self.lock:
            self.active[job.log_id] = job

    async def mark_running(self, log_id: str, stage: str) -> None:
        # semaphore를 통과해 실제 처리가 시작된 작업을 진행 중으로 바꾼다.
        async with self.lock:
            job = self.active.get(log_id)
            if not job:
                return
            job.status = "running"
            job.stage = stage
            job.started_at = job.started_at or time.time()

    async def update_stage(self, log_id: str, stage: str) -> None:
        # 진행 중 작업의 세부 단계를 갱신한다.
        async with self.lock:
            job = self.active.get(log_id)
            if job:
                job.stage = stage

    async def update_details(self, log_id: str, parsed: dict) -> None:
        # 파싱/저장 결과에서 사용자에게 보여줄 주요 지원자 정보를 갱신한다.
        async with self.lock:
            job = self.active.get(log_id)
            if job:
                apply_job_details(job, parsed)

    async def finish(self, log_id: str, status: str, stage: str) -> None:
        # 작업을 active 목록에서 제거하고 최근 완료 이력으로 이동한다.
        async with self.lock:
            job = self.active.pop(log_id, None)
            if not job:
                return
            job.status = status
            job.stage = stage
            job.finished_at = time.time()
            self.history.insert(0, job)
            del self.history[self.max_history:]

    async def snapshot(self) -> tuple[list[JobRecord], list[JobRecord]]:
        # 현재 active 작업과 최근 완료 작업의 복사본을 반환한다.
        async with self.lock:
            return list(self.active.values()), list(self.history)


def load_env_file() -> None:
    # 프로젝트 루트의 `.env` 파일을 환경변수로 로드한다.
    #
    # 이미 OS 환경변수에 같은 키가 있으면 덮어쓰지 않는다. 운영 환경에서
    # 직접 주입한 값이 `.env`보다 우선되도록 하기 위한 처리다.
    #
    if not ENV_PATH.exists():
        return

    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def required_env(name: str) -> str:
    # 필수 환경변수를 읽고, 없으면 봇 실행을 중단할 명확한 오류를 낸다.
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name}이 .env에 없습니다.")
    return value


def elapsed_text(started_at: Optional[float]) -> str:
    # 시작 시각부터 현재까지 지난 시간을 사람이 읽는 문자열로 만든다.
    if not started_at:
        return "-"
    elapsed = int(time.time() - started_at)
    minutes, seconds = divmod(elapsed, 60)
    return f"{minutes}분 {seconds}초" if minutes else f"{seconds}초"


def trim_preview(text: str, max_length: int = 50) -> str:
    # 대기 목록에 보여줄 메시지 미리보기를 한 줄로 줄인다.
    preview = " ".join(text.split())
    if len(preview) <= max_length:
        return preview
    return preview[: max_length - 3] + "..."


def command_body(content: str, commands: set[str]) -> str:
    # 명령어 뒤쪽 본문만 잘라낸다.
    stripped = content.strip()
    for command in commands:
        if stripped == command:
            return ""
        if stripped.startswith(command + "\n"):
            return stripped[len(command):].strip()
        if stripped.startswith(command + " "):
            return stripped[len(command):].strip()
    return ""


def is_exact_command(content: str, commands: set[str]) -> bool:
    # 메시지가 본문 없이 명령어 하나만 포함하는지 확인한다.
    return content.strip() in commands


def parse_audit_limit_command(content: str) -> Optional[int]:
    # `!감사` 또는 `!감사 20` 형태에서 최근 감사 개수를 읽는다.
    parts = content.strip().split()
    if not parts or parts[0] not in AUDIT_COMMANDS:
        return None
    if len(parts) == 1:
        return 10
    try:
        limit = int(parts[1])
    except ValueError:
        return 10
    return max(1, min(limit, 100))


def has_command_prefix(content: str, commands: set[str]) -> bool:
    # 메시지가 지정 명령어로 시작하는지 확인한다.
    stripped = content.strip()
    for command in commands:
        if stripped == command or stripped.startswith(command + "\n") or stripped.startswith(command + " "):
            return True
    return False


def build_unknown_command_reply() -> str:
    # 알 수 없는 `!` 명령어가 저장 파이프라인으로 들어가지 않도록 안내한다.
    return "\n".join([
        "알 수 없는 명령어입니다.",
        "지원자 저장은 !지원자, 기존 지원자 수정은 !업데이트 로 시작해주세요.",
        "사용 가능한 명령어는 !명령어 또는 !사용법 으로 확인할 수 있습니다.",
    ])


def append_user_feedback_rule(message: discord.Message, feedback: str) -> int:
    # Discord에서 받은 개선 요청을 AI가 읽는 사용자 피드백 규칙 파일에 추가한다.
    USER_FEEDBACK_RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    created_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    author_name = getattr(message.author, "display_name", str(message.author))
    cleaned_feedback = " ".join(feedback.split())
    existing = USER_FEEDBACK_RULES_PATH.read_text(encoding="utf-8") if USER_FEEDBACK_RULES_PATH.exists() else ""
    entry = f"- {created_at} | {author_name}: {cleaned_feedback}\n"
    USER_FEEDBACK_RULES_PATH.write_text(existing + entry, encoding="utf-8")
    return len([line for line in (existing + entry).splitlines() if line.strip().startswith("- ")])


def build_feedback_help() -> str:
    # `!문제점` 사용법을 Discord 사용자에게 보여준다.
    return "\n".join([
        "문제점/개선 요청 작성 방법",
        "아래처럼 보내면 다음 AI 파싱부터 참고합니다.",
        "",
        "!문제점",
        "SM엔터라고 본문에 있으면 제목의 케미콘보다 본문 회사명을 우선해줘",
        "",
        "예시",
        "!문제점 여러 후보자가 자연어 문단으로 이어져도 후보자별로 분리해줘",
        "!문제점 JLPT, 토익, 개발언어는 스킬에 넣어줘",
    ])


def build_usage_help() -> str:
    # 처음 쓰는 Discord 사용자도 바로 입력할 수 있게 명령어와 예시를 안내한다.
    return "\n".join([
        "지원자 트래커 사용법",
        "",
        "1. 지원자 저장",
        "!지원자",
        "회사명",
        "담당자명",
        "",
        "지원자 이름 1991년생 만 34세",
        "지원직무",
        "학력/경력/스킬/메모",
        "",
        "예시",
        "!지원자",
        "케미콘 주식회사",
        "최은하 담당자님",
        "",
        "김태현 1991년생 만 34세",
        "백엔드 개발자 지원",
        "조선대학교 컴퓨터공학과 졸업",
        "자바, Oracle, MSSQL, Node.js",
        "",
        "2. 기존 지원자 수정",
        "!업데이트",
        "케미콘 주식회사의 김태현 지원자님",
        "지원직무는 백엔드 개발자로 업데이트",
        "",
        "동명이인이 있으면 회사명, 생년, 만나이, 직무 중 구분 정보를 함께 적어주세요.",
        "예: !업데이트 김태현 케미콘 1991년생 백엔드 개발자 희망연봉 5500",
        "",
        "3. 상태 변경",
        "!지원자",
        "케미콘 주식회사의 김태현 지원자님",
        "서류합격으로 변경",
        "",
        "4. 여러 명 저장",
        "!지원자",
        "SM엔터 마케팅 포지션 후보자 2명 추천드립니다.",
        "김대호 1997년생 일본어 능통",
        "이영희 1995년생 영어, 일본어 가능",
        "",
        "사용 가능한 명령어",
        "- !사용법: 이 안내 보기",
        "- !명령어: 명령어 요약 보기",
        "- !지원자: 지원자 저장/수정/상태변경",
        "- !업데이트: 기존 지원자 정보만 수정",
        "- !복구: SQLite 데이터를 Notion에 다시 생성",
        "- !상태: 봇 설정 상태 확인",
        "- !대기: 진행 중/대기 중 작업 확인",
        "- !감사: 최근 10명 SQLite/메시지/AI JSON/Notion 비교",
        "- !전체감사: 전체 SQLite/Notion 비교 및 Notion 삭제 반영",
        "- !문제점: 파싱 개선 요청 등록",
        "",
        "개선 요청 예시",
        "!문제점 JLPT, 토익, 개발언어는 스킬에 넣어줘",
    ])


def build_command_help() -> str:
    # 명령어만 빠르게 확인할 수 있는 Discord 안내문을 만든다.
    return "\n".join([
        "사용 가능한 명령어",
        "",
        "!지원자",
        "- 지원자 저장/수정/상태 변경을 진행합니다. 명령어 없는 일반 메시지도 같은 방식으로 처리합니다.",
        "",
        "!업데이트",
        "- 기존 지원자 정보만 수정합니다. 동명이인이 있으면 회사명, 생년, 만나이, 직무 중 구분 정보를 함께 적어주세요.",
        "",
        "!감사 [최근 정보 수]",
        "- 최근 저장 결과를 데이터베이스, Notion, 원문 메시지, AI JSON과 비교합니다.",
        "- 파싱 누락, 값 불일치, Notion 동기화 문제를 확인할 때 사용합니다.",
        "- 확인된 문제는 이후 AI 파싱 규칙과 스킬을 개선하는 참고 자료로 사용합니다.",
        "- 예시: !감사",
        "- 예시: !감사 20",
        "",
        "!전체감사",
        "- Notion을 기준으로 전체 데이터베이스와 Notion을 비교합니다.",
        "- Notion row가 삭제/보관된 경우에만 연결된 SQLite 지원자 row를 삭제합니다.",
        "- Notion에 데이터가 있고 SQLite에 없거나 비어 있는 값은 SQLite가 Notion 기준으로 갱신됩니다.",
        "- 전체 불일치 패턴은 이후 AI 파싱 규칙과 스킬을 개선하는 참고 자료로 사용합니다.",
        "",
        "!복구",
        "- SQLite에는 남아 있지만 Notion에서 삭제되었거나 연결이 없는 지원자를 Notion에 다시 생성합니다.",
        "",
        "!대기",
        "- 현재 대기 중/진행 중/최근 완료 작업을 보여줍니다.",
        "",
        "!상태",
        "- 봇, AI, Notion, 병렬 처리 설정 상태를 보여줍니다.",
        "",
        "!문제점 내용",
        "- 파싱 개선 요청을 스킬 피드백 파일에 기록합니다.",
        "- 예시: !문제점 JLPT, 토익, 개발언어는 스킬에 넣어줘",
        "",
        "!사용법",
        "- 처음 사용하는 사람을 위한 상세 예시를 보여줍니다.",
    ])


def clean_display_value(value: object) -> str:
    # Discord 사용자 화면에 보여줄 수 있는 값만 문자열로 반환한다.
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text == "UNKNOWN" or text == "-":
        return ""
    return text


def apply_job_details(job: JobRecord, parsed: dict) -> None:
    # parsed dict에서 작업 목록에 표시할 주요 지원자 정보를 복사한다.
    candidate_name = clean_display_value(parsed.get("candidate_name"))
    company_name = clean_display_value(parsed.get("company_name"))
    birth_year = parsed.get("birth_year")
    age_international = parsed.get("age_international")
    age_korean = parsed.get("age_korean")

    if candidate_name:
        job.candidate_name = candidate_name
    if company_name:
        job.company_name = company_name
    if birth_year:
        job.birth_year = birth_year
    if age_international:
        job.age_international = age_international
    if age_korean:
        job.age_korean = age_korean


def initial_job_details(text: str) -> dict:
    # 대기 등록 시점에 표시할 수 있는 주요 정보를 가볍게 추출한다.
    try:
        return parse_email_text(text)
    except Exception:
        return {}


def format_applicant_summary(job: JobRecord) -> str:
    # 지원자/회사/나이/출생년도 중 존재하는 값만 한 줄로 만든다.
    parts = []
    if job.candidate_name:
        parts.append(f"지원자 {job.candidate_name}")
    if job.company_name:
        parts.append(f"회사 {job.company_name}")
    if job.birth_year:
        parts.append(f"출생년도 {job.birth_year}")
    if job.age_international:
        parts.append(f"만나이 {job.age_international}")
    if job.age_korean:
        parts.append(f"한국나이 {job.age_korean}")
    return " | ".join(parts) if parts else "지원자 정보 확인 중"


def append_field(lines: list[str], label: str, value: object) -> None:
    # 값이 있을 때만 Discord 답장에 한 줄을 추가한다.
    text = clean_display_value(value)
    if text:
        lines.append(f"- {label}: {text}")


def append_list_field(lines: list[str], label: str, values: list[str]) -> None:
    # 비어 있지 않은 리스트만 Discord 답장에 한 줄로 추가한다.
    cleaned = [clean_display_value(value) for value in values or []]
    cleaned = [value for value in cleaned if value]
    if cleaned:
        lines.append(f"- {label}: {', '.join(cleaned)}")


def completed_info_labels(fields: dict) -> list[str]:
    # 실패 응답에서 이미 확인된 주요 정보 라벨만 추린다.
    labels = []
    checks = [
        ("지원자", fields.get("candidate_name")),
        ("지원회사", fields.get("company_name")),
        ("출생년도", fields.get("birth_year")),
        ("만나이", fields.get("age_international")),
        ("한국나이", fields.get("age_korean")),
        ("직무", fields.get("position")),
        ("담당자", fields.get("contact_person")),
        ("스킬", fields.get("skills")),
    ]
    for label, value in checks:
        if isinstance(value, list):
            if [item for item in value if clean_display_value(item)]:
                labels.append(label)
        elif clean_display_value(value):
            labels.append(label)
    return labels


def missing_info_labels(diagnostics: dict) -> list[str]:
    # 필수/선택 누락 정보를 사용자가 이해하기 쉬운 라벨로 합친다.
    labels = []
    for label in diagnostics.get("missing_required") or []:
        if label not in labels:
            labels.append(label)
    for label in diagnostics.get("missing_optional") or []:
        if label not in labels:
            labels.append(label)
    return labels


def first_error_message(response: dict) -> str:
    # 저장 실패 원인 중 사용자에게 보여줄 첫 번째 메시지를 꺼낸다.
    errors = response.get("errors") or []
    if not errors:
        return ""
    return clean_display_value(errors[0].get("message"))


async def build_queue_reply(
    tracker: JobTracker,
    semaphore: asyncio.Semaphore,
    max_parallel: int,
) -> str:
    # `!대기` 명령에 답장할 작업 현황 문자열을 만든다.
    active, history = await tracker.snapshot()
    running = [job for job in active if job.status == "running"]
    queued = [job for job in active if job.status == "queued"]
    available_slots = getattr(semaphore, "_value", 0)

    lines = [
        "지원자 트래커 작업 현황",
        f"- 진행 중: {len(running)}",
        f"- 대기 중: {len(queued)}",
        f"- 병렬 슬롯: {max_parallel - available_slots}/{max_parallel} 사용 중",
    ]

    if running:
        lines.append("")
        lines.append("진행 중")
        for job in running[:10]:
            lines.append(
                f"- {job.stage} | {elapsed_text(job.started_at)} | "
                f"{format_applicant_summary(job)}"
            )

    if queued:
        lines.append("")
        lines.append("대기 중")
        for job in queued[:10]:
            lines.append(
                f"- {elapsed_text(job.created_at)} 대기 | "
                f"{format_applicant_summary(job)}"
            )

    if history:
        lines.append("")
        lines.append("최근 완료")
        for job in history[:5]:
            total_elapsed = elapsed_text(job.created_at)
            lines.append(f"- {job.status} | {total_elapsed} | {format_applicant_summary(job)}")

    if not active:
        lines.append("- 현재 대기/진행 중인 작업이 없습니다.")

    return "\n".join(lines)


async def attachment_text(message: discord.Message) -> str:
    # Discord 메시지에 첨부된 텍스트 파일 내용을 읽는다.
    #
    # 사용자가 긴 메일 원문을 `.txt` 파일로 올리는 경우를 위한 보조 입력
    # 경로다. 텍스트 파일이 여러 개면 빈 줄 두 개로 이어 붙인다.
    #
    chunks = []
    for attachment in message.attachments:
        filename = attachment.filename.lower()
        content_type = attachment.content_type or ""
        if filename.endswith(".txt") or content_type.startswith("text/"):
            raw = await attachment.read()
            chunks.append(raw.decode("utf-8"))
    return "\n\n".join(chunks).strip()


def clean_message_content(message: discord.Message) -> str:
    # Discord 명령어/봇 멘션을 제거하고 순수 지원자 본문만 남긴다.
    #
    # 예를 들어 `!지원자
    # SM엔터 ...`는 `SM엔터 ...`만 반환한다. 이렇게
    # 분리해두면 뒤쪽 파서가 Discord 명령어 문법을 몰라도 된다.
    #
    content = message.content.strip()

    for mention in [message.guild.me.mention if message.guild and message.guild.me else ""]:
        if mention and content.startswith(mention):
            content = content[len(mention):].strip()

    if content.startswith("!지원자"):
        content = content[len("!지원자"):].strip()
    elif content.startswith("!applicant"):
        content = content[len("!applicant"):].strip()
    elif content.startswith("!업데이트"):
        content = content[len("!업데이트"):].strip()
    elif content.startswith("!update"):
        content = content[len("!update"):].strip()

    return content


def build_reply(response: dict, extraction_method: str = "", elapsed: str = "") -> str:
    # 저장 결과 JSON을 사용자가 읽기 쉬운 Discord 답장으로 변환한다.
    #
    # 실패 시에는 단순히 "실패"만 보내지 않고, 파싱된 값/필수 누락/선택
    # 누락을 분리해 보여준다. 현장에서 어떤 정보가 부족했는지 바로 확인하기
    # 위한 진단 메시지 역할을 한다.
    #
    if response.get("batch"):
        lines = [
            f"지원자 {response.get('stored_count', 0)}/{response.get('total_count', 0)}명 저장 완료",
        ]
        if elapsed:
            lines.append(f"- 처리 시간: {elapsed}")
        for result in response.get("results") or []:
            parsed = result.get("parsed") or {}
            prefix = "완료" if result.get("status") == "ok" else "확인 필요"
            summary = []
            candidate_name = clean_display_value(parsed.get("candidate_name"))
            company_name = clean_display_value(parsed.get("company_name"))
            birth_year = clean_display_value(parsed.get("birth_year"))
            age_international = clean_display_value(parsed.get("age_international"))
            if candidate_name:
                summary.append(f"지원자 {candidate_name}")
            if company_name:
                summary.append(f"회사 {company_name}")
            if birth_year:
                summary.append(f"출생년도 {birth_year}")
            if age_international:
                summary.append(f"만나이 {age_international}")
            lines.append(f"- {prefix}: {' | '.join(summary) if summary else '지원자 정보 확인 중'}")

            missing = missing_info_labels(result.get("diagnostics") or {})
            if missing:
                lines.append(f"  부족 정보: {', '.join(missing)}")

            notion = result.get("notion") or {}
            if result.get("status") == "ok" and notion.get("action") == "error":
                lines.append("  Notion 동기화 실패")
        return "\n".join(lines)

    if response.get("status") != "ok":
        diagnostics = response.get("diagnostics") or {}
        fields = diagnostics.get("fields") or {}
        completed = completed_info_labels(fields)
        missing = missing_info_labels(diagnostics)
        error_code = (response.get("errors") or [{}])[0].get("code", "")
        lines = ["업데이트 실패" if str(error_code).startswith("UPDATE_") else "저장 실패"]
        if elapsed:
            lines.append(f"- 처리 시간: {elapsed}")
        error_message = first_error_message(response)
        if error_message:
            lines.append(f"- 사유: {error_message}")
        if completed:
            lines.append(f"- 완료 정보: {', '.join(completed)}")
        if missing:
            lines.append(f"- 부족 정보: {', '.join(missing)}")
        else:
            lines.append("- 부족 정보: 저장에 필요한 핵심 정보를 확인하지 못했습니다.")
        return "\n".join(lines)

    parsed = response["parsed"]
    diagnostics = response.get("diagnostics") or {}
    notion = response.get("notion") or {}
    notion_text = "Notion 동기화 안 됨"
    if notion.get("synced"):
        notion_text = "Notion 동기화 완료"
    elif notion.get("action") == "error":
        notion_text = "Notion 동기화 실패"

    lines = ["지원자 저장 완료"]
    if elapsed:
        lines.append(f"- 처리 시간: {elapsed}")
    append_field(lines, "회사", parsed.get("company_name"))
    append_field(lines, "지원자", parsed.get("candidate_name"))
    append_field(lines, "출생년도", parsed.get("birth_year"))
    append_field(lines, "만나이", parsed.get("age_international"))
    append_field(lines, "한국나이", parsed.get("age_korean"))
    append_field(lines, "직무", parsed.get("position"))
    append_list_field(lines, "스킬/능력", parsed.get("skills") or [])
    append_field(lines, "비고", parsed.get("notes"))
    append_field(lines, "상태", parsed.get("status"))
    missing = missing_info_labels(diagnostics)
    if missing:
        lines.append(f"- 부족 정보: {', '.join(missing)}")
    lines.append(f"- {notion_text}")
    return "\n".join(lines)


async def extract_message_text(message: discord.Message) -> str:
    # 메시지 본문 또는 `.txt` 첨부파일 중 실제 처리할 텍스트를 고른다.
    text = clean_message_content(message)
    if text:
        return text
    return await attachment_text(message)


async def process_message(
    message: discord.Message,
    sync_notion: bool,
    semaphore: asyncio.Semaphore,
    tracker: JobTracker,
    update_only: bool = False,
) -> None:
    # Discord 메시지 1건을 접수하고 저장 완료 답장까지 처리한다.
    #
    # `semaphore`는 동시에 처리되는 메시지 수를 제한한다. AI 호출이나 Notion
    # API가 느려져도 봇 전체가 무제한 작업을 쌓지 않도록 보호하는 장치다.
    #
    log_id = make_log_id(message.channel.id, message.id)
    text = await extract_message_text(message)

    if not text:
        await message.reply("처리할 메일 본문이 없습니다. 텍스트를 입력하거나 .txt 파일을 첨부해주세요.")
        return

    job = JobRecord(
        log_id=log_id,
        message_id=message.id,
        author_name=getattr(message.author, "display_name", str(message.author)),
        candidate_name="",
        company_name="",
        birth_year=None,
        age_international=None,
        age_korean=None,
        status="queued",
        stage="병렬 슬롯 대기",
        created_at=time.time(),
    )
    apply_job_details(job, initial_job_details(text))
    await tracker.add(job)
    print(f"[queued] {format_applicant_summary(job)}")

    accepted_message = await message.reply(
        "\n".join([
            "지원자 업데이트 접수 완료" if update_only else "지원자 메시지 접수 완료",
            "- AI 추출 및 저장을 시작합니다.",
            "- 진행 현황은 !대기 로 확인할 수 있습니다.",
        ])
    )

    source = f"discord:{message.guild.id if message.guild else 'dm'}:{message.channel.id}:{message.id}"

    try:
        async with semaphore:
            await tracker.mark_running(log_id, "처리 시작")
            print(f"[running] {format_applicant_summary(job)}")

            async def update_progress(stage: str) -> None:
                # message_processor가 알려주는 현재 단계를 작업 현황에 반영한다.
                await tracker.update_stage(log_id, stage)

            response, extraction_method = await process_text(
                text=text,
                source=source,
                sync_notion=sync_notion,
                log_id=log_id,
                progress=update_progress,
                update_only=update_only,
            )

        finish_status = "완료" if response.get("status") == "ok" else "실패"
        if response.get("parsed"):
            await tracker.update_details(log_id, response["parsed"])
            apply_job_details(job, response["parsed"])
        await tracker.finish(log_id, finish_status, finish_status)
        print(f"[{finish_status}] {format_applicant_summary(job)} method={extraction_method}")
        await accepted_message.reply(build_reply(response, extraction_method, elapsed_text(job.created_at)))
    except Exception as exc:
        await tracker.finish(log_id, "오류", str(exc))
        print(f"[error] {format_applicant_summary(job)} error={exc}")
        await accepted_message.reply(f"처리 중 오류가 발생했습니다: {exc}")


def main() -> None:
    # Discord 클라이언트를 생성하고 이벤트 핸들러를 등록한 뒤 봇을 실행한다.
    load_env_file()

    token = required_env("DISCORD_BOT_TOKEN")
    intake_channel_id = int(required_env("DISCORD_INTAKE_CHANNEL_ID"))
    sync_notion = os.getenv("DISCORD_SYNC_NOTION", "1") != "0"
    max_parallel = int(os.getenv("DISCORD_MAX_PARALLEL", "3"))
    semaphore = asyncio.Semaphore(max_parallel)
    tracker = JobTracker()

    intents = discord.Intents.default()
    intents.message_content = True

    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        # Discord Gateway 연결이 완료되었을 때 터미널에 현재 설정을 출력한다.
        print(f"Discord bot ready: {client.user}")
        print(f"bot version: {BOT_VERSION}")
        print(f"intake channel: {intake_channel_id}")
        print(f"sync notion: {sync_notion}")
        print(f"max parallel: {max_parallel}")

    @client.event
    async def on_message(message: discord.Message) -> None:
        # 새 Discord 메시지를 받았을 때 호출되는 메인 이벤트 핸들러.
        #
        # 지정 채널이 아니거나 봇 자신의 메시지면 무시한다. 명령어별로 답장을
        # 분리하고, 명령어가 없는 일반 메시지는 기본 지원자 입력으로 처리한다.
        #
        if message.author.bot:
            return
        if message.channel.id != intake_channel_id:
            return

        try:
            content = message.content.strip()

            if is_exact_command(content, USAGE_COMMANDS):
                await message.reply(build_usage_help())
                return

            if is_exact_command(content, COMMAND_HELP_COMMANDS):
                await message.reply(build_command_help())
                return

            if is_exact_command(content, FEEDBACK_COMMANDS):
                await message.reply(build_feedback_help())
                return

            feedback_text = command_body(content, FEEDBACK_COMMANDS)
            if feedback_text:
                total = append_user_feedback_rule(message, feedback_text)
                await message.reply(
                    "\n".join([
                        "개선 요청을 저장했습니다.",
                        "- 다음 AI 파싱부터 참고합니다.",
                        f"- 누적 피드백: {total}개",
                    ])
                )
                return

            if is_exact_command(content, STATUS_COMMANDS):
                use_ai_extract = os.getenv("DISCORD_USE_AI_EXTRACT", "1") != "0"
                ai_mode = os.getenv("DISCORD_AI_MODE", "auto")
                ai_provider = os.getenv("AI_PROVIDER", "ollama")
                ai_fallback_provider = os.getenv("AI_FALLBACK_PROVIDER", "")
                ollama_timeout = os.getenv("OLLAMA_TIMEOUT", "45")
                ollama_num_parallel = os.getenv("OLLAMA_NUM_PARALLEL", "서버 설정 확인 필요")
                ollama_max_loaded_models = os.getenv("OLLAMA_MAX_LOADED_MODELS", "서버 설정 확인 필요")
                ollama_keep_alive = os.getenv("OLLAMA_KEEP_ALIVE", "서버 설정 확인 필요")
                ollama_flash_attention = os.getenv("OLLAMA_FLASH_ATTENTION", "서버 설정 확인 필요")
                gemini_model = os.getenv("GEMINI_MODEL", "설정 안 됨")
                gemini_timeout = os.getenv("GEMINI_TIMEOUT", "설정 안 됨")
                gemini_configured = bool(os.getenv("GEMINI_API_KEY"))
                cache_ttl = os.getenv("AI_EXTRACT_CACHE_TTL_SECONDS", "3600")
                retry_on_incomplete = os.getenv("DISCORD_AI_RETRY_ON_INCOMPLETE", "1") != "0"
                rule_fallback = os.getenv("DISCORD_RULE_FALLBACK_ON_AI_ERROR", "1") != "0"
                await message.reply(
                    "\n".join([
                        "지원자 트래커 봇 상태",
                        f"- 봇 버전: {BOT_VERSION}",
                        f"- Notion 동기화: {'켜짐' if sync_notion else '꺼짐'}",
                        f"- AI 파싱: {'켜짐' if use_ai_extract else '꺼짐'}",
                        f"- AI 모드: {ai_mode}",
                        f"- 기본 AI: {ai_provider}",
                        f"- 보조 AI: {ai_fallback_provider or '없음'}",
                        f"- 미달 시 재파싱: {'켜짐' if retry_on_incomplete else '꺼짐'}",
                        f"- AI 장애 시 rule fallback: {'켜짐' if rule_fallback else '꺼짐'}",
                        f"- Discord 동시 처리: {max_parallel}개",
                        f"- Ollama 동시 처리 설정: {ollama_num_parallel}",
                        f"- Ollama 동시 모델 설정: {ollama_max_loaded_models}",
                        f"- Ollama keep alive: {ollama_keep_alive}",
                        f"- Ollama flash attention: {ollama_flash_attention}",
                        f"- Gemini 설정: {'켜짐' if gemini_configured else '꺼짐'}",
                        f"- Gemini 모델: {gemini_model}",
                        f"- Gemini 타임아웃: {gemini_timeout}초",
                        f"- AI 타임아웃: {ollama_timeout}초",
                        f"- AI 캐시 유지: {cache_ttl}초",
                    ])
                )
                return

            if is_exact_command(content, QUEUE_COMMANDS):
                await message.reply(await build_queue_reply(tracker, semaphore, max_parallel))
                return

            audit_limit = parse_audit_limit_command(content)
            if audit_limit is not None:
                accepted_message = await message.reply(
                    "\n".join([
                        f"최근 {audit_limit}명 데이터 감사를 시작합니다.",
                        "SQLite, 최근 메시지, AI JSON, Notion 값을 비교합니다.",
                    ])
                )
                try:
                    result = await asyncio.to_thread(
                        run_audit,
                        audit_limit,
                        True,
                        f"recent_{audit_limit}",
                    )
                    await accepted_message.reply(build_discord_audit_reply(result))
                except Exception as exc:
                    await accepted_message.reply(f"감사 실행 실패: {exc}")
                return

            if is_exact_command(content, RECOVERY_COMMANDS):
                accepted_message = await message.reply(
                    "\n".join([
                        "Notion 복구를 시작합니다.",
                        "SQLite에는 남아 있지만 Notion에 없거나 연결되지 않은 지원자를 다시 생성합니다.",
                    ])
                )
                try:
                    result = await asyncio.to_thread(recover_sqlite_to_notion, True)
                    await accepted_message.reply(build_discord_recovery_reply(result))
                except Exception as exc:
                    await accepted_message.reply(f"복구 실행 실패: {exc}")
                return

            if is_exact_command(content, FULL_AUDIT_COMMANDS):
                accepted_message = await message.reply(
                    "\n".join([
                        "전체 데이터 감사를 시작합니다.",
                        "SQLite와 Notion 전체 값을 비교하고 Notion 삭제분을 DB에 반영합니다.",
                    ])
                )
                try:
                    result = await asyncio.to_thread(
                        run_audit,
                        None,
                        True,
                        "all",
                        False,
                        True,
                    )
                    await accepted_message.reply(build_discord_audit_reply(result))
                except Exception as exc:
                    await accepted_message.reply(f"전체 감사 실행 실패: {exc}")
                return

            if has_command_prefix(content, APPLICANT_COMMANDS):
                asyncio.create_task(process_message(message, sync_notion, semaphore, tracker))
                return

            if has_command_prefix(content, UPDATE_COMMANDS):
                asyncio.create_task(
                    process_message(
                        message,
                        sync_notion,
                        semaphore,
                        tracker,
                        update_only=True,
                    )
                )
                return

            if content.startswith("!"):
                await message.reply(build_unknown_command_reply())
                return

            asyncio.create_task(process_message(message, sync_notion, semaphore, tracker))

        except Exception as exc:
            await message.reply(f"처리 중 오류가 발생했습니다: {exc}\n- bot_version: {BOT_VERSION}")

    client.run(token)


if __name__ == "__main__":
    main()
