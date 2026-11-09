"""Scoped, budgeted LLM grants for component invocations and the broker's ledger commands (ADR 0013).

Flow: admission writes ``lock["llm"]`` (``llm_lock``); the launcher issues a grant per attempt with only the token's
hash (``issue_llm_grant``); for each request the broker reserves before calling the provider
(``broker_reserve_call``) and settles after (``broker_settle_call``); open reservations of a finished attempt or a
dead broker are settled as uncertain (``reconcile_llm_grant``, also run by ``finish_attempt``).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from eidos.application.service import Ctx, command
from eidos.domain.errors import BudgetExhausted, DomainError, Fenced, Invalid, NotFound

TERMINAL = ("succeeded", "failed", "cancelled", "pruned")
DEFAULT_TTL = 3600
CHARS_PER_TOKEN = 4


class GrantRefused(DomainError):
    code = "GRANT_INVALID"
    http_status = 403
    next_action = "The LLM grant cannot be used for this request; nothing was sent to the provider."


def _refuse(code: str, message: str, **details: Any) -> GrantRefused:
    err = GrantRefused(message, details=details)
    err.code = code
    return err


def llm_lock(manifest: dict, policy: dict, request: dict, *, profile: str) -> dict | None:
    """The run lock's ``llm`` section: the minimum of manifest, project policy and run request limits, or None
    when the component declares no LLM use, the project does not enable the broker, or a sealed run is not
    explicitly allowed to use it."""
    use = manifest.get("llm")
    pol = (policy or {}).get("llm_broker")
    if not use or not pol or not pol.get("enabled", True):
        return None
    if profile.endswith("sealed") and not request.get("allow_llm_broker"):
        return None
    asked = dict(request.get("llm") or {})

    def lim(key: str, manifest_key: str | None = None) -> float | None:
        vals = [v for v in (use.get(manifest_key or key), pol.get(key), asked.get(key)) if v is not None]
        return min(float(v) for v in vals) if vals else None

    models = list(pol.get("models") or [])
    for narrower in (use.get("models"), asked.get("models")):
        if narrower:
            models = [m for m in models if m in narrower]
    if not models:
        raise Invalid("no model is allowed for this component's LLM grant (manifest, policy and request "
                      "allowlists do not intersect)", details={"policy_models": pol.get("models")})
    max_requests = lim("max_requests", "max_calls")
    max_tokens = lim("max_tokens")
    if max_requests is None or max_tokens is None:
        raise Invalid("an LLM grant needs max_requests and max_tokens (manifest llm.max_calls/max_tokens or policy)")
    max_usd = lim("max_usd")
    prices = {m: (pol.get("price_per_mtok") or {}).get(m) for m in models}
    if max_usd is not None and not all(prices.values()):
        raise Invalid("a max_usd limit needs policy.llm_broker.price_per_mtok for every allowed model",
                      details={"models": models})
    return {"role": use.get("role"), "prompt_revision": use.get("prompt_revision"), "models": sorted(models),
            "max_requests": int(max_requests), "max_tokens": int(max_tokens), "max_usd": max_usd,
            "price_per_mtok": {m: p for m, p in prices.items() if p},
            "ttl_seconds": int(lim("ttl_seconds") or DEFAULT_TTL),
            "sealed_allowed": bool(profile.endswith("sealed"))}


def _job(ctx: Ctx, job_id: str) -> dict:
    job = ctx.tx.get_job(job_id, lock=True)
    if not job or job["project_id"] != ctx.project_id:
        raise NotFound(f"job {job_id} not found")
    return job


@command("issue_llm_grant", "launcher")
def issue_llm_grant(ctx: Ctx, p: dict) -> dict:
    """Issue the attempt's grant. Only the SHA-256 of the token reaches the lab; the launcher keeps the token."""
    job = _job(ctx, p["job_id"])
    if job["status"] in TERMINAL or job["current_attempt"] != p["attempt_id"] or job["epoch"] != int(p["epoch"]):
        raise Fenced(f"attempt {p['attempt_id']} is not the current attempt of {job['id']}")
    spec = job["lock"].get("llm")
    if not spec:
        raise Invalid(f"job {job['id']} has no LLM grant in its lock")
    th = str(p.get("token_sha256", ""))
    if len(th) != 64:
        raise Invalid("token_sha256 must be a hex SHA-256")
    gid = f"llmg_{p['attempt_id']}"
    existing = ctx.tx.get_record("llm_grant", gid)
    if existing is not None:
        if existing.body["token_sha256"] != th:
            raise Invalid("this attempt already has a grant with another token")
        return _grant_view(existing.id, existing.body)
    expires = datetime.now(UTC) + timedelta(seconds=int(spec["ttl_seconds"]))
    body = {**{k: spec[k] for k in ("models", "max_requests", "max_tokens", "max_usd", "price_per_mtok",
                                    "role", "prompt_revision")},
            "token_sha256": th, "expires_at": expires.isoformat(), "job_id": job["id"], "attempt_id": p["attempt_id"],
            "epoch": int(p["epoch"]), "component": job["lock"].get("component", {}).get("ref"),
            "budget_scope": job["lock"].get("budget_scope"), "investigation_id": job["investigation_id"]}
    ctx.tx.put_record("llm_grant", gid, ctx.project_id, body, created_by=ctx.principal.id, status="active",
                      parent_id=job["id"])
    ctx.event("llm.grant_issued", gid, {"job_id": job["id"], "attempt_id": p["attempt_id"], "models": body["models"],
              "max_requests": body["max_requests"], "max_tokens": body["max_tokens"], "max_usd": body["max_usd"]},
              investigation_id=job["investigation_id"])
    return _grant_view(gid, body)


