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
--
-- EXAMPLE (one-time, existing hosts only): growth_agent_prospects.draft_message was
-- added after this table may already exist on a host. A fresh host gets it for free via
-- the CREATE TABLE IF NOT EXISTS below; a host with the table already created needs one
-- manual run of:
--   ALTER TABLE growth_agent_prospects ADD COLUMN IF NOT EXISTS draft_message TEXT;
-- (see README section 2 for the exact docker-wrapped psql invocation).
--
-- EXAMPLE #2 (one-time, existing hosts only): growth_agent_prospects.segment was added
-- when the 18:30 social_commerce acquisition experiment started, alongside the 12:30
-- shopify_smb one already writing to this table. DEFAULT 'shopify_smb' means a host with
-- the table already created needs only:
--   ALTER TABLE growth_agent_prospects ADD COLUMN IF NOT EXISTS segment VARCHAR NOT NULL DEFAULT 'shopify_smb';
-- which correctly backfills every pre-existing row (all from the 12:30 task) without any
-- code change to that task - it never sets segment explicitly and keeps getting the
-- default. Only the 18:30 task's own insert path sets segment = 'social_commerce'
-- explicitly.

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
    domain VARCHAR NOT NULL UNIQUE,              -- normalized dedup key (see normalize_prospect_domain);
                                                  -- for a website-less prospect, "instagram.com/<handle>" or
                                                  -- "facebook.com/<handle>" WITH the handle included
    business VARCHAR,
    website VARCHAR,
    email VARCHAR,
    draft_message TEXT,                          -- LLM-drafted outreach text, redrafted (overwritten) every resurfacing
    status VARCHAR NOT NULL DEFAULT 'surfaced',  -- 'surfaced' | 'contacted' | 'skipped'
    times_surfaced INTEGER NOT NULL DEFAULT 0,
    first_seen_at TIMESTAMP NOT NULL DEFAULT now(),
    last_surfaced_at TIMESTAMP,
    outcome_note TEXT,
    resolved_at TIMESTAMP,
    segment VARCHAR NOT NULL DEFAULT 'shopify_smb'  -- 'shopify_smb' (12:30 Apify/Shopify task) |
                                                     -- 'social_commerce' (18:30 Instagram-first task) -
                                                     -- lets the two acquisition-channel experiments share
                                                     -- one tracking table and still be compared/queried apart
);
GRANT SELECT, INSERT, UPDATE ON growth_agent_prospects TO growth_agent_tracking;
GRANT USAGE, SELECT ON SEQUENCE growth_agent_prospects_id_seq TO growth_agent_tracking;

COMMIT;
