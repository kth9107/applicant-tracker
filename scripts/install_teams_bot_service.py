#!/usr/bin/env python3
# macOS launchd에 Microsoft Teams 지원자 봇을 상시 실행 서비스로 등록한다.

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
LAUNCH_AGENTS_DIR = Path.home() / 'Library' / 'LaunchAgents'
REPORT_DIR = BASE_DIR / 'reports' / 'teams_bot'
LABEL = 'com.applicant-tracker.teams-bot'
VENV_PYTHON = BASE_DIR / '.venv' / 'bin' / 'python'
PYTHON = VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable)
PLIST_PATH = LAUNCH_AGENTS_DIR / f'{LABEL}.plist'


def plist_payload() -> dict:
    return {
        'Label': LABEL,
        'ProgramArguments': [str(PYTHON), str(BASE_DIR / 'scripts' / 'teams_bot.py')],
        'WorkingDirectory': str(BASE_DIR),
        'RunAtLoad': True,
        'KeepAlive': True,
        'StandardOutPath': str(REPORT_DIR / 'teams_bot.out.log'),
        'StandardErrorPath': str(REPORT_DIR / 'teams_bot.err.log'),
        'EnvironmentVariables': {
            'PYTHONUNBUFFERED': '1',
        },
    }


def write_plist() -> None:
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    with PLIST_PATH.open('wb') as file:
        plistlib.dump(plist_payload(), file)


def launchctl_bootout() -> None:
    uid = os.getuid()
    subprocess.run(
        ['launchctl', 'bootout', f'gui/{uid}', str(PLIST_PATH)],
        check=False,
        capture_output=True,
        text=True,
    )


def launchctl_bootstrap() -> None:
    uid = os.getuid()
    subprocess.run(['launchctl', 'bootstrap', f'gui/{uid}', str(PLIST_PATH)], check=True)


def launchctl_kickstart() -> None:
    uid = os.getuid()
    subprocess.run(['launchctl', 'kickstart', '-k', f'gui/{uid}/{LABEL}'], check=False)


def install(load: bool) -> None:
    write_plist()
    if load:
        launchctl_bootout()
        launchctl_bootstrap()
        launchctl_kickstart()


def unload() -> None:
    launchctl_bootout()


def main() -> None:
    parser = argparse.ArgumentParser(description='Microsoft Teams 지원자 봇 launchd 서비스 설치')
    parser.add_argument('--load', action='store_true', help='plist 생성 후 launchctl에 즉시 등록/시작합니다.')
    parser.add_argument('--unload', action='store_true', help='등록된 launchd 서비스를 중지/해제합니다.')
    args = parser.parse_args()

    if args.unload:
        unload()
        print(f'Teams 봇 서비스 해제 완료: {PLIST_PATH}')
        return

    install(args.load)
    print('Teams 봇 서비스 설치 완료')
    print(f'- plist: {PLIST_PATH}')
    print(f'- stdout: {REPORT_DIR / "teams_bot.out.log"}')
    print(f'- stderr: {REPORT_DIR / "teams_bot.err.log"}')
    print('- 자동 재시작: KeepAlive=True')
    print('로드 상태:', 'launchctl 등록/시작 완료' if args.load else 'plist 생성만 완료')


if __name__ == '__main__':
    main()
