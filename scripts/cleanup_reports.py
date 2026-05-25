#!/usr/bin/env python3
# reports 폴더의 오래된 실행/디버그 파일을 용도별로 최근 N개만 남긴다.
#
# 기본 실행은 dry-run이라 실제 삭제하지 않는다.
# launchd 스케줄러에서는 `--apply --keep 40`으로 실행해 40개 초과 파일을 정리한다.

import argparse
import datetime
import json
import os
import shutil
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
REPORTS_DIR = BASE_DIR / "reports"
AUDIT_DIR = REPORTS_DIR / "audit"
DEFAULT_KEEP = int(os.getenv("REPORT_CLEANUP_KEEP", "40"))
PRESERVED_FILENAMES = {
    "latest_harness_report.md",
    "latest_audit_report.md",
    "feedback_suggestions.md",
    "report_cleanup_latest.json",
}


def path_mtime(path: Path) -> float:
    # 파일/디렉터리 수정 시각을 반환하고, 사라진 항목은 가장 오래된 것으로 취급한다.
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return 0.0


def collect_items(base_dir: Path, pattern: str, include_dirs: bool = False) -> list[Path]:
    # 특정 폴더에서 정리 대상 파일 또는 디렉터리를 모은다.
    if not base_dir.exists():
        return []
    items = []
    for path in base_dir.glob(pattern):
        if path.name in PRESERVED_FILENAMES:
            continue
        if include_dirs and path.is_dir():
            items.append(path)
        elif not include_dirs and path.is_file():
            items.append(path)
    return sorted(items, key=path_mtime, reverse=True)


def report_groups() -> list[dict[str, Any]]:
    # reports 내부 산출물을 용도별 그룹으로 나눈다.
    return [
        {
            "name": "harness_markdown",
            "description": "하네스 실행 Markdown 리포트",
            "items": collect_items(REPORTS_DIR, "run_*.md"),
        },
        {
            "name": "harness_run_dirs",
            "description": "하네스 실행별 JSON 디렉터리",
            "items": collect_items(REPORTS_DIR, "run_*", include_dirs=True),
        },
        {
            "name": "debug_json",
            "description": "루트 debug JSON 파일",
            "items": collect_items(REPORTS_DIR, "debug_*.json"),
        },
        {
            "name": "one_by_one",
            "description": "샘플 단건 실행 결과",
            "items": collect_items(REPORTS_DIR / "one_by_one", "*"),
        },
        {
            "name": "discord_messages",
            "description": "Discord/AI 파이프라인 중간 파일",
            "items": collect_items(REPORTS_DIR / "discord_messages", "*"),
        },
        {
            "name": "audit_json",
            "description": "감사 JSON 리포트",
            "items": collect_items(AUDIT_DIR, "run_*.json"),
        },
        {
            "name": "audit_markdown",
            "description": "감사 Markdown 리포트",
            "items": collect_items(AUDIT_DIR, "run_*.md"),
        },
        {
            "name": "audit_logs",
            "description": "launchd 감사/정리 로그",
            "items": collect_items(AUDIT_DIR, "*.log"),
        },
    ]


def remove_path(path: Path) -> None:
    # 파일과 디렉터리를 구분해 삭제한다.
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def cleanup_reports(keep: int = DEFAULT_KEEP, apply: bool = False) -> dict[str, Any]:
    # 용도별로 최근 keep개를 남기고 오래된 항목을 삭제하거나 dry-run 결과만 만든다.
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    summary = {
        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "keep": keep,
        "applied": apply,
        "groups": [],
        "removed_total": 0,
        "candidate_total": 0,
    }

    for group in report_groups():
        items = group["items"]
        removable = items[keep:]
        removed = []
        for path in removable:
            removed.append(str(path))
            if apply:
                remove_path(path)

        group_summary = {
            "name": group["name"],
            "description": group["description"],
            "total_before": len(items),
            "keep": keep,
            "remove_count": len(removable),
            "removed": removed,
        }
        summary["groups"].append(group_summary)
        summary["removed_total"] += len(removable) if apply else 0
        summary["candidate_total"] += len(removable)

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    (AUDIT_DIR / "report_cleanup_latest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def print_summary(summary: dict[str, Any]) -> None:
    # CLI에서 확인하기 쉽게 그룹별 정리 결과를 출력한다.
    mode = "삭제 실행" if summary["applied"] else "미리보기"
    print(f"리포트 정리 {mode}")
    print(f"기준: 용도별 최근 {summary['keep']}개 유지")
    print(f"삭제 후보: {summary['candidate_total']}개")
    if summary["applied"]:
        print(f"삭제 완료: {summary['removed_total']}개")
    for group in summary["groups"]:
        print(
            f"- {group['name']}: 전체 {group['total_before']}개, "
            f"삭제 후보 {group['remove_count']}개"
        )


def main() -> None:
    # CLI 옵션을 읽고 reports 정리를 실행한다.
    parser = argparse.ArgumentParser(description="reports 폴더 정리")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="용도별로 남길 최근 항목 수")
    parser.add_argument("--apply", action="store_true", help="실제로 오래된 파일을 삭제합니다.")
    args = parser.parse_args()

    summary = cleanup_reports(keep=args.keep, apply=args.apply)
    print_summary(summary)


if __name__ == "__main__":
    main()
