"""PyGlove search-space and transform adapter (worker side).

``SearchSpace`` wraps a PyGlove hyper value (a symbolic builder with ``pg.oneof`` and friends) and
enumerates or samples ``Candidate`` objects. A ``Candidate`` is one fully materialized symbolic
object: ``build()`` calls the symbolic builder and returns a fresh torch module, ``transform()``
applies ``pg.patch`` rules on a deep clone and records them, and ``revision()`` returns the stable
identity the Eidos receipt carries (``transform_revision``).

``export()`` writes a candidate to a JSON file and ``load()`` reads it back. ``load()`` treats the
file as untrusted: it checks the transform revision, refuses marshalled code and any type that is
not inside the modules the caller explicitly allows, and only then lets PyGlove deserialize it
(``auto_import=False``). ``builder_for_eidos()`` wraps that in a callable usable as an Eidos
builder.

pyglove is imported lazily and only after ``check_pyglove_version()`` passes. torch is never
imported by this module: the built module comes from the researcher's own symbolic builder.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import importlib
import json
import os
import re
import tempfile
from importlib import metadata
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

SUPPORTED_PYGLOVE = (">=0.4.5", "<0.5")
SCHEMA = "eidos-pyglove-builder/1"

_MAX_EXPORT_BYTES = 16 * 1024 * 1024
_TYPE_KEY = "_type"
# PyGlove's own tags for plain references (classes, functions, class methods) inside JSON.
_REFERENCE_TAGS = ("type", "function", "method")
# Never loadable, whatever the caller allows: PyGlove's pickle-backed opaque value wrapper.
_DENIED_TYPE_PARTS = ("_OpaqueObject",)
# Modules that may not appear in ``allowed_modules`` at all: allowing them would let an export
# name arbitrary callables (os.system, builtins.eval, pickle.loads, torch.load, ...).
_FORBIDDEN_ALLOWED = frozenset(
    {
        "builtins", "os", "posix", "nt", "sys", "subprocess", "importlib", "pickle", "marshal",
        "shutil", "socket", "ctypes", "runpy", "code", "pty", "multiprocessing", "threading",
        "asyncio", "signal", "pathlib", "io", "pyglove",
    }
)  # fmt: skip
# Top-level packages too broad to allow as a whole (torch.load, torch.hub, ...); submodules are fine.
_FORBIDDEN_BARE = frozenset({"torch"})
_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


class PyGloveAdapterError(Exception):
    """Base class of every error raised on purpose by this adapter."""


class UnsupportedPyGloveError(PyGloveAdapterError):
    """pyglove is missing or outside ``SUPPORTED_PYGLOVE``."""


class PatchError(PyGloveAdapterError):
    """A transform patch is not recordable or could not be applied."""


class ExportIntegrityError(PyGloveAdapterError):
    """The export file is malformed, tampered with, or does not match the expected revision."""


class DisallowedTypeError(PyGloveAdapterError):
    """The export names a type, function or code object outside the allowed modules."""


# --------------------------------------------------------------------------------------------
# pyglove version pin
# --------------------------------------------------------------------------------------------

_RELEASE = re.compile(r"^[0-9]+(\.[0-9]+)*(\.post[0-9]+)?$")
_SPECIFIER = re.compile(r"^(>=|<=|==|!=|>|<)\s*([0-9]+(?:\.[0-9]+)*)$")


def _parse_version(text: str) -> Tuple[int, ...]:
    if not isinstance(text, str) or not _RELEASE.fullmatch(text):
        # pre-release, dev and local versions are not pinned releases
        raise ValueError(f"not a plain release version: {text!r}")
    return tuple(int(p) for p in text.split(".post")[0].split("."))


def _compare(a: Tuple[int, ...], b: Tuple[int, ...]) -> int:
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    return (a > b) - (a < b)


def version_supported(version: str) -> bool:
    """True when ``version`` is a plain release inside ``SUPPORTED_PYGLOVE``."""
    try:
        parsed = _parse_version(version)
    except ValueError:
        return False
    for spec in SUPPORTED_PYGLOVE:
        match = _SPECIFIER.fullmatch(spec)
        if match is None:  # pragma: no cover - the constant above is ours
            raise AssertionError(f"bad specifier {spec!r}")
        op, bound = match.group(1), _parse_version(match.group(2))
        cmp = _compare(parsed, bound)
        ok = {">=": cmp >= 0, "<=": cmp <= 0, ">": cmp > 0, "<": cmp < 0, "==": cmp == 0, "!=": cmp != 0}[op]
        if not ok:
            return False
    return True


def installed_pyglove_version() -> Optional[str]:
    """The installed pyglove distribution version, or None when pyglove is not installed."""
    try:
        return metadata.version("pyglove")
    except metadata.PackageNotFoundError:
        try:  # vendored or path-installed copies without distribution metadata
            import pyglove

            return getattr(pyglove, "__version__", None)
        except ImportError:
            return None


def check_pyglove_version(version: Optional[str] = None) -> str:
    """Return the pyglove version in use or raise ``UnsupportedPyGloveError`` outside the pin."""
    found = version if version is not None else installed_pyglove_version()
    spec = ",".join(SUPPORTED_PYGLOVE)
    if found is None:
        raise UnsupportedPyGloveError(
            f"pyglove is not installed; eidos-pyglove needs pyglove{spec} (pip install 'pyglove{spec}')"
        )
    if not version_supported(found):
        raise UnsupportedPyGloveError(
            f"pyglove {found} is outside the supported range {spec}: eidos-pyglove is pinned to the "
            f"versions it was verified against; install a supported pyglove (pip install 'pyglove{spec}')"
        )
    return found


def _pg() -> Any:
    check_pyglove_version()
    import pyglove

    return pyglove


def _adapter_id() -> str:
    import eidos_pyglove

    return f"eidos-pyglove/{eidos_pyglove.__version__}"


# --------------------------------------------------------------------------------------------
# canonical JSON and the transform revision
# --------------------------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """Deterministic JSON text: sorted keys, no whitespace, ASCII, no NaN or infinity."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PyGloveAdapterError(f"value is not canonical JSON: {exc}") from exc


