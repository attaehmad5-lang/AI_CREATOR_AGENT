"""Run unittest cases in bounded, parallel batches for faster CI feedback.

Each batch contains at most 20 test cases. Up to four batches run concurrently,
so a CI worker executes at most 80 test cases concurrently across subprocesses.
"""
from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import unittest


BATCH_SIZE = 20
MAX_PARALLEL_BATCHES = 4


def _flatten(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _run_batch(number: int, tests: list[str], env: dict[str, str]) -> tuple[int, int, str]:
    command = [sys.executable, "-m", "unittest", "-v", *tests]
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = f"\n===== TEST BATCH {number}: {len(tests)} cases; exit={result.returncode} =====\n"
    output += result.stdout
    return number, result.returncode, output


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    discovered = unittest.defaultTestLoader.discover(
        start_dir=str(root / "tests"),
        pattern="test_*.py",
        top_level_dir=str(root / "tests"),
    )
    test_ids = [test.id() for test in _flatten(discovered)]
    if not test_ids:
        print("ERROR: no tests were discovered")
        return 2

    batches = [
        test_ids[start : start + BATCH_SIZE]
        for start in range(0, len(test_ids), BATCH_SIZE)
    ]
    env = os.environ.copy()
    test_path = str(root / "tests")
    env["PYTHONPATH"] = test_path + os.pathsep + env.get("PYTHONPATH", "")
    print(
        f"Discovered {len(test_ids)} tests; running {len(batches)} batches "
        f"of at most {BATCH_SIZE}, up to {MAX_PARALLEL_BATCHES} batches concurrently."
    )

    failed = 0
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_BATCHES) as pool:
        futures = {
            pool.submit(_run_batch, index, batch, env): index
            for index, batch in enumerate(batches, start=1)
        }
        outputs = []
        for future in as_completed(futures):
            number, code, output = future.result()
            outputs.append((number, code, output))
            if code:
                failed += 1
    for _, _, output in sorted(outputs):
        print(output, flush=True)

    if failed:
        print(f"FAILED: {failed} of {len(batches)} batches failed.")
        return 1
    print(f"SUCCESS: all {len(test_ids)} tests passed in {len(batches)} batches.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
