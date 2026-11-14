#!/usr/bin/env python3
"""Check final Claude startup with network denied: this makes no provider calls."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from study.common import agent_env, sandbox_command
from study.run_arm import stop_process_tree


def probe(root):
    from study.exp.providers import load

    profile = load()
    settings = profile.cli_settings()
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    env = agent_env(root)
    env["ANTHROPIC_API_KEY"] = subprocess.check_output(
        ["security", "find-generic-password", "-a", os.environ["USER"], "-s", profile.keychain_item, "-w"],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()
    command = [
        "claude",
        "--bare",
        "-p",
        "Offline startup probe only.",
        "--model",
        settings["model"],
        "--permission-mode",
        "bypassPermissions",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
    ]
    process = subprocess.Popen(
        sandbox_command(root, command, network=False),
        cwd=root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, _ = process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        stop_process_tree(process)
        stdout, _ = process.communicate()
    stop_process_tree(process)
    events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    assert any(e.get("type") == "system" and e.get("subtype") == "init" for e in events), (
        "CLI did not initialize"
    )
    print("CLAUDE_STARTUP_OK network=DENIED provider_calls=0 partial_events=enabled")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    probe(parser.parse_args().run_dir)
