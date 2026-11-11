"""Static, self-contained HTML report of an investigation (or a whole project).

Built only from authorized read queries through the lab API client; it holds no database access and
issues no commands, so it cannot change scientific state. The output is one file with inline CSS and
inline SVG: no JavaScript, no external fetches. Every ledger-derived string is HTML-escaped because
ledger text (notes, rationales, claim statements, errors) may be model-generated.
"""

from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eidos.client import LabError

MAX_JOBS = 300
MAX_EVENT_PAGES = 20
RECENT_EVENTS = 40
NEGATIVE = ("protocol-failed", "inconclusive", "unassessed")

CSS = """
body{font:14px/1.45 system-ui,sans-serif;margin:2rem auto;max-width:1100px;padding:0 1rem;color:#1b1f23}
h1{font-size:1.6rem}h2{margin-top:2.2rem;border-bottom:2px solid #d0d7de;padding-bottom:.2rem}h3{margin-top:1.4rem}
table{border-collapse:collapse;width:100%;margin:.6rem 0;font-size:13px}
th,td{border:1px solid #d0d7de;padding:.3rem .5rem;text-align:left;vertical-align:top}
th{background:#f6f8fa}code,pre{font:12px ui-monospace,monospace;background:#f6f8fa}
pre{padding:.5rem;overflow:auto;white-space:pre-wrap;word-break:break-word}
.badge{display:inline-block;padding:0 .45rem;border-radius:.7rem;font-size:12px;border:1px solid #888;background:#fff}
.ok{border-color:#1a7f37;color:#1a7f37}.bad{border-color:#cf222e;color:#cf222e}
.warn{border-color:#9a6700;color:#9a6700;background:#fff8c5}.mute{color:#656d76}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:1rem}.card{border:1px solid #d0d7de;border-radius:6px;padding:.6rem}
.note{background:#ddf4ff;border:1px solid #54aeff;padding:.4rem .6rem;border-radius:6px}
.gap{background:#fff8c5;border:1px solid #d4a72c;padding:.4rem .6rem;border-radius:6px}
svg text{font:11px system-ui,sans-serif}
"""


def esc(x: Any) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def _slug(x: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(x)).strip("-") or "x"


def _num(x: Any) -> str:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return esc(x)
    return esc(f"{f:.0f}" if f == int(f) and abs(f) < 1e12 else f"{f:.4g}")


def _json(x: Any, limit: int = 1500) -> str:
    s = json.dumps(x, indent=1, sort_keys=True, default=str)
    if len(s) > limit:
        s = s[:limit] + "\n… (truncated; full record available through the lab API)"
    return f"<pre>{esc(s)}</pre>"


def _badge(text: Any, kind: str = "") -> str:
    return f'<span class="badge {kind}">{esc(text)}</span>'


def _outcome_badge(outcome: str) -> str:
    return _badge(outcome, {"protocol-passed": "ok", "protocol-failed": "bad"}.get(outcome, "warn"))


def _health_badge(health: str) -> str:
    return _badge(health, "ok" if health == "intact" else "bad")


def _table(headers: list[str], rows: list[list[str]], empty: str = "none recorded") -> str:
    """Cells are pre-escaped HTML fragments built by this module."""
    if not rows:
        return f'<p class="mute">{esc(empty)}</p>'
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


class _Reader:
    """Query wrapper that records failures instead of aborting: a report shows what it could not read."""

    def __init__(self, client: Any, project: str):
        self.client, self.project = client, project
        self.gaps: list[str] = []

    def q(self, name: str, params: dict | None = None, default: Any = None) -> Any:
        try:
            return self.client.query(name, params or {}, project=self.project)
        except LabError as exc:
            self.gaps.append(f"{name} {json.dumps(params or {}, default=str)[:120]}: {exc.code}")
        except Exception as exc:  # noqa: BLE001 - transport failure; keep rendering what we have
            self.gaps.append(f"{name}: {type(exc).__name__}")
        return default

    def records(self, kind: str, **extra: Any) -> list[dict]:
        return self.q("list_records", {"kind": kind, "summary": False, "limit": 1000, **extra}, []) or []


