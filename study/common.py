"""Shared paths, atomic records and the agent filesystem boundary."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from study.private_card_registry import ALL_PRIVATE_CARDS, card_bars

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / "study"
DEVELOPMENT = ROOT / "research/public/development"
MODEL = "MiniMax-M3.1-Flash"
ENVELOPE = {"wall_s": 1800, "tokens": 8_000_000, "threads": 1}
FAMILIES = {"D01": "iris", "D02": "digits", "D12": "duplicate-input-null", "D03":"digits_shift", "D04":"conditioning", "D05":"forecast", "D06":"order",
            "N01":"breast_cancer", "N02":"diabetes", "N03":"friedman1", "N04":"classification", **{card: card for card in ALL_PRIVATE_CARDS}}
IMPROVEMENT_CARDS = frozenset({'D02', 'D03', 'D04', 'D05', 'D06', 'N01', 'N02', 'N03', 'N04'}) | frozenset(ALL_PRIVATE_CARDS)
FRESH_RECIPE_CARDS = IMPROVEMENT_CARDS - {'D02'}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    tmp.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text())


def research_python():
    path = os.environ.get("EIDOS_RESEARCH_PYTHON")
    if not path or not Path(path).is_file():
        raise RuntimeError("Source /Volumes/Mrigesh SSD/eidos-v1/env.sh; EIDOS_RESEARCH_PYTHON is required")
    return path


def agent_env(run_dir):
    """A clean environment: no inherited lab secrets, repo imports or user configuration."""
    root = Path(run_dir).resolve()
    for name in (".claude-cfg", ".home", ".tmp", ".cache"):
        (root / name).mkdir(exist_ok=True)
    node = Path(shutil.which("node") or "/usr/bin/node").resolve()
    env = {k: os.environ[k] for k in ("LANG", "LC_ALL", "TERM", "USER") if k in os.environ}
    env.update(
        {
            "HOME": str(root / ".home"),
            "TMPDIR": str(root / ".tmp"),
            "CLAUDE_CODE_TMPDIR": str(root / ".tmp"),
            "XDG_CACHE_HOME": str(root / ".cache"),
            "XDG_CONFIG_HOME": str(root / ".home"),
            "CLAUDE_CONFIG_DIR": str(root / ".claude-cfg"),
            "PATH": os.pathsep.join(
                [
                    str(Path(research_python()).parent),
                    str(node.parent),
                    "/opt/homebrew/bin",
                    "/usr/bin",
                    "/bin",
                    "/usr/sbin",
                    "/sbin",
                ]
            ),
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "VECLIB_MAXIMUM_THREADS": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "ANTHROPIC_BASE_URL": "https://api.minimax.io/anthropic",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
        }
    )
    return env


def sandbox_command(run_dir, command, *, network=True, readonly=(), hidden=(), writable=()):
    """Public helper for arms 1–3. Parameters avoid profile string interpolation."""
    python = research_python()
    prefix = subprocess.check_output([python, "-c", "import sys; print(sys.prefix)"], text=True).strip()
    node = Path(shutil.which("node") or "/usr/bin/node").resolve().parent.parent
    extra = "" if network else "\n(deny network*)\n"
    readonly_params = []
    for index, path in enumerate(readonly):
        extra += f'\n(deny file-write* (subpath (param "READONLY{index}")))\n'
        readonly_params += ["-D", f"READONLY{index}=" + str(Path(path).resolve())]
    for index, path in enumerate(hidden):
        extra += f'\n(deny file-read* file-write* (subpath (param "HIDDEN{index}")))\n'
        readonly_params += ["-D", f"HIDDEN{index}=" + str(Path(path).resolve())]
    if writable:
        allowed = ['(literal "/dev/null")']
        for index, path in enumerate(writable):
            allowed.append(f'(subpath (param "WRITE{index}"))')
            readonly_params += ["-D", f"WRITE{index}=" + str(Path(path).resolve())]
        extra += "\n(deny file-write* (require-not (require-any " + " ".join(allowed) + ")) )\n"
    profile = (
        ["-p", (STUDY / "sandbox/agent.sb").read_text() + extra]
        if extra
        else ["-f", str(STUDY / "sandbox/agent.sb")]
    )
    return [
        "/usr/bin/sandbox-exec",
        *profile,
        "-D",
        "RUN_DIR=" + str(Path(run_dir).resolve()),
        "-D",
        "PYTHON_PREFIX=" + str(Path(prefix).resolve()),
        "-D",
        "NODE_PREFIX=" + str(node),
        *readonly_params,
        *map(str, command),
    ]


PROMPT_OVERRIDES = ("EIDOS_STUDY_SMOKE_PROMPT", "EIDOS_STUDY_LANE_A_ROOT")


def refuse_live_prompt_overrides(environment):
    if any(name in environment for name in PROMPT_OVERRIDES):
        raise ValueError("live study dispatch forbids smoke prompt and lane-root overrides")


def record_prompt(run_dir, meta, *, delivered=None, surface=None):
    """Bind the normal render and, at dispatch, the exact initial payload without retaining text."""
    import hashlib

    from study.exp.treatment import for_run

    treatment = for_run(meta)
    arm = meta["arm"]
    expected = render_prompt(arm, meta["card"])
    identity = {key: meta[key] for key in ("block_id", "card", "run_index", "arm", "charter_amendment_commit",
                                          "charter_amendment_sha256")}
    receipt = dict(**identity, treatment=treatment.record(), common_template_sha256=hashlib.sha256((STUDY / "prompts/task.md").read_bytes()).hexdigest(),
                   expected_render_sha256=hashlib.sha256(expected.encode()).hexdigest())
    directory = run_dir.parent / "prompt-receipts"
    if run_dir.name != f"arm{arm}" or directory.is_symlink():
        raise ValueError("unsafe trusted prompt receipt location")
    directory.mkdir(exist_ok=True)
    expected_path = directory / f"arm{arm}-expected.json"
    if delivered is None:
        target = expected_path
    else:
        if expected_path.is_symlink() or read_json(expected_path) != receipt:
            raise ValueError("prepared expected prompt receipt differs at dispatch")
        if surface == "claude-prompt" and delivered != expected:
            raise ValueError("delivered Claude prompt differs from expected live render")
        if surface == "investigation-question" and not delivered.startswith(expected + "\n\nPackaged task files:\n"):
            raise ValueError("delivered investigation question differs from expected live render")
        if surface not in ("claude-prompt", "investigation-question"):
            raise ValueError("unknown prompt delivery surface")
        receipt.update(surface=surface, delivered_sha256=hashlib.sha256(delivered.encode()).hexdigest())
        target = directory / f"arm{arm}-delivered.json"
    with target.open("x") as stream:
        json.dump(receipt, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def record_live_delivery(run_dir, meta, delivered, surface):
    if os.environ.get("EIDOS_STUDY_LIVE_DISPATCH") == "1":
        refuse_live_prompt_overrides(os.environ)
        record_prompt(run_dir, meta, delivered=delivered, surface=surface)


EXPORT_READINESS_CONTRACT = (
    "Before the first lock, every replacement lock, or submission without a lock, use your assigned time and tools to run its declared "
    "infer command in a fresh process on the supplied visible validation inputs. Use the exact "
    "candidate source and checkpoint you intend to submit and write to a new output path. Check "
    "that fitted preprocessing and other required state are serialized and reload correctly, and "
    "that the declared dependencies cover this inference path. Verify that the output has the same "
    "unique validation IDs and compare predictions by ID against the validation predictions "
    "produced during training. If the check fails, diagnose and make any changes through your "
    "ordinary work before locking, then repeat the check on the intended submission. Report any "
    "unresolved limitation or abstain. This visible check is separate from independent confirmation."
)


def render_prompt(arm, card, *, base=None):
    """All four arms use the same card table and substitution, including smoke overrides."""
    from study.exp.treatment import resolve

    treatment = resolve()
    tools_path = treatment.tools_arm4_path if arm == 4 else STUDY / f"prompts/tools_arm{arm}.md"
    paragraph = tools_path.read_text().strip()
    if "PLACEHOLDER" in paragraph:
        raise NotImplementedError(f"arm {arm} tools paragraph not implemented")
    text = Path(base or STUDY / "prompts/task.md").read_text()
    if "{{TOOLS_PARAGRAPH}}" not in text:
        raise ValueError("base prompt must contain {{TOOLS_PARAGRAPH}}")
    cards = read_json(STUDY / "cards.json")
    if card in ALL_PRIVATE_CARDS:
        metric, gain = card_bars(card)
        change = f"improves accuracy by {gain} absolute" if metric == "accuracy" else f"reduces RMSE by at least {gain:.0%}"
        cards[card] = {"practical_bar": (
            f"The task owner's practical bar: a candidate must show that its recipe {change} "
            "on the evaluator's held-out data versus the supplied baseline (seed 11) and reproduces "
            "from fresh initialization. An honest abstention or qualified null is acceptable."
        )}
    if card not in cards and not base and card != "TOY-REGRESSION":
        raise ValueError(f"unknown study card: {card}")
    if card in cards and "{{PRACTICAL_BAR}}" not in text:
        raise ValueError("study prompt must contain {{PRACTICAL_BAR}}")
    rendered = text.replace("{{TOOLS_PARAGRAPH}}", paragraph).replace(
        "{{PRACTICAL_BAR}}", cards.get(card, {}).get("practical_bar", ""))
    return rendered.rstrip() + "\n\n" + EXPORT_READINESS_CONTRACT + "\n"


def dev_command(script, *args, **kwargs):
    if script == "package_task.py" and len(args) == 4 and args[0] == "--task" and args[2] == "--out":
        if args[1] in ALL_PRIVATE_CARDS:
            from study.private_seal import configured_seal

            configured_seal(args[1]).stage_bundle(args[1], args[3])
            return
    return subprocess.run(
        [research_python(), str(STUDY / "packaging/package_card.py" if script == "package_task.py" and any(str(a) in FRESH_RECIPE_CARDS for a in args) else DEVELOPMENT / script), *map(str, args)],
        check=True,
        text=True,
        capture_output=True,
        timeout=120,
        **kwargs,
    )
