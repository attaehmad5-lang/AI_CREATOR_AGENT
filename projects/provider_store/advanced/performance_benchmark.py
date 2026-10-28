"""Reproducible, bounded benchmark for offline provider-catalog validation.

This is a local metadata-validation benchmark, not a live-provider or generation-load test.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .catalog_integrity import validate_advanced_catalogs


def run_benchmark(
    *,
    iterations: int = 25,
    workers: int = 1,
    max_seconds: float = 15.0,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    if (
        isinstance(iterations, bool)
        or not isinstance(iterations, int)
        or not 1 <= iterations <= 10000
    ):
        raise ValueError("iterations must be an integer between 1 and 10000")
    if (
        isinstance(workers, bool)
        or not isinstance(workers, int)
        or not 1 <= workers <= 32
    ):
        raise ValueError("workers must be an integer between 1 and 32")
    if (
        isinstance(max_seconds, bool)
        or not isinstance(max_seconds, (int, float))
        or not math.isfinite(max_seconds)
        or max_seconds <= 0
    ):
        raise ValueError("max_seconds must be positive")
    root = (
        Path(repo_root).resolve()
        if repo_root is not None
        else Path(__file__).resolve().parents[3]
    )
    central = root / "projects" / "provider_store" / "provider_catalog.json"
    advanced = (
        root
        / "projects"
        / "provider_store"
        / "advanced"
        / "advanced_provider_catalog.json"
    )
    categories = (
        root / "projects" / "provider_store" / "advanced" / "advanced_categories.json"
    )

    def validate_once(_: int) -> dict[str, Any]:
        result = validate_advanced_catalogs(central, advanced, categories)
        if not result["valid"]:
            raise ValueError("provider catalog integrity validation failed")
        return result

    start = time.perf_counter()
    if workers == 1:
        results = [validate_once(index) for index in range(iterations)]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(validate_once, range(iterations)))
    last_result = results[-1]
    elapsed = time.perf_counter() - start
    if elapsed > max_seconds:
        raise TimeoutError(
            f"catalog validation benchmark exceeded {max_seconds:g}s budget"
        )
    return {
        "benchmark": "offline_catalog_integrity_validation",
        "iterations": iterations,
        "workers": workers,
        "elapsed_seconds": round(elapsed, 6),
        "operations_per_second": round(iterations / elapsed, 3) if elapsed else None,
        "max_seconds": float(max_seconds),
        "valid": True,
        "central_provider_count": last_result["central_provider_count"],
        "advanced_provider_count": last_result["advanced_provider_count"],
        "category_count": last_result["category_count"],
        "scope_limit": "metadata validation only; no live network/provider generation",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=25)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max-seconds", type=float, default=15.0)
    args = parser.parse_args()
    try:
        result = run_benchmark(
            iterations=args.iterations,
            workers=args.workers,
            max_seconds=args.max_seconds,
        )
    except (OSError, ValueError, TimeoutError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