# -- SVG ------------------------------------------------------------------------------------------

def _svg_budget(budgets: dict[str, dict]) -> str:
    """Stacked horizontal bars per dimension: used | reserved | uncertain against the limit."""
    if not budgets:
        return '<p class="mute">no budget recorded</p>'
    rowh, w, left = 26, 560, 120
    h = rowh * len(budgets) + 28
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w + left + 120}" height="{h}" role="img" '
           f'aria-label="budget usage">']
    for i, (dim, b) in enumerate(sorted(budgets.items())):
        limit = float(b.get("limit_amount") or 0)
        used, res, unc = (float(b.get(k) or 0) for k in ("used", "reserved", "uncertain"))
        scale = max(limit, used + res + unc, 1e-9)
        y = 8 + i * rowh
        out.append(f'<text x="0" y="{y + 12}">{esc(dim)}</text>')
        out.append(f'<rect x="{left}" y="{y}" width="{w}" height="16" fill="#eaeef2"/>')
        x = float(left)
        for val, color in ((used, "#1f6feb"), (res, "#bf8700"), (unc, "#cf222e")):
            ww = w * val / scale
            if ww > 0:
                out.append(f'<rect x="{x:.1f}" y="{y}" width="{ww:.1f}" height="16" fill="{color}"/>')
                x += ww
        if limit:
            lx = left + w * limit / scale
            out.append(f'<line x1="{lx:.1f}" x2="{lx:.1f}" y1="{y - 3}" y2="{y + 19}" stroke="#000" stroke-width="2"/>')
        out.append(f'<text x="{left + w + 6}" y="{y + 12}">limit {_num(limit)}</text>')
    out.append(f'<text x="{left}" y="{h - 4}" fill="#1f6feb">■ used</text>'
               f'<text x="{left + 70}" y="{h - 4}" fill="#bf8700">■ reserved</text>'
               f'<text x="{left + 160}" y="{h - 4}" fill="#cf222e">■ uncertain</text></svg>')
    return "".join(out)


def _svg_lineage(nodes: dict[str, dict], edges: list[tuple[str, str, str]]) -> str:
    """Parent → child tree. nodes: id -> {label, status}; edges: (parent, child, label)."""
    if not edges:
        return '<p class="mute">no branched trials: every trial is its own training trajectory</p>'
    children: dict[str, list[str]] = {}
    has_parent = set()
    for p, c, _ in edges:
        children.setdefault(p, []).append(c)
        has_parent.add(c)
    roots = sorted({p for p, _, _ in edges} - has_parent)
    pos: dict[str, tuple[float, int]] = {}
    cursor = [0.0]

    def place(n: str, depth: int, seen: frozenset) -> float:
        kids = [k for k in children.get(n, []) if k not in seen]
        if not kids:
            x = cursor[0]
            cursor[0] += 1
        else:
            xs = [place(k, depth + 1, seen | {n}) for k in kids]
            x = sum(xs) / len(xs)
        pos[n] = (x, depth)
        return x

    for r in roots:
        place(r, 0, frozenset())
    if not pos:
        return '<p class="mute">lineage contains a cycle; not drawn</p>'
    colw, rowh = 150, 70
    width = int(max(x for x, _ in pos.values()) * colw + colw + 40)
    height = (max(d for _, d in pos.values()) + 1) * rowh + 30
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" role="img" '
           f'aria-label="checkpoint lineage">']
    for p, c, label in edges:
        if p in pos and c in pos:
            (x1, d1), (x2, d2) = pos[p], pos[c]
            out.append(f'<line x1="{x1 * colw + 75:.0f}" y1="{d1 * rowh + 30}" x2="{x2 * colw + 75:.0f}" '
                       f'y2="{d2 * rowh + 6}" stroke="#656d76"/>')
            if label:
                out.append(f'<text x="{(x1 + x2) / 2 * colw + 80:.0f}" y="{(d1 + d2) / 2 * rowh + 24:.0f}" '
                           f'fill="#656d76">{esc(label)}</text>')
    colors = {"succeeded": "#dafbe1", "failed": "#ffebe9", "running": "#ddf4ff"}
    for n, (x, d) in pos.items():
        info = nodes.get(n, {})
        fill = colors.get(info.get("status", ""), "#f6f8fa")
        out.append(f'<rect x="{x * colw + 5:.0f}" y="{d * rowh + 6}" width="140" height="24" rx="4" fill="{fill}" '
                   f'stroke="#8c959f"/><text x="{x * colw + 10:.0f}" y="{d * rowh + 22}">{esc(str(n)[:22])}</text>')
        if info.get("status"):
            out.append(f'<text x="{x * colw + 10:.0f}" y="{d * rowh + 44}" fill="#656d76">{esc(info["status"])}</text>')
    out.append("</svg>")
    return "".join(out)


