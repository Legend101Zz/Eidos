"""``python -m eidos_worker.run <invocation.json>`` — the worker-side process entry point.

Protocol: the launcher writes an :class:`eidos_wire.Invocation` JSON file and starts this module
in the project's own interpreter. The runner imports ``component.entry`` (``module:function``),
calls ``function(inputs, config, **di)`` where ``di`` contains only the keyword arguments among
``reader``, ``writer``, ``context`` that the function's signature accepts (all three if it takes
``**kwargs``), and always finishes by writing ``eidos-output-manifest.json`` into the staging
directory. The process exits 0 whenever a manifest (succeeded *or* failed) was written; exit 2
means the invocation itself was unreadable, 1 that no manifest could be written.

If the function returns a ``dict`` it is merged into the receipt; other return values are ignored.
"""

from __future__ import annotations

import importlib
import inspect
import sys
import time
from pathlib import Path
from typing import Any, Callable, List, Optional

from eidos_wire import Invocation, OutputManifest
from eidos_worker.context import RunContext
from eidos_worker.errors import ContractError, error_record
from eidos_worker.reader import ArtifactReader
from eidos_worker.writer import ArtifactWriter

LOG_TAIL_BYTES = 256 * 1024


def _prepare_path(invocation: Invocation) -> None:
    for candidate in (invocation.component.get("source_dir"), "."):
        if candidate:
            resolved = str(Path(candidate).resolve())
            if resolved not in sys.path:
                sys.path.insert(0, resolved)


def resolve_entry(entry: str) -> Callable[..., Any]:
    module_name, _, attr = entry.partition(":")
    if not module_name or not attr:
        raise ContractError(f"entry {entry!r} must be 'module:function'")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name and (module_name == exc.name or module_name.startswith(exc.name + ".")):
            raise ContractError(f"entry module {module_name!r} not found on sys.path") from exc
        raise
    obj: Any = module
    for part in attr.split("."):
        if not hasattr(obj, part):
            raise ContractError(f"entry {entry!r}: {part!r} not found")
        obj = getattr(obj, part)
    if not callable(obj):
        raise ContractError(f"entry {entry!r} is not callable")
    return obj


def _di_kwargs(func: Callable[..., Any], available: dict) -> dict:
    params = inspect.signature(func).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(available)
    return {
        k: v
        for k, v in available.items()
        if k in params
        and params[k].kind in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    }


def execute(invocation: Invocation) -> OutputManifest:
    """Run one invocation in-process and write its manifest. Never raises for component errors."""
    started = time.time()
    staging = Path(invocation.staging_dir)
    staging.mkdir(parents=True, exist_ok=True)
    ctx: Optional[RunContext] = None
    writer: Optional[ArtifactWriter] = None
    status, error = "succeeded", None
    try:
        ctx = RunContext(invocation)
        try:  # checkpoint-and-stop protocol: the launcher sends SIGUSR1 to the process group
            import signal as _signal
            _signal.signal(_signal.SIGUSR1, lambda *_: ctx.request_stop())
        except (AttributeError, ValueError):  # no SIGUSR1 (Windows) or not the main thread
            pass
        writer = ArtifactWriter(staging, invocation.limits)
        reader = ArtifactReader(invocation.inputs)
        _prepare_path(invocation)
        func = resolve_entry(invocation.component["entry"])
        kwargs = _di_kwargs(func, {"reader": reader, "writer": writer, "context": ctx})
        result = func(dict(invocation.inputs), invocation.config, **kwargs)
        if isinstance(result, dict):
            for key, value in result.items():
                ctx.record(str(key), value)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - everything becomes a failed manifest
        status, error = "failed", error_record(exc)
        if ctx is not None:
            ctx.logger.error("run failed: %s", error["message"])
    outputs: List[Any] = []
    receipt: dict = dict(ctx.receipt) if ctx else {}
    receipt.setdefault("wall_seconds", round(time.time() - started, 3))
    if writer is not None:
        if status == "succeeded":
            outputs = writer.entries
        else:
            # Atomically staged checkpoints written before the failure are citable partial results.
            outputs = [e for e in writer.entries if e.role == "checkpoint"]
            if outputs:
                receipt["partial_outputs"] = [e.slot for e in outputs]
        if ctx is not None:
            ctx.close()
            try:
                _stage_log(writer, ctx.log_path)
                outputs = outputs + [e for e in writer.entries if e.role == "log"]
            except Exception:  # noqa: BLE001 - a log must never mask the outcome
                pass
    manifest = OutputManifest(
        job_id=invocation.job_id,
        attempt_id=invocation.attempt_id,
        epoch=invocation.epoch,
        status=status,
        outputs=_dedupe(outputs),
        receipt=receipt,
        error=error,
    )
    manifest.write(staging)
    return manifest


def _dedupe(entries: list) -> list:
    seen, result = set(), []
    for e in entries:
        if e.slot not in seen:
            seen.add(e.slot)
            result.append(e)
    return result


def _stage_log(writer: ArtifactWriter, log_path: Path) -> None:
    if not log_path.is_file():
        return
    data = log_path.read_bytes()[-LOG_TAIL_BYTES:]
    slot = "log" if "log" not in {e.slot for e in writer.entries} else "run-log"
    writer.stage_file(slot, data, media_type="text/plain", role="log")


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: python -m eidos_worker.run <invocation.json>", file=sys.stderr)
        return 2
    try:
        invocation = Invocation.read(argv[0])
    except Exception as exc:  # noqa: BLE001
        print(f"cannot load invocation: {exc}", file=sys.stderr)
        return 2
    try:
        execute(invocation)
    except Exception as exc:  # noqa: BLE001
        print(f"cannot write output manifest: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
