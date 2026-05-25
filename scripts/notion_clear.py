#!/usr/bin/env python3
# Notion 지원자 데이터베이스의 row(page)를 전체 보관 처리하는 초기화 도구.
#
# Notion API는 데이터베이스 row를 완전 삭제하는 대신 `archived=true`로 숨긴다.
# 이 스크립트는 데이터베이스 속성/컬럼은 유지하고 안의 지원자 데이터만 비운다.

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


BASE_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = BASE_DIR / ".env"
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def load_env_file() -> None:
    # 프로젝트 `.env` 값을 환경변수로 로드한다.
    if not ENV_PATH.exists():
        return

    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def notion_request(method: str, path: str, payload: Optional[dict] = None) -> dict:
    # Notion REST API를 호출하고 JSON 응답을 반환한다.
    token = os.getenv("NOTION_TOKEN")
    if not token:
        raise RuntimeError("NOTION_TOKEN이 .env에 없습니다.")

    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

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


def list_active_pages(database_id: str) -> list[dict]:
    # Notion DB의 보관되지 않은 page를 모두 조회한다.
    pages = []
    payload: dict = {"page_size": 100}

    while True:
        data = notion_request("POST", f"databases/{database_id}/query", payload)
        pages.extend(data.get("results") or [])
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data.get("next_cursor")

    return pages


def archive_pages(pages: list[dict]) -> int:
    # 조회된 page들을 archive 처리하고 처리 개수를 반환한다.
    count = 0
    for page in pages:
        page_id = page.get("id")
        if not page_id:
            continue
        notion_request("PATCH", f"pages/{page_id}", {"archived": True})
        count += 1
    return count


def main() -> None:
    # CLI 옵션을 읽고 Notion DB row 초기화를 실행한다.
    load_env_file()
    parser = argparse.ArgumentParser(description="Notion 지원자 DB row 전체 초기화")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="실제로 Notion page를 archive 처리합니다. 없으면 dry-run만 합니다.",
    )
    args = parser.parse_args()

    database_id = os.getenv("NOTION_DB_ID")
    if not database_id:
        raise RuntimeError("NOTION_DB_ID가 .env에 없습니다.")

    pages = list_active_pages(database_id)
    print(f"Notion DB: {database_id}")
    print(f"활성 row: {len(pages)}건")

    if not args.apply:
        print("dry-run입니다. 실제 초기화는 --apply를 붙여 실행하세요.")
        return

    archived_count = archive_pages(pages)
    print(f"보관 처리 완료: {archived_count}건")


if __name__ == "__main__":
    main()
