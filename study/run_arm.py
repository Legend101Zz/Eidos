#!/usr/bin/env python3
"""Budgeted Claude runner and the shared four-arm dispatcher."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from study.common import ENVELOPE, MODEL, agent_env, read_json, record_live_delivery, render_prompt, sandbox_command, write_json

TOKEN_KEYS = {
    "input": ("input_tokens", "inputTokens"),
    "cache_read": ("cache_read_input_tokens", "cacheReadInputTokens"),
    "cache_write": ("cache_creation_input_tokens", "cacheCreationInputTokens"),
    "output": ("output_tokens", "outputTokens"),
}


class UsageTracker:
    """Assistant events are per-message; result.usage is a cumulative run summary.

    Claude can repeat an assistant ID for multiple content blocks. Retain the maximum
    counters for each ID, then reconcile with the final cumulative usage without
    adding it twice. All four token categories count toward the envelope.
    """

    def __init__(self):
        self.messages = {}
        self.summary = {}
        self.models = set()
        self.claim = ""
        self.final_seen = False
        self.final_is_error = False
        self.stream_message_id = None

    @staticmethod
    def counters(usage):
        return {
            key: max([int(usage.get(alias, 0) or 0) for alias in aliases])
            for key, aliases in TOKEN_KEYS.items()
        }

    def observe(self, event):
        if event.get("type") == "stream_event":
            partial = event.get("event", {})
            if partial.get("type") == "message_start":
                message = partial.get("message", {})
                self.stream_message_id = message.get("id")
                self.observe({"type": "assistant", "message": message})
            elif partial.get("type") == "message_delta" and self.stream_message_id:
                counters = self.counters(partial.get("usage", {}))
                old = self.messages.get(self.stream_message_id, dict.fromkeys(TOKEN_KEYS, 0))
                self.messages[self.stream_message_id] = {
                    k: max(old.get(k, 0), v) for k, v in counters.items()
                }
            return
        message = event.get("message", {})
        if not isinstance(message, dict):
            message = {}
        # init.model is the requested alias, not a provider-returned model id.
        for model in (message.get("model"), event.get("model") if event.get("type") == "result" else None):
            if model:
                self.models.add(model)
        if event.get("type") == "assistant" and isinstance(message.get("usage"), dict):
            key = message.get("id") or event.get("uuid") or str(len(self.messages))
            counters = self.counters(message["usage"])
            old = self.messages.get(key, {})
            self.messages[key] = {k: max(old.get(k, 0), v) for k, v in counters.items()}
        if event.get("type") == "result":
            self.final_seen = True
            self.final_is_error = bool(event_error(event))
            self.claim = event.get("result", "") or ""
            if isinstance(event.get("usage"), dict):
                self.summary = self.counters(event["usage"])
            model_usage = event.get("modelUsage", {})
            if not self.models:
                self.models.update(model_usage)
            if model_usage and not self.summary:
                self.summary = {k: sum(self.counters(v)[k] for v in model_usage.values()) for k in TOKEN_KEYS}

    @property
    def tokens(self):
        counts = {
            k: max(sum(m[k] for m in self.messages.values()), self.summary.get(k, 0)) for k in TOKEN_KEYS
        }
        return {**counts, "total": sum(counts.values())}


def incident_text(text):
    return bool(
        re.search(
            r"\b429\b|(?:http|status|error|code)[\s\"':=_-]{0,12}5\d\d\b|"
            r"\b5\d\d\s+(?:internal|server|error)|internal server error|service unavailable|bad gateway|gateway timeout|"
            r"rate[ _-]?limit|overloaded|quota|insufficient.{0,20}(?:credit|balance)|"
            r"(?:credit|balance).{0,20}(?:insufficient|exhausted)|usage limit|out of credits|capacity exceeded",
            text,
            re.I,
        )
    )


def event_error(event):
    """Inspect error fields, never arbitrary scientific text or tool outputs."""
    if event.get("type") == "error" or event.get("is_error"):
        return json.dumps(event)
    if event.get("error"):
        return json.dumps(event["error"])
    if event.get("type") == "result" and event.get("subtype", "").startswith("error"):
        return json.dumps(event.get("errors", event))
    return ""


def utc_now():
    return datetime.now(UTC).isoformat()


def stop_process_tree(process):
    """Kill only our own launched process and its descendants, individually by PID."""
    rows = subprocess.check_output(["/bin/ps", "-axo", "pid=,ppid=,pgid="], text=True).splitlines()
    children = {}
    group_members = []
    for row in rows:
        pid, parent, group = map(int, row.split())
        children.setdefault(parent, []).append(pid)
        if group == process.pid:
            group_members.append(pid)
    owned = []

    def collect(pid):
        for child in children.get(pid, []):
            collect(child)
        owned.append(pid)

    collect(process.pid)
    for pid in dict.fromkeys(owned + group_members):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=10)


def run_stream(run_dir, command, env, wall_s=1800, token_limit=8_000_000):
    root = Path(run_dir).resolve()
    arm = read_json(root / "arm.json")
    started, clock = utc_now(), time.monotonic()
    usage = UsageTracker()
    status, reason = "ok", None
    last_incident = None  # pilot-2 addendum 1: provider failure seen; decided at the end
    transcript = root / "transcript.jsonl"
    process = None
    with transcript.open("w") as log:
        try:
            process = subprocess.Popen(
                command,
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            selector = selectors.DefaultSelector()
            buffers = {}
            for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                selector.register(stream, selectors.EVENT_READ, name)
                buffers[name] = b""

            def consume(line, name):
                nonlocal status, reason, last_incident
                text = line.decode("utf-8", errors="replace")
                for name_of_secret in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
                    secret = env.get(name_of_secret)
                    if secret:
                        text = text.replace(secret, "[REDACTED]")
                if not text.strip():
                    return
                if name == "stdout":
                    try:
                        event = json.loads(text)
                        if not isinstance(event, dict):
                            raise ValueError("non-object stream event")
                    except (ValueError, TypeError):
                        event = {"type": "runner_unparsed_stdout", "text": text}
                else:
                    event = {"type": "runner_stderr", "text": text}
                log.write(json.dumps(event, ensure_ascii=False) + "\n")
                log.flush()
                usage.observe(event)
                if status == "ok" and time.monotonic() - clock >= wall_s:
                    status = "timeout"
                if status == "ok" and usage.tokens["total"] > token_limit:
                    status = "token_budget"
                error = text if name == "stderr" else event_error(event)
                # Pilot-2 addendum 1: a provider 429/5xx text never stops a run while it is in progress, and the
                # client's own retry notices (system/api_retry) are ignored. The run is an incident only if it
                # then ends without a valid final result (decided after the stream ends).
                retry_notice = event.get("type") == "system" and event.get("subtype") == "api_retry"
                if error and not retry_notice and incident_text(error):
                    last_incident = error[:2000]

            while selector.get_map():
                if time.monotonic() - clock >= wall_s and status == "ok":
                    status = "timeout"
                if usage.tokens["total"] > token_limit and status == "ok":
                    status = "token_budget"
                if status != "ok":
                    stop_process_tree(process)
                    # Drain bytes already written, so a final usage receipt is retained.
                    for key in list(selector.get_map().values()):
                        rest = key.fileobj.read()
                        buffers[key.data] += rest or b""
                        selector.unregister(key.fileobj)
                    break
                for key, _ in selector.select(timeout=0.2):
                    chunk = os.read(key.fd, 65536)
                    name = key.data
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffers[name] += chunk
                    while b"\n" in buffers[name]:
                        line, buffers[name] = buffers[name].split(b"\n", 1)
                        consume(line, name)
            for name, pending in buffers.items():
                for line in pending.splitlines():
                    consume(line, name)
            selector.close()
            process.wait(timeout=10)
            stop_process_tree(process)
            if status == "ok" and usage.tokens["total"] > token_limit:
                status = "token_budget"
            # A successful final result survives recovered tool/request errors and a
            # later process-exit error. The caller still validates out/answer.json.
            if status == "ok" and (not usage.final_seen or usage.final_is_error):
                if last_incident:
                    status, reason = "incident", last_incident
                else:
                    status = "crash"
        except Exception as exc:
            if process is not None and process.poll() is None:
                stop_process_tree(process)
            status, reason = "incident", f"runner infrastructure: {type(exc).__name__}: {exc}"
            log.write(json.dumps({"type": "runner_error", "error": reason}) + "\n")
        finally:
            if process is not None:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
    record = {k: arm[k] for k in ("arm", "card", "run_index", "block_id")}
    record.update(
        status=status,
        incident_reason=reason,
        started_at=started,
        ended_at=utc_now(),
        wall_s=time.monotonic() - clock,
        tokens=usage.tokens,
        billed_usd=None,
        llm_broker_enabled=False,
        llm_broker_policy="No component LLM-broker grants in any study arm",
        model_ids_seen=sorted(usage.models),
        agent_claim=usage.claim,
        transcript=str(transcript),
        exit_code=process.returncode if process else None,
    )
    write_json(root / "run_record.json", record)
    return record


def run_claude(run_dir, arm, wall_s=None):
    root = Path(run_dir).resolve()
    metadata = read_json(root / "arm.json")
    if arm == 2:
        import shutil

        from study.toolkit.build_toolkit import build_toolkit

        build_toolkit(root)
        shutil.copyfile(Path(__file__).parent / "METHOD.md", root / "METHOD.md")
    env = agent_env(root)
    # The key exists only in this local environment, handed directly to the child.
    env["ANTHROPIC_API_KEY"] = subprocess.check_output(
        ["security", "find-generic-password", "-a", os.environ["USER"], "-s", "minimax_api_key", "-w"],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()
    initial_prompt = render_prompt(arm, metadata["card"])
    command = [
        "claude",
        "--bare",
        "-p",
        initial_prompt,
        "--model",
        MODEL,
        "--permission-mode",
        "bypassPermissions",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
    ]
    record_live_delivery(root, metadata, initial_prompt, "claude-prompt")
    limits = metadata.get("envelope", ENVELOPE)
    record = run_stream(
        root,
        sandbox_command(root, command),
        env,
        wall_s=min(wall_s or 1800, limits.get("wall_s", 1800)),
        token_limit=limits.get("tokens", 8_000_000),
    )
    if record["status"] == "ok":
        from study.common import dev_command

        answer = root / "out/answer.json"
        try:
            if not answer.resolve().is_relative_to(root / "out"):
                raise ValueError("answer path escapes out/")
            dev_command("validate_answer.py", answer)
        except (OSError, ValueError, subprocess.SubprocessError):
            record["status"] = "invalid_output"
            write_json(root / "run_record.json", record)
    return record


def dispatch(arm, run_dir, wall_s=None, *, eval_audit_config=None):
    if eval_audit_config is not None and arm != 4:
        raise ValueError("provider audit configuration is restricted to arm 4")
    if arm in (1, 2):
        return run_claude(run_dir, arm, wall_s)
    try:
        module = importlib.import_module("study.eidos_arms")
    except ModuleNotFoundError as exc:
        if exc.name != "study.eidos_arms":
            raise
        raise NotImplementedError(f"arm {arm} not implemented") from exc
    if eval_audit_config is not None:
        return module.run_arm4(Path(run_dir).resolve(), eval_audit_config=eval_audit_config)
    return getattr(module, f"run_arm{arm}")(Path(run_dir).resolve())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", required=True, type=int, choices=(1, 2, 3, 4))
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--wall-s", type=int, help="lower wall ceiling for smoke checks")
    parser.add_argument("--eval-audit-config", type=Path)
    args = parser.parse_args()
    if args.wall_s is not None and not 0 < args.wall_s <= 1800:
        parser.error("--wall-s must be between 1 and 1800")
    try:
        record = dispatch(args.arm, args.run_dir, args.wall_s, eval_audit_config=args.eval_audit_config)
        if not isinstance(record, dict):
            record = read_json(args.run_dir / "run_record.json")
    except Exception as exc:
        meta = read_json(args.run_dir / "arm.json")
        now = utc_now()
        record = {k: meta[k] for k in ("arm", "card", "run_index", "block_id")}
        transcript = args.run_dir / "transcript.jsonl"
        if not transcript.exists():
            transcript.write_text("")
        record.update(
            status="incident",
            llm_broker_enabled=False,
            incident_reason=f"{type(exc).__name__}: {exc}",
            started_at=now,
            ended_at=now,
            wall_s=0,
            tokens={**dict.fromkeys(TOKEN_KEYS, 0), "total": 0},
            billed_usd=None,
            model_ids_seen=[],
            agent_claim="",
            transcript=str(transcript),
        )
        write_json(args.run_dir / "run_record.json", record)
    print(json.dumps(record))
    return 0 if record["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
