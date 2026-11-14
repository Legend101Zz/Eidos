"""Explicit, credential-free intake of untrusted PMLB TSV and dense OpenML ARFF."""

import argparse
import csv
import fcntl
import gzip
import io
import json
import re
import shlex
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

import numpy as np

from .common import read_json, sha, write_json

CACHE_ROOT = Path("/Volumes/Mrigesh SSD/eidos-v1/improvement/cache")
PMLB_BASE = "https://github.com/EpistasisLab/pmlb/raw/master/datasets"
SUMMARY_URL = "https://github.com/EpistasisLab/pmlb/raw/master/pmlb/all_summary_stats.tsv"
OPENML_API = "https://www.openml.org/api/v1/json"
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


def download(url, *, timeout=60):
    _check_url(url)
    request = Request(url, headers={"User-Agent": "eidos-card-intake"})
    with build_opener(_Redirects()).open(request, timeout=timeout) as response:
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
        if nominal and name in nominal:
            labels = nominal[name]
            codes = {label: code for code, label in enumerate(labels)}
            if any(value not in codes for value in values):
                raise ValueError("value outside declared ARFF nominal vocabulary") from None
            numeric = np.asarray([codes[value] for value in values])
            encodings[name] = labels
        else:
            try:
                numeric = values.astype(np.float64)
            except ValueError:
                raise ValueError("nonnumeric feature without a declared nominal vocabulary") from None
        columns.append(numeric)
    if any(value.strip().lower() in {"?", "", "nan", "inf", "-inf"} for value in a[:, target_index]):
        raise ValueError("missing/nonfinite target values are unsupported")
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


def intake(source, key, cache_root, *, allow_root=False, target=None):
    destination = cache_directory(cache_root, source, key, allow_root)
    urls, files = [], {}

    def fetch(name, url):
        if source == "openml":
            # Shared across build workers: exactly one API/data request at a time, 1 s apart.
            lock_path = destination.parent / ".request.lock"
            stamp = destination.parent / ".request.time"
            with lock_path.open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if stamp.exists():
                    time.sleep(max(0, 1 - (time.time() - float(stamp.read_text()))))
                try:
                    data = download(url)
                finally:
                    stamp.write_text(str(time.time()))
        else:
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
        target = target or description["default_target_attribute"]
        if "," in target:
            raise ValueError("multi-target OpenML data is unsupported")
        url = description.get("url")
        if not url or url.lower().endswith((".parquet", ".csv")):
            raise ValueError("OpenML source must supply a dense ARFF URL")
        table = parse_arff(fetch("data.arff", url), target)
        licence = description.get("licence", description.get("license", "unspecified"))
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



class SuiteListing:
    """Serial, paced, bounded listing requests; retain HTTP error bodies verbatim."""

    def __init__(self, cache_root, allow_root=False, max_requests=120, pause=1.0):
        self.root = Path(cache_root) / "raw/openml/_suite"
        if not self.root.exists():
            cache_directory(cache_root, "openml", "_suite", allow_root)
        else:
            root = Path(cache_root).resolve()
            if not allow_root and not root.is_relative_to(CACHE_ROOT):
                raise ValueError("cache root outside allowed root")
            if any((p / ".git").exists() for p in (root, *root.parents)):
                raise ValueError("cache root is inside a git work tree")
        self.requests = 0
        self.max_requests = max_requests
        self.pause = pause
        self.last_request = None
        self.started = time.monotonic()
        self.responses = []

    def get(self, endpoint):
        name = endpoint.strip("/").replace("/", "_")
        path = self.root / (name + ".json")
        evidence = self.root / (name + ".response.json")
        url = f"{OPENML_API}/{endpoint}"
        if path.exists() and evidence.exists():
            info = read_json(evidence)
            if sha(path.read_bytes()) != info["sha256"]:
                raise ValueError("suite cache digest mismatch")
        else:
            if self.requests >= self.max_requests or time.monotonic() - self.started >= 600:
                raise RuntimeError("OpenML listing request/time limit reached")
            if self.last_request is not None:
                time.sleep(max(0, self.pause - (time.monotonic() - self.last_request)))
            self.requests += 1
            try:
                data, status = download(url, timeout=min(60, max(0.1, 600 - (time.monotonic() - self.started)))), 200
            except HTTPError as error:
                data, status = error.read(), error.code
            path.write_bytes(data)
            info = dict(url=url, status=status, sha256=sha(data),
                        retrieved_utc=datetime.now(UTC).isoformat())
            write_json(evidence, info)
            self.last_request = time.monotonic()
        self.responses.append(info)
        if info["status"] != 200:
            raise ValueError(f"{url}: HTTP {info['status']}: {path.read_text(errors='replace')}")
        return json.loads(path.read_bytes())