def _svg_metric(name: str, points: list[tuple[str, float]]) -> str:
    """Dot plot of one metric across jobs (jobs on x in launch order)."""
    vals = [v for _, v in points]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    w, h, pad = 40 + 60 * len(points), 120, 30
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h + 20}" role="img" '
           f'aria-label="{esc(name)} across jobs"><text x="4" y="12">{esc(name)}</text>'
           f'<text x="4" y="{h + 14}" fill="#656d76">min {_num(lo)} · max {_num(hi)} (descriptive; not a test)</text>']
    for i, (jid, v) in enumerate(points):
        x = 30 + 60 * i
        y = h - pad - (v - lo) / span * (h - 2 * pad) + 10
        out.append(f'<circle cx="{x}" cy="{y:.1f}" r="4" fill="#1f6feb"/><text x="{x - 14}" y="{h - 4}">'
                   f'{esc(str(jid)[-6:])}</text><text x="{x - 10}" y="{y - 7:.1f}">{_num(v)}</text>')
    out.append("</svg>")
    return "".join(out)


# -- sections ----------------------------------------------------------------------------------------

def _charter_section(rd: _Reader, inv: dict) -> str:
    ref = (inv.get("body") or {}).get("charter_ref")
    if not ref:
        return ('<div class="gap"><b>No committed charter.</b> This investigation has no pinned success rules; '
                "any results below are exploratory and no claim can be framed as confirmed.</div>")
    cid = ref["id"]
    hist = rd.q("record_history", {"kind": "charter", "id": cid}, []) or []
    cur = rd.q("get_record", {"kind": "charter", "id": cid}, {}) or {}
    body = cur.get("body") or {}
    parts = [f"<p>Charter <code>{esc(cid)}</code> pinned at revision {esc(ref.get('revision'))} "
             f"(<code>{esc(ref.get('digest'))}</code>); current status {_badge(cur.get('status', '?'))}. "
             f"Exploration only: <b>{esc(body.get('exploration_only'))}</b>.</p>"]
    if body.get("question"):
        parts.append(f"<p><b>Charter question:</b> {esc(body['question'])}</p>")
    if body.get("success_rules"):
        parts.append("<h3>Success rules</h3>" + _table(
            ["id", "metric", "min effect", "decision standard"],
            [[esc(r.get("id")), esc(r.get("metric")), esc(r.get("min_effect")),
              f"<code>{esc(json.dumps(r.get('decision_standard'), sort_keys=True, default=str))}</code>"]
             for r in body["success_rules"]]))
    if body.get("known_exposures"):
        parts.append("<p><b>Known exposures at commit:</b> " + esc("; ".join(map(str, body["known_exposures"]))) + "</p>")
    rows = []
    for rev in hist:
        b = rev.get("body") or {}
        am = b.get("amendment")
        if am:
            flags = []
            if am.get("exposure_informed"):
                flags.append(_badge("exposure-informed", "warn"))
            if am.get("changes_success_rule"):
                flags.append(_badge("changes success rule", "warn"))
            if b.get("exposure_informed_lineage"):
                flags.append(_badge("lineage exposure-informed", "bad"))
            detail = (f"<b>rationale:</b> {esc(am.get('rationale'))}<br>changed: "
                      f"{esc(', '.join(sorted((am.get('changes') or {}).keys())))}")
        else:
            flags, detail = [_badge("committed before exploration", "ok")] if b.get("committed_before_exploration") else [], ""
        rows.append([esc(rev.get("revision")), esc(rev.get("created_by")), esc(rev.get("created_at")),
                     " ".join(flags), detail])
    parts.append("<h3>Revision and amendment history</h3>" + _table(
        ["rev", "author", "at", "flags", "amendment"], rows))
    if (inv.get("body") or {}).get("steering"):
        parts.append("<h3>Steering instructions (cannot change success rules)</h3>" + _table(
            ["by", "instruction"], [[esc(s.get("by")), esc(s.get("instruction"))] for s in inv["body"]["steering"]]))
    return "".join(parts)


