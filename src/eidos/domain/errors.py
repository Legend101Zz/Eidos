"""Machine-readable domain errors. Every error carries a stable code and a next action."""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    code = "INVALID"
    http_status = 400
    next_action = "Correct the request and retry with a new command key."

    def __init__(self, message: str, *, details: dict[str, Any] | None = None, next_action: str | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if next_action:
            self.next_action = next_action

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details, "next_action": self.next_action}


class Invalid(DomainError):
    code = "INVALID"


class NotFound(DomainError):
    code = "NOT_FOUND"
    http_status = 404
    next_action = "Check the identifier and your project access."


class Forbidden(DomainError):
    code = "FORBIDDEN"
    http_status = 403
    next_action = "Use a principal holding the required grant."


class StaleRevision(DomainError):
    code = "STALE_REVISION"
    http_status = 409
    next_action = "Re-read the current revision and resubmit against it."


class CommandConflict(DomainError):
    code = "COMMAND_KEY_CONFLICT"
    http_status = 409
    next_action = "A different payload was already submitted with this command key; use a new key."


class BudgetExhausted(DomainError):
    code = "BUDGET_EXHAUSTED"
    http_status = 409
    next_action = "Request a budget amendment or stop with a scoped conclusion."


class CharterRequired(DomainError):
    code = "CHARTER_REQUIRED"
    http_status = 409
    next_action = "Commit a study charter (or an exploration-only charter) before exploratory trials."


class ThresholdOverride(DomainError):
    code = "THRESHOLD_OVERRIDE"
    http_status = 409
    next_action = "Success rules come from the pinned charter; amend the charter instead."


class QualificationMissing(DomainError):
    code = "QUALIFICATION_MISSING"
    http_status = 409
    next_action = "Run the qualification suite with planted-effect and null controls first."


class Fenced(DomainError):
    code = "FENCED"
    http_status = 409
    next_action = "This attempt's epoch is no longer current; its outputs cannot be published."


class ResolutionError(DomainError):
    code = "RESOLUTION_ERROR"
    http_status = 422
    next_action = "Bind the missing or ambiguous capability, or adjust the request."


class IncompatibleHook(ResolutionError):
    code = "INCOMPATIBLE_HOOK"


class OutcomeUnknown(DomainError):
    code = "OUTCOME_UNKNOWN"
    http_status = 409
    next_action = "Reconcile the backend before launching a replacement; possible spend is reserved."


class PayloadTooLarge(DomainError):
    code = "PAYLOAD_TOO_LARGE"
    http_status = 413
    next_action = "Stage the content as an artifact and pass its handle."


class IsolationUnsatisfied(DomainError):
    code = "ISOLATION_UNSATISFIED"
    http_status = 409
    next_action = "Select an execution profile whose qualified controls satisfy the plan."


class Unauthenticated(DomainError):
    code = "UNAUTHENTICATED"
    http_status = 401
    next_action = "Supply a valid bearer token."


class ContentUnverifiable(DomainError):
    code = "CONTENT_UNVERIFIABLE"
    http_status = 409
    next_action = ("Staged bytes are missing or no longer match their declared digest/size; nothing was published. "
                   "Re-stage the content from its source, or report the attempt as failed.")


class WithdrawnMethod(DomainError):
    code = "WITHDRAWN_METHOD"
    http_status = 409
    next_action = ("A release this work depends on was withdrawn. Reassess under a corrected, approved release "
                   "(reassess_evidence_plan) or rely on an alternate justification.")


class QuestionRevised(DomainError):
    code = "QUESTION_REVISED"
    http_status = 409
    next_action = "The investigation question changed after this turn began; propose new work in the next turn."


class EffectNotAdmitted(DomainError):
    code = "EFFECT_NOT_ADMITTED"
    http_status = 409
    next_action = ("The decision this effect belongs to may no longer act (the investigation was stopped or paused, the "
                   "turn closed, the decision was revoked, or another run owns the turn); nothing was written.")
