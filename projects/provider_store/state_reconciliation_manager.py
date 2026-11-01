"""Read-only cross-manager provider request state reconciliation."""
from __future__ import annotations

from collections import defaultdict
from typing import Any


class ProviderStoreReconciliationManager:
    """Compare local job, quota, credit and cost ledgers without mutating them.

    Findings are diagnostics only. This manager never releases reservations or
    assumes an ambiguous provider outcome was free of charge.
    """

    def __init__(
        self,
        *,
        job_manager: Any,
        quota_manager: Any,
        credit_manager: Any,
        cost_policy: Any,
    ) -> None:
        requirements = (
            (job_manager, "data", "jobs"),
            (quota_manager, "data", "requests"),
            (credit_manager, "data", "reservations"),
            (cost_policy, "ledger", "projects"),
        )
        if any(
            not isinstance(getattr(manager, attribute, None), dict)
            or not isinstance(getattr(manager, attribute).get(key), dict)
            for manager, attribute, key in requirements
        ):
            raise ValueError("all managers must expose validated state mappings")
        self.job_manager = job_manager
        self.quota_manager = quota_manager
        self.credit_manager = credit_manager
        self.cost_policy = cost_policy

    @staticmethod
    def _add(
        index: dict[str, list[dict[str, Any]]],
        request_id: Any,
        source: str,
        record: Any,
        *,
        project_id: str | None = None,
    ) -> None:
        if not isinstance(request_id, str) or not request_id.strip() or not isinstance(record, dict):
            return
        index[request_id].append({
            "source": source,
            "provider_id": record.get("provider_id"),
            "status": record.get("status"),
            "project_id": project_id,
        })

    def inspect(self) -> dict[str, Any]:
        """Return a deterministic inventory and manual-review findings."""
        index: dict[str, list[dict[str, Any]]] = defaultdict(list)
        jobs = self.job_manager.data["jobs"]
        quota = self.quota_manager.data["requests"]
        credit = self.credit_manager.data["reservations"]
        credit_usage = self.credit_manager.data["usage"]
        projects = self.cost_policy.ledger["projects"]

        for request_id, record in jobs.items():
            self._add(index, request_id, "job", record)
        for request_id, record in quota.items():
            self._add(index, request_id, "quota", record)
        for request_id, record in credit.items():
            self._add(index, request_id, "credit_reservation", record)
        for request_id, record in credit_usage.items():
            self._add(index, request_id, "credit_usage", record)
        for project_id, project in projects.items():
            if not isinstance(project_id, str) or not isinstance(project, dict):
                continue
            for source_key, source_name in (("reservations", "cost_reservation"), ("requests", "cost_usage")):
                records = project.get(source_key, {})
                if not isinstance(records, dict):
                    continue
                for request_id, record in records.items():
                    self._add(
                        index, request_id, source_name, record,
                        project_id=project_id,
                    )

        findings: list[dict[str, Any]] = []
        inventory: list[dict[str, Any]] = []
        active_reservations = {"RESERVED"}
        terminal_job_states = {"SUCCEEDED", "FAILED", "CANCELLED"}
        for request_id in sorted(index):
            records = sorted(index[request_id], key=lambda item: item["source"])
            job_records = [item for item in records if item["source"] == "job"]
            job = job_records[0] if job_records else None
            provider_ids = sorted({
                item["provider_id"] for item in records
                if isinstance(item.get("provider_id"), str) and item["provider_id"]
            })
            if len(provider_ids) > 1:
                findings.append({
                    "code": "REQUEST_PROVIDER_MISMATCH",
                    "request_id": request_id,
                    "provider_ids": provider_ids,
                    "sources": [item["source"] for item in records],
                })
            if job and job.get("status") in terminal_job_states:
                active = [
                    item for item in records
                    if item["source"] in {"quota", "credit_reservation", "cost_reservation"}
                    and item.get("status") in active_reservations
                ]
                if active:
                    findings.append({
                        "code": "TERMINAL_JOB_HAS_ACTIVE_RESERVATION",
                        "request_id": request_id,
                        "job_status": job["status"],
                        "reservation_sources": [item["source"] for item in active],
                        "action": "MANUAL_BILLING_AND_USAGE_REVIEW",
                    })
            inventory.append({
                "request_id": request_id,
                "sources": [item["source"] for item in records],
                "provider_ids": provider_ids,
                "job_status": job.get("status") if job else None,
            })

        return {
            "status": "RECONCILIATION_REQUIRED" if findings else "RECONCILED",
            "read_only": True,
            "request_count": len(inventory),
            "finding_count": len(findings),
            "requests": inventory,
            "findings": findings,
            "automatic_releases": 0,
        }
