#!/usr/bin/env python3
# macOS launchd에 지원자 감사 스케줄을 등록하는 도구.
#
# 등록되는 작업:
# - 매일 12:00: 최근 10명 감사 (`notion_check.py --audit --limit 10`)
# - 매일 00:00: 전체 DB/Notion 감사 및 Notion 삭제 반영 (`notion_check.py --audit --all`)
# - 매일 00:10: reports 폴더 용도별 최근 40개 초과 파일 정리

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
REPORT_DIR = BASE_DIR / "reports" / "audit"
VENV_PYTHON = BASE_DIR / ".venv" / "bin" / "python"
PYTHON = VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable)


def plist_payload(label: str, script_name: str, hour: int, minute: int, arguments: list[str]) -> dict:
    # launchd plist payload를 만든다.
    return {
        "Label": label,
        "ProgramArguments": [str(PYTHON), str(BASE_DIR / "scripts" / script_name), *arguments],
        "WorkingDirectory": str(BASE_DIR),
        "StartCalendarInterval": {
            "Hour": hour,
            "Minute": minute,
        },
        "StandardOutPath": str(REPORT_DIR / f"{label}.out.log"),
        "StandardErrorPath": str(REPORT_DIR / f"{label}.err.log"),
        "RunAtLoad": False,
    }


def write_plist(path: Path, payload: dict) -> None:
    # plist 파일을 바이너리 plist 형식으로 저장한다.
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as file:
        plistlib.dump(payload, file)


def launchctl_bootstrap(path: Path) -> None:
    # 현재 사용자 GUI 세션에 launchd 작업을 로드한다.
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(path)],
        check=True,
    )


def install(load: bool) -> list[Path]:
    # 감사/리포트 정리 스케줄 plist를 설치하고 필요하면 launchctl에 로드한다.
    jobs = [
        (
            "com.applicant-tracker.audit.recent10",
            "notion_check.py",
            12,
            0,
            ["--audit", "--limit", "10"],
        ),
        (
            "com.applicant-tracker.audit.all",
            "notion_check.py",
            0,
            0,
            ["--audit", "--all"],
        ),
        (
            "com.applicant-tracker.reports.cleanup",
            "cleanup_reports.py",
            0,
            10,
            ["--apply", "--keep", "40"],
        ),
    ]
    paths = []
    for label, script_name, hour, minute, args in jobs:
        path = LAUNCH_AGENTS_DIR / f"{label}.plist"
        write_plist(path, plist_payload(label, script_name, hour, minute, args))
        if load:
            launchctl_bootstrap(path)
        paths.append(path)
    return paths


def main() -> None:
    # CLI 옵션을 읽고 launchd 스케줄을 설치한다.
    parser = argparse.ArgumentParser(description="지원자 감사 launchd 스케줄 설치")
    parser.add_argument("--load", action="store_true", help="plist 생성 후 launchctl에 즉시 로드합니다.")
    args = parser.parse_args()

    paths = install(args.load)
    print("감사 스케줄 설치 완료")
    for path in paths:
        print(f"- {path}")
    print("12:00: 최근 10명 감사")
    print("00:00: 전체 DB/Notion 감사 및 Notion 삭제 반영")
    print("00:10: reports 용도별 최근 40개 초과 파일 정리")
    print("로드 상태:", "launchctl 등록 완료" if args.load else "plist 생성만 완료")


if __name__ == "__main__":
    main()
