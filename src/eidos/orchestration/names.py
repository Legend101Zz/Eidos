"""Stable task-queue and workflow identifiers. Changing these breaks in-flight work."""

WORKFLOW_QUEUE = "eidos-workflows"
INVESTIGATOR_QUEUE = "eidos-investigator"
RUNNER_QUEUE = "eidos-runner"
SEALED_QUEUE = "eidos-sealed"
ASSESSOR_QUEUE = "eidos-assessor"


def investigation_workflow_id(inv_id: str) -> str:
    return f"inv-{inv_id}"


def job_workflow_id(job_id: str) -> str:
    return f"job-{job_id}"


def assessment_workflow_id(plan_id: str) -> str:
    return f"assess-{plan_id}"