def _grant_view(gid: str, body: dict) -> dict:
    return {"grant_id": gid, **{k: body[k] for k in ("models", "max_requests", "max_tokens", "max_usd", "expires_at")}}


def _calls(ctx: Ctx, gid: str) -> list:
    return ctx.tx.list_records(ctx.project_id, "llm_call", parent_id=gid, limit=100000)


def _usd(prices: dict, model: str, input_tokens: float, output_tokens: float) -> float:
    p = prices.get(model) or {}
    return (input_tokens * float(p.get("input", 0)) + output_tokens * float(p.get("output", 0))) / 1e6


@command("broker_reserve_call", "llm_broker")
def broker_reserve_call(ctx: Ctx, p: dict) -> dict:
    """Authenticate a grant by token hash, check every limit and the invocation identity, and reserve the request's
    maximum against the grant and the job's budget scope. Refuses (nothing is sent to a provider) otherwise."""
    th = str(p.get("token_sha256", ""))
    grants = [g for g in ctx.tx.list_records(ctx.project_id, "llm_grant", limit=100000) if g.body["token_sha256"] == th]
    if not grants:
        raise _refuse("GRANT_INVALID", "unknown LLM grant")
    g = ctx.tx.get_record("llm_grant", grants[0].id, lock=True)
    assert g is not None
    if g.status != "active":
        raise _refuse("GRANT_INVALID", f"grant {g.id} is {g.status}")
    if datetime.fromisoformat(g.body["expires_at"]) <= datetime.now(UTC):
        raise _refuse("GRANT_EXPIRED", f"grant {g.id} expired at {g.body['expires_at']}")
    job = ctx.tx.get_job(g.body["job_id"])
    if (not job or job["status"] in TERMINAL or job["current_attempt"] != g.body["attempt_id"]
            or job["epoch"] != g.body["epoch"]):
        raise _refuse("WRONG_INVOCATION", f"grant {g.id} belongs to attempt {g.body['attempt_id']}, which is no "
                      "longer the job's current, running attempt")
    model = p.get("model") or g.body["models"][0]
    if model not in g.body["models"]:
        raise _refuse("MODEL_NOT_ALLOWED", f"model {model!r} is not in this grant", allowed=g.body["models"])
    calls = _calls(ctx, g.id)
    rk = p.get("request_key")
    if rk:
        for c in calls:
            if c.body.get("request_key") == rk:
                return {"call_id": c.id, "grant_id": g.id, "model": c.body["model"], "replayed": True,
                        "status": c.status, "result": c.body.get("result")}
    max_out = int(p.get("max_tokens") or 0)
    if max_out <= 0:
        raise Invalid("max_tokens must be a positive integer")
    est_in = int(p.get("input_chars", 0)) // CHARS_PER_TOKEN + 1
    tokens = float(est_in + max_out)
    used_req = len(calls)
    used_tok = sum(float(c.body.get("charged_tokens", c.body["reserved_tokens"])) for c in calls)
    used_usd = sum(float(c.body.get("charged_usd", c.body["reserved_usd"])) for c in calls)
    usd = _usd(g.body.get("price_per_mtok") or {}, model, est_in, max_out)
    if used_req + 1 > g.body["max_requests"]:
        raise _refuse("GRANT_EXHAUSTED", f"grant {g.id} allows {g.body['max_requests']} requests", used=used_req)
    if used_tok + tokens > g.body["max_tokens"]:
        raise _refuse("GRANT_EXHAUSTED", f"this request may use {int(tokens)} tokens; the grant has "
                      f"{int(g.body['max_tokens'] - used_tok)} left", used_tokens=used_tok)
    if g.body.get("max_usd") is not None and used_usd + usd > float(g.body["max_usd"]):
        raise _refuse("GRANT_EXHAUSTED", f"this request may cost {usd:.6f} USD; the grant has "
                      f"{float(g.body['max_usd']) - used_usd:.6f} left", used_usd=used_usd)
    cid = f"llmc_{uuid.uuid4().hex[:16]}"
    scope = g.body.get("budget_scope")
    reserved: dict[str, float] = {}
    if scope:
        budgets = ctx.tx.budgets(scope)
        try:
            for dim, amount in (("model_tokens", tokens), ("model_usd", usd)):
                if dim in budgets and amount > 0:
                    ctx.tx.reserve(scope, dim, amount, f"llm:{g.id}:{cid}")
                    reserved[dim] = amount
        except BudgetExhausted as exc:
            raise _refuse("BUDGET_EXHAUSTED", f"the investigation's ledger cannot cover this request: {exc.message}",
                          scope=scope) from None
    body = {"grant_id": g.id, "job_id": g.body["job_id"], "attempt_id": g.body["attempt_id"], "model": model,
            "request_key": rk, "max_tokens": max_out, "input_tokens_estimate": est_in, "reserved_tokens": tokens,
            "reserved_usd": usd, "ledger_reserved": reserved, "budget_scope": scope}
    ctx.tx.put_record("llm_call", cid, ctx.project_id, body, created_by=ctx.principal.id, status="reserved",
                      parent_id=g.id)
    return {"call_id": cid, "grant_id": g.id, "model": model, "replayed": False, "status": "reserved"}