def _needs_input_note(mark: Any) -> str:
    """The operator-attention mark of an investigation (ADR 0019 codes), or nothing."""
    if not isinstance(mark, dict):
        return ""
    return (f'<p class="gap"><b>Needs operator input</b> ({esc(mark.get("code") or "unspecified")}, since '
            f'{esc(mark.get("since"))}): {esc(mark.get("reason"))}</p>')


def _turns_section(rd: _Reader, inv: dict) -> str:
    out = []
    for t in inv.get("turns") or []:
        full = rd.q("get_turn", {"turn_id": t["id"]}, None)
        res = (full or {}).get("result") or {}
        head = (f"<h3 id=\"{_slug(t['id'])}\">Turn {esc(t.get('seq'))} {_badge(t.get('status'))} "
                f"stop reason: <b>{esc(t.get('stop_reason') or res.get('stop_reason') or '—')}</b></h3>"
                f'<p class="mute">horizon {esc(t.get("horizon"))}</p>')
        failure = res.get("failure") or {}
        if failure:  # ADR 0019: why the turn failed
            head += (f'<p class="gap">failed: {esc(failure.get("error_type"))} '
                     f'({"retryable" if failure.get("retryable") else "not retryable"}): '
                     f'{esc(failure.get("message"))}</p>')
        if not full:
            out.append(head + '<p class="gap">turn detail unavailable</p>')
            continue
        rows = []
        for d in full.get("decisions") or []:
            b = d.get("body") or {}
            calls = ", ".join(str(c.get("tool")) for c in b.get("calls") or []) or "—"
            usage = b.get("usage") or {}
            tokens = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
            status = _badge("accepted", "ok") if d.get("accepted") else _badge(
                "unapplied: " + str(b.get("reason") or "?"), "warn")
            if d.get("accepted") and d.get("revoked"):  # F2c: accepted by a failed attempt, never executed
                status = _badge("revoked: attempt failed", "warn")
            fixture = " " + _badge("fixture provider", "warn") if usage.get("fixture") else ""
            rows.append([esc(d.get("slot")), status + fixture, esc(b.get("rationale") or ""), esc(calls),
                         esc(tokens), esc(b.get("prompt_revision") or "")])
        out.append(head + _table(["slot", "decision", "rationale", "tool calls", "tokens", "prompt"], rows,
                                 "no decisions recorded") +
                   (f"<p>{esc(res.get('summary'))}</p>" if res.get("summary") else ""))
    return "".join(out) or '<p class="mute">no turns yet</p>'


def _job_metrics(job: dict) -> dict[str, Any]:
    rec = (job.get("terminal") or {}).get("receipt") or {}
    m = rec.get("metrics")
    return m if isinstance(m, dict) else {}


