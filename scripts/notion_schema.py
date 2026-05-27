#!/usr/bin/env python3
# Notion 지원자 데이터베이스의 컬럼을 프로젝트 고정 컬럼 기준으로 맞추는 도구.
#
# 이 스크립트는 row 데이터는 건드리지 않고 데이터베이스 속성만 정리한다.
# Notion API는 기존 컬럼 삭제를 안정적으로 지원하지 않으므로, 필요한 고정 컬럼을
# 생성하고 가능한 경우 기존 컬럼명을 새 이름으로 바꾼다. 이전 컬럼이 남더라도
# 프로그램은 이를 동적 컬럼으로 취급하지 않는다.

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional


BASE_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = BASE_DIR / ".env"
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
FIXED_PROPERTIES = {
    "기업명": {"rich_text": {}},
    "포지션": {"rich_text": {}},
    "회사담당자": {"rich_text": {}},
    "생년": {"number": {}},
    "나이": {"number": {}},
    "희망연봉": {"rich_text": {}},
    "최종연봉": {"rich_text": {}},
    "기타": {"rich_text": {}},
}
RENAME_CANDIDATES = {
    "지원회사": "기업명",
    "지원직무": "포지션",
    "출생년도": "생년",
    "만나이": "나이",
    "비고": "기타",
}


def load_env_file() -> None:
    # `.env` 값을 환경변수에 올린다.
    if not ENV_PATH.exists():
        return

    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def notion_request(method: str, path: str, payload: Optional[dict] = None) -> dict:
    # Notion REST API를 호출한다.
    token = os.getenv("NOTION_TOKEN")
    if not token:
        raise RuntimeError("NOTION_TOKEN이 .env에 없습니다.")

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{NOTION_API_BASE}/{path.lstrip('/')}",
        data=data,
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


def fetch_database(database_id: str) -> dict[str, Any]:
    # Notion DB 메타데이터를 조회한다.
    return notion_request("GET", f"databases/{database_id}")


def current_properties(database_id: str) -> dict[str, Any]:
    # 현재 Notion DB 속성 dict를 반환한다.
    return fetch_database(database_id).get("properties", {})


def find_title_property(properties: dict[str, Any]) -> str:
    # 데이터베이스의 title 컬럼명을 찾는다.
    for name, prop in properties.items():
        if prop.get("type") == "title":
            return name
    return ""


def patch_database_properties(database_id: str, properties: dict[str, Any]) -> None:
    # Notion DB 속성을 PATCH한다.
    notion_request("PATCH", f"databases/{database_id}", {"properties": properties})


def rename_property(database_id: str, old_name: str, new_name: str) -> bool:
    # 기존 속성명을 새 이름으로 바꾼다. 실패하면 False를 반환해 생성 방식으로 fallback한다.
    try:
        patch_database_properties(database_id, {old_name: {"name": new_name}})
        return True
    except Exception:
        return False


def ensure_fixed_schema(database_id: str, apply: bool) -> list[str]:
    # 고정 컬럼 존재를 보장하고 변경 로그를 반환한다.
    logs = []
    properties = current_properties(database_id)

    title_name = find_title_property(properties)
    if title_name and title_name != "이름":
        logs.append(f"title 컬럼명 변경: {title_name} -> 이름")
        if apply:
            rename_property(database_id, title_name, "이름")
            properties = current_properties(database_id)
    elif not title_name:
        logs.append("경고: title 컬럼을 찾지 못했습니다. Notion에서 이름 title 컬럼을 확인해주세요.")

    for old_name, new_name in RENAME_CANDIDATES.items():
        if new_name in properties or old_name not in properties:
            continue
        logs.append(f"컬럼명 변경: {old_name} -> {new_name}")
        if apply and rename_property(database_id, old_name, new_name):
            properties = current_properties(database_id)

    missing = {
        name: schema
        for name, schema in FIXED_PROPERTIES.items()
        if name not in properties
    }
    if missing:
        logs.append(f"누락 고정 컬럼 생성: {', '.join(missing)}")
        if apply:
            patch_database_properties(database_id, missing)
            properties = current_properties(database_id)
    elif apply:
        properties = current_properties(database_id)

    fixed_names = {"이름", *FIXED_PROPERTIES}
    remaining_old = [name for name in RENAME_CANDIDATES if name in properties]
    if remaining_old:
        logs.append(
            "이전 컬럼이 남아있습니다. Notion API로 삭제하지 않고 프로그램에서 무시합니다: "
            + ", ".join(remaining_old)
        )

    current_fixed = [name for name in ["기업명", "포지션", "회사담당자", "이름", "생년", "나이", "희망연봉", "최종연봉", "기타"] if name in properties or name == "이름"]
    logs.append(f"적용 기준 고정 컬럼: {', '.join(current_fixed)}")

    dynamic = [
        name
        for name in properties
        if name not in fixed_names and name not in RENAME_CANDIDATES
    ]
    if dynamic:
        logs.append(f"현재 동적 후보 컬럼: {', '.join(dynamic)}")
    return logs


def main() -> None:
    # CLI 옵션을 읽고 Notion DB 스키마 점검/적용을 실행한다.
    load_env_file()
    parser = argparse.ArgumentParser(description="Notion DB 고정 컬럼 스키마 적용")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="실제로 Notion DB 컬럼을 생성/이름 변경합니다. 없으면 dry-run입니다.",
    )
    args = parser.parse_args()

    database_id = os.getenv("NOTION_DB_ID")
    if not database_id:
        raise RuntimeError("NOTION_DB_ID가 .env에 없습니다.")

    print(f"Notion DB: {database_id}")
    print("mode:", "apply" if args.apply else "dry-run")
    for log in ensure_fixed_schema(database_id, args.apply):
        print("-", log)


if __name__ == "__main__":
    main()
