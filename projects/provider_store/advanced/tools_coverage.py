"""Stdlib-only coverage gate for the committed Advanced Provider Store core."""
from __future__ import annotations

import os
import sys
import trace
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PROJECT_ROOT.parents[2]
sys.path.insert(0, str(REPO_ROOT))
PACKAGE_DIR = PROJECT_ROOT / "advps"
TEST_DIR = REPO_ROOT / "tests"


def run_tests():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.discover(str(TEST_DIR), pattern="test_advps_core.py"))
    suite.addTests(loader.discover(str(TEST_DIR), pattern="test_advanced_store_bridge.py"))
    suite.addTests(loader.discover(str(TEST_DIR), pattern="test_provider_store_compat.py"))
    suite.addTests(loader.discover(str(TEST_DIR), pattern="test_provider_service.py"))
    return unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, "w")).run(suite)


def main() -> int:
    minimum = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
    tracer = trace.Trace(count=True, trace=False, ignoredirs=[sys.prefix, sys.exec_prefix])
    result = tracer.runfunc(run_tests)
    counts = tracer.results().counts
    total_exec = total_hit = 0
    for path in sorted(PACKAGE_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8").splitlines()
        executable = trace._find_executable_linenos(str(path))
        executable = {
            line for line in executable
            if isinstance(line, int) and 0 < line <= len(source)
            and "pragma: no cover" not in source[line - 1]
        }
        hit = {line for (filename, line), count in counts.items()
               if Path(filename).resolve() == path.resolve() and line in executable}
        total_exec += len(executable)
        total_hit += len(hit)
        print(f"{path.name:16s} {len(hit):4d}/{len(executable):4d} "
              f"{100 * len(hit) / max(len(executable), 1):6.1f}%")
    percentage = 100 * total_hit / max(total_exec, 1)
    print(f"tests run={result.testsRun} failures={len(result.failures)} errors={len(result.errors)}")
    print(f"TOTAL {total_hit}/{total_exec} = {percentage:.1f}% (required {minimum:.1f}%)")
    return 0 if result.wasSuccessful() and percentage >= minimum else 1


if __name__ == "__main__":
    raise SystemExit(main())
