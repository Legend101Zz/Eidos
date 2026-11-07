"""Read-only access to the invocation's declared input artifacts."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import IO, Any, Dict, Iterator, Union

from eidos_wire import InputBinding, sha256_file
from eidos_worker.errors import ContractError

_JSONL = ("jsonl", "ndjson")


def _hex(digest: str) -> str:
    return digest.split(":", 1)[1] if digest.startswith("sha256:") else digest


class ArtifactReader:
    """Open declared inputs by input name (or the binding obtained from ``inputs[name]``).

    The sha256 digest (and size, if declared) is verified the first time an input is touched; a
    mismatch raises :class:`ContractError`. Anything that is not a declared input — unknown names,
    foreign bindings, raw paths — is refused.
    """

    def __init__(self, inputs: Dict[str, InputBinding]):
        self._inputs = dict(inputs)
        self._verified: Dict[str, str] = {}

    def binding(self, name: Union[str, InputBinding]) -> InputBinding:
        if isinstance(name, InputBinding):
            for declared in self._inputs.values():
                if declared == name:
                    return declared
            raise ContractError("binding is not one of this invocation's declared inputs")
        if not isinstance(name, str) or name not in self._inputs:
            raise ContractError(f"{name!r} is not a declared input (declared: {sorted(self._inputs)})")
        return self._inputs[name]

    def _name_of(self, binding: InputBinding) -> str:
        return next(k for k, v in self._inputs.items() if v == binding)

    def path(self, name: Union[str, InputBinding]) -> Path:
        """Verified filesystem path of an input file."""
        binding = self.binding(name)
        key = self._name_of(binding)
        path = Path(binding.path)
        if key not in self._verified:
            if not path.is_file():
                raise ContractError(f"input {key!r}: {path} is not a regular file")
            digest, size = sha256_file(path)
            if digest != _hex(binding.digest):
                raise ContractError(
                    f"input {key!r}: digest mismatch (expected {_hex(binding.digest)}, got {digest})"
                )
            if binding.size is not None and binding.size != size:
                raise ContractError(f"input {key!r}: size mismatch (expected {binding.size}, got {size})")
            self._verified[key] = digest
        return path

    def open(self, name: Union[str, InputBinding], mode: str = "rb", **kwargs: Any) -> IO:
        if any(c in mode for c in "wax+"):
            raise ContractError("inputs are read-only")
        return open(self.path(name), mode, **kwargs)

    def json(self, name: Union[str, InputBinding]) -> Any:
        with self.open(name, "r", encoding="utf-8") as stream:
            return json.load(stream)

    def rows(self, name: Union[str, InputBinding]) -> Iterator[Dict[str, Any]]:
        """Iterate records of a csv (dict rows, string values) or jsonl input, chosen by media type
        (falling back to the file suffix)."""
        binding = self.binding(name)
        path = self.path(binding)
        media = (binding.media_type or "").lower()
        suffix = path.suffix.lower().lstrip(".")
        if "csv" in media or (media in ("", "application/octet-stream") and suffix == "csv"):
            kind = "csv"
        elif any(t in media for t in _JSONL) or suffix in _JSONL:
            kind = "jsonl"
        else:
            raise ContractError(f"input has media type {binding.media_type!r}; rows() supports csv and jsonl")
        return self._iter(path, kind)

    @staticmethod
    def _iter(path: Path, kind: str) -> Iterator[Dict[str, Any]]:
        with open(path, encoding="utf-8", newline="") as stream:
            if kind == "csv":
                yield from csv.DictReader(stream)
            else:
                for line in stream:
                    if line.strip():
                        yield json.loads(line)
