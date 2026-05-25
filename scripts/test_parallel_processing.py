# Discord 처리 코어가 병렬 메시지를 안정적으로 처리하는지 확인하는 테스트.
#
# 실제 Discord Gateway를 띄우지 않고 `message_processor.process_text()`를 직접
# 동시에 호출한다. 병렬 처리, semaphore 제한, rule-only/AI 처리 경로의 속도를 확인할 때
# 사용한다.

import asyncio
import time
from pathlib import Path

from message_processor import process_text


BASE_DIR = Path(__file__).resolve().parents[1]
TEST_DATA_DIR = BASE_DIR / "scripts" / "test_data"


async def run_case(index: int, sample_file: Path, semaphore: asyncio.Semaphore) -> dict:
    # 샘플 파일 1개를 비동기 처리하고 요약 결과와 소요 시간을 반환한다.
    text = sample_file.read_text(encoding="utf-8")
    source = f"parallel-test:{index}:{sample_file.name}"
    log_id = f"parallel_test_{index}_{sample_file.stem}"

    started_at = time.perf_counter()
    async with semaphore:
        response, extraction_method = await process_text(
            text=text,
            source=source,
            sync_notion=False,
            log_id=log_id,
        )
    elapsed = time.perf_counter() - started_at

    parsed = response.get("parsed") or {}
    return {
        "index": index,
        "file": str(sample_file),
        "status": response.get("status"),
        "candidate_name": parsed.get("candidate_name"),
        "company_name": parsed.get("company_name"),
        "extraction_method": extraction_method,
        "elapsed": round(elapsed, 3),
    }


async def main() -> None:
    # 여러 샘플을 동시에 실행하고 전체 실패 수와 소요 시간을 출력한다.
    sample_files = [
        TEST_DATA_DIR / "sample_discord_kim_taehyun.txt",
        TEST_DATA_DIR / "sample_email_10_01.txt",
        TEST_DATA_DIR / "sample_email_10_02.txt",
        TEST_DATA_DIR / "sample_email_10_03.txt",
        TEST_DATA_DIR / "sample_email_10_04.txt",
    ]
    sample_files = [path for path in sample_files if path.exists()]

    semaphore = asyncio.Semaphore(3)
    started_at = time.perf_counter()
    results = await asyncio.gather(
        *[
            run_case(index, sample_file, semaphore)
            for index, sample_file in enumerate(sample_files, start=1)
        ]
    )
    total_elapsed = time.perf_counter() - started_at

    for result in results:
        print(result)
    print(f"total_elapsed={total_elapsed:.3f}")
    print(f"total={len(results)} failures={len([item for item in results if item['status'] != 'ok'])}")


if __name__ == "__main__":
    asyncio.run(main())
