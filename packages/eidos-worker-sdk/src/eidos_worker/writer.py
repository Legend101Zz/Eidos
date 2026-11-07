"""Atomic, bounded staging of output files."""

from __future__ import annotations

import csv
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union

from eidos_wire import OutputEntry, canonical_json
from eidos_worker.errors import ContractError

OUTPUT_DIR = "outputs"
SLOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")
_EXT = {
    "application/json": ".json",
    "application/jsonl": ".jsonl",
    "text/csv": ".csv",
    "text/plain": ".txt",
    "application/octet-stream": ".bin",
}
ROLES = ("output", "checkpoint", "model-export", "log", "context")


class ArtifactWriter:
    """Stage outputs under ``staging_dir/outputs`` and remember an :class:`OutputEntry` per slot.

    Each file is written to a temporary name and renamed into place, so a crash never leaves a
    half-written file under a recorded name. Slots are unique, names are restricted to
    ``[A-Za-z0-9._-]`` (no separators, no leading dot), symlinks are rejected (both as sources
    and as the output directory) and the total staged size is bounded by
    ``limits["max_output_bytes"]``.
    """

    def __init__(self, staging_dir: Union[str, Path], limits: Optional[Mapping[str, Any]] = None):
        self.staging_dir = Path(staging_dir)
        self.limits = dict(limits or {})
        self._max = self.limits.get("max_output_bytes")
        self._entries: Dict[str, OutputEntry] = {}
        self._out = self.staging_dir / OUTPUT_DIR
        if self._out.is_symlink():
            raise ContractError("outputs directory must not be a symlink")
        self._out.mkdir(parents=True, exist_ok=True)

    @property
    def entries(self) -> List[OutputEntry]:
        return list(self._entries.values())

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e in self._entries.values())

    # -- public staging API ------------------------------------------------------------------
    def stage_json(
        self, slot: str, value: Any, *, schema: Optional[str] = None, role: str = "output"
    ) -> OutputEntry:
        """Stage ``value`` as canonical JSON (sorted keys; NaN/Infinity rejected)."""
        try:
            data = canonical_json(value).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ContractError(f"slot {slot!r}: value is not strict JSON: {exc}") from exc
        return self.stage_file(slot, data, media_type="application/json", schema=schema, role=role)

    def stage_rows(
        self,
        slot: str,
        rows: Iterable[Mapping[str, Any]],
        *,
        format: str = "jsonl",
        fieldnames: Optional[Sequence[str]] = None,
        schema: Optional[str] = None,
        role: str = "output",
    ) -> OutputEntry:
        """Stage records as jsonl or csv, streaming. csv columns default to the first row's keys."""
        if format not in ("jsonl", "csv"):
            raise ContractError("format must be 'jsonl' or 'csv'")
        media = "application/jsonl" if format == "jsonl" else "text/csv"
        self._check_slot(slot)

        def produce(stream) -> None:
            if format == "jsonl":
                for row in rows:
                    stream.write(canonical_json(dict(row)) + "\n")
                return
            csv_writer = None
            for row in rows:
                if csv_writer is None:
                    names = list(fieldnames) if fieldnames is not None else list(row)
                    csv_writer = csv.DictWriter(stream, fieldnames=names, lineterminator="\n")
                    csv_writer.writeheader()
                csv_writer.writerow(row)
            if csv_writer is None and fieldnames is not None:
                csv.DictWriter(stream, fieldnames=list(fieldnames), lineterminator="\n").writeheader()

        return self._commit(slot, produce, media, schema, role, text=True)

    def stage_file(
        self,
        slot: str,
        source: Union[str, Path, bytes],
        *,
        media_type: str = "application/octet-stream",
        schema: Optional[str] = None,
        role: str = "output",
    ) -> OutputEntry:
        """Stage bytes or copy a regular (non-symlink) file."""
        self._check_slot(slot)
        if isinstance(source, (bytes, bytearray, memoryview)):
            data = bytes(source)
            if role != "log":
                self._check_budget(len(data))

            def produce(stream) -> None:
                stream.write(data)

        else:
            src = Path(source)
            if src.is_symlink() or not src.is_file():
                raise ContractError(f"slot {slot!r}: source {src} must be a regular file and not a symlink")
            if role != "log":
                self._check_budget(src.stat().st_size)

            def produce(stream) -> None:
                with open(src, "rb") as handle:
                    shutil.copyfileobj(handle, stream, 1 << 20)

        return self._commit(slot, produce, media_type, schema, role, text=False)

    # -- internals ---------------------------------------------------------------------------
    def _check_slot(self, slot: str) -> None:
        if not isinstance(slot, str) or not SLOT_RE.match(slot) or ".." in slot:
            raise ContractError(f"unsafe slot name {slot!r}")
        if slot in self._entries:
            raise ContractError(f"slot {slot!r} already staged")

    def _check_budget(self, extra: int) -> None:
        if self._max is not None and self.total_bytes + extra > self._max:
            raise ContractError(
                f"output limit exceeded: {self.total_bytes + extra} bytes > max_output_bytes={self._max}"
            )

    def _commit(self, slot, produce, media_type, schema, role, text: bool) -> OutputEntry:
        if role not in ROLES:
            raise ContractError(f"unknown role {role!r}; expected one of {ROLES}")
        self._check_slot(slot)
        if self._out.is_symlink():
            raise ContractError("outputs directory must not be a symlink")
        ext = _EXT.get(media_type, ".bin")
        final = self._out / f"{slot}{ext}"
        if final.exists() or final.is_symlink():
            raise ContractError(f"output file for slot {slot!r} already exists")
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(self._out))
        tmp = Path(tmp_name)
        try:
            stream: Any = os.fdopen(fd, "w", encoding="utf-8", newline="") if text else os.fdopen(fd, "wb")
            with stream:
                produce(stream)
            if role != "log":  # logs are tail-truncated by the runner and exempt from the budget
                self._check_budget(tmp.stat().st_size)
            from eidos_wire import sha256_file

            digest, size = sha256_file(tmp)
            os.replace(tmp, final)
        except BaseException:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise
        entry = OutputEntry(
            slot=slot,
            filename=f"{OUTPUT_DIR}/{final.name}",
            digest=digest,
            size=size,
            media_type=media_type,
            schema=schema,
            role=role,
        )
        self._entries[slot] = entry
        return entry