def _suite_tasks(study):
    tasks = study.get("tasks", {}).get("task", study.get("tasks", {}).get("task_id", []))
    if not isinstance(tasks, list):
        tasks = [tasks]
    return [str(task.get("task_id", task.get("id"))) if isinstance(task, dict) else str(task) for task in tasks]


def openml_suite(cache_root, *, allow_root=False, pause=1.0):
    """Resolve CTR23, its tasks, declared targets, metadata and size qualities."""
    listing = SuiteListing(cache_root, allow_root, pause=pause)
    result = dict(name="OpenML-CTR23", cards=[], exclusions=[], errors=[], responses=[])
    study = None
    try:
        study = listing.get("study/353").get("study", {})
        if "ctr23" not in str(study.get("name", "")).lower():
            result["errors"].append(f"study/353 name was {study.get('name')!r}, not CTR23")
            study = None
    except (ValueError, OSError, RuntimeError) as error:
        result["errors"].append(str(error))
    if study is None:
        try:
            studies = listing.get("study/list/main_entity_type/task").get("studies", {}).get("study", [])
            if isinstance(studies, dict):
                studies = [studies]
            match = next(s for s in studies if "ctr23" in str(s.get("name", "")).lower())
            study = listing.get(f"study/{match.get('id', match.get('study_id'))}")["study"]
            if "ctr23" not in str(study.get("name", "")).lower():
                raise ValueError("resolved suite name does not mention CTR23")
        except (ValueError, KeyError, StopIteration, OSError, RuntimeError) as error:
            result["errors"].append(f"suite fallback failed: {type(error).__name__}: {error}")
            study = None
    if study is not None:
        result["returned_name"] = study.get("name")
        result["task_ids"] = _suite_tasks(study)
        for task_id in result["task_ids"]:
            try:
                task = listing.get(f"task/{task_id}")["task"]
                inputs = task.get("input", [])
                if isinstance(inputs, dict):
                    inputs = [inputs]
                dataset = next(item["data_set"] for item in inputs if "data_set" in item)
                key, target = str(dataset["data_set_id"]), dataset["target_feature"]
                description = listing.get(f"data/{key}")["data_set_description"]
                qualities = listing.get(f"data/qualities/{key}")["data_qualities"]["quality"]
                q = {item["name"]: float(item["value"]) for item in qualities if item["name"] in {"NumberOfInstances", "NumberOfFeatures"}}
                result["cards"].append(dict(source="openml", key=key, name=description["name"],
                    task_id=task_id, target=target, task_type="regression", n_classes=0,
                    n_rows=int(q["NumberOfInstances"]), n_features=int(q["NumberOfFeatures"]) - 1,
                    minority_fraction=None, licence_string=description.get("licence", description.get("license", "unspecified"))))
            except (ValueError, KeyError, StopIteration, OSError, RuntimeError) as error:
                result["exclusions"].append(dict(task_id=task_id, reason=f"{type(error).__name__}: {error}"))
    result["responses"] = listing.responses
    result["network_requests"] = listing.requests
    write_json(listing.root / "listing.json", result)
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", choices=["pmlb", "openml", "openml-suite"])
    parser.add_argument("key")
    parser.add_argument("--cache", required=True)
    parser.add_argument("--allow-root", action="store_true")
    args = parser.parse_args()
    if args.source == "openml-suite":
        print(json.dumps(openml_suite(args.cache, allow_root=args.allow_root), indent=2))
    else:
        print(intake(args.source, args.key, args.cache, allow_root=args.allow_root))


if __name__ == "__main__":
    main()
