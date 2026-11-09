"""Provider keys in the OS secret store (ADR 0012).

``lab credentials set|delete|status <provider>`` store a provider key in the operating system's secret store, and
``lab start`` reads stored keys when ``--credentials-stdin`` is not given. Keys then travel exactly as before: over
the supervisor's stdin pipe, kept in its memory, and added only to the investigator worker's environment.

Backends:
- macOS Keychain through ``/usr/bin/security``. The value is written with ``security -i`` (commands read from
  stdin), so it never appears in a process's argv. Reads use ``find-generic-password -w`` over a pipe.
- Linux libsecret through ``secret-tool`` (the value is read from stdin by ``secret-tool store``).
There is no plaintext fallback: if neither is available, every command fails with a clear error.

Item names: service ``eidos/<lab id>/<provider>``, account ``<ENV VAR>`` (e.g. ``ANTHROPIC_API_KEY``). The lab id
is derived from the lab's home path, so two labs on one machine keep separate keys.

Values never appear in errors, logs, receipts or command output: every error message built here is passed
through ``redact`` with the value, and ``status`` reports presence only.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# provider name -> environment variable the investigator runtime reads (pydantic_ai_runtime: api_key_env defaults)
PROVIDERS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai-compatible": "OPENAI_API_KEY",
}
SECURITY = "/usr/bin/security"
# Printable ASCII without whitespace, at most 1024 characters: provider keys are ~100-200 characters, and
# `security -i` reads one command line of ~4 KiB, so a longer value could be cut and echoed in its error.
MAX_VALUE = 1024
_VALUE_OK = re.compile(r"^[\x21-\x7e]{1," + str(MAX_VALUE) + r"}$")


class SecretStoreError(Exception):
    """A secret-store operation failed. The message never contains a secret value."""


class SecretStoreUnavailable(SecretStoreError):
    pass


def redact(text: str, *values: str | None) -> str:
    for v in values:
        if v:
            text = text.replace(v, "[redacted]")
    return text


def lab_id(home: str | os.PathLike) -> str:
    """A stable id for a lab, derived from its resolved home directory."""
    return "lab-" + hashlib.sha256(str(Path(home).expanduser().resolve()).encode()).hexdigest()[:12]


def env_var_for(provider: str) -> str:
    try:
        return PROVIDERS[provider]
    except KeyError:
        raise SecretStoreError(f"unknown provider {provider!r}; known: {', '.join(sorted(PROVIDERS))}") from None


def check_value(value: str) -> str:
    value = value.strip()
    if not _VALUE_OK.match(value):
        # Deliberately says nothing about the value itself.
        raise SecretStoreError("the key must be 1-1024 printable ASCII characters without spaces or quotes")
    if any(c in value for c in "\"'\\"):
        raise SecretStoreError("the key must be 1-1024 printable ASCII characters without spaces or quotes")
    return value


class Backend(Protocol):
    name: str

    def get(self, service: str, account: str) -> str | None: ...
    def set(self, service: str, account: str, value: str, label: str) -> None: ...
    def delete(self, service: str, account: str) -> bool: ...


def _run(argv: list[str], *, stdin: str | None = None, value: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=30,
                              env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": os.environ.get("HOME", "/"),
                                   **{k: v for k, v in os.environ.items()
                                      if k in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "DISPLAY")}})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SecretStoreError(redact(f"{argv[0]} failed: {exc}", value)) from None


class MacKeychain:
    """macOS Keychain via /usr/bin/security; ``keychain`` selects a keychain file (tests use a throwaway one),
    otherwise the user's default keychain search list is used."""

    name = "macos-keychain"

    def __init__(self, keychain: str | None = None):
        self.keychain = keychain

    def _kc(self) -> list[str]:
        return [self.keychain] if self.keychain else []

    def get(self, service: str, account: str) -> str | None:
        r = _run([SECURITY, "find-generic-password", "-s", service, "-a", account, "-w", *self._kc()])
        if r.returncode == 44:  # errSecItemNotFound
            return None
        if r.returncode != 0:
            raise SecretStoreError(f"keychain read failed for {service} ({r.returncode}): "
                                   f"{redact(r.stderr.strip(), r.stdout.strip())[:300]}")
        return r.stdout.rstrip("\n")

    def exists(self, service: str, account: str) -> bool:
        r = _run([SECURITY, "find-generic-password", "-s", service, "-a", account, *self._kc()])
        return r.returncode == 0

    def set(self, service: str, account: str, value: str, label: str) -> None:
        kc = f' "{self.keychain}"' if self.keychain else ""
        if any(c in (service + account + label + (self.keychain or "")) for c in "\"\\\n"):
            raise SecretStoreError("service, account, label and keychain path must not contain quotes or newlines")
        # `security -i` reads the command (and so the value) from stdin: nothing secret in any argv.
        cmd = f'add-generic-password -U -s "{service}" -a "{account}" -l "{label}" -w "{value}"{kc}\n'
        r = _run([SECURITY, "-i"], stdin=cmd, value=value)
        if r.returncode != 0 or "error" in (r.stderr or "").lower() or "unknown command" in (r.stderr or "").lower():
            # security's stderr can quote the command line, i.e. parts of the value: never pass it on.
            raise SecretStoreError(f"keychain write failed for {service} (security exit {r.returncode}); "
                                   "check that the keychain exists and is unlocked")

    def delete(self, service: str, account: str) -> bool:
        r = _run([SECURITY, "delete-generic-password", "-s", service, "-a", account, *self._kc()])
        if r.returncode == 44:
            return False
        if r.returncode != 0:
            raise SecretStoreError(f"keychain delete failed for {service} ({r.returncode}): {r.stderr.strip()[:300]}")
        return True


