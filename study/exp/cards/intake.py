"""Explicit, credential-free intake of untrusted PMLB TSV and dense OpenML ARFF."""

import argparse
import csv
import gzip
import io
import json
import re
import shlex
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

import numpy as np

from .common import read_json, sha, write_json

CACHE_ROOT = Path("/Volumes/Mrigesh SSD/eidos-v1/improvement/cache")
PMLB_BASE = "https://github.com/EpistasisLab/pmlb/raw/master/datasets"
SUMMARY_URL = "https://github.com/EpistasisLab/pmlb/raw/master/pmlb/all_summary_stats.tsv"
OPENML_BASE = "https://www.openml.org/api/v1/json/data"
HOSTS = {"github.com", "raw.githubusercontent.com", "media.githubusercontent.com", "www.openml.org",
         "openml.org", "api.openml.org", "data.openml.org"}


def _check_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in HOSTS or parsed.username or parsed.password:
        raise ValueError("download URL is outside the allowed HTTPS data hosts")


class _Redirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url):
    _check_url(url)
    request = Request(url, headers={"User-Agent": "eidos-card-intake"})
    with build_opener(_Redirects()).open(request, timeout=60) as response:
        return response.read()


def cache_directory(cache_root, source, key, allow_root=False):
    root = Path(cache_root).resolve()
    if not allow_root and not root.is_relative_to(CACHE_ROOT):
        raise ValueError("cache root must be inside the improvement cache (or use --allow-root)")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(key)) or str(key) in {".", ".."}:
        raise ValueError("invalid source key")
    destination = root / "raw" / source / str(key)
    if source not in {"pmlb", "openml"}:
        raise ValueError("unsupported source")
    for parent in (destination, *destination.parents):
        if (parent / ".git").exists():
            raise ValueError("cache root is inside a git work tree")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("intake directory must be new or empty")
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def _table(header, rows, target, nominal=None):
    if target not in header or len(set(header)) != len(header) or not rows:
        raise ValueError("missing/ambiguous target or empty table")
    if any(len(row) != len(header) for row in rows):
        raise ValueError("ragged table")
    a = np.asarray(rows, dtype=str)
    target_index = header.index(target)
    columns, encodings = [], {}
    for i, name in enumerate(header):
        if i == target_index:
            continue
        values = a[:, i]
        if any(value in {"?", "", "NaN", "nan"} for value in values):
            raise ValueError("missing feature values are unsupported")
        try:
            numeric = values.astype(np.float64)
        except ValueError:
            if not nominal or name not in nominal:
                raise ValueError("nonnumeric feature without a declared nominal vocabulary") from None
            labels = nominal[name]
            codes = {label: code for code, label in enumerate(labels)}
            if any(value not in codes for value in values):
                raise ValueError("value outside declared ARFF nominal vocabulary") from None
            numeric = np.asarray([codes[value] for value in values])
            encodings[name] = labels
        columns.append(numeric)
    x = np.asarray(columns, dtype=np.float32).T
    if not np.isfinite(x).all():
        raise ValueError("nonfinite features")
    return {"x": x, "y": a[:, target_index], "columns": [h for h in header if h != target],
            "target": target, "encodings": encodings}


def parse_pmlb(data):
    rows = list(csv.reader(io.StringIO(gzip.decompress(data).decode("utf-8")), delimiter="\t"))
    return _table(rows[0], rows[1:], "target")