def _jobs_section(jobs: list[dict]) -> str:
    rows = []
    series: dict[str, list[tuple[str, float]]] = {}
    for j in jobs:
        term = j.get("terminal") or {}
        rec = term.get("receipt") or {}
        lock = j.get("lock") or {}
        metrics = _job_metrics(j)
        for k, v in metrics.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                series.setdefault(k, []).append((j["id"], float(v)))
        realized = {k: rec.get(k) for k in ("examples", "steps", "device", "seconds") if rec.get(k) is not None}
        cond = {"seed": lock.get("seed"), "isolation": lock.get("isolation_profile"), **realized}
        cond = {k: v for k, v in cond.items() if v is not None}
        status = j.get("status")
        kind = {"succeeded": "ok", "failed": "bad", "cancelled": "warn"}.get(str(status), "")
        err = term.get("error")
        rows.append([f"<code>{esc(j['id'])}</code>", esc(j.get("purpose")), _badge(status, kind) +
                     (" " + _badge("cancel requested", "warn") if j.get("cancel_requested") else ""),
                     esc((lock.get("component") or {}).get("ref")), esc(j.get("trial_id") or ""),
                     esc(", ".join(f"{k}={_num(v)}" for k, v in sorted(metrics.items())) or "—"),
                     esc(", ".join(f"{k}={v}" for k, v in cond.items()) or "—"),
                     esc(json.dumps(err, default=str)[:300]) if err else ""])
    out = [_table(["job", "purpose", "status", "component", "trial", "metrics", "realized conditions", "error"],
                  rows, "no jobs")]
    plots = [_svg_metric(k, pts) for k, pts in sorted(series.items()) if len(pts) >= 2]
    if plots:
        out.append("<h3>Metric values across jobs</h3>" + "".join(plots))
    return "".join(out)


def _lineage(rd: _Reader, jobs: dict[str, dict], inv_id: str) -> str:
    branches = [b for b in rd.records("branch") if (b.get("body") or {}).get("trial_id")]
    nodes: dict[str, dict] = {}
    edges: list[tuple[str, str, str]] = []
    by_trial = {j.get("trial_id"): j for j in jobs.values() if j.get("trial_id")}
    mine = set(by_trial)
    rows = []
    for b in branches:
        body = b["body"]
        parent = body.get("parent") or {}
        child = str(body["trial_id"])
        ptrial = str(parent.get("trial_id") or parent.get("checkpoint_id"))
        if child not in mine and ptrial not in mine:
            continue
        for t in (child, ptrial):
            nodes.setdefault(t, {"status": (by_trial.get(t) or {}).get("status", "")})
        edges.append((ptrial, child, f"@step {parent.get('step')}" if parent.get("step") is not None else ""))
        rows.append([esc(ptrial), esc(child), esc(parent.get("checkpoint_id")), esc(body.get("restore_class")),
                     esc(body.get("stream_policy")), esc((body.get("intervention") or {}).get("kind")),
                     esc(body.get("independence_unit"))])
    note = ('<p class="note">Branches from one parent share a training trajectory (same independence unit); they '
            "are not independent replications.</p>") if rows else ""
    return (_svg_lineage(nodes, edges) + note +
            (_table(["parent trial", "child trial", "parent checkpoint", "restore class", "stream", "intervention",
                     "independence unit"], rows) if rows else ""))


