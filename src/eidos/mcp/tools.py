"""The stable MCP tool vocabulary.

Every tool resolves to exactly one lab domain command or query (or, for ``get_artifact_access``,
two reads) through the HTTP command API. Installed components are *data* returned by
``list_components`` / ``describe_component``; there is never one tool per component.

Tool arguments are a small typed envelope (``project``, ``command_key``, ``expected_revision``)
plus the named payload fields of the command, plus an optional ``payload`` object for any
additional documented field. Named fields win over ``payload`` entries of the same name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_T = {"s": "string", "i": "integer", "n": "number", "b": "boolean", "o": "object", "a": "array"}

COMMAND_KEY_DOC = (
    "Caller-chosen idempotency key (required, <=200 chars). Retry after a lost reply with the SAME key and "
    "the SAME arguments: the lab returns the original receipt. The same key with different arguments is "
    "rejected with COMMAND_KEY_CONFLICT. Use a fresh key for an intentionally new operation."
)
EXPECTED_REVISION_DOC = (
    "Optional optimistic-concurrency guard: the revision of the record you last read. A mismatch is "
    "rejected with STALE_REVISION and the observed revision."
)


@dataclass(frozen=True)
class Param:
    name: str
    type: str  # one of _T keys
    doc: str = ""
    required: bool = False


@dataclass(frozen=True)
class Tool:
    name: str
    kind: str  # "command" | "query" | "artifact_access"
    target: str  # lab command/query name
    doc: str
    params: tuple[Param, ...] = ()
    project: bool = True  # whether a project id is required
    fixed: dict[str, Any] = field(default_factory=dict)  # constant payload fields (e.g. record kind)
    rename: dict[str, str] = field(default_factory=dict)  # tool arg name -> payload key
    grant: str = ""

    @property
    def mutating(self) -> bool:
        return self.kind == "command"

    def input_schema(self) -> dict[str, Any]:
        props: dict[str, Any] = {}
        required: list[str] = []
        if self.project:
            props["project"] = {"type": "string", "description": "Project id."}
            required.append("project")
        if self.mutating:
            props["command_key"] = {"type": "string", "minLength": 1, "maxLength": 200,
                                    "description": COMMAND_KEY_DOC}
            required.append("command_key")
            props["expected_revision"] = {"type": "integer", "description": EXPECTED_REVISION_DOC}
        for p in self.params:
            d: dict[str, Any] = {"type": _T[p.type]}
            if p.doc:
                d["description"] = p.doc
            props[p.name] = d
            if p.required:
                required.append(p.name)
        props["payload"] = {"type": "object", "description": "Additional documented fields of the underlying "
                            "domain operation not listed above; named arguments take precedence."}
        return {"type": "object", "properties": props, "required": required, "additionalProperties": False}

    def description(self) -> str:
        tail = f" [grant: {self.grant}]" if self.grant else ""
        kind = "Mutating command" if self.mutating else "Read-only query"
        return f"{self.doc} ({kind} `{self.target}`.{tail})"


def P(name: str, type_: str, doc: str = "", required: bool = False) -> Param:
    return Param(name, type_, doc, required)


_RUN = (
    P("component_ref", "s", "Release ref, e.g. name@1.2.3 (from list_components).", True),
    P("inputs", "o", "Port name -> artifact id."),
    P("config", "o", "Component configuration, validated against the release's config_schema (describe_component). "
      "Send JSON types exactly: arrays as JSON arrays, integers and numbers as JSON numbers."),
    P("overrides", "o", "Explicit overrides of bound defaults."),
    P("environment", "s", "Project environment name."),
    P("seed", "i"), P("wall_seconds", "i", "Wall-clock ceiling for the job."),
    P("purpose", "s", "e.g. open-search | confirmation | qualification."),
    P("prediction", "s"), P("falsifier", "s"), P("hypothesis", "s"),
    P("investigation_id", "s", "Charge/attribute to this investigation."),
)

TOOLS: tuple[Tool, ...] = (
    # -- components ---------------------------------------------------------------------------
    Tool("list_components", "query", "search_components",
         "Search installed component releases (data, not tools): authorized releases with contract, effects, "
         "qualification and unmet requirements.",
         (P("text", "s", "Free-text filter."), P("capability", "s", "Capability the release implements."),
          P("effect", "s", "Scientific effect declared by the release."),
          P("include_inactive", "b", "Include retired/withdrawn releases."),
          P("cursor", "i", "next_cursor from the previous page."), P("limit", "i")),
         grant="project:read"),
    Tool("describe_component", "query", "describe_component",
         "Full release record for a component ref: manifest/contract, approvals and qualifications.",
         (P("component_ref", "s", required=True),), grant="project:read"),
    Tool("validate_component", "command", "validate_component",
         "Admit a build/validation job for an uploaded immutable source bundle. Returns a job handle; poll get_run.",
         (P("bundle_artifact_id", "s", required=True), P("environment", "s"), P("isolation_profile", "s"),
          P("wall_seconds", "i"), P("investigation_id", "s")), grant="build"),
    Tool("publish_component", "command", "publish_component",
         "Publish a release from a bundle whose validation job succeeded. Does not bind it (see bind_component).",
         (P("bundle_artifact_id", "s", required=True), P("validation_job_id", "s", required=True),
          P("origin", "s"), P("investigation_id", "s")), grant="publish_component"),
    Tool("bind_component", "command", "bind_component",
         "Bind a release to a capability in a new project profile revision (affects future resolutions only).",
         (P("component_ref", "s", required=True), P("capability", "s"), P("mode", "s", "replace | add"),
          P("as_default", "b"), P("acknowledge_metric_change", "b",
                                  "Required when the binding changes a metric definition.")),
         grant="manage_profile"),
    Tool("retire_component", "command", "retire_component",
         "Hide a release from new bindings; live jobs, exports and evidence are unaffected.",
         (P("component_ref", "s", required=True),), grant="manage_profile"),
    Tool("withdraw_component", "command", "withdraw_component",
         "Scientific/security withdrawal: name the defect and affected-condition predicate; marks affected "
         "assessments and queues reassessment. History is appended to, never rewritten.",
         (P("component_ref", "s", required=True), P("defect", "s", required=True),
          P("condition", "o", "Affected-condition predicate."), P("severity", "s", "scientific | security")),
         grant="withdraw"),
    # -- investigations -----------------------------------------------------------------------
    Tool("start_investigation", "command", "start_investigation",
         "Create a durable investigation (the id is returned and survives disconnects). Exploration needs a "
         "committed charter: pass charter_id from commit_study_charter.",
         (P("question", "s", required=True), P("model_artifact_id", "s"), P("environment", "s"),
          P("charter_id", "s"), P("limits", "o", "Budget ceilings by dimension."),
          P("confirmation_envelope", "o", "Separate confirmation budget by dimension."),
          P("investigator", "o", "Investigator policy overrides."), P("investigation_id", "s"), P("notes", "s")),
         grant="investigate"),
    Tool("steer_investigation", "command", "steer_investigation",
         "Append an attributed instruction effective at the next turn. Cannot change success rules "
         "(use amend_study_charter; threshold changes are rejected with THRESHOLD_OVERRIDE).",
         (P("investigation_id", "s", required=True), P("instruction", "s", required=True),
          P("structured", "o")), grant="steer"),
    Tool("pause_investigation", "command", "pause_investigation",
         "Stop new turns; declare what happens to running jobs.",
         (P("investigation_id", "s", required=True),
          P("running_job_policy", "s", "drain (default) | checkpoint-and-stop | cancel")), grant="control"),
    Tool("resume_investigation", "command", "resume_investigation",
         "Resume a paused investigation.", (P("investigation_id", "s", required=True),), grant="control"),
    Tool("commit_study_charter", "command", "commit_study_charter",
         "Commit an immutable charter (question, metrics, success rules, population, comparator, known "
         "exposures) before exploration. Optionally attach it to an existing investigation.",
         (P("charter", "o", "Charter body; study_id and question are required.", True),
          P("investigation_id", "s")), grant="define_study"),
    Tool("amend_study_charter", "command", "amend_study_charter",
         "Create a new charter revision preserving prior values, rationale, author and exposures.",
         (P("charter_id", "s", required=True), P("charter", "o", "Amended charter body.", True),
          P("rationale", "s"), P("exposure_refs", "a")), grant="amend_charter"),
    Tool("get_study_charter", "query", "get_record", "Read a charter (optionally a specific revision).",
         (P("charter_id", "s", required=True), P("revision", "i"),
          P("history", "b", "Return the full amendment history instead of one revision.")),
         fixed={"kind": "charter"}, rename={"charter_id": "id"}, grant="project:read"),
    # -- trials and runs ------------------------------------------------------------------------
    Tool("plan_trial", "command", "plan_trial",
         "Resolve an immutable plan with estimated cost. Nothing runs until launch_trial.", _RUN, grant="plan"),
    Tool("launch_trial", "command", "launch_trial",
         "Admit a planned trial: validates the plan and the active budget reservation. Returns the job id.",
         (P("trial_id", "s", required=True),), grant="spend_compute"),
    Tool("run_component", "command", "run_component",
         "Resolve, admit and dispatch one component invocation (same path as a trial). Returns a job handle.",
         _RUN, grant="invoke"),
    Tool("cancel_run", "command", "cancel_run",
         "Request cancellation of an admitted job. The receipt means the request is committed; "
         "cancel_requested persists until the backend confirms termination.",
         (P("job_id", "s", required=True), P("policy", "s")), grant="control"),
    # -- claims and evidence ----------------------------------------------------------------------
    Tool("file_claim", "command", "file_claim",
         "File (or, with supersedes_revision, revise) a scoped claim.",
         (P("statement", "s", required=True), P("kind", "s", required=True), P("scope", "o"),
          P("assumptions", "a"), P("alternatives", "a"), P("supporting", "a"), P("contradicting", "a"),
          P("fork_point", "o"), P("claim_id", "s"), P("supersedes_revision", "i"), P("investigation_id", "s")),
         grant="propose"),
    Tool("draft_evidence_plan", "command", "draft_evidence_plan",
         "Compile a draft evidence plan against the pinned charter; success rules come from the charter, "
         "any threshold in the draft is rejected (THRESHOLD_OVERRIDE).",
         (P("claim_id", "s", required=True), P("charter_id", "s", required=True),
          P("template_ref", "s", required=True), P("draft", "o"), P("draft_artifact_id", "s"),
          P("claim_revision", "i"), P("charter_revision", "i"), P("plan_id", "s"), P("investigation_id", "s")),
         grant="propose"),
    Tool("freeze_evidence_plan", "command", "freeze_evidence_plan",
         "Freeze a qualified draft into an immutable plan digest. Needs approved template/assessor releases "
         "and the freeze grant or an auto-freeze policy.",
         (P("plan_id", "s", required=True),), grant="propose"),
    Tool("launch_evidence_plan", "command", "launch_evidence_plan",
         "Launch a frozen plan: admits one job per arm and returns the job ids.",
         (P("plan_id", "s", required=True),), grant="spend_compute"),
    Tool("get_claim", "query", "get_claim",
         "Claim with its assessments (outcome, conformance, evidence health) and support status.",
         (P("claim_id", "s", required=True), P("revision", "i")), grant="project:read"),
    Tool("get_evidence_plan", "query", "get_record", "Read an evidence plan (optionally a revision).",
         (P("plan_id", "s", required=True), P("revision", "i")),
         fixed={"kind": "evidence_plan"}, rename={"plan_id": "id"}, grant="project:read"),
    Tool("get_assessment", "query", "get_assessment",
         "Assessment outcome, conformance and evidence health.",
         (P("assessment_id", "s", required=True),), grant="project:read"),
    # -- branching and protocols --------------------------------------------------------------------
    Tool("plan_branch", "command", "plan_branch",
         "Plan a child trial from an immutable parent checkpoint with explicit migration, stream policy "
         "and restore class. Returns a trial id for launch_branch.",
         (P("parent_checkpoint_id", "s", required=True), P("intervention", "o"), P("migration", "o"),
          P("stream_policy", "s", "paired | independent (independent needs an explicit seed)"),
          P("new_state", "a"), P("additional_budget", "o"), P("claimed_restore_class", "s"), *_RUN),
         grant="plan"),
    Tool("launch_branch", "command", "launch_branch", "Admit a planned branch trial.",
         (P("trial_id", "s", required=True),), grant="spend_compute"),
    Tool("qualify_protocol", "command", "qualify_protocol",
         "Admit an independent qualification job running a template/assessor release on every committed "
         "control of a registered suite.",
         (P("suite_id", "s", required=True), P("release_ref", "s", required=True), P("runner_ref", "s"),
          P("inputs", "o"), P("wall_seconds", "i")), grant="qualify"),
    Tool("promote_protocol", "command", "promote_protocol",
         "Approve a template/assessor for specified claim kinds. Requires passing planted and null "
         "qualification receipts regardless of who asks.",
         (P("release_ref", "s", required=True), P("scope", "a", "Claim kinds to approve for.", True),
          P("note", "s")), grant="promote_protocol"),
    # -- reads -----------------------------------------------------------------------------------------
    Tool("get_investigation", "query", "get_investigation",
         "Investigation status, revision, turns with stop reasons, jobs and budgets.",
         (P("investigation_id", "s", required=True),), grant="project:read"),
    Tool("get_run", "query", "get_job",
         "A job: status, lock, attempts, outputs, terminal receipt.",
         (P("job_id", "s", required=True), P("include_lock", "b")), grant="project:read"),
    Tool("list_events", "query", "list_events",
         "Cursor-paginated events with a snapshot horizon. Resume from next_cursor; pass horizon to keep a "
         "stable snapshot across pages.",
         (P("investigation_id", "s"), P("cursor", "i"), P("horizon", "i"), P("limit", "i"), P("types", "a")),
         grant="project:read"),
    Tool("compare_trials", "query", "compare_trials",
         "Side-by-side receipts for jobs with eligibility for matched comparison and shared-trajectory "
         "warnings.", (P("job_ids", "a", required=True), P("investigation_id", "s")), grant="project:read"),
    Tool("get_artifact_access", "artifact_access", "get_artifact",
         "Artifact metadata plus an authorized representation: 'metadata' (default), 'preview' (bounded "
         "text/JSON/CSV excerpt) or 'content' (returns the lab API path to stream the bytes with your own "
         "bearer token; no credential-bearing URL is minted).",
         (P("artifact_id", "s", required=True), P("purpose", "s", "Why access is needed (recorded in the call)."),
          P("representation", "s", "metadata | preview | content"), P("max_bytes", "i")),
         grant="artifact:read"),
)

BY_NAME: dict[str, Tool] = {t.name: t for t in TOOLS}
assert len(BY_NAME) == len(TOOLS)


def build_call(tool: Tool, args: dict[str, Any]) -> tuple[dict[str, Any], str | None, str | None, int | None]:
    """Split validated tool arguments into (payload, project, command_key, expected_revision)."""
    payload: dict[str, Any] = dict(args.get("payload") or {})
    for p in tool.params:
        if p.name in args and args[p.name] is not None:
            payload[tool.rename.get(p.name, p.name)] = args[p.name]
    payload.update(tool.fixed)
    return payload, args.get("project"), args.get("command_key"), args.get("expected_revision")