class SecretTool:
    """Linux libsecret via ``secret-tool`` (needs a running Secret Service, e.g. gnome-keyring on a session bus)."""

    name = "libsecret"

    def __init__(self, binary: str = "secret-tool"):
        self.binary = binary

    def get(self, service: str, account: str) -> str | None:
        r = _run([self.binary, "lookup", "service", service, "account", account])
        if r.returncode != 0:
            if not r.stdout and not r.stderr.strip():
                return None  # secret-tool exits 1 with no output when nothing matches
            raise SecretStoreError(f"secret-tool lookup failed for {service}: "
                                   f"{redact(r.stderr.strip(), r.stdout.strip())[:300]}")
        return r.stdout.rstrip("\n") or None

    def set(self, service: str, account: str, value: str, label: str) -> None:
        r = _run([self.binary, "store", f"--label={label}", "service", service, "account", account],
                 stdin=value, value=value)
        if r.returncode != 0:
            raise SecretStoreError(f"secret-tool store failed for {service}: {redact(r.stderr.strip(), value)[:300]}")

    def delete(self, service: str, account: str) -> bool:
        existed = self.get(service, account) is not None
        r = _run([self.binary, "clear", "service", service, "account", account])
        if r.returncode != 0 and existed:
            raise SecretStoreError(f"secret-tool clear failed for {service}: {r.stderr.strip()[:300]}")
        return existed


class MemoryBackend:
    """In-memory backend for tests only (never selected automatically)."""

    name = "memory"

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def get(self, service: str, account: str) -> str | None:
        return self.items.get((service, account))

    def set(self, service: str, account: str, value: str, label: str) -> None:
        self.items[(service, account)] = value

    def delete(self, service: str, account: str) -> bool:
        return self.items.pop((service, account), None) is not None


def select_backend() -> Backend:
    """The OS secret store of this machine. ``EIDOS_KEYCHAIN`` names a keychain file on macOS (tests and
    operators who keep lab keys in a dedicated keychain). No plaintext fallback exists."""
    if sys.platform == "darwin" and os.path.exists(SECURITY):
        return MacKeychain(os.environ.get("EIDOS_KEYCHAIN") or None)
    tool = shutil.which("secret-tool")
    if sys.platform.startswith("linux") and tool:
        return SecretTool(tool)
    raise SecretStoreUnavailable(
        "no OS secret store available: macOS needs /usr/bin/security; Linux needs libsecret's `secret-tool` "
        "(package libsecret-tools) and a running Secret Service (e.g. gnome-keyring). Eidos never stores provider "
        "keys in plaintext; use `lab start --credentials-stdin` instead")


@dataclass
class CredentialStore:
    backend: Backend
    lab: str

    def service(self, provider: str) -> str:
        env_var_for(provider)
        return f"eidos/{self.lab}/{provider}"

    def set(self, provider: str, value: str) -> dict:
        env = env_var_for(provider)
        value = check_value(value)
        try:
            self.backend.set(self.service(provider), env, value, label=f"Eidos {self.lab} {provider} key")
        except SecretStoreError as exc:
            raise SecretStoreError(redact(str(exc), value)) from None
        return {"provider": provider, "stored": True, "env_var": env, "service": self.service(provider),
                "backend": self.backend.name}

    def delete(self, provider: str) -> dict:
        env = env_var_for(provider)
        removed = self.backend.delete(self.service(provider), env)
        return {"provider": provider, "deleted": removed, "service": self.service(provider),
                "backend": self.backend.name}

    def status(self, provider: str) -> dict:
        env = env_var_for(provider)
        exists = getattr(self.backend, "exists", None)
        stored = bool(exists(self.service(provider), env)) if exists else self.backend.get(self.service(provider), env) is not None
        return {"provider": provider, "stored": stored, "env_var": env, "service": self.service(provider),
                "backend": self.backend.name}

    def load_all(self) -> dict[str, str]:
        """NAME -> value for every provider with a stored key (for ``lab start``)."""
        out: dict[str, str] = {}
        for provider, env in PROVIDERS.items():
            if env in out:
                continue
            value = self.backend.get(self.service(provider), env)
            if value:
                out[env] = value
        return out
