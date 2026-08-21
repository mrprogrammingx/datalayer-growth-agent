# datalayer-growth-agent

Internal growth system for DataLayer (usedatalayer.com). Once a day it:

1. Reads DataLayer's own Postgres metrics (signups, uploads, free-tool leads, and
   unconverted free-tool leads for outreach research) via a read-only DB role.
2. Reads real GA4 traffic/funnel data (sessions, per-tool-page views, funnel event
   counts) and Search Console data (top queries/pages) via Google APIs, authenticated
   with a personal OAuth refresh token (not a service account).
3. Searches a curated list of subreddits for recent posts relevant to DataLayer's free
   tools, via a read-only Reddit API app.
4. Sends all of that, plus any of its own prior recommendations still pending, to an LLM
   (`gpt-4o-mini`) to identify the single biggest growth problem, compute real funnel
   drop-off, surface SEO opportunities, score and rank recommended actions by
   Impact x Confidence x Ease (see "Recommended-action scoring" below), and draft
   Reddit replies / lead outreach messages / LinkedIn-Facebook post ideas.
5. Emails the resulting brief once a day via Resend.
6. Persists each "Recommended action" / "Suggested experiment" as its own row, so Amir
   can mark it done/skipped with an outcome note (`scripts/mark_action.py`) and future
   briefs can reference still-open items instead of repeating them as if new.
7. For items marked done 7-14 days ago, shows DataLayer's own signup/upload counts in
   the 7 days before vs. after — correlational, small-sample context only, never
   presented as evidence of causation (see "Conversion attribution" below).
8. Once a week, sends a separate deterministic rollup email — no LLM call — of
   week-over-week metrics, tracking-item counts, and recent action outcomes (see
   "Automated weekly reporting" below).

Every Reddit reply and outreach message the brief drafts is explicitly labeled a draft
for human review - this system never posts or sends anything on its own. Amir reads the
email and decides what to use.

Deliberately minimal: Flask + Postgres + OpenAI API + GA4/Search Console APIs + Reddit
API + APScheduler + Docker only. No autonomous posting/outreach, no multi-agent frameworks, no LangChain/vector
DB/Redis/Celery/Kubernetes.

This is a separate repo from `../datalayer-ecommerce` (read-only reference for schema
and conventions) and does not modify it, except that you must create a new read-only
DB role on its Postgres instance (see below).

## 1. Create the read-only DB role

