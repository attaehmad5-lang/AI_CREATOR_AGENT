"""Coordinated, fail-closed subject deletion and retention across registered stores.

Adapters own the actual deletion mechanics for their database, object store, or
provider. This coordinator requires explicit adapter confirmation and exposes
only bounded status/count metadata; it never returns subject identifiers or raw
adapter responses.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

_COMPONENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_REFERENCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DELETE_STATUSES = frozenset({"DELETED", "ALREADY_ABSENT", "PENDING", "FAILED"})
_RETENTION_STATUSES = frozenset({"PURGED", "ALREADY_EMPTY", "PENDING", "FAILED"})


class PrivacyDataLifecycleManager:
    """Coordinate deletion/retention adapters without claiming unverified success.

    Each registered adapter must implement:
      * delete_subject_data(subject_ref, project_id, tenant_id, request_id)
      * purge_expired_data(cutoff, project_id, tenant_id, request_id)

    It must return a mapping with confirmed (bool), status (a supported status),
    and deleted_count (a non-negative integer). request_id is an idempotency key:
    adapters must persistently deduplicate it across restarts.
    """

    def __init__(self, stores: Mapping[str, Any] | None = None, *, max_stores: int = 64) -> None:
        if isinstance(max_stores, bool) or not isinstance(max_stores, int) or not 1 <= max_stores <= 256:
            raise ValueError("max_stores must be within 1..256")
        if stores is not None and not isinstance(stores, Mapping):
            raise ValueError("privacy stores must be a mapping")
        self.max_stores = max_stores
        self._stores: dict[str, Any] = {}
        for store_id, adapter in (stores or {}).items():
            self.register_store(store_id, adapter)

    @property
    def registered_store_count(self) -> int:
        return len(self._stores)

    def register_store(self, store_id: str, adapter: Any) -> dict[str, Any]:
        """Register one explicit adapter; ambiguous or duplicate names are rejected."""
        if not isinstance(store_id, str) or _COMPONENT_ID.fullmatch(store_id) is None:
            raise ValueError("invalid privacy store identifier")
        if store_id in self._stores:
            raise ValueError("privacy store is already registered")
        if len(self._stores) >= self.max_stores:
            raise ValueError("privacy store capacity reached")
        if not callable(getattr(adapter, "delete_subject_data", None)):
            raise ValueError("privacy store must support delete_subject_data")
        if not callable(getattr(adapter, "purge_expired_data", None)):
            raise ValueError("privacy store must support purge_expired_data")
        self._stores[store_id] = adapter
        return {"status": "PRIVACY_STORE_REGISTERED", "registered": True}

    @staticmethod
    def _valid_scope(project_id: Any, tenant_id: Any, request_id: Any) -> bool:
        return (
            isinstance(project_id, str)
            and _REFERENCE_ID.fullmatch(project_id) is not None
            and (
                tenant_id is None
                or (
                    isinstance(tenant_id, str)
                    and _REFERENCE_ID.fullmatch(tenant_id) is not None
                )
            )
            and isinstance(request_id, str)
            and _REFERENCE_ID.fullmatch(request_id) is not None
        )

    @staticmethod
    def _safe_adapter_result(result: Any, *, allowed_statuses: frozenset[str]) -> tuple[bool, str, int]:
        if not isinstance(result, Mapping):
            return False, "INVALID_ADAPTER_RESPONSE", 0
        confirmed = result.get("confirmed")
        status = result.get("status")
        count = result.get("deleted_count")
        if (
            not isinstance(confirmed, bool)
            or not isinstance(status, str)
            or status not in allowed_statuses
            or isinstance(count, bool)
            or not isinstance(count, int)
            or not 0 <= count <= 1_000_000_000_000
        ):
            return False, "INVALID_ADAPTER_RESPONSE", 0
        successful = status in {"DELETED", "ALREADY_ABSENT", "PURGED", "ALREADY_EMPTY"}
        if confirmed is not successful:
            return False, "INVALID_ADAPTER_RESPONSE", 0
        return confirmed, status, count

    def _run(
        self, *, operation: str, project_id: str, tenant_id: str | None,
        request_id: str, subject_ref: str | None = None, cutoff: datetime | None = None,
    ) -> dict[str, Any]:
        if not self._stores:
            return {
                "status": "PRIVACY_STORES_NOT_CONFIGURED", "confirmed": False,
                "completed": False, "store_count": 0, "deleted_count": 0, "stores": {},
            }
        store_results: dict[str, dict[str, Any]] = {}
        total_deleted = 0
        all_confirmed = True
        method_name = "delete_subject_data" if operation == "delete" else "purge_expired_data"
        allowed_statuses = _DELETE_STATUSES if operation == "delete" else _RETENTION_STATUSES
        for store_id, adapter in self._stores.items():
            try:
                method = getattr(adapter, method_name)
                if operation == "delete":
                    raw = method(
                        subject_ref=subject_ref, project_id=project_id,
                        tenant_id=tenant_id, request_id=request_id,
                    )
                else:
                    raw = method(
                        cutoff=cutoff, project_id=project_id,
                        tenant_id=tenant_id, request_id=request_id,
                    )
                confirmed, status, deleted_count = self._safe_adapter_result(
                    raw, allowed_statuses=allowed_statuses,
                )
                store_results[store_id] = {
                    "confirmed": confirmed, "status": status, "deleted_count": deleted_count,
                }
                if confirmed:
                    total_deleted += deleted_count
                else:
                    all_confirmed = False
            except Exception as exc:
                error_type = type(exc).__name__
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", error_type):
                    error_type = "AdapterError"
                store_results[store_id] = {
                    "confirmed": False, "status": "ADAPTER_OPERATION_FAILED",
                    "deleted_count": 0, "error_type": error_type,
                }
                all_confirmed = False
        if operation == "delete":
            success_status, partial_status = "SUBJECT_DATA_DELETED", "SUBJECT_DATA_DELETION_PARTIAL"
        else:
            success_status, partial_status = "RETENTION_PURGE_CONFIRMED", "RETENTION_PURGE_PARTIAL"
        return {
            "status": success_status if all_confirmed else partial_status,
            "confirmed": all_confirmed, "completed": all_confirmed,
            "store_count": len(self._stores), "deleted_count": total_deleted,
            "stores": store_results,
        }

    def delete_subject_data(
        self, *, subject_ref: str, project_id: str, tenant_id: str | None,
        request_id: str, confirmed: bool = False,
    ) -> dict[str, Any]:
        """Delete one pseudonymous subject reference across every registered store."""
        if confirmed is not True:
            return {
                "status": "SUBJECT_DELETION_CONFIRMATION_REQUIRED", "confirmed": False,
                "completed": False, "store_count": len(self._stores),
                "deleted_count": 0, "stores": {},
            }
        if (
            not isinstance(subject_ref, str)
            or _REFERENCE_ID.fullmatch(subject_ref) is None
            or not self._valid_scope(project_id, tenant_id, request_id)
        ):
            return {
                "status": "INVALID_SUBJECT_DELETION_SCOPE", "confirmed": False,
                "completed": False, "store_count": len(self._stores),
                "deleted_count": 0, "stores": {},
            }
        return self._run(
            operation="delete", subject_ref=subject_ref,
            project_id=project_id, tenant_id=tenant_id, request_id=request_id,
        )

    def purge_expired_data(
        self, *, cutoff: datetime, project_id: str, tenant_id: str | None,
        request_id: str, confirmed: bool = False,
    ) -> dict[str, Any]:
        """Purge expired data through all stores using one timezone-aware cutoff."""
        if confirmed is not True:
            return {
                "status": "RETENTION_PURGE_CONFIRMATION_REQUIRED", "confirmed": False,
                "completed": False, "store_count": len(self._stores),
                "deleted_count": 0, "stores": {},
            }
        if (
            not isinstance(cutoff, datetime)
            or cutoff.tzinfo is None
            or cutoff.utcoffset() is None
            or not self._valid_scope(project_id, tenant_id, request_id)
        ):
            return {
                "status": "INVALID_RETENTION_PURGE_SCOPE", "confirmed": False,
                "completed": False, "store_count": len(self._stores),
                "deleted_count": 0, "stores": {},
            }
        return self._run(
            operation="retention", project_id=project_id, tenant_id=tenant_id,
            request_id=request_id, cutoff=cutoff,
        )
