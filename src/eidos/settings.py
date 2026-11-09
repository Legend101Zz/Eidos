"""Lab installation layout and configuration (``$EIDOS_HOME``, default ``~/.eidos``).

Configuration, credentials, service data, artifact blobs, job scratch and caches live in
distinct directories. Secrets are files with mode 0600 under ``config/secrets``; every process
reads only the token for its own role.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path

SERVICE_ROLES = {
    # principal id -> (roles, extra scopes)
    "svc-investigator": (["investigator"], []),
    "svc-workflow": (["workflow"], []),
    "svc-launcher": (["launcher"], []),
    "svc-sealed-launcher": (["launcher"], ["sealed:read"]),
    "svc-assessor": (["assessor"], []),
    "svc-qualifier": (["qualifier"], []),
    "svc-reporter": (["reader"], []),
    "svc-llm-broker": (["llm-broker"], []),
}


TEMPORAL_PORT_BLOCK = 9
# Temporal Server records each service's membership port in a PostgreSQL smallint column, so every port of
# its block must be <= 32767; a higher block makes the server exit at startup ("out of range for type smallint").
TEMPORAL_MAX_PORT = 32767


def check_ports(api_port: int, pg_port: int, temporal_port: int) -> None:
    """Refuse port choices the lab's services cannot use, before anything is created."""
    for name, port in (("api", api_port), ("pg", pg_port), ("temporal", temporal_port)):
        if not 1024 <= int(port) <= 65535:
            raise ValueError(f"--{name}-port {port} must be between 1024 and 65535")
    last = int(temporal_port) + TEMPORAL_PORT_BLOCK - 1
    if last > TEMPORAL_MAX_PORT:
        raise ValueError(f"--temporal-port {temporal_port}: Temporal uses {TEMPORAL_PORT_BLOCK} consecutive ports "
                         f"({temporal_port}-{last}) and stores them as PostgreSQL smallint, so the block must end at or "
                         f"below {TEMPORAL_MAX_PORT}; choose a temporal port <= {TEMPORAL_MAX_PORT - TEMPORAL_PORT_BLOCK + 1}")
    block = range(int(temporal_port), last + 1)
    clash = [n for n, port in (("api", api_port), ("pg", pg_port)) if int(port) in block]
    if clash:
        raise ValueError(f"--{clash[0]}-port lies inside the Temporal port block {temporal_port}-{last}")


@dataclass
class LabConfig:
    home: str
    api_host: str = "127.0.0.1"
    api_port: int = 8765
    pg_port: int = 5544
    pg_bin: str = ""
    temporal_port: int = 7633
    temporal_bin: str = ""
    temporal_namespace: str = "default"
    db_name: str = "eidos"
    db_prefix: str = "eidos"
    lab_python: str = ""
    min_free_bytes: int = 512 << 20
    history_limit: int = 1500
    turns_per_run: int = 20
    idle_poll_seconds: int = 60
    sealed_available: bool = False
    sealed_profile: str = ""  # linux:bwrap-sealed | macos:seatbelt-sealed | "" (none)
    version: str = "0.1.0"
    extra: dict = field(default_factory=dict)

    # -- paths -------------------------------------------------------------------------------
    @property
    def root(self) -> Path:
        return Path(self.home)

    def p(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    @property
    def api_url(self) -> str:
        return f"http://{self.api_host}:{self.api_port}"

    @property
    def temporal_target(self) -> str:
        return f"127.0.0.1:{self.temporal_port}"

    def save(self) -> None:
        self.p("config").mkdir(parents=True, exist_ok=True)
        path = self.p("config", "lab.json")
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=True))

    @staticmethod
    def load(home: str | os.PathLike | None = None) -> LabConfig:
        home = str(home or os.environ.get("EIDOS_HOME") or Path.home() / ".eidos")
        data = json.loads((Path(home) / "config" / "lab.json").read_text())
        data["home"] = home
        return LabConfig(**{k: v for k, v in data.items() if k in LabConfig.__dataclass_fields__})

    # -- secrets -----------------------------------------------------------------------------
    def secret_path(self, name: str) -> Path:
        return self.p("config", "secrets", name)

    def write_secret(self, name: str, value: str) -> None:
        d = self.p("config", "secrets")
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        path = d / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(value)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)

    def read_secret(self, name: str) -> str:
        return self.secret_path(name).read_text().strip()

    def token(self, principal: str) -> str:
        return self.read_secret(f"{principal}.token")

    def db_secrets(self) -> dict:
        return json.loads(self.read_secret("db.json"))