def _plain(value: Any) -> Any:
    """Round-trip through canonical JSON so tuples, ints and floats match what a file reload gives."""
    return json.loads(canonical_json(value))


def transform_revision(builder_json: Any, patches: Sequence[Any], pyglove_version: str) -> str:
    """``sha256:`` over the canonical JSON of the builder, the patch log and the pyglove version."""
    payload = canonical_json(
        {"builder_json": builder_json, "patches": list(patches), "pyglove_version": pyglove_version}
    )
    return "sha256:" + hashlib.sha256(payload.encode("ascii")).hexdigest()


# --------------------------------------------------------------------------------------------
# allowed-module checks (the security gate of load())
# --------------------------------------------------------------------------------------------


def _normalize_allowed(allowed_modules: Any) -> List[str]:
    if isinstance(allowed_modules, (str, bytes)) or not isinstance(
        allowed_modules, (list, tuple, set, frozenset)
    ):
        raise TypeError("allowed_modules must be a list of module names, not a single string")
    names: List[str] = []
    for name in allowed_modules:
        if not isinstance(name, str) or not _DOTTED.fullmatch(name):
            raise ValueError(f"allowed_modules entry {name!r} is not a dotted module name")
        top = name.split(".")[0]
        if top in _FORBIDDEN_ALLOWED or name in _FORBIDDEN_BARE:
            raise ValueError(
                f"module {name!r} cannot be allowed: it would let an export name arbitrary callables; "
                "list only your own builder modules and narrow library modules such as 'torch.nn'"
            )
        if name not in names:
            names.append(name)
    if not names:
        raise ValueError("allowed_modules is empty: name the modules the export may reference")
    return names


def _within(name: str, allowed: Sequence[str]) -> bool:
    return any(name == m or name.startswith(m + ".") for m in allowed)


