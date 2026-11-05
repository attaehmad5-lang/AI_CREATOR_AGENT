"""Bounded, dependency-aware async execution for independent development tasks.

This is an orchestration foundation, not an AI coding agent or a claim that
500 workers are currently running. Callers provide the task executor.
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable


@dataclass(frozen=True)
class WorkItem:
    task_id: str
    payload: Any = None
    depends_on: tuple[str, ...] = ()


@dataclass
class WorkResult:
    task_id: str
    status: str
    attempts: int
    value: Any = None
    error: str | None = None


@dataclass(frozen=True)
class EngineConfig:
    max_workers: int = 5
    max_worker_limit: int = 500
    retries: int = 1
    retry_backoff_seconds: float = 0.1

    def __post_init__(self) -> None:
        if not 1 <= self.max_worker_limit <= 500:
            raise ValueError("max_worker_limit must be between 1 and 500")
        if not 1 <= self.max_workers <= self.max_worker_limit:
            raise ValueError("max_workers must be between 1 and max_worker_limit")
        if self.retries < 0:
            raise ValueError("retries cannot be negative")
        if self.retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds cannot be negative")


Executor = Callable[[WorkItem], Awaitable[Any] | Any]


class ParallelDevelopmentEngine:
    """Execute a validated DAG with bounded concurrency and deterministic results."""

    def __init__(self, executor: Executor, config: EngineConfig | None = None):
        if not callable(executor):
            raise TypeError("executor must be callable")
        self.executor = executor
        self.config = config or EngineConfig()

    @staticmethod
    def _validate(items: Iterable[WorkItem]) -> dict[str, WorkItem]:
        tasks: dict[str, WorkItem] = {}
        for item in items:
            if not isinstance(item, WorkItem) or not item.task_id.strip():
                raise ValueError("each work item needs a non-empty task_id")
            if item.task_id in tasks:
                raise ValueError(f"duplicate task_id: {item.task_id}")
            tasks[item.task_id] = item
        for item in tasks.values():
            missing = set(item.depends_on) - tasks.keys()
            if missing:
                raise ValueError(f"{item.task_id} has unknown dependencies: {sorted(missing)}")
            if item.task_id in item.depends_on:
                raise ValueError(f"{item.task_id} cannot depend on itself")
        # Kahn cycle check before any task is executed.
        pending = {k: set(v.depends_on) for k, v in tasks.items()}
        ready = [k for k, deps in pending.items() if not deps]
        visited = 0
        while ready:
            done = ready.pop()
            visited += 1
            for key, deps in pending.items():
                if done in deps:
                    deps.remove(done)
                    if not deps:
                        ready.append(key)
        if visited != len(tasks):
            raise ValueError("dependency cycle detected")
        return tasks

    async def _run_one(self, item: WorkItem) -> WorkResult:
        for attempt in range(1, self.config.retries + 2):
            try:
                value = self.executor(item)
                if inspect.isawaitable(value):
                    value = await value
                return WorkResult(item.task_id, "passed", attempt, value=value)
            except Exception as exc:
                if attempt > self.config.retries:
                    return WorkResult(
                        item.task_id, "failed", attempt,
                        error=f"{type(exc).__name__}: {exc}"[:1000],
                    )
                delay = self.config.retry_backoff_seconds * (2 ** (attempt - 1))
                if delay:
                    await asyncio.sleep(delay)
        raise RuntimeError("unreachable retry state")

    async def run(self, items: Iterable[WorkItem]) -> list[WorkResult]:
        tasks = self._validate(items)
        results: dict[str, WorkResult] = {}
        semaphore = asyncio.Semaphore(self.config.max_workers)
        remaining = set(tasks)

        async def bounded(item: WorkItem) -> WorkResult:
            async with semaphore:
                return await self._run_one(item)

        while remaining:
            ready = sorted(
                task_id for task_id in remaining
                if all(dep in results for dep in tasks[task_id].depends_on)
            )
            if not ready:
                raise RuntimeError("scheduler stalled despite DAG validation")

            runnable = [
                task_id for task_id in ready
                if all(results[dep].status == "passed" for dep in tasks[task_id].depends_on)
            ]
            blocked = set(ready) - set(runnable)
            for task_id in blocked:
                results[task_id] = WorkResult(
                    task_id, "skipped", 0, error="dependency did not pass"
                )
                remaining.remove(task_id)

            batch = await asyncio.gather(*(bounded(tasks[k]) for k in runnable))
            for result in batch:
                results[result.task_id] = result
                remaining.remove(result.task_id)

        return [results[key] for key in tasks]