def _claims_section(rd: _Reader, inv_id: str | None) -> tuple[str, dict[str, int]]:
    claims = [c for c in rd.records("claim") if inv_id is None or (c.get("body") or {}).get("investigation_id") == inv_id]
    tally: dict[str, int] = {}
    rows, negatives = [], []
    for c in claims:
        full = rd.q("get_claim", {"claim_id": c["id"]}, None)
        body = c.get("body") or {}
        assessments = (full or {}).get("assessments") or []
        support = ((full or {}).get("support") or {}).get("status", "unknown")
        cells = []
        for a in assessments:
            det = rd.q("get_assessment", {"assessment_id": a["id"]}, None) or {}
            ab = det.get("body") or {}
            tally[a["outcome"]] = tally.get(a["outcome"], 0) + 1
            tally["health:" + a["health"]] = tally.get("health:" + a["health"], 0) + 1
            cells.append(
                f'<div class="card"><div class="cols"><div><b>Protocol outcome</b><br>{_outcome_badge(a["outcome"])}'
                f'<br>conformance: {_badge(a["conformance"], "ok" if a["conformance"] == "conformant" else "warn")}</div>'
                f'<div><b>Evidence health</b><br>{_health_badge(a["health"])}</div></div>'
                f'<small>assessment <code>{esc(a["id"])}</code>, plan <code>{esc(a.get("plan_id"))}</code></small>'
                + ((f'<br><small>findings: {esc(json.dumps(ab.get("findings"), default=str)[:400])}</small>')
                   if ab.get("findings") else "")
                + (f"<br><small>evaluation: {esc(json.dumps(ab.get('evaluation'), default=str)[:400])}</small>"
                   if ab.get("evaluation") else "") + "</div>")
            if a["outcome"] in NEGATIVE:
                negatives.append((c, a))
        if not assessments:
            cells.append(_badge("unassessed", "warn") + ' <span class="mute">no assessment committed</span>')
            tally["unassessed"] = tally.get("unassessed", 0) + 1
            negatives.append((c, {"id": "—", "outcome": "unassessed", "health": "—", "conformance": "—"}))
        scope = body.get("scope")
        rows.append([f"<code>{esc(c['id'])}</code>", esc(body.get("statement")),
                     f"{esc(body.get('kind'))}<br><small>{esc(json.dumps(scope, default=str)[:200])}</small>",
                     _badge(c.get("status")) + f"<br>support: {esc(support)}", "".join(cells)])
    out = [_table(["claim", "statement", "kind / scope", "status", "assessments (protocol outcome and evidence health "
                   "shown separately)"], rows, "no claims filed")]
    if tally:
        bits = " ".join(f"{_outcome_badge(k)}×{v}" for k, v in sorted(tally.items()) if not k.startswith("health:"))
        hb = " ".join(f"{_health_badge(k[7:])}×{v}" for k, v in sorted(tally.items()) if k.startswith("health:"))
        out.insert(0, f"<p>Outcome tally: {bits}<br>Evidence health tally: {hb or '—'}</p>")
    neg_rows = [[f"<code>{esc(c['id'])}</code>", esc((c.get('body') or {}).get('statement')),
                 _outcome_badge(a["outcome"]), esc(a.get("conformance")), esc(a["id"])] for c, a in negatives]
    out.append("<h3>Negative, failed and inconclusive results (kept visible)</h3>" + _table(
        ["claim", "statement", "outcome", "conformance", "assessment"], neg_rows,
        "none: every assessed claim passed its protocol"))
    return "".join(out), tally


def _plans_section(rd: _Reader, inv_id: str | None) -> str:
    plans = [p for p in rd.records("evidence_plan") if inv_id is None or (p.get("body") or {}).get("investigation_id") == inv_id]
    return _table(["plan", "status", "revision", "digest", "claim", "template", "assessor", "arms"], [[
        f"<code>{esc(p['id'])}</code>", _badge(p.get("status")), esc(p.get("revision")), esc(p.get("digest")),
        esc(((p.get("body") or {}).get("claim_ref") or {}).get("id")),
        esc(((p.get("body") or {}).get("template") or {}).get("ref")),
        esc(((p.get("body") or {}).get("assessor") or {}).get("ref")),
        esc(", ".join(str(a.get("id")) for a in (p.get("body") or {}).get("arms") or []))] for p in plans],
        "no evidence plans")


def _budget_section(inv: dict) -> str:
    b = inv.get("budgets") or {}
    parts = []
    for name in ("exploration", "confirmation"):
        if b.get(name):
            parts.append(f"<h3>{esc(name.title())} budget</h3>" + _svg_budget(b[name]) + _table(
                ["dimension", "limit", "used", "reserved", "uncertain (possible spend)"],
                [[esc(d), _num(v.get("limit_amount")), _num(v.get("used")), _num(v.get("reserved")),
                  _num(v.get("uncertain"))] for d, v in sorted(b[name].items())]))
    return "".join(parts) or '<p class="mute">no budgets configured for this investigation</p>'


