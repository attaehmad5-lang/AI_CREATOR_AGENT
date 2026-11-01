"""Safe runtime registry for explicitly injected provider generation adapters.

The registry does not implement vendor APIs. It binds an application-supplied callable
to a catalog provider and refuses dispatch until that provider is enabled and verified.
"""
from __future__ import annotations

import re
import threading
from typing import Any, Callable


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class ProviderAdapterRegistry:
    """Thread-safe adapter registry with verified-provider and capability gates."""

    def __init__(self, connection_manager: Any) -> None:
        if not callable(getattr(connection_manager, "get_provider", None)):
            raise ValueError("connection_manager must support get_provider")
        if not callable(getattr(connection_manager, "get_state", None)):
            raise ValueError("connection_manager must support get_state")
        self.connection_manager = connection_manager
        self._lock = threading.RLock()
        self._adapters: dict[str, dict[str, Any]] = {}

    def register(
        self,
        provider_id: str,
        adapter: Callable[..., Any],
        *,
        modalities: list[str] | tuple[str, ...],
        model_ids: list[str] | tuple[str, ...] | None = None,
        streaming_modalities: list[str] | tuple[str, ...] | None = None,
        replace: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not _IDENTIFIER.fullmatch(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "registered": False}
        if not callable(adapter):
            return {"status": "INVALID_ADAPTER", "registered": False}
        if not isinstance(replace, bool):
            return {"status": "INVALID_REPLACE_FLAG", "registered": False}
        if not isinstance(modalities, (list, tuple)) or not modalities:
            return {"status": "INVALID_MODALITIES", "registered": False}
        normalized_modalities: set[str] = set()
        for modality in modalities:
            if not isinstance(modality, str) or not _IDENTIFIER.fullmatch(modality):
                return {"status": "INVALID_MODALITIES", "registered": False}
            normalized_modalities.add(modality)
        if model_ids is not None:
            if not isinstance(model_ids, (list, tuple)):
                return {"status": "INVALID_MODEL_IDS", "registered": False}
            if any(not isinstance(model, str) or not _IDENTIFIER.fullmatch(model) for model in model_ids):
                return {"status": "INVALID_MODEL_IDS", "registered": False}
            normalized_models = set(model_ids)
        else:
            normalized_models = None
        if streaming_modalities is None:
            normalized_streaming: set[str] = set()
        elif not isinstance(streaming_modalities, (list, tuple)):
            return {"status": "INVALID_STREAMING_MODALITIES", "registered": False}
        else:
            if any(
                not isinstance(modality, str) or not _IDENTIFIER.fullmatch(modality)
                for modality in streaming_modalities
            ):
                return {"status": "INVALID_STREAMING_MODALITIES", "registered": False}
            normalized_streaming = set(streaming_modalities)
            if not normalized_streaming.issubset(normalized_modalities):
                return {"status": "STREAMING_MODALITY_NOT_REGISTERED", "registered": False}
        provider = self.connection_manager.get_provider(provider_id)
        if provider is None:
            return {"status": "PROVIDER_NOT_FOUND", "registered": False}
        with self._lock:
            if provider_id in self._adapters and not replace:
                return {"status": "ADAPTER_ALREADY_REGISTERED", "registered": False}
            self._adapters[provider_id] = {
                "adapter": adapter,
                "modalities": frozenset(normalized_modalities),
                "model_ids": None if normalized_models is None else frozenset(normalized_models),
                "streaming_modalities": frozenset(normalized_streaming),
            }
        return {"status": "ADAPTER_REGISTERED", "registered": True, "provider_id": provider_id}

    def unregister(self, provider_id: str) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not _IDENTIFIER.fullmatch(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "unregistered": False}
        with self._lock:
            if provider_id not in self._adapters:
                return {"status": "ADAPTER_NOT_REGISTERED", "unregistered": False}
            del self._adapters[provider_id]
        return {"status": "ADAPTER_UNREGISTERED", "unregistered": True, "provider_id": provider_id}

    def list_adapters(self) -> dict[str, Any]:
        with self._lock:
            entries = [
                {
                    "provider_id": provider_id,
                    "modalities": sorted(entry["modalities"]),
                    "model_ids": sorted(entry["model_ids"]) if entry["model_ids"] is not None else None,
                    "streaming_modalities": sorted(entry["streaming_modalities"]),
                }
                for provider_id, entry in sorted(self._adapters.items())
            ]
        return {"status": "ADAPTERS_AVAILABLE" if entries else "NO_ADAPTERS", "count": len(entries), "adapters": entries}

    def resolve(
        self, provider_id: str, *, modality: str, model_id: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(provider_id, str) or not _IDENTIFIER.fullmatch(provider_id):
            return {"status": "INVALID_PROVIDER_ID", "resolved": False}
        if not isinstance(modality, str) or not _IDENTIFIER.fullmatch(modality):
            return {"status": "INVALID_MODALITY", "resolved": False}
        if model_id is not None and (not isinstance(model_id, str) or not _IDENTIFIER.fullmatch(model_id)):
            return {"status": "INVALID_MODEL_ID", "resolved": False}
        provider = self.connection_manager.get_provider(provider_id)
        if provider is None:
            return {"status": "PROVIDER_NOT_FOUND", "resolved": False}
        if provider.get("enabled") is not True:
            return {"status": "PROVIDER_DISABLED", "resolved": False}
        state = self.connection_manager.get_state(provider_id)
        if not isinstance(state, dict) or state.get("status") != "CONNECTED" or state.get("verified") is not True:
            return {"status": "PROVIDER_NOT_VERIFIED", "resolved": False}
        with self._lock:
            entry = self._adapters.get(provider_id)
        if entry is None:
            return {"status": "ADAPTER_NOT_REGISTERED", "resolved": False}
        if modality not in entry["modalities"]:
            return {"status": "MODALITY_UNSUPPORTED", "resolved": False}
        if entry["model_ids"] is not None and model_id not in entry["model_ids"]:
            return {"status": "MODEL_UNSUPPORTED", "resolved": False}
        catalog_models = provider.get("supported_models")
        if model_id is not None and isinstance(catalog_models, list) and catalog_models:
            catalog_model_ids = {
                item.get("id") for item in catalog_models
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            if model_id not in catalog_model_ids:
                return {"status": "MODEL_NOT_IN_CATALOG", "resolved": False}
        return {"status": "ADAPTER_RESOLVED", "resolved": True, "adapter": entry["adapter"], "provider_id": provider_id}

    def resolve_streaming(
        self, provider_id: str, *, modality: str, model_id: str | None = None
    ) -> dict[str, Any]:
        """Resolve only adapters that explicitly advertise streaming for this modality."""
        resolved = self.resolve(provider_id, modality=modality, model_id=model_id)
        if resolved.get("resolved") is not True:
            return resolved
        with self._lock:
            entry = self._adapters.get(provider_id)
        if entry is None or modality not in entry["streaming_modalities"]:
            return {"status": "STREAMING_UNSUPPORTED", "resolved": False}
        return {
            "status": "STREAMING_ADAPTER_RESOLVED",
            "resolved": True,
            "adapter": resolved["adapter"],
            "provider_id": provider_id,
            "modality": modality,
        }

    def open_stream_session(
        self,
        provider_id: str,
        *,
        modality: str,
        request_id: str,
        model_id: str | None = None,
        max_chunks: int = 10_000,
        max_bytes: int = 64 * 1024 * 1024,
        timeout_seconds: float = 300.0,
    ) -> dict[str, Any]:
        """Create a bounded stream accumulator only for a verified streaming adapter.

        This creates local lifecycle state; it does not start a vendor network
        request or imply that the adapter has begun producing chunks.
        """
        resolved = self.resolve_streaming(
            provider_id, modality=modality, model_id=model_id
        )
        if resolved.get("resolved") is not True:
            return {
                "status": resolved.get("status", "STREAMING_ADAPTER_UNAVAILABLE"),
                "opened": False,
            }
        from .streaming_response_manager import ProviderStreamSession

        try:
            session = ProviderStreamSession(
                request_id,
                max_chunks=max_chunks,
                max_bytes=max_bytes,
                timeout_seconds=timeout_seconds,
            )
        except (TypeError, ValueError) as exc:
            return {
                "status": "INVALID_STREAM_SESSION_OPTIONS",
                "opened": False,
                "error_type": type(exc).__name__,
            }
        return {
            "status": "STREAM_SESSION_OPENED",
            "opened": True,
            "provider_id": provider_id,
            "modality": modality,
            "request_id": request_id,
            "session": session,
        }


    def execute_stream(
        self,
        provider_id: str,
        /,
        *,
        modality: str,
        request_id: str,
        model_id: str | None = None,
        max_chunks: int = 10_000,
        max_bytes: int = 64 * 1024 * 1024,
        timeout_seconds: float = 300.0,
        **options: Any,
    ) -> dict[str, Any]:
        """Dispatch an explicitly streaming adapter and safely assemble its chunks.

        The adapter must return an iterable of bytes or UTF-8 strings. Vendor
        reconnect/resume semantics remain adapter-specific and are not inferred.
        """
        if not isinstance(request_id, str) or not _IDENTIFIER.fullmatch(request_id):
            return {"status": "INVALID_REQUEST_ID", "executed": False}
        if {"provider_id", "modality", "request_id", "model_id"}.intersection(options):
            return {"status": "RESERVED_ADAPTER_OPTION", "executed": False}
        opened = self.open_stream_session(
            provider_id,
            modality=modality,
            request_id=request_id,
            model_id=model_id,
            max_chunks=max_chunks,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
        )
        if opened.get("opened") is not True:
            return {
                "status": opened.get("status", "STREAMING_ADAPTER_UNAVAILABLE"),
                "executed": False,
                "started": False,
            }
        adapter = self.resolve_streaming(
            provider_id, modality=modality, model_id=model_id
        ).get("adapter")
        session = opened["session"]
        try:
            chunks = adapter(
                provider_id=provider_id,
                modality=modality,
                request_id=request_id,
                model_id=model_id,
                **options,
            )
            if isinstance(chunks, (str, bytes, bytearray, dict)) or not hasattr(chunks, "__iter__"):
                session.cancel(reason="INVALID_ADAPTER_STREAM")
                return {
                    "status": "ADAPTER_STREAM_INVALID",
                    "executed": False,
                    "started": True,
                    "outcome_known": False,
                    "reservations_retained": True,
                }
            iterator = iter(chunks)
            try:
                for sequence, chunk in enumerate(iterator):
                    accepted = session.append(sequence=sequence, chunk=chunk)
                    if accepted.get("accepted") is not True:
                        session.cancel(reason="STREAM_CHUNK_REJECTED")
                        return {
                            "status": accepted.get("status", "STREAM_CHUNK_REJECTED"),
                            "executed": False,
                            "started": True,
                            "outcome_known": False,
                            "reservations_retained": True,
                            "request_id": request_id,
                        }
            finally:
                close = getattr(iterator, "close", None)
                if callable(close):
                    close()
            completed = session.finish()
            if completed.get("finished") is not True:
                return {
                    "status": completed.get("status", "STREAM_FINISH_FAILED"),
                    "executed": False,
                    "started": True,
                    "outcome_known": False,
                    "reservations_retained": True,
                    "request_id": request_id,
                }
            return {
                "status": "STREAM_EXECUTED",
                "executed": True,
                "started": True,
                "provider_id": provider_id,
                "request_id": request_id,
                "result": completed,
            }
        except Exception as exc:
            session.cancel(reason="ADAPTER_STREAM_FAILED")
            return {
                "status": "ADAPTER_STREAM_OUTCOME_UNKNOWN",
                "executed": False,
                "started": True,
                "outcome_known": False,
                "reservations_retained": True,
                "provider_id": provider_id,
                "request_id": request_id,
                "error_type": type(exc).__name__,
            }

    def as_callable(
        self,
        provider_id: str,
        *,
        modality: str,
        model_id: str | None = None,
    ) -> Callable[..., Any]:
        """Return an adapter callable compatible with ProviderService execution.

        Registry/policy rejection happens before the vendor callable is invoked and
        returns an explicit not-started result. Vendor exceptions are allowed to
        propagate so ProviderService can conservatively retain ambiguous reservations.
        """
        if not isinstance(provider_id, str) or not _IDENTIFIER.fullmatch(provider_id):
            raise ValueError("invalid provider_id")
        if not isinstance(modality, str) or not _IDENTIFIER.fullmatch(modality):
            raise ValueError("invalid modality")
        if model_id is not None and (not isinstance(model_id, str) or not _IDENTIFIER.fullmatch(model_id)):
            raise ValueError("invalid model_id")

        def invoke(**options: Any) -> Any:
            request_id = options.get("request_id")
            requested_model_id = options.get("model_id", model_id)
            if (
                options.get("provider_id") != provider_id
                or options.get("modality", modality) != modality
                or (model_id is not None and requested_model_id not in (None, model_id))
                or not isinstance(request_id, str)
                or not _IDENTIFIER.fullmatch(request_id)
            ):
                return {
                    "started": False,
                    "success": False,
                    "charge_outcome": "not_incurred",
                    "credit_outcome": "not_used",
                    "error_code": "ADAPTER_CONTRACT_REJECTED",
                }
            effective_model_id = model_id if model_id is not None else requested_model_id
            resolved = self.resolve(
                provider_id, modality=modality, model_id=effective_model_id
            )
            if resolved.get("resolved") is not True:
                return {
                    "started": False,
                    "success": False,
                    "charge_outcome": "not_incurred",
                    "credit_outcome": "not_used",
                    "error_code": resolved.get("status", "ADAPTER_NOT_AVAILABLE"),
                }
            adapter_options = {
                key: value for key, value in options.items()
                if key not in {"provider_id", "request_id", "model_id", "modality"}
            }
            return resolved["adapter"](
                provider_id=provider_id,
                modality=modality,
                request_id=request_id,
                model_id=effective_model_id,
                **adapter_options,
            )

        return invoke

    def execute(
        self,
        provider_id: str,
        /,
        *,
        modality: str,
        request_id: str,
        model_id: str | None = None,
        **options: Any,
    ) -> dict[str, Any]:
        if not isinstance(request_id, str) or not _IDENTIFIER.fullmatch(request_id):
            return {"status": "INVALID_REQUEST_ID", "executed": False}
        reserved = {"provider_id", "modality", "request_id", "model_id"}
        if reserved.intersection(options):
            return {"status": "RESERVED_ADAPTER_OPTION", "executed": False}
        resolved = self.resolve(provider_id, modality=modality, model_id=model_id)
        if resolved.get("resolved") is not True:
            return {"status": resolved["status"], "executed": False}
        try:
            result = resolved["adapter"](
                provider_id=provider_id,
                modality=modality,
                request_id=request_id,
                model_id=model_id,
                **options,
            )
        except Exception as exc:
            # The vendor may have accepted the request before the exception surfaced.
            # Never label this as a confirmed no-start; callers must retain reservations.
            return {
                "status": "ADAPTER_OUTCOME_UNKNOWN",
                "executed": False,
                "outcome_known": False,
                "reservations_retained": True,
                "provider_id": provider_id,
                "request_id": request_id,
                "error_type": type(exc).__name__,
            }
        if not isinstance(result, dict) or not isinstance(result.get("started"), bool):
            return {
                "status": "ADAPTER_RESPONSE_INVALID",
                "executed": False,
                "provider_id": provider_id,
                "request_id": request_id,
            }
        return {
            "status": "ADAPTER_EXECUTED",
            "executed": True,
            "provider_id": provider_id,
            "request_id": request_id,
            "result": result,
        }
