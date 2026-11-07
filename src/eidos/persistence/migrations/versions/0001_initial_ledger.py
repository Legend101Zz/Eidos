"""Initial research ledger schema, append-only guards and role grants.

Revision ID: 0001
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

APPEND_ONLY = ("records", "events", "budget_entries", "lineage", "publications", "exposures", "decisions")

DDL = r"""
CREATE SCHEMA IF NOT EXISTS ledger;
SET search_path TO ledger;

CREATE TABLE principals (
  id text PRIMARY KEY,
  kind text NOT NULL CHECK (kind IN ('human','service','job')),
  display text NOT NULL DEFAULT '',
  project_ids text[] NOT NULL DEFAULT '{}',
  scopes text[] NOT NULL DEFAULT '{}',
  token_hash text UNIQUE,
  attributes jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz,
  revoked_at timestamptz
);

CREATE TABLE projects (
  id text PRIMARY KEY,
  name text NOT NULL,
  created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE commands (
  id bigserial PRIMARY KEY,
  project_id text NOT NULL,
  principal_id text NOT NULL,
  command_key text NOT NULL,
  command_type text NOT NULL,
  payload_hash text NOT NULL,
  status text NOT NULL,
  receipt jsonb,
  delegation jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, principal_id, command_key)
);

CREATE TABLE records (
  kind text NOT NULL,
  id text NOT NULL,
  revision integer NOT NULL CHECK (revision >= 1),
  project_id text NOT NULL REFERENCES projects(id),
  body jsonb NOT NULL,
  digest text NOT NULL,
  created_by text NOT NULL,
  command_id bigint REFERENCES commands(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (kind, id, revision)
);

CREATE TABLE heads (
  kind text NOT NULL,
  id text NOT NULL,
  project_id text NOT NULL REFERENCES projects(id),
  revision integer NOT NULL,
  status text NOT NULL,
  parent_id text,
  labels jsonb NOT NULL DEFAULT '{}',
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (kind, id)
);
CREATE INDEX heads_project_kind ON heads (project_id, kind);
CREATE INDEX heads_parent ON heads (parent_id);

CREATE TABLE events (
  seq bigserial PRIMARY KEY,
  project_id text NOT NULL,
  investigation_id text,
  type text NOT NULL,
  subject text,
  body jsonb NOT NULL DEFAULT '{}',
  command_id bigint,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX events_project_seq ON events (project_id, seq);
CREATE INDEX events_investigation_seq ON events (investigation_id, seq);

CREATE TABLE outbox (
  id bigserial PRIMARY KEY,
  event_seq bigint,
  topic text NOT NULL,
  dedupe_key text NOT NULL UNIQUE,
  payload jsonb NOT NULL,
  available_at timestamptz NOT NULL DEFAULT now(),
  dispatched_at timestamptz,
  attempts integer NOT NULL DEFAULT 0,
  last_error text
);
CREATE INDEX outbox_pending ON outbox (available_at) WHERE dispatched_at IS NULL;

CREATE TABLE jobs (
  id text PRIMARY KEY,
  project_id text NOT NULL REFERENCES projects(id),
  investigation_id text,
  kind text NOT NULL,
  trial_id text,
  purpose text NOT NULL DEFAULT 'open-search',
  status text NOT NULL,
  epoch integer NOT NULL DEFAULT 0,
  lock jsonb NOT NULL,
  lock_digest text NOT NULL,
  current_attempt text,
  cancel_requested boolean NOT NULL DEFAULT false,
  terminal jsonb,
  created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_project_status ON jobs (project_id, status);
CREATE INDEX jobs_investigation ON jobs (investigation_id);

CREATE TABLE attempts (
  id text PRIMARY KEY,
  job_id text NOT NULL REFERENCES jobs(id),
  epoch integer NOT NULL,
  status text NOT NULL,
  launcher text NOT NULL,
  backend_handle jsonb NOT NULL DEFAULT '{}',
  lease_expires_at timestamptz,
  usage jsonb NOT NULL DEFAULT '{}',
  receipt jsonb,
  started_at timestamptz NOT NULL DEFAULT now(),
  ended_at timestamptz,
  UNIQUE (job_id, epoch)
);

CREATE TABLE artifacts (
  id text PRIMARY KEY,
  project_id text NOT NULL REFERENCES projects(id),
  digest text NOT NULL,
  size bigint NOT NULL,
  media_type text NOT NULL,
  schema text,
  role text NOT NULL DEFAULT 'output',
  access_class text NOT NULL DEFAULT 'project',
  origin text NOT NULL,
  producer_job text,
  producer_attempt text,
  meta jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX artifacts_digest ON artifacts (digest);
CREATE INDEX artifacts_project ON artifacts (project_id);

CREATE TABLE publications (
  job_id text NOT NULL REFERENCES jobs(id),
  slot text NOT NULL,
  epoch integer NOT NULL,
  attempt_id text NOT NULL,
  artifact_id text NOT NULL REFERENCES artifacts(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (job_id, slot)
);

CREATE TABLE lineage (
  src text NOT NULL,
  dst text NOT NULL,
  relation text NOT NULL,
  project_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (src, dst, relation)
);
CREATE INDEX lineage_dst ON lineage (dst);

CREATE TABLE budgets (
  scope_id text NOT NULL,
  dimension text NOT NULL,
  project_id text NOT NULL,
  limit_amount numeric NOT NULL CHECK (limit_amount >= 0),
  reserved numeric NOT NULL DEFAULT 0 CHECK (reserved >= 0),
  used numeric NOT NULL DEFAULT 0 CHECK (used >= 0),
  uncertain numeric NOT NULL DEFAULT 0 CHECK (uncertain >= 0),
  PRIMARY KEY (scope_id, dimension)
);
-- Admission enforces reserved + used <= limit when reserving; actual usage may exceed it
-- (late provider charges) and must stay visible rather than be clamped.

CREATE TABLE budget_entries (
  id bigserial PRIMARY KEY,
  scope_id text NOT NULL,
  dimension text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('reserve','release','usage','possible-usage','correction')),
  amount numeric NOT NULL,
  ref text,
  detail jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX budget_entries_scope ON budget_entries (scope_id, dimension);

CREATE TABLE turns (
  id text PRIMARY KEY,
  investigation_id text NOT NULL,
  project_id text NOT NULL,
  seq integer NOT NULL,
  status text NOT NULL,
  study_revision integer NOT NULL,
  horizon bigint NOT NULL,
  spec jsonb NOT NULL,
  result jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  ended_at timestamptz,
  UNIQUE (investigation_id, seq)
);

CREATE TABLE decisions (
  id text PRIMARY KEY,
  turn_id text NOT NULL REFERENCES turns(id),
  slot integer NOT NULL,
  accepted boolean NOT NULL,
  body jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX decisions_one_accepted_per_slot ON decisions (turn_id, slot) WHERE accepted;

CREATE TABLE exposures (
  id bigserial PRIMARY KEY,
  project_id text NOT NULL,
  study_id text,
  principal_id text NOT NULL,
  subject text NOT NULL,
  disclosed jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX exposures_study ON exposures (project_id, study_id);

-- Append-only guard: even a role with UPDATE/DELETE privilege cannot rewrite history.
CREATE OR REPLACE FUNCTION ledger.forbid_rewrite() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'ledger table % is append-only', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END $$;
"""


def upgrade() -> None:
    op.execute(DDL)
    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON ledger.{table} "
            f"FOR EACH ROW EXECUTE FUNCTION ledger.forbid_rewrite()"
        )
    # decisions: the 'accepted' flag is written once at insert; no updates allowed.


def downgrade() -> None:
    op.execute("DROP SCHEMA ledger CASCADE")