def _events(rd: _Reader, inv_id: str | None) -> tuple[list[dict], int | None, int | None]:
    cursor, horizon, evs = 0, None, []
    for _ in range(MAX_EVENT_PAGES):
        params: dict[str, Any] = {"cursor": cursor, "limit": 500}
        if inv_id:
            params["investigation_id"] = inv_id
        if horizon:
            params["horizon"] = horizon
        page = rd.q("list_events", params, None)
        if not page:
            break
        evs.extend(page["events"])
        horizon, cursor = page["horizon"], page["next_cursor"]
        if not page.get("more"):
            break
    return evs, cursor, horizon


def _investigation_block(rd: _Reader, inv_id: str) -> tuple[str, dict[str, Any]]:
    inv = rd.q("get_investigation", {"investigation_id": inv_id}, None)
    if not inv:
        return f'<h2>Investigation <code>{esc(inv_id)}</code></h2><p class="gap">not readable (see data gaps)</p>', {}
    body = inv.get("body") or {}
    jobs = []
    for jref in (inv.get("jobs") or [])[:MAX_JOBS]:
        j = rd.q("get_job", {"job_id": jref["id"], "include_lock": True}, None)
        if j:
            jobs.append(j)
    jobs_by_id = {j["id"]: j for j in jobs}
    evs, cursor, horizon = _events(rd, inv_id)
    claims_html, tally = _claims_section(rd, inv_id)
    inv_cfg = body.get("investigator") or {}
    runtime = f"{inv_cfg.get('runtime')} / {inv_cfg.get('model') or 'no model'}"
    fixture_note = (' <span class="badge warn">fixture investigator: not real LLM behaviour</span>'
                    if inv_cfg.get("runtime") == "fixture" else "")
    sections = [
        f'<h2 id="{_slug(inv_id)}">Investigation <code>{esc(inv_id)}</code> {_badge(inv.get("status"))}</h2>',
        _needs_input_note(inv.get("needs_input")),
        f"<p><b>Question:</b> {esc(body.get('question'))}</p>",
        f"<p>Revision {esc(inv.get('revision'))} (head {esc(inv.get('head_revision'))}), created by "
        f"{esc(inv.get('created_by'))} at {esc(inv.get('created_at'))}; investigator {esc(runtime)}{fixture_note}; "
        f"environment {esc(body.get('environment'))}; model artifact {esc(body.get('model_artifact_id') or 'none')}.</p>",
        "<h3>Charter</h3>" + _charter_section(rd, inv),
        "<h2>Turns and decisions</h2>" + _turns_section(rd, inv),
        "<h2>Jobs</h2>" + _jobs_section(jobs),
        "<h2>Checkpoint lineage</h2>" + _lineage(rd, jobs_by_id, inv_id),
        "<h2>Claims and assessments</h2>" + claims_html,
        "<h3>Evidence plans</h3>" + _plans_section(rd, inv_id),
        "<h2>Budgets</h2>" + _budget_section(inv),
        "<h2>Recent events</h2>" + _table(["seq", "type", "subject", "at"], [
            [esc(e.get("seq")), esc(e.get("type")), esc(e.get("subject")), esc(e.get("created_at"))]
            for e in evs[-RECENT_EVENTS:]], "no events") +
        f'<p class="mute">{len(evs)} events read; event cursor {esc(cursor)} at horizon {esc(horizon)}.</p>',
    ]
    return "".join(sections), {"cursor": cursor, "horizon": horizon, "tally": tally}


