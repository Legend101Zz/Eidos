"""Secret redaction for failure data (ADR 0020, addendum R10).

Failure text comes from code the lab does not trust and is stored in the ledger and shown to the investigator. Both
paths use these functions, on the whole text, before any cut: a cut can remove the name that identifies a secret.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[redacted]"

# ``tokenizer`` names no secret. The name may be prefixed or suffixed (OPENAI_API_KEY, AWS_SECRET_ACCESS_KEY). The
# lookbehind makes a match start only where a name can start, which keeps the scan linear on long runs of word characters.
_NAME = (r"(?<![\w.-])[\w.-]*?(?:api[-_]?key|access[-_]?key|private[-_]?key|client[-_]?secret|secret|password|passwd|pwd"
         r"|credential|authorization|token(?!iz))[\w.-]*")
_VALUE = (r"""\\"(?:(?!\\").)*(?:\\"|$)"""  # a double-quoted string inside a JSON string
          r"""|"(?:[^"\\\n]|\\.)*(?:"|$)"""  # a quoted string, possibly cut at the end of the line
          r"""|\\'(?:(?!\\').)*(?:\\'|$)"""
          r"""|'(?:[^'\\\n]|\\.)*(?:'|$)"""
          r"""|\{[^{}\n]*\}|\[[^\[\]\n]*\]"""  # a flat object or list
          r"""|(?:(?:bearer|basic|token)\s+)?\S+""")  # a bare value, which may start with its scheme
_SECRET = re.compile(rf"""(?im)(?P<open>\\*["'])?(?P<name>{_NAME})(?P<close>\\*["'])?(?P<sep>\s*[:=]\s*)(?P<value>{_VALUE})""")
_NAME_ONLY = re.compile(rf"(?i){_NAME}")
_KEY_LIKE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_\w{16,}|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}|eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}|Bearer\s+\S{12,})")
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL)
_URL_PASSWORD = re.compile(r"(?<=://)([^/\s:@]+):[^/\s@]+(?=@)")
_NOT_A_VALUE = ("", "true", "false", "null", "none")
MAX_DEPTH = 12


def _is_harmless(name: str, value: str) -> bool:
    """A flag or an empty value hides nothing. A number under a name that only says ``token`` is a count (max_tokens=64);
    under any other name it can be a numeric password."""
    bare = value.strip("\\\"',;)}] ").lower()
    if bare in _NOT_A_VALUE:
        return True
    only_counts = "token" in name.lower() and not re.search(r"(?i)key|secret|pass|auth|credential", name)
    return only_counts and bool(re.fullmatch(r"\d+(?:\.\d+)?", bare))


def _replace(m: re.Match[str]) -> str:
    if _is_harmless(m["name"], m["value"]):
        return m[0]
    return f"{m['open'] or ''}{m['name']}{m['close'] or ''}{m['sep']}{REDACTED}"


def redact_text(text: str) -> str:
    """``text`` with secrets replaced: name-value pairs, quoted (JSON, Python) or bare, and credential-shaped strings."""
    text = _PEM.sub(REDACTED, text)
    text = _URL_PASSWORD.sub(lambda m: f"{m[1]}:{REDACTED}", text)
    return _KEY_LIKE.sub(REDACTED, _SECRET.sub(_replace, text))


def redact_value(value: Any, depth: int = 0) -> Any:
    """A structure with the same shape, safe to store: values under secret-named keys and secrets inside strings go."""
    if isinstance(value, str):
        return redact_text(value)
    if depth >= MAX_DEPTH and isinstance(value, dict | list | tuple):
        return REDACTED
    if isinstance(value, dict):
        return {k: REDACTED if (isinstance(k, str) and _NAME_ONLY.fullmatch(k) and not _is_harmless(k, str(v))
                                and not isinstance(v, dict | list))
                else redact_value(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact_value(v, depth + 1) for v in value]
    return value
