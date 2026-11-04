"""Authenticated encryption for persistent provider-store JSON state.

Keys are supplied by deployment code through a context-aware callback. No
key material is generated, stored, logged, or committed by this module.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .interprocess_lock import atomic_replace_with_retry, interprocess_file_lock

_FORMAT = "AI_CREATOR_AGENT_ENCRYPTED_JSON_STATE"
_VERSION = 1
_ALGORITHM = "AES-256-GCM"
_NONCE_BYTES = 12
_KEY_BYTES = 32
_DEFAULT_MAX_STATE_BYTES = 64 * 1024 * 1024


class EncryptedStateError(ValueError):
    """Raised when encrypted state cannot be safely read or written."""


class EncryptedJSONStateStore:
    """Read/write authenticated JSON state using a deployment-provided AES-256 key.

    The key callback receives a stable purpose context and must return exactly
    32 bytes. Existing plaintext state is rejected unless one-time migration is
    explicitly enabled. Migration is completed only when the caller persists
    the state while holding its normal interprocess lock.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        key_provider: Callable[[str], bytes],
        context: str,
        migrate_legacy_plaintext: bool = False,
        max_state_bytes: int = _DEFAULT_MAX_STATE_BYTES,
    ) -> None:
        if not callable(key_provider):
            raise ValueError("state encryption key provider must be callable")
        if (
            not isinstance(context, str)
            or not context
            or len(context) > 128
            or any(ord(char) < 33 or ord(char) > 126 for char in context)
        ):
            raise ValueError("state encryption context is invalid")
        if not isinstance(migrate_legacy_plaintext, bool):
            raise ValueError("legacy plaintext migration flag must be boolean")
        if (
            isinstance(max_state_bytes, bool)
            or not isinstance(max_state_bytes, int)
            or not 1024 <= max_state_bytes <= 512 * 1024 * 1024
        ):
            raise ValueError("maximum encrypted state size is outside safe limits")
        self.path = Path(path)
        self.key_provider = key_provider
        self.context = context
        self.migrate_legacy_plaintext = migrate_legacy_plaintext
        self.max_state_bytes = max_state_bytes
        self.migration_required = False

    def rotate_key(
        self,
        default: Any,
        *,
        new_key_provider: Callable[[str], bytes],
    ) -> dict[str, Any]:
        """Re-encrypt an existing ciphertext file with a new deployment key.

        The full read/re-encrypt/atomic-replace operation is serialized with
        normal state writers. Plaintext migration is deliberately not treated
        as key rotation; use the manager's explicit legacy migration flow.
        """
        if not callable(new_key_provider):
            raise ValueError("new state encryption key provider must be callable")
        if not self.path.is_file():
            raise EncryptedStateError("state key rotation requires an existing encrypted file")

        lock_path = self.path.with_name(self.path.name + ".lock")
        with interprocess_file_lock(lock_path):
            value = self.read(default)
            if self.migration_required:
                raise EncryptedStateError("plaintext state must be migrated before key rotation")
            previous_provider = self.key_provider
            self.key_provider = new_key_provider
            try:
                # write() encrypts to a private temporary file and only replaces
                # the original after the new key and payload have been validated.
                self.write(value)
            except Exception:
                self.key_provider = previous_provider
                raise
        return {
            "status": "STATE_KEY_ROTATED",
            "rotated": True,
            "context": self.context,
        }

    def _key(self) -> bytes:
        try:
            key = self.key_provider(self.context)
        except Exception as exc:
            raise EncryptedStateError("state encryption key is unavailable") from exc
        if not isinstance(key, bytes) or len(key) != _KEY_BYTES:
            raise EncryptedStateError("state encryption key must be exactly 32 bytes")
        return key

    def _aad(self) -> bytes:
        return f"{_FORMAT}:{_VERSION}:{self.context}".encode("ascii")

    def read(self, default: Any) -> Any:
        """Read JSON state or perform an explicitly enabled legacy plaintext read."""
        self.migration_required = False
        if not self.path.exists():
            return json.loads(json.dumps(default, ensure_ascii=False))
        try:
            size = self.path.stat().st_size
            if size < 1 or size > self.max_state_bytes:
                raise EncryptedStateError("state file size is outside safe limits")
            raw = self.path.read_bytes()
            if len(raw) != size:
                raise EncryptedStateError("state file changed while being read")
            document = json.loads(raw.decode("utf-8"))
        except EncryptedStateError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise EncryptedStateError("state file is unreadable") from exc

        if isinstance(document, dict) and document.get("format") == _FORMAT:
            if set(document) != {"format", "version", "algorithm", "nonce", "ciphertext"}:
                raise EncryptedStateError("encrypted state envelope schema is invalid")
            if document.get("version") != _VERSION or document.get("algorithm") != _ALGORITHM:
                raise EncryptedStateError("encrypted state envelope version is unsupported")
            try:
                nonce = base64.b64decode(document["nonce"], validate=True)
                ciphertext = base64.b64decode(document["ciphertext"], validate=True)
            except (binascii.Error, ValueError, TypeError) as exc:
                raise EncryptedStateError("encrypted state envelope encoding is invalid") from exc
            if len(nonce) != _NONCE_BYTES or len(ciphertext) < 16 or len(ciphertext) > self.max_state_bytes:
                raise EncryptedStateError("encrypted state envelope size is invalid")
            try:
                plaintext = AESGCM(self._key()).decrypt(nonce, ciphertext, self._aad())
            except (InvalidTag, ValueError) as exc:
                raise EncryptedStateError("encrypted state authentication failed") from exc
            except Exception as exc:
                raise EncryptedStateError("encrypted state decryption failed") from exc
            if len(plaintext) > self.max_state_bytes:
                raise EncryptedStateError("decrypted state exceeds configured size limit")
            try:
                value = json.loads(plaintext.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise EncryptedStateError("decrypted state payload is invalid") from exc
            return value

        if self.migrate_legacy_plaintext:
            self.migration_required = True
            return document
        raise EncryptedStateError("plaintext state is not accepted when encryption is enabled")

    def write(self, value: Any) -> None:
        """Encrypt and atomically persist JSON state without exposing key material."""
        try:
            plaintext = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise EncryptedStateError("state payload is not valid JSON data") from exc
        if len(plaintext) > self.max_state_bytes:
            raise EncryptedStateError("state payload exceeds configured size limit")
        try:
            nonce = os.urandom(_NONCE_BYTES)
            ciphertext = AESGCM(self._key()).encrypt(nonce, plaintext, self._aad())
            envelope = {
                "format": _FORMAT,
                "version": _VERSION,
                "algorithm": _ALGORITHM,
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            }
            raw = (
                json.dumps(envelope, ensure_ascii=True, separators=(",", ":")) + "\n"
            ).encode("ascii")
        except EncryptedStateError:
            raise
        except Exception as exc:
            raise EncryptedStateError("state encryption failed") from exc
        if len(raw) > self.max_state_bytes:
            raise EncryptedStateError("encrypted state exceeds configured size limit")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace_with_retry(temp_name, self.path)
        except Exception as exc:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise EncryptedStateError("encrypted state persistence failed") from exc
        self.migration_required = False