def _resolve_symbol(name: str, allowed: Sequence[str]) -> Any:
    """Import the longest allowed module prefix of ``name`` and walk the remaining attributes."""
    parts = name.split(".")
    for i in range(len(parts) - 1, 0, -1):
        module_name = ".".join(parts[:i])
        if not _within(module_name, allowed):
            break
        try:
            obj = importlib.import_module(module_name)
        except ImportError:
            continue
        try:
            for attr in parts[i:]:
                obj = getattr(obj, attr)
        except AttributeError as exc:
            raise DisallowedTypeError(f"{name!r} does not resolve inside the allowed modules: {exc}") from exc
        return obj
    raise DisallowedTypeError(f"{name!r} is not importable from the allowed modules {list(allowed)}")


def _typed_nodes(root: Any) -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Every dict in the JSON tree that carries a ``_type`` key, with a path for messages."""
    stack: List[Tuple[str, Any]] = [("$", root)]
    while stack:
        path, node = stack.pop()
        if isinstance(node, dict):
            if _TYPE_KEY in node:
                yield path, node
            stack.extend((f"{path}.{k}", v) for k, v in node.items())
        elif isinstance(node, list):
            stack.extend((f"{path}[{i}]", v) for i, v in enumerate(node))


def _check_types(builder_json: Any, allowed: Optional[Sequence[str]]) -> None:
    """Refuse code objects and (when ``allowed`` is given) any type outside the allowed modules."""
    for path, node in _typed_nodes(builder_json):
        tag = node[_TYPE_KEY]
        if not isinstance(tag, str):
            raise DisallowedTypeError(f"{path}: _type must be a string, got {type(tag).__name__}")
        if any(part in tag for part in _DENIED_TYPE_PARTS):
            raise DisallowedTypeError(f"{path}: type {tag!r} is never loadable (pickle-backed)")
        if tag in _REFERENCE_TAGS:
            if "code" in node:
                raise DisallowedTypeError(
                    f"{path}: {tag} reference carries marshalled code; export builders that use "
                    "module-level functions and classes, not lambdas or local functions"
                )
            name = node.get("name")
        else:
            name = tag
        if not isinstance(name, str) or not _DOTTED.fullmatch(name):
            raise DisallowedTypeError(f"{path}: {name!r} is not a dotted importable name")
        if allowed is None:
            continue
        if not _within(name, allowed):
            raise DisallowedTypeError(f"{path}: type {name!r} is outside the allowed modules {list(allowed)}")
        if tag in _REFERENCE_TAGS:
            symbol = _resolve_symbol(name, allowed)
            home = getattr(symbol, "__module__", None)
            if not isinstance(home, str) or not _within(home, allowed):
                raise DisallowedTypeError(
                    f"{path}: {name!r} resolves to an object defined in {home!r}, outside the allowed "
                    f"modules {list(allowed)}"
                )


# --------------------------------------------------------------------------------------------
# Candidate
# --------------------------------------------------------------------------------------------


def _patch_record(patch: Any) -> Any:
    """The JSON form a patch is logged (and hashed) as; only string and dict rules are recordable."""
    if isinstance(patch, str):
        return patch
    if isinstance(patch, dict):
        pg = _pg()
        try:
            return _plain(pg.to_json(patch))
        except Exception as exc:
            raise PatchError(f"dict patch {patch!r} has values that are not JSON-recordable: {exc}") from exc
    raise PatchError(
        f"unsupported patch {patch!r}: use a string rule ('name?arg=value') or a dict of path to value; "
        "callables cannot be recorded in a transform revision"
    )


class Candidate:
    """One fully materialized symbolic object plus how it was reached (DNA and patch log)."""

    def __init__(
        self,
        value: Any,
        dna: Sequence[Any] = (),
        transform_log: Sequence[Any] = (),
        pyglove_version: Optional[str] = None,
    ) -> None:
        pg = _pg()
        if not isinstance(value, pg.Symbolic):
            raise TypeError(f"a Candidate wraps a PyGlove symbolic object, got {type(value).__name__}")
        if not pg.template(value).is_constant:
            raise ValueError("the object still contains hyper primitives; materialize it before wrapping")
        self._value = value
        self._dna = list(dna)
        self._log = list(transform_log)
        self._pyglove_version = pyglove_version if pyglove_version is not None else check_pyglove_version()

    # -- inspection ------------------------------------------------------------------------
    @property
    def dna(self) -> List[Any]:
        """The decisions (flattened DNA numbers) that produced this candidate from its space."""
        return list(self._dna)

    @property
    def transform_log(self) -> List[Any]:
        """The patches applied since materialization, in order, in their JSON recorded form."""
        return copy.deepcopy(self._log)

    @property
    def pyglove_version(self) -> str:
        return self._pyglove_version

    @property
    def value(self) -> Any:
        """A deep clone of the symbolic object (mutating it does not affect this candidate)."""
        return self._value.clone(deep=True)

    @property
    def sym_hash(self) -> int:
        """PyGlove's structural hash. Process-local (Python hashing): never persist or compare it
        across processes; ``transform_revision`` is the stable identity."""
        return self._value.sym_hash()

    def builder_json(self) -> Any:
        return _plain(self._value.to_json())

    def __repr__(self) -> str:
        return f"Candidate(dna={self._dna}, patches={len(self._log)}, value={self._value!r})"

    # -- building and transforming ---------------------------------------------------------
    def build(self) -> Any:
        """Call the symbolic builder and return a freshly built module (new parameters every call)."""
        pg = _pg()
        fresh = self._value.clone(deep=True)
        if isinstance(fresh, pg.Functor):
            return fresh()
        # a symbolized class: the instance is itself the built object
        return fresh

    def transform(self, patches: Any) -> Candidate:
        """Apply ``pg.patch`` rules to a deep clone and return a new Candidate.

        ``patches`` is one rule or a list of rules; each rule is a string (``"name?arg=value"``,
        resolved against registered ``@pg.patching.patcher`` patchers) or a dict of path to value.
        Every rule is appended to ``transform_log`` and changes ``transform_revision``.
        """
        pg = _pg()
        rules = list(patches) if isinstance(patches, (list, tuple)) else [patches]
        if not rules:
            raise PatchError("no patches given")
        records = [_patch_record(rule) for rule in rules]
        working = self._value.clone(deep=True)
        for rule, record in zip(rules, records):
            try:
                working = pg.patch(working, [rule])
            except Exception as exc:
                raise PatchError(f"patch {record!r} failed: {type(exc).__name__}: {exc}") from exc
        return Candidate(working, self._dna, self._log + records)

    # -- identity --------------------------------------------------------------------------
    def revision(self) -> Dict[str, Any]:
        """The record an Eidos receipt carries for this candidate."""
        builder_json = self.builder_json()
        return {
            "adapter": _adapter_id(),
            "pyglove_version": self._pyglove_version,
            "dna": list(self._dna),
            "patches": copy.deepcopy(self._log),
            "sym_hash": self.sym_hash,
            "builder_json": builder_json,
            "transform_revision": transform_revision(builder_json, self._log, self._pyglove_version),
        }


# --------------------------------------------------------------------------------------------
# SearchSpace
# --------------------------------------------------------------------------------------------


def _count(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {value!r}")
    return value


class SearchSpace:
    """A PyGlove hyper value (symbolic builder with ``pg.oneof`` and friends) as a search space."""

    def __init__(self, hyper_value: Any) -> None:
        pg = _pg()
        if not isinstance(hyper_value, pg.Symbolic):
            raise TypeError(
                f"a SearchSpace wraps a PyGlove symbolic hyper value, got {type(hyper_value).__name__}"
            )
        if pg.template(hyper_value).is_constant:
            raise ValueError(
                "the value contains no hyper primitives (pg.oneof, pg.manyof, ...): nothing to search"
            )
        self._value = hyper_value
        self._dna_spec = pg.dna_spec(hyper_value)

    @property
    def size(self) -> Optional[int]:
        """Number of distinct candidates, or None when the space is not finite (continuous)."""
        n = self._dna_spec.space_size
        return n if isinstance(n, int) and n >= 0 else None

    def iter_candidates(self, limit: Optional[int] = None) -> Iterator[Candidate]:
        """Deterministic sweep (``pg.iter`` with ``pg.geno.Sweeping``): same order on every run."""
        pg = _pg()
        if limit is not None:
            _count("limit", limit)
        elif self.size is None:
            raise ValueError("the space is not finite; pass limit=N to take the first N candidates")
        for example, feedback in pg.iter(
            self._value, num_examples=limit, algorithm=pg.geno.Sweeping(), force_feedback=True
        ):
            yield Candidate(example, feedback.dna.to_numbers())

    def candidates(self, limit: Optional[int] = None) -> List[Candidate]:
        return list(self.iter_candidates(limit))

    def materialize(self, dna: Sequence[Any]) -> Candidate:
        """The candidate for explicit decisions (the flattened DNA numbers, e.g. ``[1, 0]``)."""
        pg = _pg()
        if isinstance(dna, (str, bytes)) or not isinstance(dna, (list, tuple)):
            raise TypeError("dna must be a list of decisions")
        numbers = list(dna)
        if any(isinstance(n, bool) or not isinstance(n, (int, float)) for n in numbers):
            raise ValueError(f"dna must contain only numbers, got {numbers!r}")
        dna_obj = pg.DNA.from_numbers(
            numbers, self._dna_spec
        )  # ValueError when too short, long or out of range
        return Candidate(pg.materialize(self._value, dna_obj), numbers)

    def sample(self, n: int, seed: int) -> List[Candidate]:
        """``n`` random candidates, reproducible from ``seed`` (``pg.random_sample`` semantics).

        Never use ``pg.sample`` here: without a feedback loop it proposes forever.
        """
        pg = _pg()
        _count("n", n)
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("seed must be an integer: unseeded samples are not reproducible")
        return [
            Candidate(example, feedback.dna.to_numbers())
            for example, feedback in pg.iter(
                self._value, num_examples=n, algorithm=pg.geno.Random(seed), force_feedback=True
            )
        ]


# --------------------------------------------------------------------------------------------
# export / load
# --------------------------------------------------------------------------------------------


def export(candidate: Candidate, path: Any) -> Dict[str, Any]:
    """Write ``candidate`` as an ``eidos-pyglove-builder/1`` JSON file; returns its revision."""
    revision = candidate.revision()
    _check_types(revision["builder_json"], None)  # refuse marshalled code at the source
    document = {"schema": SCHEMA, **revision}
    text = json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
    target = os.fspath(path)
    directory = os.path.dirname(os.path.abspath(target))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".eidos-pyglove-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(text)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return revision


def _reject_constant(token: str) -> Any:
    raise ValueError(f"non-finite number {token} in export")


def _read_export(path: Any) -> Dict[str, Any]:
    target = os.fspath(path)
    try:
        size = os.path.getsize(target)
        if size > _MAX_EXPORT_BYTES:
            raise ExportIntegrityError(
                f"export file is {size} bytes, over the {_MAX_EXPORT_BYTES} byte limit"
            )
        with open(target, "rb") as handle:
            raw = handle.read()
        document = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except ExportIntegrityError:
        raise
    except (OSError, ValueError, RecursionError) as exc:
        raise ExportIntegrityError(f"cannot read export {target!r}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(document, dict):
        raise ExportIntegrityError("export is not a JSON object")
    if document.get("schema") != SCHEMA:
        raise ExportIntegrityError(
            f"unsupported export schema {document.get('schema')!r}; expected {SCHEMA!r}"
        )
    return document


def load(path: Any, *, allowed_modules: Sequence[str], expected_revision: Optional[str] = None) -> Candidate:
    """Load an export as an untrusted file and rebuild its Candidate.

    Order of checks: schema, ``transform_revision`` recomputed from the file's own builder_json,
    patches and pyglove_version (a tampered file is refused), optional ``expected_revision`` pin,
    every ``_type`` and type/function reference against ``allowed_modules`` (marshalled code is
    always refused), and only then ``pg.from_json(..., auto_import=False)``.

    The revision hash detects accidental or naive modification; it is not authentication (anyone
    can recompute it). The allowed-module gate is what bounds what a hostile file can reference.
    """
    allowed = _normalize_allowed(allowed_modules)
    check_pyglove_version()
    document = _read_export(path)

    builder_json = document.get("builder_json")
    patches = document.get("patches")
    exported_with = document.get("pyglove_version")
    stored = document.get("transform_revision")
    dna = document.get("dna", [])
    if not isinstance(builder_json, dict) or not isinstance(patches, list):
        raise ExportIntegrityError("export lacks a builder_json object or a patches list")
    if not isinstance(stored, str) or not isinstance(exported_with, str):
        raise ExportIntegrityError("export lacks transform_revision or pyglove_version")
    if not isinstance(dna, list) or any(
        isinstance(n, bool) or not isinstance(n, (int, float, str)) for n in dna
    ):
        raise ExportIntegrityError("export dna must be a list of numbers")
    if any(not isinstance(p, (str, dict)) for p in patches):
        raise ExportIntegrityError("export patches must be strings or objects")

    computed = transform_revision(builder_json, patches, exported_with)
    if not hmac.compare_digest(computed.encode("ascii"), stored.encode("utf-8", "replace")):
        raise ExportIntegrityError(
            f"transform_revision mismatch: file says {stored!r}, contents hash to {computed!r}; "
            "the export was modified after it was written"
        )
    if expected_revision is not None and not hmac.compare_digest(
        computed.encode("ascii"), str(expected_revision).encode("utf-8", "replace")
    ):
        raise ExportIntegrityError(f"export revision {computed!r} is not the expected {expected_revision!r}")
    if not version_supported(exported_with):
        raise ExportIntegrityError(
            f"export was written with pyglove {exported_with!r}, outside the supported range "
            f"{','.join(SUPPORTED_PYGLOVE)}"
        )

    _check_types(builder_json, allowed)
    for module_name in allowed:
        try:
            importlib.import_module(module_name)
        except ImportError as exc:
            raise PyGloveAdapterError(f"allowed module {module_name!r} cannot be imported: {exc}") from exc

    pg = _pg()
    try:
        value = pg.from_json(copy.deepcopy(builder_json), auto_import=False)
    except PyGloveAdapterError:
        raise
    except Exception as exc:
        raise PyGloveAdapterError(
            f"cannot rebuild the symbolic object: {type(exc).__name__}: {exc} "
            "(is the module that defines the builder in allowed_modules and importable?)"
        ) from exc
    candidate = Candidate(value, dna, patches, pyglove_version=exported_with)
    if candidate.revision()["transform_revision"] != stored:
        raise ExportIntegrityError(
            "the rebuilt object does not serialize back to the stored builder_json; "
            "refusing a file that does not round-trip"
        )
    return candidate


def builder_for_eidos(
    path: Any, allowed_modules: Sequence[str], expected_revision: Optional[str] = None
) -> Tuple[Any, Dict[str, Any]]:
    """``(build, revision)`` for an export: ``build()`` returns a fresh module each call.

    ``build`` takes an optional ``config`` and ignores it, so it fits the Eidos named-builder
    signature ``build(config) -> model`` as well as a zero-argument call. Put the returned
    ``revision`` (its ``transform_revision`` above all) in the receipt.
    """
    candidate = load(path, allowed_modules=allowed_modules, expected_revision=expected_revision)
    revision = candidate.revision()

    def build(config: Any = None) -> Any:
        del config
        return candidate.build()

    build.revision = copy.deepcopy(revision)  # type: ignore[attr-defined]
    build.transform_revision = revision["transform_revision"]  # type: ignore[attr-defined]
    return build, revision
