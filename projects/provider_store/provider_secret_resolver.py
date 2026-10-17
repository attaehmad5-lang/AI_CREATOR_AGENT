"""Resolve provider credentials from environment variables or an injected secret vault.

Secret values are returned only to the caller that needs them and are never persisted
or included in resolver errors.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

_ENV_REF = re.compile(r"env:([A-Z_][A-Z0-9_]{0,127})\Z")
_VAULT_REF = re.compile(r"vault:([A-Za-z0-9._-]{1,64}/[A-Za-z0-9._-]{1,128})\Z")


class ProviderSecretResolver:
    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        vault_resolver: Any = None,
    ) -> None:
        if environ is not None and not isinstance(environ, Mapping):
            raise ValueError("environ must be a mapping")
        if vault_resolver is not None and not callable(vault_resolver):
            raise ValueError("vault_resolver must be callable")
        self.environ = environ
        self.vault_resolver = vault_resolver

    def resolve(self, reference: str) -> str:
        if not isinstance(reference, str) or len(reference) > 256:
            raise ValueError("invalid credential reference")
        env_match = _ENV_REF.fullmatch(reference)
        if env_match:
            import os
            source = os.environ if self.environ is None else self.environ
            value = source.get(env_match.group(1))
            if not isinstance(value, str) or not value.strip():
                raise LookupError("credential reference is unavailable")
            return value
        vault_match = _VAULT_REF.fullmatch(reference)
        if vault_match:
            if self.vault_resolver is None:
                raise LookupError("secret vault is not configured")
            try:
                value = self.vault_resolver(vault_match.group(1))
            except Exception:
                raise LookupError("credential reference is unavailable") from None
            if not isinstance(value, str) or not value.strip():
                raise LookupError("credential reference is unavailable")
            return value
        raise ValueError("credential reference must use env:NAME or vault:namespace/name")
