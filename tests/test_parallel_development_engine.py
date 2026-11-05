import asyncio
import unittest

from tools.parallel_engine import (
    EngineConfig, ParallelDevelopmentEngine, WorkItem,
)


class ParallelDevelopmentEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_respects_worker_limit_and_dependencies(self):
        active = 0
        peak = 0

        async def execute(item):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return item.task_id

        engine = ParallelDevelopmentEngine(execute, EngineConfig(max_workers=2))
        items = [
            WorkItem("a"), WorkItem("b"), WorkItem("c"),
            WorkItem("d", depends_on=("a", "b")),
        ]
        results = await engine.run(items)
        self.assertLessEqual(peak, 2)
        self.assertEqual([r.status for r in results], ["passed"] * 4)

    async def test_retries_then_passes(self):
        calls = 0

        async def execute(item):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient")
            return "ok"

        engine = ParallelDevelopmentEngine(
            execute, EngineConfig(max_workers=1, retries=1)
        )
        result = (await engine.run([WorkItem("a")]))[0]
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.attempts, 2)

    async def test_failed_dependency_skips_dependent(self):
        async def execute(item):
            if item.task_id == "a":
                raise RuntimeError("failure")
            return True

        engine = ParallelDevelopmentEngine(
            execute, EngineConfig(max_workers=2, retries=0)
        )
        results = await engine.run([WorkItem("a"), WorkItem("b", depends_on=("a",))])
        self.assertEqual([r.status for r in results], ["failed", "skipped"])

    async def test_rejects_cycle_before_execution(self):
        called = False

        async def execute(item):
            nonlocal called
            called = True

        engine = ParallelDevelopmentEngine(execute)
        with self.assertRaisesRegex(ValueError, "cycle"):
            await engine.run([
                WorkItem("a", depends_on=("b",)),
                WorkItem("b", depends_on=("a",)),
            ])
        self.assertFalse(called)

    async def test_worker_limit_cannot_exceed_500(self):
        with self.assertRaises(ValueError):
            EngineConfig(max_workers=501)


if __name__ == "__main__":
    unittest.main()
