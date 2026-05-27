#!/usr/bin/env python3
# Microsoft Teams 채널에서 지원자 메시지를 받아 저장 파이프라인으로 넘기는 봇.
#
# Discord 봇은 그대로 유지하고, 이 파일은 Azure Bot Framework HTTP 엔드포인트만
# 담당한다. 실제 파싱/저장 로직은 Discord와 동일하게 `message_processor.process_text()`를
# 호출해서 공유한다.

import asyncio
import datetime
import html
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from aiohttp import web
from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings, TurnContext
from botbuilder.schema import Activity, ActivityTypes

from discord_bot import (
    APPLICANT_COMMANDS,
    AUDIT_COMMANDS,
    COMMAND_HELP_COMMANDS,
    FEEDBACK_COMMANDS,
    FULL_AUDIT_COMMANDS,
    QUEUE_COMMANDS,
    RECOVERY_COMMANDS,
    RESTART_COMMANDS,
    STATUS_COMMANDS,
    UPDATE_COMMANDS,
    USAGE_COMMANDS,
    JobRecord,
    JobTracker,
    append_change_log,
    append_field,
    append_list_field,
    audit_command_summary,
    build_command_help,
    build_discord_audit_reply,
    build_discord_recovery_reply,
    build_feedback_help,
    build_queue_reply,
    build_reply,
    build_unknown_command_reply,
    build_usage_help,
    clean_display_value,
    command_body,
    completed_info_labels,
    elapsed_text,
    first_error_message,
    format_applicant_summary,
    has_command_prefix,
    initial_job_details,
    is_exact_command,
    make_command_job as _discord_make_command_job,
    missing_info_labels,
    parse_audit_limit_command,
    recover_sqlite_to_notion,
    restart_blocking_reply,
    run_audit,
)
from message_processor import make_log_id, process_text
from store import parse_email_text


BASE_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = BASE_DIR / '.env'
BOT_VERSION = '2026-05-27-teams-v1'
USER_FEEDBACK_RULES_PATH = (
    BASE_DIR
    / 'ollama'
    / 'skills'
    / 'applicant-parser'
    / 'user_feedback_rules.md'
)


def load_env_file() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip())


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f'{name}이 .env에 없습니다.')
    return value


def teams_sender_name(activity: Activity) -> str:
    sender = getattr(activity, 'from_property', None)
    if not sender:
        return 'Teams 사용자'
    return sender.name or sender.id or 'Teams 사용자'


def teams_message_id(activity: Activity) -> str:
    return str(activity.id or int(time.time() * 1000))


def teams_conversation_id(activity: Activity) -> str:
    conversation = getattr(activity, 'conversation', None)
    return str(getattr(conversation, 'id', '') or 'teams')


def teams_channel_id(activity: Activity) -> str:
    channel_data = activity.channel_data or {}
    if isinstance(channel_data, dict):
        channel = channel_data.get('channel') or {}
        if isinstance(channel, dict) and channel.get('id'):
            return str(channel.get('id'))
    return teams_conversation_id(activity)


