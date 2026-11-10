"""HTTP client for the lab API. Used by the CLI, the MCP adapter and every worker process; none
of them hold database credentials."""

from __future__ import annotations

import hashlib
import time
import uuid
from pathlib import Path
from typing import Any

import httpx


class LabError(Exception):
    def __init__(self, status: int, error: dict[str, Any]):
        super().__init__(f"{error.get('code')}: {error.get('message')}")
        self.status = status
        self.error = error
        self.code = error.get("code")


class LabClient:
    def __init__(self, url: str, token: str, *, client_name: str = "eidos-client", timeout: float = 60.0):
        self.url = url.rstrip("/")
        self.http = httpx.Client(base_url=self.url, timeout=timeout,
                                 headers={"authorization": f"Bearer {token}", "x-eidos-client": client_name})

    def close(self) -> None:
        self.http.close()

    def _handle(self, r: httpx.Response) -> Any:
        if r.status_code >= 400:
            try:
                err = r.json().get("error") or {"code": "HTTP_" + str(r.status_code), "message": r.text[:500]}
            except Exception:
                err = {"code": "HTTP_" + str(r.status_code), "message": r.text[:500]}
            raise LabError(r.status_code, err)
        return r.json()

    def _post(self, path: str, body: dict, retries: int = 6) -> Any:
        """Transport retries are safe: commands are idempotent by command key."""
        delay = 0.5
        for i in range(retries + 1):
            try:
                return self._handle(self.http.post(path, json=body))
            except LabError as exc:  # quiesced for backup/upgrade: same command key is safe to resend
                if exc.code != "MAINTENANCE" or i == retries:
                    raise
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError):
                if i == retries:
                    raise
            time.sleep(delay)
            delay = min(delay * 2, 8)
        raise AssertionError("unreachable")

    def command(self, name: str, payload: dict | None = None, *, project: str | None = None,
                key: str | None = None, expected_revision: int | None = None,
                normalized_arguments: list[dict] | None = None) -> dict:
        body: dict[str, Any] = {"project_id": project, "command_key": key or uuid.uuid4().hex,
                                "payload": payload or {}, "expected_revision": expected_revision}
        if normalized_arguments:  # MCP adapter only (ADR 0017): recorded in the receipt, never validated against
            body["normalized_arguments"] = normalized_arguments
        return self._post(f"/v1/commands/{name}", body)

    def query(self, name: str, params: dict | None = None, *, project: str | None = None) -> Any:
        return self._post(f"/v1/queries/{name}", {"project_id": project, "params": params or {}})["result"]

    def health(self) -> dict:
        return self._handle(self.http.get("/v1/health"))

    def whoami(self) -> dict:
        return self._handle(self.http.get("/v1/whoami"))

    def upload(self, project: str, path: str | Path | None = None, *, data: bytes | None = None,
               media_type: str = "application/octet-stream", schema: str | None = None, role: str = "input",
               access_class: str = "project", name: str | None = None, key: str | None = None,
               investigation: str | None = None) -> dict:
        if data is not None:
            content = data
        elif path is not None:
            content = Path(path).read_bytes()
        else:  # Path(None) raised TypeError before; same type, clearer message
            raise TypeError("upload() needs a path or data")
        headers = {"content-type": media_type, "x-eidos-role": role, "x-eidos-access-class": access_class,
                   "x-eidos-sha256": hashlib.sha256(content).hexdigest(),
                   "x-eidos-command-key": key or "upload:" + hashlib.sha256(content).hexdigest()[:32] + ":" + (name or "")}
        if schema:
            headers["x-eidos-schema"] = schema
        if name:
            headers["x-eidos-name"] = name
        if investigation:
            headers["x-eidos-investigation"] = investigation
        return self._handle(self.http.post(f"/v1/projects/{project}/artifacts", content=content, headers=headers))

    def download(self, project: str, artifact_id: str, dest: str | Path) -> Path:
        dest = Path(dest)
        with self.http.stream("GET", f"/v1/projects/{project}/artifacts/{artifact_id}/content") as r:
            if r.status_code >= 400:
                r.read()
                self._handle(r)
            with open(dest, "wb") as fh:
                for chunk in r.iter_bytes():
                    fh.write(chunk)
        return dest