def _project_block(rd: _Reader) -> str:
    comps = rd.q("list_records", {"kind": "component_release", "limit": 1000}, []) or []
    exports = rd.records("export")
    withdrawals = rd.records("withdrawal")
    parts = ["<h2>Exports</h2>" + _table(["export", "status", "checkpoint", "builder", "behaviour check", "claims"], [[
        f"<code>{esc(x['id'])}</code>", _badge(x.get("status")), esc((x.get("body") or {}).get("checkpoint_artifact_id")
                                                                      or (x.get("body") or {}).get("checkpoint")),
        esc((x.get("body") or {}).get("builder")),
        esc(((x.get("body") or {}).get("behavior_check") or {}).get("job_id")),
        esc(", ".join(map(str, (x.get("body") or {}).get("claims") or [])))] for x in exports], "no exports"),
        "<h2>Withdrawals</h2>" + _table(["withdrawal", "release", "severity", "defect", "affected"], [[
            f"<code>{esc(w['id'])}</code>", esc((w.get('body') or {}).get("release_ref")),
            esc((w.get("body") or {}).get("severity")), esc((w.get("body") or {}).get("defect")),
            esc(", ".join(map(str, (w.get("body") or {}).get("affected") or [])))] for w in withdrawals],
            "no withdrawals"),
        "<h2>Component releases in this project</h2>" + _table(["release", "status", "digest", "labels"], [[
            f"<code>{esc(c['id'])}</code>", _badge(c.get("status"), "bad" if c.get("status") == "withdrawn" else ""),
            esc(c.get("digest")), esc(json.dumps(c.get("labels"), sort_keys=True, default=str))] for c in comps],
            "no components")]
    pb = rd.q("budgets", {}, None)
    if pb and pb.get("budgets"):
        parts.append("<h2>Project budget</h2>" + _svg_budget(pb["budgets"]))
    return "".join(parts)


def _lab_version(client: Any) -> str:
    try:
        h = client.health()
        api = h.get("api")
    except Exception:  # noqa: BLE001
        api = None
    try:
        from importlib.metadata import version
        pkg = version("eidos-lab")
    except Exception:  # noqa: BLE001
        pkg = "unknown"
    return f"eidos-lab {pkg}, API {api or 'unreachable'}"


def render_report(client: Any, project: str, investigation: str | None = None) -> str:
    rd = _Reader(client, project)
    if investigation:
        inv_ids = [investigation]
    else:
        inv_ids = [r["id"] for r in rd.q("list_records", {"kind": "investigation", "limit": 1000}, []) or []]
    blocks: list[str] = []
    last: dict[str, Any] = {}
    for iid in inv_ids:
        html_block, meta = _investigation_block(rd, iid)
        blocks.append(html_block)
        last = meta or last
    project_html = _project_block(rd)
    summary = rd.q("project_summary", {}, {}) or {}
    horizon = summary.get("horizon") or last.get("horizon")
    title = f"Eidos report: {project}" + (f" / {investigation}" if investigation else "")
    gaps = ("" if not rd.gaps else '<div class="gap"><b>Data gaps.</b> These reads failed, so the report is '
            "incomplete for them:<ul>" + "".join(f"<li>{esc(g)}</li>" for g in rd.gaps) + "</ul></div>")
    footer = (
        f'<hr><h2>Provenance</h2><p>Lab: {esc(_lab_version(client))}. Project <code>{esc(project)}</code>. '
        f"Ledger horizon {esc(horizon)}; investigation event cursor {esc(last.get('cursor'))}. "
        f"Generated {esc(datetime.now(UTC).isoformat(timespec='seconds'))}.</p>"
        '<p class="note">This report is a read-only rendering of authorized lab views. It cannot change scientific '
        "state: it issues no commands, and reading evidence views records an exposure for the reporting principal in "
        "the lab like any other read. A protocol outcome is a statement about a frozen protocol, not about the world; "
        "evidence health is reported next to it and can change after later withdrawals.</p>" + gaps)
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{esc(title)}</title>'
            f"<style>{CSS}</style></head><body><h1>{esc(title)}</h1>" + "".join(blocks) + project_html + footer +
            "</body></html>")


def write_report(client: Any, project: str, investigation: str | None, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{_slug(project)}-{_slug(investigation) if investigation else 'project'}-report.html"
    path.write_text(render_report(client, project, investigation), encoding="utf-8")
    return path
