"""Unified provider interface (section F) and capability intelligence (section G)."""
from __future__ import annotations

import copy
import inspect
import re
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping

from ..connection_manager import MODALITY_CONTRACTS, MODALITY_OUTPUT_KINDS
from ..provider_error_policy import classify_provider_error
from .core import RegistryCore
from .storage import RegistryStore

INTERFACE_VERSION = "1.0"

# capability name -> method that proves it (used by detection + verification)
CAPABILITY_METHODS: dict[str, tuple[str, ...]] = {
    "image_generation": ("generate_image",), "video_generation": ("generate_video",),
    "speech_generation": ("generate_speech", "generate_audio"), "audio_generation": ("generate_audio",),
    "music_generation": ("generate_music",), "text_generation": ("generate_text",),
    "transcription": ("transcribe",), "translation": ("translate",), "dubbing": ("dub",),
    "image_editing": ("edit_image",), "image_upscaling": ("upscale_image",), "video_upscaling": ("upscale_video",),
    "lipsync": ("generate_lipsync",), "video_editing": ("edit_video",), "animation": ("generate_animation",),
    "document_generation": ("generate_document",), "research": ("research",),
}


# ------------------------------------------------------------------ F: schemas / errors
ERROR_CODES = {"INVALID_REQUEST", "AUTHENTICATION_FAILED", "PERMISSION_DENIED", "RATE_LIMITED", "CREDITS_EXHAUSTED",
               "TIMEOUT", "PROVIDER_UNAVAILABLE", "UNSUPPORTED_OPERATION", "INVALID_OUTPUT", "NOT_LOADED", "INTERNAL_ERROR"}