def parse_arff(data, target):
    """Dense numeric/nominal ARFF only. Reject sparse, string, date and relational types."""
    header, rows, in_data = [], [], False
    nominal = {}
    for raw in data.decode("utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("%"):
            continue
        if in_data:
            if line.startswith("{"):
                raise ValueError("sparse ARFF is unsupported")
            # ARFF permits both quoting styles; shlex handles quoted commas before CSV parsing.
            lexer = shlex.shlex(line, posix=True)
            lexer.whitespace = ","
            lexer.whitespace_split = True
            lexer.commenters = "%"
            rows.append([value.strip() for value in lexer])
        elif line.lower().startswith("@attribute "):
            parts = shlex.split(line, comments=False)
            if len(parts) < 3:
                raise ValueError("malformed ARFF attribute")
            dtype = " ".join(parts[2:]).lower()
            if dtype not in {"numeric", "real", "integer"} and not (dtype.startswith("{") and dtype.endswith("}")):
                raise ValueError("unsupported ARFF attribute type")
            header.append(parts[1])
            if dtype.startswith("{"):
                declaration = line[line.index("{") + 1:line.rindex("}")]
                lexer = shlex.shlex(declaration, posix=True)
                lexer.whitespace = ","
                lexer.whitespace_split = True
                lexer.commenters = ""
                nominal[parts[1]] = [value.strip() for value in lexer]
        elif line.lower() == "@data":
            in_data = True
        elif not line.lower().startswith("@relation "):
            raise ValueError("unsupported ARFF declaration")
    return _table(header, rows, target, nominal)


def intake(source, key, cache_root, *, allow_root=False):
    destination = cache_directory(cache_root, source, key, allow_root)
    urls, files = [], {}

    def fetch(name, url):
        data = download(url)
        (destination / name).write_bytes(data)
        urls.append(url)
        files[name] = sha(data)
        return data

    if source == "pmlb":
        base = f"{PMLB_BASE}/{quote(str(key), safe='')}"
        table = parse_pmlb(fetch("data.tsv.gz", f"{base}/{quote(str(key), safe='')}.tsv.gz"))
        metadata = fetch("metadata.yaml", f"{base}/metadata.yaml").decode("utf-8")
        # Keep YAML as original evidence; never load YAML objects or execute tags.
        licence = next((s.partition(":")[2].strip() for s in metadata.splitlines()
                        if s.strip().lower().startswith(("license:", "licence:"))), "unspecified in metadata")
        licence_url = ""
        terms = "PMLB metadata retained verbatim; source-specific licence/terms require review before redistribution."
    else:
        description = json.loads(fetch("metadata.json", f"{OPENML_BASE}/{quote(str(key), safe='')}"))[
            "data_set_description"]
        target = description["default_target_attribute"]
        if "," in target:
            raise ValueError("multi-target OpenML data is unsupported")
        url = description.get("url")
        if not url or url.lower().endswith((".parquet", ".csv")):
            raise ValueError("OpenML source must supply a dense ARFF URL")
        table = parse_arff(fetch("data.arff", url), target)
        licence = description.get("licence", "unspecified")
        licence_url = description.get("licence_url", "")
        terms = "OpenML source metadata retained; licence string is not permission to redistribute."
    source_info = dict(source=source, key=str(key), urls=urls,
                       retrieved_utc=datetime.now(UTC).isoformat(), sha256=files,
                       n_rows=len(table["y"]), n_cols=table["x"].shape[1], target=table["target"],
                       licence_string=licence, licence_url=licence_url, terms_note=terms)
    write_json(destination / "SOURCE.json", source_info)
    return destination


def load_table(directory):
    directory = Path(directory)
    metadata = read_json(directory / "SOURCE.json")
    for name, digest in metadata["sha256"].items():
        if Path(name).name != name or sha((directory / name).read_bytes()) != digest:
            raise ValueError("raw source digest mismatch")
    if metadata["source"] == "pmlb":
        return parse_pmlb((directory / "data.tsv.gz").read_bytes())
    return parse_arff((directory / "data.arff").read_bytes(), metadata["target"])


def summary(cache_root, *, allow_root=False):
    """Summary acquisition shares exactly the intake root and network restrictions."""
    directory = Path(cache_root).resolve() / "raw/pmlb/_summary"
    if (directory / "SOURCE.json").exists():
        metadata = read_json(directory / "SOURCE.json")
        data = (directory / "all_summary_stats.tsv").read_bytes()
        if sha(data) != metadata["sha256"]["all_summary_stats.tsv"]:
            raise ValueError("summary cache digest mismatch")
        # Apply root/git validation even on cache hits, without the nonempty refusal.
        root = Path(cache_root).resolve()
        if not allow_root and not root.is_relative_to(CACHE_ROOT):
            raise ValueError("cache root outside allowed root")
        if any((p / ".git").exists() for p in (directory, *directory.parents)):
            raise ValueError("cache root is inside a git work tree")
        return data
    directory = cache_directory(cache_root, "pmlb", "_summary", allow_root)
    data = download(SUMMARY_URL)
    (directory / "all_summary_stats.tsv").write_bytes(data)
    write_json(directory / "SOURCE.json", dict(source="pmlb", key="_summary", urls=[SUMMARY_URL],
               retrieved_utc=datetime.now(UTC).isoformat(), sha256={"all_summary_stats.tsv": sha(data)}))
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=["pmlb", "openml"])
    parser.add_argument("key")
    parser.add_argument("--cache", required=True)
    parser.add_argument("--allow-root", action="store_true")
    args = parser.parse_args()
    print(intake(args.source, args.key, args.cache, allow_root=args.allow_root))


if __name__ == "__main__":
    main()
