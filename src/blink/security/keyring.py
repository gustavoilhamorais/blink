"""Secure credential storage via system keyring.

Provides a unified interface for storing, retrieving, and deleting API keys
and other credentials.  Uses the system keyring (via the ``keyring`` library)
when available, with a graceful fallback to an encrypted file stored in
``~/.blink/credentials.json``.

Usage::

    store = CredentialStore()
    await store.store("providers", "openai_api_key", "sk-...")
    key = await store.get("providers", "openai_api_key")
    await store.delete("providers", "openai_api_key")

The file-based fallback uses a simple XOR obfuscation keyed from a
machine-specific token.  It is NOT cryptographically secure but prevents
accidental exposure in log files and plaintext storage.  For production
secrets, prefer the system keyring.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BLINK_DIR = Path(os.environ.get("BLINK_DIR", Path.home() / ".blink"))
_CREDENTIALS_FILE = _BLINK_DIR / "credentials.json"
_SERVICE_PREFIX = "blink"


# ---------------------------------------------------------------------------
# CredentialStore
# ---------------------------------------------------------------------------


class CredentialStore:
    """Store and retrieve credentials securely.

    Tries to use the system keyring first.  Falls back to an obfuscated
    JSON file if the ``keyring`` package is not installed or the system
    keyring service is unavailable.

    Args:
        use_keyring: Whether to attempt using the system keyring.  Defaults
                     to ``True``.  Set to ``False`` in tests.
        credentials_file: Override the path to the fallback credential file.
    """

    def __init__(
        self,
        *,
        use_keyring: bool = True,
        credentials_file: Path | None = None,
    ) -> None:
        self._use_keyring = use_keyring
        self._credentials_file = credentials_file or _CREDENTIALS_FILE
        self._keyring_available: bool | None = None  # lazy check

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def store(self, service: str, key: str, value: str) -> None:
        """Store a credential in the system keyring (or fallback file).

        Args:
            service: Logical service name (e.g. ``"providers"``).
            key: Credential identifier (e.g. ``"openai_api_key"``).
            value: The secret value to store.
        """
        full_service = f"{_SERVICE_PREFIX}:{service}"

        if self._use_keyring and await self._keyring_ok():
            try:
                import keyring  # type: ignore[import]

                keyring.set_password(full_service, key, value)
                logger.debug("Stored credential %s/%s in system keyring", service, key)
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("Keyring store failed, falling back to file: %s", exc)

        await self._file_store(service, key, value)

    async def get(self, service: str, key: str) -> str | None:
        """Retrieve a credential.

        Returns ``None`` if the credential does not exist.

        Args:
            service: Logical service name.
            key: Credential identifier.
        """
        full_service = f"{_SERVICE_PREFIX}:{service}"

        if self._use_keyring and await self._keyring_ok():
            try:
                import keyring  # type: ignore[import]

                value = keyring.get_password(full_service, key)
                if value is not None:
                    return value
            except Exception as exc:  # noqa: BLE001
                logger.warning("Keyring get failed, falling back to file: %s", exc)

        return await self._file_get(service, key)

    async def delete(self, service: str, key: str) -> None:
        """Delete a credential.

        Silently succeeds if the credential does not exist.

        Args:
            service: Logical service name.
            key: Credential identifier.
        """
        full_service = f"{_SERVICE_PREFIX}:{service}"

        if self._use_keyring and await self._keyring_ok():
            try:
                import keyring  # type: ignore[import]

                keyring.delete_password(full_service, key)
                logger.debug("Deleted credential %s/%s from system keyring", service, key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Keyring delete failed: %s", exc)

        # Also clean up file fallback in case both were used
        await self._file_delete(service, key)

    async def list_keys(self, service: str) -> list[str]:
        """List all credential keys stored for a service.

        Note: The system keyring API does not support listing; this returns
        keys from the fallback file only.

        Args:
            service: Logical service name.

        Returns:
            List of key names.
        """
        data = await self._file_load()
        return list(data.get(service, {}).keys())

    # ------------------------------------------------------------------
    # Keyring availability check
    # ------------------------------------------------------------------

    async def _keyring_ok(self) -> bool:
        """Return ``True`` if the system keyring is usable."""
        if self._keyring_available is None:
            try:
                import keyring  # type: ignore[import]
                import keyring.errors  # type: ignore[import]

                # A minimal test: attempt to get a non-existent key
                keyring.get_password("blink:probe", "__probe__")
                self._keyring_available = True
            except Exception:  # noqa: BLE001
                self._keyring_available = False
                logger.info("System keyring not available — using file fallback")

        return bool(self._keyring_available)

    # ------------------------------------------------------------------
    # File-based fallback
    # ------------------------------------------------------------------

    async def _file_store(self, service: str, key: str, value: str) -> None:
        data = await self._file_load()
        if service not in data:
            data[service] = {}
        data[service][key] = _obfuscate(value)
        await self._file_save(data)
        logger.debug("Stored credential %s/%s in file fallback", service, key)

    async def _file_get(self, service: str, key: str) -> str | None:
        data = await self._file_load()
        obfuscated = data.get(service, {}).get(key)
        if obfuscated is None:
            return None
        try:
            return _deobfuscate(obfuscated)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not decode credential %s/%s: %s", service, key, exc)
            return None

    async def _file_delete(self, service: str, key: str) -> None:
        data = await self._file_load()
        if service in data and key in data[service]:
            del data[service][key]
            if not data[service]:
                del data[service]
            await self._file_save(data)

    async def _file_load(self) -> dict[str, Any]:
        if not self._credentials_file.exists():
            return {}
        try:
            return json.loads(self._credentials_file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not load credentials file: %s", exc)
            return {}

    async def _file_save(self, data: dict[str, Any]) -> None:
        self._credentials_file.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically via a temp file
        tmp = self._credentials_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2))
            tmp.chmod(0o600)
            tmp.replace(self._credentials_file)
        except OSError as exc:
            logger.error("Could not save credentials file: %s", exc)
            tmp.unlink(missing_ok=True)
            raise


# ---------------------------------------------------------------------------
# Obfuscation helpers (NOT cryptographic security)
# ---------------------------------------------------------------------------


def _machine_key() -> bytes:
    """Derive a simple machine-specific key for obfuscation.

    Uses the hostname + user ID to produce a deterministic 32-byte key.
    This is obfuscation, not encryption — do not rely on it for security.
    """
    import socket

    try:
        hostname = socket.gethostname()
    except Exception:  # noqa: BLE001
        hostname = "localhost"

    uid = str(os.getuid()) if hasattr(os, "getuid") else "0"
    raw = f"blink-cred-{hostname}-{uid}".encode()
    return hashlib.sha256(raw).digest()


def _obfuscate(value: str) -> str:
    """XOR-obfuscate *value* and return a base64-encoded string."""
    key = _machine_key()
    data = value.encode("utf-8")
    xored = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return base64.b64encode(xored).decode("ascii")


def _deobfuscate(obfuscated: str) -> str:
    """Reverse :func:`_obfuscate`."""
    key = _machine_key()
    data = base64.b64decode(obfuscated.encode("ascii"))
    xored = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return xored.decode("utf-8")


__all__ = ["CredentialStore"]
