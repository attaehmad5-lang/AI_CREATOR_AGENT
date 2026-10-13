"""Run the discovered unittest suite in sequential, reviewable batches.

Every batch contains at most 10 individual tests. All batches are attempted even if
one fails, so CI reports the complete failure set; the overall process exits nonzero
if any batch fails.
"""
from __future__ import annotations

import sys
import unittest
from collections.abc import Iterator
from pathlib import Path


# Executing this file sets sys.path[0] to tests/, so explicitly add the repository
# root before unittest imports test modules that use the "projects" package.
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


BATCH_SIZE = 10


def iter_cases(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_cases(item)
        else:
            yield item


def main() -> int:
    discovered = unittest.defaultTestLoader.discover("tests", pattern="test_*.py")
    cases = list(iter_cases(discovered))
    if not cases:
        print("ERROR: unittest discovery found no tests.")
        return 2

    failures = 0
    errors = 0
    total = len(cases)
    batches = (total + BATCH_SIZE - 1) // BATCH_SIZE
    print(f"Discovered {total} tests; running {batches} sequential batches of at most {BATCH_SIZE}.", flush=True)

    for start in range(0, total, BATCH_SIZE):
        batch_number = start // BATCH_SIZE + 1
        batch_cases = cases[start:start + BATCH_SIZE]
        print(
            f"\n===== BATCH {batch_number}/{batches}: tests {start + 1}-{start + len(batch_cases)} of {total} =====",
            flush=True,
        )
        result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(batch_cases))
        failures += len(result.failures)
        errors += len(result.errors)
        print(
            f"===== BATCH {batch_number}/{batches} RESULT: "
            f"{'PASS' if result.wasSuccessful() else 'FAIL'}; "
            f"run={result.testsRun}, failures={len(result.failures)}, errors={len(result.errors)} =====",
            flush=True,
        )

    print(
        f"\nFINAL RESULT: tests={total}, batches={batches}, "
        f"failures={failures}, errors={errors}, "
        f"status={'PASS' if failures == 0 and errors == 0 else 'FAIL'}",
        flush=True,
    )
    return 0 if failures == 0 and errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