def clean_teams_text(text: str) -> str:
    # Teams는 봇 멘션을 <at>봇이름</at> HTML 형태로 넣는다.
    text = text or ''
    text = re.sub(r'<at>.*?</at>', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()


def strip_applicant_command(content: str) -> str:
    stripped = content.strip()
    for command in [*APPLICANT_COMMANDS, *UPDATE_COMMANDS]:
        if stripped == command:
            return ''
        if stripped.startswith(command + '\n') or stripped.startswith(command + ' '):
            return stripped[len(command):].strip()
    return stripped


def append_user_feedback_rule(author_name: str, feedback: str) -> int:
    USER_FEEDBACK_RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    created_at = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cleaned_feedback = ' '.join(feedback.split())
    existing = USER_FEEDBACK_RULES_PATH.read_text(encoding='utf-8') if USER_FEEDBACK_RULES_PATH.exists() else ''
    entry = f'- {created_at} | Teams/{author_name}: {cleaned_feedback}\n'
    USER_FEEDBACK_RULES_PATH.write_text(existing + entry, encoding='utf-8')
    return len([line for line in (existing + entry).splitlines() if line.strip().startswith('- ')])


def make_job(
    activity: Activity,
    log_id: str,
    stage: str,
    text: str = '',
    command_name: str = '',
) -> JobRecord:
    job = JobRecord(
        log_id=log_id,
        message_id=0,
        author_name=teams_sender_name(activity),
        candidate_name=command_name,
        company_name='명령어' if command_name else '',
        birth_year=None,
        age_international=None,
        age_korean=None,
        status='queued',
        stage=stage,
        created_at=time.time(),
    )
    if text and not command_name:
        try:
            parsed = parse_email_text(text)
            candidate_name = clean_display_value(parsed.get('candidate_name'))
            company_name = clean_display_value(parsed.get('company_name'))
            if candidate_name:
                job.candidate_name = candidate_name
            if company_name:
                job.company_name = company_name
            if parsed.get('birth_year'):
                job.birth_year = parsed.get('birth_year')
            if parsed.get('age_international'):
                job.age_international = parsed.get('age_international')
            if parsed.get('age_korean'):
                job.age_korean = parsed.get('age_korean')
        except Exception:
            pass
    return job


async def send_text(context: TurnContext, text: str) -> None:
    await context.send_activity(text)


async def process_teams_message(
    context: TurnContext,
    activity: Activity,
    text: str,
    sync_notion: bool,
    semaphore: asyncio.Semaphore,
    tracker: JobTracker,
    update_only: bool = False,
) -> None:
    body = strip_applicant_command(text)
    if not body:
        await send_text(context, '처리할 메일 본문이 없습니다. 텍스트를 입력해주세요.')
        return

    log_id = make_log_id(teams_channel_id(activity), teams_message_id(activity))
    job = make_job(activity, log_id, '병렬 슬롯 대기', body)
    await tracker.add(job)
    print(f'[teams-queued] {format_applicant_summary(job)}')

    await send_text(
        context,
        '\n'.join([
            '지원자 업데이트 접수 완료' if update_only else '지원자 메시지 접수 완료',
            '- AI 추출 및 저장을 시작합니다.',
            '- 진행 현황은 !대기 로 확인할 수 있습니다.',
        ]),
    )

    source = f'teams:{teams_conversation_id(activity)}:{teams_message_id(activity)}'
    try:
        async with semaphore:
            await tracker.mark_running(log_id, '처리 시작')
            print(f'[teams-running] {format_applicant_summary(job)}')

            async def update_progress(stage: str) -> None:
                await tracker.update_stage(log_id, stage)

            response, extraction_method = await process_text(
                text=body,
                source=source,
                sync_notion=sync_notion,
                log_id=log_id,
                progress=update_progress,
                update_only=update_only,
            )

        finish_status = '완료' if response.get('status') == 'ok' else '실패'
        if response.get('parsed'):
            await tracker.update_details(log_id, response['parsed'])
        await tracker.finish(log_id, finish_status, finish_status)
        print(f'[teams-{finish_status}] {format_applicant_summary(job)} method={extraction_method}')
        await send_text(context, build_reply(response, extraction_method, elapsed_text(job.created_at)))
    except Exception as exc:
        await tracker.finish(log_id, '오류', str(exc))
        print(f'[teams-error] {format_applicant_summary(job)} error={exc}')
        await send_text(context, f'처리 중 오류가 발생했습니다: {exc}')


async def handle_activity(
    context: TurnContext,
    sync_notion: bool,
    semaphore: asyncio.Semaphore,
    tracker: JobTracker,
    max_parallel: int,
    state: dict[str, bool],
) -> None:
    activity = context.activity
    if activity.type != ActivityTypes.message:
        return

    allowed_channel_id = os.getenv('TEAMS_INTAKE_CHANNEL_ID', '').strip()
    current_channel_id = teams_channel_id(activity)
    if allowed_channel_id and current_channel_id != allowed_channel_id:
        return

    content = clean_teams_text(activity.text or '')
    if not content:
        await send_text(context, '처리할 메시지 본문이 없습니다.')
        return

    if is_exact_command(content, USAGE_COMMANDS):
        await send_text(context, build_usage_help())
        return

    if is_exact_command(content, COMMAND_HELP_COMMANDS):
        await send_text(context, build_command_help())
        return

    if is_exact_command(content, FEEDBACK_COMMANDS):
        await send_text(context, build_feedback_help())
        return

    feedback_text = command_body(content, FEEDBACK_COMMANDS)
    if feedback_text:
        total = append_user_feedback_rule(teams_sender_name(activity), feedback_text)
        await send_text(
            context,
            '\n'.join([
                '개선 요청을 저장했습니다.',
                '- 다음 AI 파싱부터 참고합니다.',
                f'- 누적 피드백: {total}개',
            ]),
        )
        return

    if is_exact_command(content, STATUS_COMMANDS):
        use_ai_extract = os.getenv('DISCORD_USE_AI_EXTRACT', '1') != '0'
        ai_mode = os.getenv('DISCORD_AI_MODE', 'auto')
        ai_provider = os.getenv('AI_PROVIDER', 'ollama')
        ai_fallback_provider = os.getenv('AI_FALLBACK_PROVIDER', '')
        ollama_timeout = os.getenv('OLLAMA_TIMEOUT', '45')
        retry_on_incomplete = os.getenv('DISCORD_AI_RETRY_ON_INCOMPLETE', '1') != '0'
        rule_fallback = os.getenv('DISCORD_RULE_FALLBACK_ON_AI_ERROR', '1') != '0'
        await send_text(
            context,
            '\n'.join([
                '지원자 트래커 Teams 봇 상태',
                f'- 봇 버전: {BOT_VERSION}',
                f'- Notion 동기화: {"켜짐" if sync_notion else "꺼짐"}',
                f'- AI 파싱: {"켜짐" if use_ai_extract else "꺼짐"}',
                f'- AI 모드: {ai_mode}',
                f'- 기본 AI: {ai_provider}',
                f'- 보조 AI: {ai_fallback_provider or "없음"}',
                f'- 미달 시 재파싱: {"켜짐" if retry_on_incomplete else "꺼짐"}',
                f'- AI 장애 시 rule fallback: {"켜짐" if rule_fallback else "꺼짐"}',
                f'- Teams 동시 처리: {max_parallel}개',
                f'- AI 타임아웃: {ollama_timeout}초',
                f'- Teams 채널 ID: {current_channel_id}',
            ]),
        )
        return

    if is_exact_command(content, QUEUE_COMMANDS):
        await send_text(context, await build_queue_reply(tracker, semaphore, max_parallel))
        return

    if is_exact_command(content, RESTART_COMMANDS):
        if state['restart_requested']:
            await send_text(context, '이미 재시작이 예약되어 있습니다. 진행 현황은 !대기 로 확인해주세요.')
            return
        state['restart_requested'] = True
        state['accepting_new_jobs'] = False
        log_id = make_log_id(current_channel_id, teams_message_id(activity))
        job = make_job(activity, log_id, '새 작업 차단', command_name='Teams재시작예약')
        await tracker.add(job)
        await tracker.mark_running(log_id, '진행 중 작업 확인')
        await send_text(
            context,
            '\n'.join([
                'Teams 봇 안전 재시작을 예약했습니다.',
                '- 지금부터 새 지원자/감사/복구 작업은 받지 않습니다.',
                '- 진행 중 작업이 끝나면 봇을 종료합니다.',
                '- launchd 서비스가 등록되어 있으면 자동으로 다시 시작됩니다.',
                '- 진행 현황은 !대기 로 확인할 수 있습니다.',
            ]),
        )
        print(f'[teams-restart] requested by {teams_sender_name(activity)}')

        async def wait_for_safe_restart() -> None:
            while True:
                active, _ = await tracker.snapshot()
                running_others = [item for item in active if item.log_id != log_id]
                if not running_others:
                    break
                await tracker.update_stage(log_id, f'진행 중 작업 {len(running_others)}건 대기')
                await asyncio.sleep(2)
            await tracker.update_stage(log_id, '안전 종료 준비')
            await tracker.finish(log_id, '완료', '재시작을 위한 안전 종료')
            print('[teams-restart] safe shutdown requested')
            asyncio.get_running_loop().stop()

        asyncio.create_task(wait_for_safe_restart())
        return

    if not state['accepting_new_jobs'] and (
        parse_audit_limit_command(content) is not None
        or is_exact_command(content, RECOVERY_COMMANDS)
        or is_exact_command(content, FULL_AUDIT_COMMANDS)
        or has_command_prefix(content, APPLICANT_COMMANDS)
        or has_command_prefix(content, UPDATE_COMMANDS)
        or not content.startswith('!')
    ):
        await send_text(context, restart_blocking_reply())
        return

    audit_limit = parse_audit_limit_command(content)
    if audit_limit is not None:
        log_id = make_log_id(current_channel_id, teams_message_id(activity))
        job = make_job(activity, log_id, '감사 대기', command_name=f'Teams감사 {audit_limit}')
        await tracker.add(job)
        await send_text(
            context,
            '\n'.join([
                f'최근 {audit_limit}명 데이터 감사를 시작합니다.',
                'SQLite, 최근 메시지, AI JSON, Notion 값을 비교합니다.',
                '- 진행 현황은 !대기 로 확인할 수 있습니다.',
            ]),
        )
        try:
            await tracker.mark_running(log_id, 'SQLite/Notion 감사 실행')
            result = await asyncio.to_thread(run_audit, audit_limit, True, f'teams_recent_{audit_limit}')
            print(f'[teams-command-completed] {format_applicant_summary(job)} {audit_command_summary(result)}')
            await tracker.finish(log_id, '완료', f'감사 완료: 불일치 {result.get("diff_count", 0)}건')
            await send_text(context, build_discord_audit_reply(result))
        except Exception as exc:
            await tracker.finish(log_id, '오류', str(exc))
            print(f'[teams-command-error] {format_applicant_summary(job)} error={exc}')
            await send_text(context, f'감사 실행 실패: {exc}')
        return

    if is_exact_command(content, RECOVERY_COMMANDS):
        log_id = make_log_id(current_channel_id, teams_message_id(activity))
        job = make_job(activity, log_id, '복구 대기', command_name='Teams복구')
        await tracker.add(job)
        await send_text(
            context,
            '\n'.join([
                'Notion 복구를 시작합니다.',
                'SQLite에는 남아 있지만 Notion에 없거나 연결되지 않은 지원자를 다시 생성합니다.',
                '- 진행 현황은 !대기 로 확인할 수 있습니다.',
            ]),
        )
        try:
            await tracker.mark_running(log_id, 'Notion 복구 실행')
            result = await asyncio.to_thread(recover_sqlite_to_notion, True)
            print(f'[teams-command-completed] {format_applicant_summary(job)} recovered={result.get("recovered_count")} skipped={result.get("skipped_count")}')
            await tracker.finish(log_id, '완료', f'복구 완료: {result.get("recovered_count", 0)}명')
            await send_text(context, build_discord_recovery_reply(result))
        except Exception as exc:
            await tracker.finish(log_id, '오류', str(exc))
            print(f'[teams-command-error] {format_applicant_summary(job)} error={exc}')
            await send_text(context, f'복구 실행 실패: {exc}')
        return

    if is_exact_command(content, FULL_AUDIT_COMMANDS):
        log_id = make_log_id(current_channel_id, teams_message_id(activity))
        job = make_job(activity, log_id, '전체감사 대기', command_name='Teams전체감사')
        await tracker.add(job)
        await send_text(
            context,
            '\n'.join([
                '전체 데이터 감사를 시작합니다.',
                'SQLite와 Notion 전체 값을 비교하고 Notion 삭제분을 DB에 반영합니다.',
                '- 진행 현황은 !대기 로 확인할 수 있습니다.',
            ]),
        )
        try:
            await tracker.mark_running(log_id, '전체 SQLite/Notion 감사 실행')
            result = await asyncio.to_thread(run_audit, None, True, 'teams_all', False, True)
            print(f'[teams-command-completed] {format_applicant_summary(job)} {audit_command_summary(result)}')
            await tracker.finish(log_id, '완료', f'전체감사 완료: 불일치 {result.get("diff_count", 0)}건')
            await send_text(context, build_discord_audit_reply(result))
        except Exception as exc:
            await tracker.finish(log_id, '오류', str(exc))
            print(f'[teams-command-error] {format_applicant_summary(job)} error={exc}')
            await send_text(context, f'전체 감사 실행 실패: {exc}')
        return

    if has_command_prefix(content, APPLICANT_COMMANDS):
        asyncio.create_task(process_teams_message(context, activity, content, sync_notion, semaphore, tracker))
        return

    if has_command_prefix(content, UPDATE_COMMANDS):
        asyncio.create_task(process_teams_message(context, activity, content, sync_notion, semaphore, tracker, update_only=True))
        return

    if content.startswith('!'):
        await send_text(context, build_unknown_command_reply())
        return

    asyncio.create_task(process_teams_message(context, activity, content, sync_notion, semaphore, tracker))


async def messages(request: web.Request) -> web.Response:
    adapter: BotFrameworkAdapter = request.app['adapter']
    activity = Activity().deserialize(await request.json())
    auth_header = request.headers.get('Authorization', '')

    async def aux_func(turn_context: TurnContext) -> None:
        await handle_activity(
            turn_context,
            request.app['sync_notion'],
            request.app['semaphore'],
            request.app['tracker'],
            request.app['max_parallel'],
            request.app['state'],
        )

    invoke_response = await adapter.process_activity(activity, auth_header, aux_func)
    if invoke_response:
        return web.json_response(data=invoke_response.body, status=invoke_response.status)
    return web.Response(status=201)


async def health(_: web.Request) -> web.Response:
    return web.json_response({'status': 'ok', 'bot': 'teams', 'version': BOT_VERSION})


def create_app() -> web.Application:
    load_env_file()
    app_id = required_env('TEAMS_APP_ID')
    app_password = required_env('TEAMS_APP_PASSWORD')
    port = int(os.getenv('TEAMS_BOT_PORT', '3978'))
    sync_notion = os.getenv('TEAMS_SYNC_NOTION', os.getenv('DISCORD_SYNC_NOTION', '1')) != '0'
    max_parallel = int(os.getenv('TEAMS_MAX_PARALLEL', os.getenv('DISCORD_MAX_PARALLEL', '3')))

    settings = BotFrameworkAdapterSettings(app_id, app_password)
    adapter = BotFrameworkAdapter(settings)

    async def on_error(context: TurnContext, error: Exception) -> None:
        print(f'[teams-on-error] {error}')
        await context.send_activity(f'처리 중 오류가 발생했습니다: {error}')

    adapter.on_turn_error = on_error

    app = web.Application()
    app['adapter'] = adapter
    app['sync_notion'] = sync_notion
    app['max_parallel'] = max_parallel
    app['semaphore'] = asyncio.Semaphore(max_parallel)
    app['tracker'] = JobTracker()
    app['state'] = {
        'restart_requested': False,
        'accepting_new_jobs': True,
    }
    app.router.add_post('/api/messages', messages)
    app.router.add_get('/health', health)

    print('Teams bot ready')
    print(f'bot version: {BOT_VERSION}')
    print(f'port: {port}')
    print(f'sync notion: {sync_notion}')
    print(f'max parallel: {max_parallel}')
    print(f'public endpoint: {os.getenv("TEAMS_PUBLIC_ENDPOINT", "") or "설정 안 됨"}')
    return app


def main() -> None:
    load_env_file()
    port = int(os.getenv('TEAMS_BOT_PORT', '3978'))
    web.run_app(create_app(), host='127.0.0.1', port=port)


if __name__ == '__main__':
    main()