Postgres runs as the `db` service in `../datalayer-ecommerce/docker-compose.yml` — the
same container/instance on both local dev and the prod VPS (`docker-compose.prod.yml`
just adds a host port-forward to it, it's not a separate database). Run this against it
(**not** auto-run by this repo):

```bash
docker compose -f ../datalayer-ecommerce/docker-compose.yml exec db psql -U datalayer -d datalayer
```

```sql
CREATE ROLE growth_agent_ro WITH LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE datalayer TO growth_agent_ro;
GRANT USAGE ON SCHEMA public TO growth_agent_ro;
GRANT SELECT ON users, uploads, csv_tool_leads TO growth_agent_ro;
```

Do **not** grant access to the `emails` table (outbound send log — not needed here).

Put the resulting connection string in `.env` as `GROWTH_AGENT_DATABASE_URL`, using `db`
as the host (see network wiring in step 5) — no SSL needed on the private compose
network, same as datalayer-ecommerce's own internal `web` → `db` connection.

## 2. Create the tracking role + table

Action/experiment tracking needs to persist state — something no other part of this
app does. Rather than reuse `growth_agent_ro` (which must stay genuinely,
DB-enforced read-only), this is a second, separate role scoped to `INSERT`/`SELECT`/
`UPDATE` on one table only, so a bug in the tracking path can never put the read-only
guarantee at risk. Run this once, same way as step 1:

```bash
docker compose -f ../datalayer-ecommerce/docker-compose.yml exec db psql -U datalayer -d datalayer
```

```sql
CREATE ROLE growth_agent_tracking WITH LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE datalayer TO growth_agent_tracking;
GRANT USAGE ON SCHEMA public TO growth_agent_tracking;

CREATE TABLE growth_agent_tracked_items (
    id SERIAL PRIMARY KEY,
    brief_date DATE NOT NULL,
    category VARCHAR NOT NULL,       -- 'action' | 'experiment' (convention only)
    description TEXT NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'pending',  -- 'pending' | 'done' | 'skipped'
    outcome_note TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    resolved_at TIMESTAMP
);

GRANT SELECT, INSERT, UPDATE ON growth_agent_tracked_items TO growth_agent_tracking;
GRANT USAGE, SELECT ON SEQUENCE growth_agent_tracked_items_id_seq TO growth_agent_tracking;
```

The last GRANT matters — `SERIAL` creates an implicit sequence that needs its own
grant under a non-owner role, or every `INSERT` fails. Do **not** grant this role
anything on `users`/`uploads`/`csv_tool_leads`, same as the read-only role above.

Put the resulting connection string in `.env` as `GROWTH_AGENT_TRACKING_DATABASE_URL`.
This is optional — if unset, tracking degrades gracefully (see `data_gaps` in the
brief) and the daily email still sends normally.

**Same instance, needs running twice.** Like `growth_agent_ro`, local dev's Postgres
and the VPS's Postgres are separate instances — run this SQL block once against each.

**Known limitation, not solved by this table**: it lives outside `datalayer-ecommerce`'s
Alembic migration history (deliberately — see below), so a future DB restore that
doesn't know about it could silently drop it. Same exposure `growth_agent_ro` already
has today.

*(Why not an Alembic migration in `datalayer-ecommerce` instead? That repo's migration
chain never issues GRANT/CREATE ROLE either — it's always a manual step, exactly like
`growth_agent_ro`'s own setup above. Coupling this table's schema to the main app's
migration chain would be unnecessary cross-repo entanglement for a table only
growth-agent ever touches.)*

**Also add the lead-outreach tracking table**, same role, one more `GRANT` — no new
`CREATE ROLE`, no new env var:

```sql
CREATE TABLE growth_agent_lead_outreach (
    id SERIAL PRIMARY KEY,
    email VARCHAR NOT NULL UNIQUE,
    status VARCHAR NOT NULL DEFAULT 'pending',  -- 'pending' | 'contacted' | 'skipped'
    outcome_note TEXT,
    first_seen_at TIMESTAMP NOT NULL DEFAULT now(),
    resolved_at TIMESTAMP
);

GRANT SELECT, INSERT, UPDATE ON growth_agent_lead_outreach TO growth_agent_tracking;
GRANT USAGE, SELECT ON SEQUENCE growth_agent_lead_outreach_id_seq TO growth_agent_tracking;
```

`email` is a plain `VARCHAR` with exact-match comparison, matching
`users`/`csv_tool_leads.email`'s existing convention elsewhere (no `citext`, no
lowercasing) — copy-paste the email from the brief rather than retyping it, to avoid a
case mismatch silently missing an exclusion. See "Lead outreach tracking" below for
what this table is for.

## 3. Google Analytics / Search Console setup

