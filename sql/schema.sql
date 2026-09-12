-- growth-agent schema: the three tables this service owns and writes to
-- (growth_agent_tracked_items, growth_agent_lead_outreach, growth_agent_prospects).
--
-- WHAT THIS IS: the single, checked-in source of truth for those tables' shape. They
-- previously existed only as inline SQL in README section 2 - not executable, easy to
-- drift from what was actually live.
--
-- WHEN TO RUN IT: once per Postgres host (local dev + the VPS are separate instances),
-- on a fresh database or after a restore that predates these tables:
--
--   psql -v ON_ERROR_STOP=1 -h <host> -U datalayer -d datalayer -f sql/schema.sql
--
-- (README section 2 has the exact docker-wrapped form for this project's setup.)
--
-- Idempotent: every statement is CREATE TABLE IF NOT EXISTS or a GRANT, so re-running it
-- against a database that already has the tables is a harmless no-op.
--
-- PREREQUISITE: the growth_agent_ro and growth_agent_tracking roles must already exist
-- (README sections 1-2 - that step carries a password and stays manual). This file only
-- creates tables and grants them to growth_agent_tracking; it never creates or alters a
-- role.
--
-- NOT A MIGRATION TOOL: CREATE TABLE IF NOT EXISTS is a no-op when the table already
-- exists, even if its shape is stale - this file will not add a missing column or alter
-- an existing one. A schema change to any of these tables is a manual ALTER run against
-- each host, followed by updating this file to match. This is deliberately not wired
-- into either repo's Alembic migration chain - see README "why not an Alembic migration".

BEGIN;

CREATE TABLE IF NOT EXISTS growth_agent_tracked_items (
    id SERIAL PRIMARY KEY,
    brief_date DATE NOT NULL,
    category VARCHAR NOT NULL,                   -- 'action' | 'experiment' (convention only)
    description TEXT NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'pending',   -- 'pending' | 'done' | 'skipped'
    outcome_note TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    resolved_at TIMESTAMP
);
GRANT SELECT, INSERT, UPDATE ON growth_agent_tracked_items TO growth_agent_tracking;
GRANT USAGE, SELECT ON SEQUENCE growth_agent_tracked_items_id_seq TO growth_agent_tracking;

CREATE TABLE IF NOT EXISTS growth_agent_lead_outreach (
    id SERIAL PRIMARY KEY,
    email VARCHAR NOT NULL UNIQUE,
    status VARCHAR NOT NULL DEFAULT 'pending',   -- 'pending' | 'contacted' | 'skipped'
    outcome_note TEXT,
    first_seen_at TIMESTAMP NOT NULL DEFAULT now(),
    resolved_at TIMESTAMP
);
GRANT SELECT, INSERT, UPDATE ON growth_agent_lead_outreach TO growth_agent_tracking;
GRANT USAGE, SELECT ON SEQUENCE growth_agent_lead_outreach_id_seq TO growth_agent_tracking;

CREATE TABLE IF NOT EXISTS growth_agent_prospects (
    id SERIAL PRIMARY KEY,
    domain VARCHAR NOT NULL UNIQUE,              -- normalized dedup key (see normalize_prospect_domain)
    business VARCHAR,
    website VARCHAR,
    email VARCHAR,
    status VARCHAR NOT NULL DEFAULT 'surfaced',  -- 'surfaced' | 'contacted' | 'skipped'
    times_surfaced INTEGER NOT NULL DEFAULT 0,
    first_seen_at TIMESTAMP NOT NULL DEFAULT now(),
    last_surfaced_at TIMESTAMP,
    outcome_note TEXT,
    resolved_at TIMESTAMP
);
GRANT SELECT, INSERT, UPDATE ON growth_agent_prospects TO growth_agent_tracking;
GRANT USAGE, SELECT ON SEQUENCE growth_agent_prospects_id_seq TO growth_agent_tracking;

COMMIT;