def error_response(code: str, message: str, *, provider_id: str | None = None, retryable: bool = False,
                   details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """F07: the single error shape every registry-mediated call returns."""
    if code not in ERROR_CODES:
        code = "INTERNAL_ERROR"
    return {"ok": False, "error": {"code": code, "message": str(message)[:300], "retryable": bool(retryable),
                                   "provider": provider_id, "details": dict(details or {})}}


def normalize_exception(exc: BaseException, provider_id: str | None = None) -> dict[str, Any]:
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    if isinstance(status, int) and not isinstance(status, bool):
        verdict = classify_provider_error(status, provider_id=provider_id, provider_code=getattr(exc, "code", None))
        mapping = {"CREDITS_EXHAUSTED": "CREDITS_EXHAUSTED", "AUTHENTICATION_FAILED": "AUTHENTICATION_FAILED",
                   "PROVIDER_TEMPORARILY_UNAVAILABLE": "PROVIDER_UNAVAILABLE", "PROVIDER_REQUEST_REJECTED": "INVALID_REQUEST"}
        code = mapping.get(verdict.get("status"), "RATE_LIMITED" if status == 429 else "PROVIDER_UNAVAILABLE")
        return error_response(code, f"provider returned HTTP {status}", provider_id=provider_id,
                              retryable=bool(verdict.get("retryable")), details={"http_status": status})
    if isinstance(exc, TimeoutError):
        return error_response("TIMEOUT", "provider timed out", provider_id=provider_id, retryable=True)
    if isinstance(exc, PermissionError):
        return error_response("PERMISSION_DENIED", "permission denied", provider_id=provider_id)
    if isinstance(exc, (ValueError, TypeError)):
        return error_response("INVALID_REQUEST", "provider rejected request", provider_id=provider_id)
    if isinstance(exc, (NotImplementedError, AttributeError)):
        return error_response("UNSUPPORTED_OPERATION", "operation not supported", provider_id=provider_id)
    if isinstance(exc, (ConnectionError, OSError)):
        return error_response("PROVIDER_UNAVAILABLE", "provider unreachable", provider_id=provider_id, retryable=True)
    return error_response("INTERNAL_ERROR", type(exc).__name__, provider_id=provider_id)


@dataclass
class GenerationRequest:
    """F05: standard input. ``params`` carries modality-specific, non-secret options."""
    prompt: str = ""
    modality: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    input_files: list[str] = field(default_factory=list)
    output_format: str | None = None
    request_id: str | None = None


@dataclass
class GenerationResult:
    """F06: standard output."""
    modality: str
    provider_id: str
    output: Any = None
    output_kind: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0


def validate_request(request: Any, *, modality: str | None = None) -> dict[str, Any]:
    errors = []
    if not isinstance(request, GenerationRequest):
        return {"valid": False, "errors": ["request must be a GenerationRequest"]}
    if not isinstance(request.prompt, str) or len(request.prompt) > 100_000:
        errors.append("prompt must be a string <= 100000 chars")
    if not isinstance(request.params, dict):
        errors.append("params must be an object")
    if not isinstance(request.input_files, list) or not all(isinstance(f, str) for f in request.input_files):
        errors.append("input_files must be a list of strings")
    if request.request_id is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", str(request.request_id)):
        errors.append("invalid request_id")
    if modality and request.modality and request.modality != modality:
        errors.append(f"modality mismatch: {request.modality} != {modality}")
    return {"valid": not errors, "errors": errors}


def validate_result(result: Any, *, expected_kinds: frozenset[str] | None = None) -> dict[str, Any]:
    errors = []
    if not isinstance(result, GenerationResult):
        return {"valid": False, "errors": ["result must be a GenerationResult"]}
    if result.output is None:
        errors.append("output is empty")
    if expected_kinds and result.output_kind not in expected_kinds:
        errors.append(f"output_kind {result.output_kind!r} not in {sorted(expected_kinds)}")
    return {"valid": not errors, "errors": errors}


# ------------------------------------------------------------------ F: interfaces (F01, F03, F09-F13)
class CapabilityInterface(ABC):
    @abstractmethod
    def capabilities(self) -> list[str]: ...


class AuthenticationInterface(ABC):
    @abstractmethod
    def authenticate(self) -> dict[str, Any]: ...


class HealthInterface(ABC):
    @abstractmethod
    def health(self) -> dict[str, Any]: ...


class ConfigurationInterface(ABC):
    @abstractmethod
    def configure(self, config: Mapping[str, Any]) -> None: ...


class EventInterface(ABC):
    @abstractmethod
    def on_event(self, event: Mapping[str, Any]) -> None: ...


class ProviderBase(CapabilityInterface, HealthInterface, ConfigurationInterface, EventInterface):
    """F03: convenient base class. Subclass, set PROVIDER_META, implement the modality method(s)."""
    INTERFACE_VERSION = INTERFACE_VERSION
    PROVIDER_META: dict[str, Any] = {}

    def capabilities(self) -> list[str]:
        return detect_capabilities(self)

    def health(self) -> dict[str, Any]:
        return {"healthy": True}

    def configure(self, config: Mapping[str, Any]) -> None:
        self.config = dict(config)

    def on_event(self, event: Mapping[str, Any]) -> None:
        return None

    def models(self) -> list[dict[str, Any]]:
        return []


INTERFACES: dict[str, tuple[type, tuple[str, ...]]] = {
    "capability": (CapabilityInterface, ("capabilities",)),
    "authentication": (AuthenticationInterface, ("authenticate",)),
    "health": (HealthInterface, ("health",)),
    "configuration": (ConfigurationInterface, ("configure",)),
    "event": (EventInterface, ("on_event",)),
}


def implemented_interfaces(obj: Any) -> list[str]:
    return sorted(name for name, (_abc, methods) in INTERFACES.items()
                  if all(callable(getattr(obj, m, None)) for m in methods))


def detect_capabilities(obj: Any) -> list[str]:
    """G01: capabilities proven by callable methods on the object (no reliance on declarations)."""
    return sorted(cap for cap, methods in CAPABILITY_METHODS.items()
                  if any(callable(getattr(obj, m, None)) for m in methods))


def parse_version(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in str(text).split("."))


class LegacyProviderAdapter:
    """F15: wrap an object that uses non-standard method names behind the standard contract."""

    def __init__(self, legacy: Any, method_map: Mapping[str, str], *, meta: Mapping[str, Any] | None = None) -> None:
        self._legacy = legacy
        self._map = dict(method_map)
        self.INTERFACE_VERSION = INTERFACE_VERSION
        self.PROVIDER_META = dict(meta or {})
        for standard, legacy_name in self._map.items():
            target = getattr(legacy, legacy_name, None)
            if callable(target):
                setattr(self, standard, self._bind(target))

    @staticmethod
    def _bind(target: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            return target(*args, **kwargs)
        return call

    def capabilities(self) -> list[str]:
        return detect_capabilities(self)

    def health(self) -> dict[str, Any]:
        probe = getattr(self._legacy, "ping", None) or getattr(self._legacy, "health", None)
        return {"healthy": bool(probe()) if callable(probe) else True}

    def close(self) -> None:
        closer = getattr(self._legacy, "close", None)
        if callable(closer):
            closer()


class UnifiedInterface:
    """F01/F02/F04/F08/F14 + G: contract checks, standardized invocation, capability intelligence."""

    def __init__(self, core: RegistryCore, store: RegistryStore, *, health_recorder: Callable[..., Any] | None = None) -> None:
        self.core, self.store, self.cm, self.bus = core, store, core.cm, core.bus
        self.health_recorder = health_recorder  # set by the engine so invoke() feeds the health system
        with store.transaction() as data:
            for key in ("capabilities", "models", "limits"):
                data["ext"].setdefault(key, {})

    # ---- contract (F01, F02, F04, F14) ----
    def contract_for(self, provider_id: str) -> dict[str, Any]:
        binding = self.cm.get_modality_binding(provider_id)
        return {"status": binding.get("status"), "interface_version": INTERFACE_VERSION,
                "methods": binding.get("contract_methods", []), "output_kinds": binding.get("output_modalities", [])}

    def check_compatibility(self, provider_id: str, instance: Any) -> dict[str, Any]:
        """F04: modality contract + interface version + callable signatures."""
        contract = self.cm.validate_provider_contract(provider_id, instance)
        problems = list(contract.get("missing_methods", []))
        version = getattr(instance, "INTERFACE_VERSION", None)
        if version is not None:
            try:
                if parse_version(version)[0] != parse_version(INTERFACE_VERSION)[0]:
                    problems.append(f"interface major version {version} != {INTERFACE_VERSION}")
            except ValueError:
                problems.append("invalid INTERFACE_VERSION")
        provider = self.cm.get_provider(provider_id) or {}
        category = str(provider.get("category", "")).casefold()
        for alternatives in MODALITY_CONTRACTS.get(category, ()):
            for name in alternatives:
                method = getattr(instance, name, None)
                if callable(method):
                    try:
                        params = inspect.signature(method).parameters.values()
                    except (TypeError, ValueError):
                        break
                    accepts = any(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD, p.POSITIONAL_OR_KEYWORD, p.POSITIONAL_ONLY, p.KEYWORD_ONLY) for p in params)
                    if not accepts:
                        problems.append(f"{name} accepts no arguments")
                    break
        return {"compatible": contract.get("valid") is True and not problems, "problems": problems,
                "interfaces": implemented_interfaces(instance), "interface_version": version or "unspecified"}

    def negotiate_version(self, client_version: str) -> dict[str, Any]:
        """F14: same major required; minor of the registry must be >= client's."""
        try:
            client, ours = parse_version(client_version), parse_version(INTERFACE_VERSION)
        except ValueError:
            return {"compatible": False, "reason": "INVALID_VERSION"}
        ok = client[0] == ours[0] and (client[1:] <= ours[1:])
        return {"compatible": ok, "registry_interface": INTERFACE_VERSION, "client": client_version}

    # ---- standardized invocation (F05-F08) ----
    def method_for(self, provider_id: str, instance: Any) -> str | None:
        provider = self.cm.get_provider(provider_id) or {}
        category = str(provider.get("category", "")).casefold()
        for alternatives in MODALITY_CONTRACTS.get(category, ()):
            for name in alternatives:
                if callable(getattr(instance, name, None)):
                    return name
        return None

    def invoke(self, provider_id: str, request: GenerationRequest) -> dict[str, Any]:
        """Run the provider's standard generation method; always returns {'ok': bool, ...}."""
        provider = self.cm.get_provider(provider_id)
        if provider is None:
            return error_response("INVALID_REQUEST", "unknown provider", provider_id=provider_id)
        category = str(provider.get("category", "")).casefold()
        checked = validate_request(request, modality=category)
        if not checked["valid"]:
            return error_response("INVALID_REQUEST", "; ".join(checked["errors"]), provider_id=provider_id)
        instance = self.cm.get_loaded_provider(provider_id)
        if instance is None:
            return error_response("NOT_LOADED", "provider is not loaded", provider_id=provider_id)
        method = self.method_for(provider_id, instance)
        if method is None:
            return error_response("UNSUPPORTED_OPERATION", "no contract method", provider_id=provider_id)
        started = time.perf_counter()
        try:
            raw = getattr(instance, method)(request)
        except Exception as exc:
            latency = (time.perf_counter() - started) * 1000
            failure = normalize_exception(exc, provider_id)
            self._record(provider_id, False, latency, failure["error"]["code"])
            self.bus.emit("provider.failure", {"provider": provider_id, "code": failure["error"]["code"]})
            return failure
        latency = (time.perf_counter() - started) * 1000
        kinds = MODALITY_OUTPUT_KINDS.get(category)
        if isinstance(raw, GenerationResult):
            result = raw
        else:
            result = GenerationResult(modality=category, provider_id=provider_id, output=raw,
                                      output_kind=next(iter(sorted(kinds))) if kinds and len(kinds) == 1 else "")
        result.provider_id, result.latency_ms = provider_id, latency
        verdict = validate_result(result, expected_kinds=kinds if result.output_kind else None)
        if not verdict["valid"]:
            self._record(provider_id, False, latency, "INVALID_OUTPUT")
            return error_response("INVALID_OUTPUT", "; ".join(verdict["errors"]), provider_id=provider_id)
        self._record(provider_id, True, latency, None)
        return {"ok": True, "result": asdict(result)}

    def _record(self, provider_id: str, success: bool, latency: float, code: str | None) -> None:
        self.core.metrics.incr("interface.calls")
        self.core.metrics.observe("interface.latency", latency / 1000)
        if self.health_recorder is not None:
            self.health_recorder(provider_id, success=success, latency_ms=latency, error_code=code)

    # ------------------------------------------------------------------ G: capabilities
    def declared_capabilities(self, provider_id: str) -> list[str]:
        provider = self.cm.get_provider(provider_id)
        if provider is None:
            return []
        data = self.store.read()["ext"]["capabilities"].get(provider_id, {})
        return sorted({str(c) for c in provider.get("capabilities", [])} | set(data))

    def register_capability(self, provider_id: str, capability: str, **attributes: Any) -> dict[str, Any]:
        """G02: registry-owned capability record (attributes such as max_resolution, qualities...)."""
        if self.cm.get_provider(provider_id) is None:
            return {"status": "NOT_FOUND", "provider": provider_id}
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,59}", str(capability)):
            return {"status": "INVALID_CAPABILITY"}
        if self.cm._contains_credential_value_field(attributes):
            return {"status": "SENSITIVE_METADATA_REJECTED"}
        with self.store.transaction() as data:
            record = data["ext"]["capabilities"].setdefault(provider_id, {}).setdefault(capability, {"verified": False})
            record.update(copy.deepcopy(attributes))
        return {"status": "CAPABILITY_REGISTERED", "provider": provider_id, "capability": capability}

    def detect(self, provider_id: str) -> dict[str, Any]:
        """G01 on a loaded instance; with auto-registration of what it proves."""
        instance = self.cm.get_loaded_provider(provider_id)
        if instance is None:
            return {"status": "NOT_LOADED", "provider": provider_id}
        found = detect_capabilities(instance)
        for cap in found:
            self.register_capability(provider_id, cap, detected=True)
        return {"status": "DETECTED", "provider": provider_id, "capabilities": found}

    def verify_capability(self, provider_id: str, capability: str, *, probe: Callable[[Any], Any] | None = None) -> dict[str, Any]:
        """G03: the loaded instance must implement the proving method (and pass an optional probe)."""
        instance = self.cm.get_loaded_provider(provider_id)
        if instance is None:
            return {"status": "NOT_LOADED", "verified": False}
        methods = CAPABILITY_METHODS.get(capability)
        if not methods or not any(callable(getattr(instance, m, None)) for m in methods):
            return {"status": "CAPABILITY_NOT_IMPLEMENTED", "verified": False, "capability": capability}
        if probe is not None:
            try:
                ok = bool(probe(instance))
            except Exception:
                ok = False
            if not ok:
                return {"status": "PROBE_FAILED", "verified": False, "capability": capability}
        self.register_capability(provider_id, capability)
        with self.store.transaction() as data:
            data["ext"]["capabilities"][provider_id][capability]["verified"] = True
        return {"status": "VERIFIED", "verified": True, "capability": capability}

    def match(self, capability: str, *, verified_only: bool = False, enabled_only: bool = True) -> list[str]:
        """G04: providers offering a capability (catalog declaration or registered), optionally verified."""
        caps = self.store.read()["ext"]["capabilities"]
        out = []
        for provider in self.cm.list_providers():
            pid = provider["id"]
            if enabled_only and provider.get("enabled") is not True:
                continue
            record = caps.get(pid, {}).get(capability)
            declared = capability in {str(c) for c in provider.get("capabilities", [])}
            if verified_only:
                if record and record.get("verified"):
                    out.append(pid)
            elif declared or record is not None:
                out.append(pid)
        return sorted(out)

    # ---- models (G05-G09) ----
    def discover_models(self, provider_id: str) -> dict[str, Any]:
        instance = self.cm.get_loaded_provider(provider_id)
        if instance is None:
            return {"status": "NOT_LOADED", "provider": provider_id}
        lister = getattr(instance, "models", None)
        if not callable(lister):
            return {"status": "NO_MODEL_LISTING", "provider": provider_id}
        try:
            items = lister()
        except Exception as exc:
            return {"status": "DISCOVERY_FAILED", "error_type": type(exc).__name__}
        added = []
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                name = item["name"]
                self.set_model_metadata(provider_id, name, {k: v for k, v in item.items() if k != "name"})
                if "version" in item:
                    self.track_model_version(provider_id, name, str(item["version"]))
                added.append(name)
        return {"status": "DISCOVERED", "provider": provider_id, "models": added}

    def set_model_metadata(self, provider_id: str, model: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
        if self.cm.get_provider(provider_id) is None:
            return {"status": "NOT_FOUND"}
        if not isinstance(model, str) or not model.strip() or len(model) > 200 or self.cm._contains_credential_value_field(dict(metadata)):
            return {"status": "INVALID_MODEL"}
        with self.store.transaction() as data:
            entry = data["ext"]["models"].setdefault(provider_id, {}).setdefault(model, {"versions": [], "available": True})
            entry.update(copy.deepcopy({k: v for k, v in metadata.items() if k not in {"versions"}}))
        return {"status": "MODEL_UPDATED", "provider": provider_id, "model": model}

    def models(self, provider_id: str) -> dict[str, Any]:
        return copy.deepcopy(self.store.read()["ext"]["models"].get(provider_id, {}))

    def track_model_version(self, provider_id: str, model: str, version: str) -> dict[str, Any]:
        """G07: append-only version history; latest = highest parsed version."""
        try:
            parse_version(version)
        except ValueError:
            return {"status": "INVALID_VERSION"}
        if model not in self.models(provider_id):
            self.set_model_metadata(provider_id, model, {})
        with self.store.transaction() as data:
            versions = data["ext"]["models"][provider_id][model]["versions"]
            if version not in versions:
                versions.append(version)
                versions.sort(key=parse_version)
        return {"status": "VERSION_TRACKED", "latest": self.models(provider_id)[model]["versions"][-1]}

    def set_model_available(self, provider_id: str, model: str, available: bool) -> dict[str, Any]:
        if model not in self.models(provider_id) or not isinstance(available, bool):
            return {"status": "INVALID_MODEL"}
        with self.store.transaction() as data:
            data["ext"]["models"][provider_id][model]["available"] = available
        return {"status": "AVAILABILITY_SET", "available": available}

    def model_available(self, provider_id: str, model: str) -> dict[str, Any]:
        """G08: ask the live provider when it can answer; otherwise use the recorded flag."""
        record = self.models(provider_id).get(model)
        if record is None:
            return {"status": "UNKNOWN_MODEL", "available": False}
        instance = self.cm.get_loaded_provider(provider_id)
        checker = getattr(instance, "model_available", None) if instance is not None else None
        if callable(checker):
            try:
                return {"status": "CHECKED_LIVE", "available": bool(checker(model))}
            except Exception:
                return {"status": "CHECK_FAILED", "available": False}
        return {"status": "RECORDED", "available": bool(record.get("available", True))}

    def check_model_compatibility(self, provider_id: str, model: str, *, input_format: str | None = None,
                                  output_format: str | None = None, resolution: tuple[int, int] | None = None,
                                  duration: float | None = None, quality: str | None = None) -> dict[str, Any]:
        """G09-G14: one place that evaluates a model/provider against concrete requirements."""
        meta = self.models(provider_id).get(model)
        if meta is None:
            return {"compatible": False, "reasons": ["UNKNOWN_MODEL"]}
        reasons = []
        if not meta.get("available", True):
            reasons.append("MODEL_UNAVAILABLE")
        if input_format and meta.get("input_formats") and input_format.casefold() not in {f.casefold() for f in meta["input_formats"]}:
            reasons.append("INPUT_FORMAT_UNSUPPORTED")
        if output_format and meta.get("output_formats") and output_format.casefold() not in {f.casefold() for f in meta["output_formats"]}:
            reasons.append("OUTPUT_FORMAT_UNSUPPORTED")
        if resolution and meta.get("max_resolution"):
            mw, mh = meta["max_resolution"]
            if resolution[0] > mw or resolution[1] > mh:
                reasons.append("RESOLUTION_EXCEEDS_MAX")
        if duration is not None and meta.get("max_duration_seconds") is not None and duration > meta["max_duration_seconds"]:
            reasons.append("DURATION_EXCEEDS_MAX")
        if duration is not None and meta.get("min_duration_seconds") is not None and duration < meta["min_duration_seconds"]:
            reasons.append("DURATION_BELOW_MIN")
        if quality and meta.get("qualities") and quality not in meta["qualities"]:
            reasons.append("QUALITY_UNSUPPORTED")
        return {"compatible": not reasons, "reasons": reasons}

    def provider_supports(self, provider_id: str, **requirements: Any) -> bool:
        """True when at least one available model of the provider satisfies the requirements (G10-G14)."""
        return any(self.check_model_compatibility(provider_id, name, **requirements)["compatible"]
                   for name in self.models(provider_id))

    def filter_providers(self, *, capability: str | None = None, verified_only: bool = False, category: str | None = None,
                         tag: str | None = None, health_check: Callable[[str], bool] | None = None,
                         **requirements: Any) -> list[str]:
        """G15: combine capability, category, tag, health and per-model requirements."""
        pool = self.match(capability, verified_only=verified_only) if capability else [p["id"] for p in self.cm.list_providers() if p.get("enabled") is True]
        out = []
        for pid in pool:
            if category and category.casefold() not in self.core.categories_of(pid):
                continue
            if tag and pid not in {p["id"] for p in self.core.find_by_tag(tag)}:
                continue
            if requirements and not self.provider_supports(pid, **requirements):
                continue
            if health_check is not None and not health_check(pid):
                continue
            out.append(pid)
        return sorted(out)