The growth brief pulls real GA4 and Search Console data via **OAuth using your own
Google account** — NOT a service account. `usedatalayer.com` is a Google Workspace
domain, and Workspace-linked Cloud orgs enforce the `iam.disableServiceAccountKeyCreation`
policy by default, which blocks downloadable service-account JSON keys entirely
(Google's own "Secure by Default" setting — don't try to disable it, that reopens the
exact risk it's there to prevent). OAuth client creation isn't affected by that policy,
and since it's your own account authorizing, there's no separate "grant access" step
needed — you already have access to both properties.

1. **Create a new GCP project.** Go to the [Cloud Console](https://console.cloud.google.com/)
   and create a new project (e.g. named `datalayer-growth-agent`). This takes about a
   minute and is free — no billing account is required for the two read-only APIs below.
2. **Enable the two APIs.** In that project, go to **APIs & Services > Library** and
   enable:
   - `Google Analytics Data API`
   - `Google Search Console API`

   Both are free — no billing required beyond normal quota limits.
3. **Configure the OAuth consent screen (Google calls this "Google Auth Platform" in the
   newer Console UI).** Go to **APIs & Services > OAuth consent screen** (or the "Google
   Auth Platform" entry in the sidebar) and click **Get started**.
   - **Audience**: set to **External**, NOT Internal — Internal only works if the Google
     account with GA4/Search Console access belongs to the `usedatalayer.com` Workspace
     domain. If that access is actually under a personal Gmail account (as it is for
     DataLayer), Internal mode will always fail with `Error 403: org_internal`, since a
     personal account can never belong to any Workspace org.
   - Fill in the minimal required fields (app name, support email, contact email) and
     finish setup.
   - Go to the **Audience** tab > **Test users** > **Add users**, and add the Google
     account (personal Gmail) that actually has GA4/Search Console access.
   - **Caveat**: apps left in "Testing" publish status sometimes have refresh tokens that
     expire after ~7 days. If that happens, `/debug/ga4`/`/debug/search-console` will
     start returning an auth error (caught gracefully, not a crash - see `data_gaps` in
     the brief) - the fix is just re-running step 5 below once for a fresh token. Not
     worth chasing app verification/publishing to production to avoid this for a
     single-user internal tool.
4. **Create an OAuth Client ID.** Go to **APIs & Services > Credentials > Create
   Credentials > OAuth client ID** (or the **Clients** tab in Google Auth Platform).
   Application type: **Desktop app**. Name it something like `growth-agent-oauth`. Note
   the **Client ID** and **Client Secret** shown.
5. **Run the one-time authorization script**, locally, on a machine with a browser (your
   laptop — not the VPS):

   ```bash
   python -m pip install google-auth-oauthlib
   python scripts/authorize_google.py <client_id> <client_secret>
   ```

   (Use `python -m pip install ...` rather than a bare `pip install ...` - on a machine
   with multiple Python installs, e.g. both conda and system Python, a bare `pip` can
   silently install into a different interpreter than the one running the script,
   causing a `ModuleNotFoundError` even though the install reported success.)

   A browser window opens — log in with the Google account that has access to
   DataLayer's GA4 property and Search Console property (the one just added as a test
   user above), and approve. You'll see an "unverified app" warning screen first - that's
   expected for an app that hasn't gone through Google's review process; click
   **Advanced > Go to \<app name\> (unsafe)** to proceed, since this is your own app and
   only you will ever use it. The script then prints a **refresh token**. Paste it into
   `.env` as `GOOGLE_OAUTH_REFRESH_TOKEN` — this same value (along with the client
   ID/secret) works on **every host**, unlike a service-account key file, so you only
   need to run this once and copy the three values to both local dev's `.env` and the
   VPS's `.env`.
6. **Record the GA4 Property ID.** In GA4, go to **Admin > Property Settings** and copy
   the numeric **Property ID** (NOT the `G-XXXXXXX` measurement ID used in the site's
   tracking snippet — that's a different identifier). This goes in `.env` as
   `GA4_PROPERTY_ID`.
7. **Confirm the Search Console property type.** In the Search Console UI, check whether
   the verified property is a **domain property** (shown as `sc-domain:usedatalayer.com`)
   or a **URL-prefix property** (shown as `https://usedatalayer.com/`). Copy the exact
   value shown — this goes in `.env` as `SEARCH_CONSOLE_SITE_URL` and must match exactly
   (including the trailing slash for URL-prefix properties).

## 4. Reddit API setup

The brief searches a curated list of subreddits for recent, relevant posts using
**read-only application-only OAuth** - no Reddit account password is ever used, since
this only ever lists/searches public posts, never posts or comments as a user.

1. Log into the Reddit account that should own this API app (any account is fine, since
   it's never used to post/comment).
2. Go to [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps) and click **create
   app** (or **create another app**).
3. Choose type **script**, give it a name (e.g. `datalayer-growth-agent`), leave the
   description/about URL blank, and set the **redirect uri** to `http://localhost:8080`
   (required by the form, not actually used for this read-only flow).
4. After creating it, note the **client ID** (the string under the app name) and
   **client secret**.
5. Put these in `.env` as `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET`, and set
   `REDDIT_USER_AGENT` to something following Reddit's recommended format, e.g.
   `server:datalayer-growth-agent:v1.0 (by /u/<your-reddit-username>)` - Reddit's API
   rules ask for a descriptive user agent and can rate-limit-penalize generic/missing
   ones.

Like the Google refresh token, these three values work on every host - copy them into
both local dev's `.env` and the VPS's `.env`, no per-host setup needed.

## 5. Configure environment

```bash
cp .env.example .env
```

Fill in:

- `GROWTH_AGENT_DATABASE_URL` — from step 1 (`db` host, `growth_agent_ro` user)
- `GROWTH_AGENT_TRACKING_DATABASE_URL` — from step 2 (`db` host, `growth_agent_tracking`
  user); optional, tracking degrades gracefully if unset
- `OPENROUTER_API_KEY` — reuses the same OpenRouter account already used elsewhere in
  datalayer-ecommerce (see its `.env`'s `Openrouter_API_KEY`); `brief.py` points the
  OpenAI SDK's `base_url` at `https://openrouter.ai/api/v1` since OpenRouter is
  OpenAI-API-compatible, so no separate OpenAI account is needed
- `RESEND_API_KEY`, `EMAIL_FROM` — reuses datalayer-ecommerce's existing verified Resend
  sender
- `GROWTH_BRIEF_RECIPIENT`
- `BRIEF_SEND_HOUR_UTC` (default `8`)
- `WEEKLY_REPORT_SEND_DAY_UTC` (default `mon`), `WEEKLY_REPORT_SEND_HOUR_UTC` (default
  `9`) — see "Automated weekly reporting" below
- `GROWTH_AGENT_NETWORK` — see step 6
- `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN`,
  `GA4_PROPERTY_ID`, `SEARCH_CONSOLE_SITE_URL` — from step 3
- `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT` — from step 4

## 6. Confirm the Docker network name

This service has no published port and is not routed through nginx — it must not be
publicly reachable. It joins the same Docker network as `../datalayer-ecommerce`'s `db`
service so it can reach Postgres by hostname (`db`).

`docker-compose.yml` declares that network as `external: true` with its name read from
`GROWTH_AGENT_NETWORK` in `.env` (default `datalayer-ecommerce_default`, Compose's
default `<project_dir_name>_default` naming). This is set via `.env` rather than hardcoded
in `docker-compose.yml` because it varies per host — it depends on what directory name
`datalayer-ecommerce` was cloned into there (e.g. local dev might use
`datalayer-ecommerce`, a VPS might use a shorter `datalayer`). **Confirm the actual name
on whichever host you're deploying to** and set `GROWTH_AGENT_NETWORK` in `.env` to match:

```bash
docker network ls | grep datalayer
```

## 7. Run it

```bash
docker compose up -d --build
```

This boots the Flask app (internal-only, no published port) and starts both APScheduler
jobs: the daily brief (default 08:00 UTC, configurable via `BRIEF_SEND_HOUR_UTC`) and
the weekly report (default Monday 09:00 UTC, configurable via
`WEEKLY_REPORT_SEND_DAY_UTC`/`WEEKLY_REPORT_SEND_HOUR_UTC`) — both emails send on
report day, the weekly report does not replace that day's daily brief.

Gunicorn runs with `--timeout 120` (not the 5-workers/no-timeout default) — a real
`/run-now` was observed crashing the worker (`SIGABRT`→`SIGKILL`) on gunicorn's default
30s timeout once the prompt grew large enough that LLM generation plus the email send
occasionally took longer than that. Confirmed live: the email had actually already sent
successfully by the time the worker was killed, so the failure mode is a false-negative
500 response, not a lost brief — but still worth the fix so `/run-now`/
`/weekly-report-now` return correctly instead of crashing.

## 8. Verify GA4/Search Console/Reddit credentials before testing the full brief

Before triggering a real (billed) `/run-now`, confirm the GA4, Search Console, and
Reddit credentials/permissions work using the three debug-only routes — these call the
fetch functions directly and return raw JSON, without touching the LLM or sending any
email:

```bash
docker compose exec growth-agent python -c "
import urllib.request
print(urllib.request.urlopen('http://localhost:8080/debug/ga4').read().decode())
"
docker compose exec growth-agent python -c "
import urllib.request
print(urllib.request.urlopen('http://localhost:8080/debug/search-console').read().decode())
"
docker compose exec growth-agent python -c "
import urllib.request
print(urllib.request.urlopen('http://localhost:8080/debug/reddit').read().decode())
"
```

If either GA4/Search Console call returns an error, double check: `GOOGLE_OAUTH_CLIENT_ID`/
`GOOGLE_OAUTH_CLIENT_SECRET`/`GOOGLE_OAUTH_REFRESH_TOKEN` are all set in `.env` (from
step 2.5), `GA4_PROPERTY_ID` is the numeric Property ID (not the `G-XXXXXXX` measurement
ID), `SEARCH_CONSOLE_SITE_URL` exactly matches the verified property's format
(`sc-domain:...` vs. `https://.../`), and — if this used to work and suddenly doesn't —
the refresh token may have expired (Testing-status apps sometimes expire tokens after
~7 days; re-run `scripts/authorize_google.py` for a fresh one).

If `/debug/reddit` returns an error, double check `REDDIT_CLIENT_ID`/
`REDDIT_CLIENT_SECRET`/`REDDIT_USER_AGENT` are set in `.env` (from step 4). A clean
empty list (`[]`) is a valid response — it means no posts matched the curated
subreddits/keywords in the last couple of days, not a credentials problem.

Only once all three debug routes return real, clean data (or a clean empty list for
Reddit) should you move on to a full end-to-end test.

There is no `/debug/tracking` route — unlike the other three sources, tracking has
no external credentials to verify ahead of time; if `GROWTH_AGENT_TRACKING_DATABASE_URL`
is set but wrong, that surfaces directly in `data_gaps` on the next real run.

## 9. Manually test end-to-end

`/run-now` triggers a real OpenRouter API call (billed) and a real email send via Resend.
**Confirm with whoever's paying before running this** if you're not sure about budget —
it's a single cheap `gpt-4o-mini` call, but it's still real usage.

This route is intentionally not exposed to the host, and the image has no `curl`
installed (kept minimal), so hit it from inside the container with Python instead:

```bash
docker compose exec growth-agent python -c "
import urllib.request
req = urllib.request.Request('http://localhost:8080/run-now', method='POST')
print(urllib.request.urlopen(req).read().decode())
"
```

Or check health:

```bash
docker compose exec growth-agent python -c "
import urllib.request
print(urllib.request.urlopen('http://localhost:8080/health').read().decode())
"
```

`/weekly-report-now` tests the weekly rollup the same way — no LLM call/OpenRouter
cost (it's deterministic), but it still sends a real email via Resend, so still confirm
before running it:

```bash
docker compose exec growth-agent python -c "
import urllib.request
req = urllib.request.Request('http://localhost:8080/weekly-report-now', method='POST')
print(urllib.request.urlopen(req).read().decode())
"
```

## 10. Review and resolve tracked items

Each day's "Recommended actions" and "Suggested experiments" get persisted as
individual rows (after the email sends, so the email itself never contains an item's
id — `list` is how you find one):

```bash
docker compose exec growth-agent python scripts/mark_action.py list
docker compose exec growth-agent python scripts/mark_action.py 7 done "shipped the popup, +12 signups"
docker compose exec growth-agent python scripts/mark_action.py 8 skipped "not worth it, low traffic page"
```

Items you haven't marked done/skipped within 3 days show up in the next brief's
`pending_from_prior_briefs` context, so the LLM can reference them as still-open
instead of silently re-recommending the same thing as if it were new. **Known
limitation**: this only catches items the LLM re-describes with the *exact same*
wording as an existing pending item — a reworded repeat of the same idea isn't
de-duped and can accumulate as a separate row. Not solved in this slice (true de-dupe
would be the kind of automation deliberately out of scope for now).

Leads work the same way, but with a **separate** script (`scripts/mark_lead.py`),
keyed by email instead of an id — the email is already printed directly in the brief,
so `list` is a convenience here, not required:

```bash
docker compose exec growth-agent python scripts/mark_lead.py list
docker compose exec growth-agent python scripts/mark_lead.py someone@example.com contacted "sent outreach email Aug 20"
docker compose exec growth-agent python scripts/mark_lead.py someone@example.com skipped "bounced, not a fit"
```

See "Lead outreach tracking" below for why leads get their own table/script instead of
reusing `growth_agent_tracked_items`/`mark_action.py`.

## Repo layout

```
docker-compose.yml
Dockerfile
requirements.txt
.env.example
scripts/
  authorize_google.py One-time local OAuth authorization helper (not in the Docker image)
  mark_action.py      CLI to list/resolve tracked action+experiment items (IS in the
                       Docker image - runs inside the container to reach Postgres)
  mark_lead.py        CLI to list/resolve lead-outreach status, keyed by email (IS in
                       the Docker image)
app/
  main.py             Flask app: GET /health, GET /debug/ga4, GET /debug/search-console,
                       GET /debug/reddit, POST /run-now, POST /weekly-report-now
  scheduler.py        Two APScheduler jobs: daily brief (default 08:00 UTC) and
                       weekly report (default Monday 09:00 UTC)
  db.py               Read-only Postgres connection
  tracking.py         Write-scoped Postgres connection (separate role) for
                       action/experiment tracking AND lead-outreach status - the only
                       module that ever writes
  metrics.py          Signup/upload/lead queries + GA4/Search Console/Reddit/pending-
                       tracking/attribution/lead-exclusion merge -> plain JSON; also
                       the weekly week-over-week metrics + shared attribution helper
  google_auth.py      Shared OAuth credential helper (GA4 + Search Console)
  ga4.py              GA4 Data API: sessions, tool-page views, funnel event counts
  search_console.py   Search Console API: top queries/pages by clicks
  reddit_discovery.py Reddit API (read-only): recent posts in curated subreddits
                       matching curated keywords
  brief.py            Builds LLM prompt, calls OpenAI, returns (markdown brief,
                       trackable action/experiment items)
  weekly_report.py    Deterministic weekly rollup - NO LLM call, plain-text formatter
  email_sender.py     Sends an email via Resend (daily brief or weekly report)
```

## Data sources

DataLayer's own Postgres (signups, uploads, free-tool leads, unconverted free-tool
leads for outreach research), GA4 (traffic, per-tool-page sessions, funnel event
counts), Search Console (top search queries/pages), and Reddit (recent posts in a
curated list of subreddits matching curated keywords) are all wired in. If a GA4,
Search Console, or Reddit fetch fails on a given run, that run's brief still sends —
the failure is reported under `data_gaps` in the metrics JSON instead of crashing the
whole pipeline, since the DB-only brief is still valuable on its own. DB, LLM, and
email failures are NOT handled this way — they still fail loudly, since there's no
meaningful fallback for those.

Every Reddit reply and lead-outreach message the brief drafts is explicitly labeled a
draft for human review — this system never posts to Reddit/LinkedIn/Facebook or sends
outreach on its own.

## Action/experiment tracking

Every "Recommended action" and "Suggested experiment" the LLM writes gets persisted as
its own row (`app/tracking.py`, via a second, write-scoped DB role separate from the
read-only one — see setup step 2), so it has a stable id, a status
(`pending`/`done`/`skipped`), and an optional free-text outcome note. See setup step 10
for how to review/resolve items via `scripts/mark_action.py`.

**Known monitoring gap**: the tracking writes (`record_new_items`,
`register_new_leads` in `app/scheduler.py`) happen after the day's email has already
sent, each wrapped in its own try/except that only logs (prefixed `TRACKING WRITE
FAILED:` so it's greppable) — this is deliberate, a tracking failure must never block
the email. But if `GROWTH_AGENT_TRACKING_DATABASE_URL` is misconfigured in a way that
breaks writes specifically (not simply unset, and not a connectivity problem that would
also break the reads `pending_from_prior_briefs`/`resolved_action_outcomes` already
do earlier in the same run and surface via `data_gaps`) - e.g. a role permission
drifted to no longer allow `INSERT` - there's currently no alerting on that log line,
only a container-log read would reveal it. Not solved here; would need real log-based
alerting infrastructure, out of scope for this project's stated minimalism.

## Lead outreach tracking

`lead_research` (up to 10 most-recent unconverted free-tool leads) previously showed the
same people, with a freshly-reworded but functionally repeat outreach draft, in every
single brief indefinitely — nothing let Amir mark "already reached out" or "not a fit"
and have it stick. Fixed with a **separate** table,
`growth_agent_lead_outreach` (`email` `UNIQUE`, `status`
[`pending`/`contacted`/`skipped`], `outcome_note`) — deliberately NOT a new category on
`growth_agent_tracked_items`, since that table's de-dupe is exact-string matching on
`description`, which is fine for LLM-invented recommendation text (an accepted
limitation) but wrong for leads, where the LLM will reword the same person's outreach
draft differently every day. Email is a real, stable identity; `growth_agent_tracked_items`'s
identity model isn't.

Each run: `_lead_research()` fetches a wider buffer (50, not 10) of unconverted leads,
`collect_metrics()` filters out anyone already `contacted`/`skipped` (a Python-side
merge across the read-only and tracking connections, same pattern as conversion
attribution), THEN slices to the real display cap of 10 — filtering the already-capped
top 10 would just shrink the shown list toward empty as leads get resolved, instead of
backfilling with the next-oldest untouched lead. After the email sends, any newly-shown
lead not yet in the table gets registered as `pending` (same non-blocking pattern as
action/experiment tracking).

**Different failure behavior from everything else optional**: if the exclusion lookup
fails, `lead_research` falls back to the *unfiltered* (but still capped) list rather
than going empty or null — hiding all leads because filtering broke would be worse than
briefly re-showing an already-contacted one. Recorded in `data_gaps` as a distinct
"filtering unavailable" note.

Review/resolve via `scripts/mark_lead.py` (setup step 10) — kept as a separate script
from `mark_action.py` since the two key on genuinely different identity types (email
string vs. integer id), and `mark_lead()` upserts (unlike `mark_item()`'s UPDATE-only)
since a lead's email is visible directly in the brief and might be marked before that
day's registration write ever runs.

## Recommended-action scoring

"Recommended actions" are scored and ranked by **Impact x Confidence x Ease** (each
1-5, so max score 125) — the original design goal from the project spec, avoiding both
generic advice ("post more on social media") and an unranked wall of suggestions. Up to
5 shown, highest score first; fewer than 5 if fewer are genuinely supported by that
run's data (never padded with filler to reach 5). Each item shows its scores in the
email, so the ranking is visible, not a black box.

**The sort order is enforced in code, not trusted to the LLM.** Live testing found
gpt-4o-mini computed scores correctly but got the *list order* wrong on two separate
real runs, even after the prompt was strengthened with explicit "sort mechanically,
double-check before finalizing" wording. Rather than keep tightening prompt language
indefinitely, `app/brief.py`'s `_sort_recommended_actions_by_score()` parses each
item's `Score:` value out of the LLM's own output and re-sorts it after generation —
the same philosophy already used for `low_signal`/`NO_CONTROL_GROUP_CAVEAT` in
conversion attribution: don't trust the model with a mechanical judgment it doesn't
need to make. No-ops safely (leaves the text unchanged) if the section or any score
can't be parsed, rather than risk corrupting the brief.

## Conversion attribution

For items marked `done`, the brief's "Past action outcomes" section shows DataLayer's
own signup/upload counts in the 7 days immediately before vs. after the item was
resolved (`app/metrics.py`'s `_resolved_action_outcomes`/`_anchored_window_counts`/
`_totals_before_after`, `app/tracking.py`'s `get_resolved_items_for_attribution`).
`skipped` items are excluded (nothing was executed, so a delta isn't meaningful).

**The eligibility window is sized differently for the daily brief vs. the weekly
report**, both via the same `get_resolved_action_outcomes()`: the daily brief
(`collect_metrics()`) uses a 1-day-wide window (item eligible on days
`ATTRIBUTION_MIN_DAYS_SINCE_RESOLVED`=7 to 8 since resolved), the weekly report uses the
function's default 7-day-wide window (days 7-14). Each is sized to exactly match its
own cadence, so an item appears in exactly one daily email AND exactly one weekly
email, never repeating. (Earlier versions of this feature reused the same 7-14-day
window for both, which meant the daily brief repeated the same "Past action outcomes"
entry — same before/after numbers — for ~7 consecutive days; fixed by giving the daily
path its own narrower window instead of adding new "already shown" state.)

**This is deliberately NOT causal attribution — it's correlational, small-sample
context only**, for a specific reason: DataLayer's real traffic is small enough
(single-digit-to-low-double-digit signups per month) that a 7-day window typically
contains 0-2 events total, with no control group. To keep the LLM from overclaiming:
no percentage change is ever computed for these entries (a base of 0 or 1 makes any
percentage misleading), a `low_signal` flag + pre-written explanatory note is computed
in code whenever the combined before+after total is under 5 (which will be most of the
time, at DataLayer's actual volume — that's the honest answer, not a mis-tuned
threshold), and the prompt requires a fixed sentence template rather than open-ended
"be cautious" language.

**Known, accepted limitation**: `resolved_at` is when Amir *ran `mark_action.py`*, not
necessarily when the action actually took effect in the world — if something ships and
gets marked done a few days later, the before/after windows shift by that much. Not
solved here, same category as the exact-string-match de-dupe limitation above.

Like `pending_from_prior_briefs`, this degrades gracefully (`data_gaps`) if
`GROWTH_AGENT_TRACKING_DATABASE_URL` is unset or unreachable — no new env var, no new
debug route (it's an internal DB-to-DB computation, no external credential to
pre-verify).

## Automated weekly reporting

Once a week (default Monday 09:00 UTC, configurable via
`WEEKLY_REPORT_SEND_DAY_UTC`/`WEEKLY_REPORT_SEND_HOUR_UTC`), a second, separate email
sends a deterministic rollup (`app/weekly_report.py`) — **no LLM call, no OpenRouter
cost**. This is intentional, not a cut corner: a report that's pure counts/trends
doesn't need an LLM, and skipping one avoids both a recurring cost and the same
small-sample overclaiming risk conversion attribution above had to solve carefully.
Sends *in addition to* that day's regular daily brief, not instead of it — the two are
different content (narrated LLM analysis + drafts vs. plain rollup numbers).

Three sections:
- **DataLayer metrics (last 7 days vs. prior 7 days)** — signups/uploads/csv_tool_leads,
  a new week-scale view distinct from the daily brief's 30-day rolling windows. No
  percentage change shown, same reasoning as conversion attribution's omission of one.
- **Tracking summary** — items created this week (by category), items resolved this
  week (by status), and the current all-time pending total.
- **Recent action outcomes** — reuses the *exact same* 7-14-day attribution window and
  data the daily brief's "Past action outcomes" computes (`app/metrics.py`'s
  `get_resolved_action_outcomes()`, shared by both paths). This isn't a coincidence:
  the window's width (7 days) exactly matches the report's weekly cadence, so each
  `done` item appears in exactly one weekly report, with no gaps or overlap. The same
  caution language used in the daily brief's LLM prompt (`NO_CONTROL_GROUP_CAVEAT` in
  `app/metrics.py`) is reused here as a plain-code constant, so the wording can't drift
  between the two.

Degrades gracefully the same way as everything tracking-dependent: with
`GROWTH_AGENT_TRACKING_DATABASE_URL` unset, the DataLayer-metrics section still sends
(it doesn't depend on tracking), while the tracking-summary and recent-outcomes
sections show the `data_gaps` reason instead of crashing the whole report.