def _settle(ctx: Ctx, call, *, status: str, usage: dict, request_id: str | None, response_digest: str | None,
            error: dict | None, reason: str) -> dict:
    body = dict(call.body)
    g = ctx.tx.require("llm_grant", body["grant_id"])
    uncertain = status == "uncertain"
    if uncertain:  # the request may have been charged: the reserved maximum is possible usage
        tokens, usd = float(body["reserved_tokens"]), float(body["reserved_usd"])
        inp, out = None, None
    else:
        inp = int(usage.get("input_tokens") or 0)
        out = int(usage.get("output_tokens") or 0)
        tokens = float(inp + out)
        usd = _usd(g.body.get("price_per_mtok") or {}, body["model"], inp, out)
    scope = body.get("budget_scope")
    detail = {"provider": "llm-broker", "model": body["model"], "request_ids": [request_id] if request_id else [],
              "grant": g.id, "call": call.id, "job": body["job_id"]}
    if scope:
        for dim, actual in (("model_tokens", tokens), ("model_usd", usd)):
            held = float(body["ledger_reserved"].get(dim, 0))
            if actual or held:
                ctx.tx.settle(scope, dim, reserved=held, actual=actual, ref=f"llm:{g.id}:{call.id}",
                              uncertain=uncertain, detail=detail)
    body.update({"charged_tokens": tokens, "charged_usd": usd, "uncertain": uncertain,
                 "usage": {"input_tokens": inp, "output_tokens": out}, "provider_request_id": request_id,
                 "response_digest": response_digest, "error": error, "settled_by": reason,
                 "result": {"usage": {"input_tokens": inp, "output_tokens": out}, "usd": usd}})
    ctx.tx.put_record("llm_call", call.id, ctx.project_id, body, created_by=ctx.principal.id, status=status,
                      parent_id=g.id)
    ctx.event("llm.call", call.id, {"grant": g.id, "job_id": body["job_id"], "model": body["model"], "status": status,
              "tokens": tokens, "usd": round(usd, 8), "uncertain": uncertain, "request_id": request_id},
              investigation_id=g.body.get("investigation_id"))
    return {"call_id": call.id, "status": status, "charged_tokens": tokens, "charged_usd": usd, "uncertain": uncertain}


@command("broker_settle_call", "llm_broker")
def broker_settle_call(ctx: Ctx, p: dict) -> dict:
    """Settle one reserved call: ``ok`` charges actual usage and releases the rest; ``error`` (the provider
    refused before doing work) charges nothing; ``uncertain`` (timeout/transport error after sending) charges the
    reserved maximum as possible usage with the request id."""
    call = ctx.tx.get_record("llm_call", p["call_id"], lock=True)
    if call is None or call.project_id != ctx.project_id:
        raise NotFound(f"call {p['call_id']} not found")
    if call.status != "reserved":
        return {"call_id": call.id, "status": call.status, "already_settled": True}
    status = p.get("status")
    if status not in ("ok", "error", "uncertain"):
        raise Invalid("status must be ok, error or uncertain")
    return _settle(ctx, call, status=status, usage=p.get("usage") or {}, request_id=p.get("provider_request_id"),
                   response_digest=p.get("response_digest"), error=p.get("error"), reason="broker")


def reconcile_grant(ctx: Ctx, gid: str, *, reason: str) -> dict:
    """Close a grant; settle every call still reserved (the broker died or never reported) as uncertain."""
    g = ctx.tx.get_record("llm_grant", gid, lock=True)
    if g is None:
        return {"grant_id": gid, "closed": False}
    open_calls = [c for c in _calls(ctx, gid) if c.status == "reserved"]
    for c in open_calls:
        _settle(ctx, c, status="uncertain", usage={}, request_id=None, response_digest=None,
                error={"category": "unreported", "message": f"no settlement from the broker ({reason})"},
                reason=reason)
    if g.status == "active":
        ctx.tx.set_status("llm_grant", gid, "closed", labels={"closed_by": reason})
    return {"grant_id": gid, "closed": True, "uncertain_calls": [c.id for c in open_calls]}


@command("reconcile_llm_grant", "launcher")
def reconcile_llm_grant(ctx: Ctx, p: dict) -> dict:
    return reconcile_grant(ctx, p["grant_id"], reason=str(p.get("reason", "reconcile"))[:200])


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
