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
from projects.provider_store.streaming_response_manager import ProviderStreamSession


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



def run_stream_benchmark(
    *,
    iterations: int = 25,
    chunks_per_stream: int = 64,
    chunk_size_bytes: int = 1024,
    workers: int = 1,
    max_seconds: float = 15.0,
) -> dict[str, Any]:
    """Measure bounded in-memory stream assembly, not upstream provider throughput."""
    for name, value, maximum in (
        ("iterations", iterations, 10000),
        ("chunks_per_stream", chunks_per_stream, 10000),
        ("chunk_size_bytes", chunk_size_bytes, 1024 * 1024),
        ("workers", workers, 32),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    if (
        isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float))
        or not math.isfinite(max_seconds) or max_seconds <= 0
    ):
        raise ValueError("max_seconds must be positive")
    total_per_stream = chunks_per_stream * chunk_size_bytes
    if total_per_stream > 64 * 1024 * 1024:
        raise ValueError("stream benchmark payload exceeds 64 MiB per session")
    payload = b"x" * chunk_size_bytes

    def run_one(index: int) -> dict[str, Any]:
        stream = ProviderStreamSession(
            f"benchmark-{index}", max_chunks=chunks_per_stream,
            max_bytes=total_per_stream,
        )
        for sequence in range(chunks_per_stream):
            accepted = stream.append(sequence=sequence, chunk=payload)
            if accepted.get("accepted") is not True:
                raise RuntimeError("stream benchmark chunk rejected")
        result = stream.finish()
        if result.get("finished") is not True or result.get("total_bytes") != total_per_stream:
            raise RuntimeError("stream benchmark session did not complete")
        return result

    start = time.perf_counter()
    if workers == 1:
        results = [run_one(index) for index in range(iterations)]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(run_one, range(iterations)))
    elapsed = time.perf_counter() - start
    if elapsed > max_seconds:
        raise TimeoutError(f"stream benchmark exceeded {max_seconds:g}s budget")
    total_bytes = iterations * total_per_stream
    return {
        "benchmark": "bounded_stream_assembly",
        "iterations": iterations,
        "workers": workers,
        "chunks_per_stream": chunks_per_stream,
        "chunk_size_bytes": chunk_size_bytes,
        "total_bytes": total_bytes,
        "elapsed_seconds": round(elapsed, 6),
        "streams_per_second": round(iterations / elapsed, 3) if elapsed else None,
        "bytes_per_second": round(total_bytes / elapsed, 3) if elapsed else None,
        "sha256_verified": all(len(item.get("sha256", "")) == 64 for item in results),
        "scope_limit": "in-memory stream assembly only; no provider network or codec throughput",
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
