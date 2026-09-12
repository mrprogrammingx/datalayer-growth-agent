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
   (`gpt-4o-mini`) to identify the single biggest funnel bottleneck, compute real funnel
   drop-off, surface SEO/content/GEO/community opportunities, and pick exactly ONE #1
   Priority plus up to 3 Quick Wins and at most one Experiment, each scored and displayed
   by Impact x Confidence x Ease (see "Brief format and prioritization" below) — and draft
   Reddit replies / lead outreach messages / LinkedIn-Facebook-Instagram content ideas.
5. Emails the resulting brief once a day via Resend.
6. Persists the #1 Priority and each Quick Win / Experiment as its own row, so Amir can
   mark it done/skipped with an outcome note (`scripts/mark_action.py`) and future briefs
   can reference still-open items instead of repeating them as if new.
7. For items marked done recently enough to fall in the attribution window, shows
   DataLayer's own signup/upload counts before vs. after, labeled SIGNAL — correlational,
   small-sample context only, never presented as evidence of causation (see "Conversion
   attribution" below).
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

## 2. Create the tracking role + tables

Action/experiment tracking needs to persist state — something no other part of this
app does. Rather than reuse `growth_agent_ro` (which must stay genuinely,
DB-enforced read-only), this is a second, separate role scoped to `INSERT`/`SELECT`/
`UPDATE` on its own tables only, so a bug in the tracking path can never put the
read-only guarantee at risk. Run this once, same way as step 1:

```bash
docker compose -f ../datalayer-ecommerce/docker-compose.yml exec db psql -U datalayer -d datalayer
```

```sql
CREATE ROLE growth_agent_tracking WITH LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE datalayer TO growth_agent_tracking;
GRANT USAGE ON SCHEMA public TO growth_agent_tracking;
```

Do **not** grant this role anything on `users`/`uploads`/`csv_tool_leads`, same as the
read-only role above.

Put the resulting connection string in `.env` as `GROWTH_AGENT_TRACKING_DATABASE_URL`.
This is optional — if unset, tracking degrades gracefully (see `data_gaps` in the
brief) and the daily email still sends normally.

**Then create the three tables this role writes to** —
`growth_agent_tracked_items`, `growth_agent_lead_outreach`, and
`growth_agent_prospects`. They live in a single checked-in [`sql/schema.sql`](sql/schema.sql);
run it against the same Postgres instance:

```bash
docker compose -f ../datalayer-ecommerce/docker-compose.yml exec -T db \
  psql -v ON_ERROR_STOP=1 -U datalayer -d datalayer < sql/schema.sql
```

Each `CREATE TABLE IF NOT EXISTS` is immediately followed by its `SELECT, INSERT,
UPDATE` table grant and the `USAGE, SELECT` grant on its `_id_seq` sequence — a `SERIAL`
column's implicit sequence needs its own grant under a non-owner role, or every
`INSERT` fails. The file is wrapped in `BEGIN; … COMMIT;`, so a partial apply can't
happen. It only *creates* missing tables; it never migrates an existing one (see the
file's header comment and "why not an Alembic migration" below).

**Runs once per host** — like `growth_agent_ro`, local dev's Postgres and the VPS's
Postgres are separate instances, so run both the `CREATE ROLE` block above and
`sql/schema.sql` once against each — the same commands on the VPS.

**Known limitation**: `sql/schema.sql` is a checked-in file in this repo, deliberately
not wired into either repo's Alembic migration chain (see below), so a DB restore that
predates these tables won't bring them back on its own — re-run `sql/schema.sql` after
such a restore. Same exposure `growth_agent_ro` already has today.

**One-time manual step for hosts that already have `growth_agent_prospects`**:
`sql/schema.sql`'s `CREATE TABLE IF NOT EXISTS` is a no-op on a table that already
exists, so it will never add the `draft_message` column to a `growth_agent_prospects`
table created before this column existed (see the file's own "NOT A MIGRATION TOOL"
note). On any such host, run once:

```bash
docker compose -f ../datalayer-ecommerce/docker-compose.yml exec -T db \
  psql -v ON_ERROR_STOP=1 -U datalayer -d datalayer -c \
  'ALTER TABLE growth_agent_prospects ADD COLUMN IF NOT EXISTS draft_message TEXT;'
```

A fresh host that runs `sql/schema.sql` for the first time gets this column
automatically and needs no separate step.

*(Why not an Alembic migration in `datalayer-ecommerce` instead? That repo's migration
chain never issues GRANT/CREATE ROLE either — it's always a manual step, exactly like
`growth_agent_ro`'s own setup above. Coupling this schema to the main app's migration
chain would be unnecessary cross-repo entanglement for tables only growth-agent ever
touches.)*

**Why three separate tables**, all written by this one role:

- `growth_agent_tracked_items` — the daily brief's #1 Priority / Quick Wins /
  Experiment, de-duped by exact-string match on `description`. Fine for LLM-invented
  recommendation text (an accepted limitation); wrong for the two identities below.
- `growth_agent_lead_outreach` — unconverted free-tool leads, keyed by `email`
  `UNIQUE`. Email is a real, stable identity; `growth_agent_tracked_items`' string-match
  model isn't, and the LLM rewords the same person's outreach draft differently every
  day. `email` is a plain `VARCHAR` with exact-match comparison, matching
  `users`/`csv_tool_leads.email`'s existing convention elsewhere (no `citext`, no
  lowercasing) — copy-paste the email from the brief rather than retyping it, to avoid a
  case mismatch silently missing an exclusion. See "Lead outreach tracking" below.
- `growth_agent_prospects` — the acquisition report's Apify storefronts, keyed on
  normalized storefront `domain` (a third identity type, distinct from a tracked item's
  integer id and a lead's email; the `*.myshopify.com` handle when available, else the
  custom domain — see `normalize_prospect_domain` in `app/apify_prospecting.py`). A row
  exists only once a prospect has actually been surfaced in a report (or was marked
  `contacted`/`skipped` directly via `scripts/mark_prospect.py`, which upserts) — there
  is no pre-surfaced state, so there is no `'new'` status. See "Prospect tracking" below.

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
- `ACQUISITION_REPORT_SEND_HOUR_UTC` (default `8`) — see "Automated customer acquisition
  reporting" below
- `WEEKLY_REPORT_SEND_DAY_UTC` (default `mon`), `WEEKLY_REPORT_SEND_HOUR_UTC` (default
  `9`) — see "Automated weekly reporting" below
- `GROWTH_AGENT_NETWORK` — see step 6
- `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN`,
  `GA4_PROPERTY_ID`, `SEARCH_CONSOLE_SITE_URL` — from step 3
- `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT` — from step 4
- `APIFY_API_TOKEN` (from Apify Console > Settings > Integrations), `APIFY_PROSPECT_QUERY`
  (optional niche keyword, empty = broad discovery) — powers the customer acquisition
  report's PROSPECTING section; optional, degrades gracefully if unset (see
  "Automated customer acquisition reporting" below)
- `PROSPECT_COOLDOWN_DAYS` (optional, default `30`), `PROSPECT_DISPLAY_LIMIT` (optional,
  default `5`), `PROSPECT_MIN_PRODUCTS` / `PROSPECT_MAX_PRODUCTS` (optional, defaults `3`
  / `3000` — the SMB product-count band used to demote obvious non-fits from the surfaced
  set) — tune the acquisition report's prospect cooldown/selection; see "Prospect
  tracking" below

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

This boots the Flask app (internal-only, no published port) and starts all three
APScheduler jobs: the daily growth brief (default 08:00 UTC, configurable via
`BRIEF_SEND_HOUR_UTC`), the daily customer acquisition report (default 08:30 UTC,
configurable via `ACQUISITION_REPORT_SEND_HOUR_UTC` — fires 30 minutes after the growth
brief's hour so the two LLM calls don't race each other), and the weekly report (default
Monday 09:00 UTC, configurable via `WEEKLY_REPORT_SEND_DAY_UTC`/
`WEEKLY_REPORT_SEND_HOUR_UTC`) — all three are separate emails; none replaces another.

Gunicorn runs with `--timeout 120` (not the 5-workers/no-timeout default) — a real
`/run-now` was observed crashing the worker (`SIGABRT`→`SIGKILL`) on gunicorn's default
30s timeout once the prompt grew large enough that LLM generation plus the email send
occasionally took longer than that. Confirmed live: the email had actually already sent
successfully by the time the worker was killed, so the failure mode is a false-negative
500 response, not a lost brief — but still worth the fix so `/run-now`/
`/acquisition-report-now`/`/weekly-report-now` return correctly instead of crashing.

## 8. Verify GA4/Search Console/Reddit/Apify credentials before testing the full brief

Before triggering a real (billed) `/run-now` or `/acquisition-report-now`, confirm the
GA4, Search Console, Reddit, and Apify credentials/permissions work using the four
debug-only routes — these call the fetch functions directly and return raw JSON, without
touching the LLM or sending any email:

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
docker compose exec growth-agent python -c "
import urllib.request
print(urllib.request.urlopen('http://localhost:8080/debug/apify').read().decode())
"
```

`/debug/apify` runs the real Apify Actor with `maxItems=3` — a small, cheap credential
check, not a free call (Apify bills by compute usage, unlike the other three debug
routes).

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

If `/debug/apify` returns an error, double check `APIFY_API_TOKEN` is set in `.env` and
is a valid, unexpired token from Apify Console > Settings > Integrations, and that your
Apify account has enough credit/compute quota to run an Actor. A clean empty list (`[]`)
is a valid response — it means the Actor found no matching stores this run, not a
credentials problem.

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

`/acquisition-report-now` tests the customer acquisition report the same way — it also
triggers a real (billed) OpenRouter call and a real email send, same caution as `/run-now`:

```bash
docker compose exec growth-agent python -c "
import urllib.request
req = urllib.request.Request('http://localhost:8080/acquisition-report-now', method='POST')
print(urllib.request.urlopen(req).read().decode())
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

Each day's #1 Priority, Quick Wins, and Experiment (if any) get persisted as individual rows
(after the email sends, so the email itself never contains an item's id — `list` is how
you find one):

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

Prospects from the customer acquisition report's PROSPECTING section work the same way
again, with a **third** script (`scripts/mark_prospect.py`), keyed by normalized
storefront domain — printed on each prospect's `Domain:` line in the report, so `list`
is a convenience here too (and the CLI normalizes whatever host/URL form you paste):

```bash
docker compose exec growth-agent python scripts/mark_prospect.py list
docker compose exec growth-agent python scripts/mark_prospect.py examplestore.myshopify.com contacted "sent outreach Sep 10"
docker compose exec growth-agent python scripts/mark_prospect.py examplestore.com skipped "already on Triple Whale, not a fit"
```

See "Prospect tracking" below for why prospects get their own table/script, and for the
~30-day cooldown that suppresses a business even before it's marked.

## Repo layout

```
docker-compose.yml
Dockerfile
requirements.txt
.env.example
sql/
  schema.sql          Idempotent CREATE TABLE IF NOT EXISTS + grants for the three
                       tables this service owns (tracked_items, lead_outreach,
                       prospects); run once per host on a fresh/restored DB, not a
                       migration tool (see setup step 2)
scripts/
  authorize_google.py One-time local OAuth authorization helper (not in the Docker image)
  mark_action.py      CLI to list/resolve tracked action+experiment items (IS in the
                       Docker image - runs inside the container to reach Postgres)
  mark_lead.py        CLI to list/resolve lead-outreach status, keyed by email (IS in
                       the Docker image)
  mark_prospect.py    CLI to list/resolve acquisition-report prospect status, keyed by
                       normalized storefront domain (IS in the Docker image)
app/
  main.py             Flask app: GET /health, GET /debug/ga4, GET /debug/search-console,
                       GET /debug/reddit, GET /debug/apify, POST /run-now,
                       POST /acquisition-report-now, POST /weekly-report-now
  scheduler.py        Three APScheduler jobs: daily growth brief (default 08:00 UTC),
                       daily customer acquisition report (default 08:30 UTC), and
                       weekly report (default Monday 09:00 UTC)
  db.py               Read-only Postgres connection
  tracking.py         Write-scoped Postgres connection (separate role) for
                       action/experiment tracking, lead-outreach status, AND
                       acquisition-report prospect dedup/cooldown - the only module that
                       ever writes; shared by both daily LLM reports
  metrics.py          Signup/upload/lead queries + GA4/Search Console/Reddit/pending-
                       tracking/attribution/lead-exclusion merge -> plain JSON; also
                       the weekly week-over-week metrics + shared attribution helper
  google_auth.py      Shared OAuth credential helper (GA4 + Search Console)
  ga4.py              GA4 Data API: sessions, tool-page views, funnel event counts
  search_console.py   Search Console API: top queries/pages by clicks
  reddit_discovery.py Reddit API (read-only): recent posts in curated subreddits
                       matching curated keywords
  brief.py            Builds the Growth Brief LLM prompt, calls OpenAI, returns
                       (markdown brief, trackable action/experiment items)
  acquisition_report.py
                       Builds the Customer Acquisition Report LLM prompt (existing
                       leads, community, prospecting, daily target) - a separate email
                       from the Growth Brief, reuses brief.py's OpenRouter config and
                       tracking-JSON extraction; collect_acquisition_data() layers
                       apify_prospecting.py on top of metrics.collect_metrics()
  apify_prospecting.py Apify REST API: runs the "Shopify Store Finder" Actor to find
                       real, live Shopify storefronts for the acquisition report's
                       PROSPECTING section
  weekly_report.py    Deterministic weekly rollup - NO LLM call, plain-text formatter
  email_sender.py     Sends an email via Resend (growth brief, acquisition report, or
                       weekly report)
```

## Data sources

DataLayer's own Postgres (signups, uploads, free-tool leads, unconverted free-tool
leads for outreach research), GA4 (traffic, per-tool-page sessions, funnel event
counts), Search Console (top search queries/pages), Reddit (recent posts in a
curated list of subreddits matching curated keywords), and Apify (real, live Shopify
storefronts for the acquisition report's PROSPECTING section — see
`app/apify_prospecting.py`) are all wired in. If a GA4, Search Console, Reddit, or Apify
fetch fails on a given run, that run's report still sends — the failure is reported under
`data_gaps` in the metrics JSON instead of crashing the whole pipeline, since the rest of
the report is still valuable on its own. DB, LLM, and email failures are NOT handled this
way — they still fail loudly, since there's no meaningful fallback for those.

Every Reddit reply, lead-outreach message, and prospect outreach draft is explicitly
labeled a draft for human review — this system never posts to Reddit/LinkedIn/Facebook or
sends outreach on its own.

## Action/experiment tracking

The #1 Priority, every Quick Win, and the Experiment (if any) the LLM writes get persisted
as their own row (`app/tracking.py`, via a second, write-scoped DB role separate from
the read-only one — see setup step 2), so each has a stable id, a status
(`pending`/`done`/`skipped`), and an optional free-text outcome note. See setup step 10
for how to review/resolve items via `scripts/mark_action.py`.

**Known monitoring gap**: the tracking writes (`record_new_items`,
`register_new_leads`, `mark_prospects_surfaced` in `app/scheduler.py`) happen after the
day's email has already sent, each wrapped in its own try/except that only logs
(prefixed `TRACKING WRITE FAILED:` so it's greppable) — this is deliberate, a tracking failure must never block
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

## Prospect tracking

The customer acquisition report's PROSPECTING section re-runs the same Apify actor with
the same query every day, so it returns largely the same storefronts. The old dedup
(an email filter against `growth_agent_lead_outreach`) missed prospects two ways: most
Shopify storefronts publish no email at all, and a business only becomes excludable
once someone runs a `mark_*` script. The same handful of businesses reappeared day
after day.

Fixed with a dedicated `growth_agent_prospects` table (setup step 2), keyed on
**normalized storefront domain** — a third identity type, distinct from a tracked
item's integer id and a lead's email, which is why it gets its own table and its own
`scripts/mark_prospect.py`. The domain is the `*.myshopify.com` handle when the actor
returns one (immutable per store), else the custom domain
(`normalize_prospect_domain` in `app/apify_prospecting.py`).

Lifecycle: `surfaced` → `contacted`/`skipped`. A row is created the moment a prospect
is actually put in a report (`mark_prospects_surfaced`, after the email sends — same
non-blocking pattern as the other tracking writes), or directly when marked via the
CLI. `get_excluded_prospect_domains` then hides a domain from future reports while it's
within the `PROSPECT_COOLDOWN_DAYS` (default 30) window since it was last surfaced, and
**permanently** once it's `contacted`/`skipped`.

**The surfaced set is chosen deterministically in code, not by the model.**
`collect_acquisition_data()` filters the fetched list against the cooldown, then
`_select_prospects_to_surface()` ranks and caps it to `PROSPECT_DISPLAY_LIMIT` — and
that exact list is both what goes into the prompt and what
`mark_prospects_surfaced()` writes to the cooldown table afterward. Nothing is parsed
back out of the LLM's rendered markdown, so the cooldown record is always exact. The
ranking preserves the actor's own discovery order but demotes (never drops) prospects
with no trackable domain and prospects whose `product_count_range` falls outside the
`PROSPECT_MIN_PRODUCTS`–`PROSPECT_MAX_PRODUCTS` SMB band, so obvious non-fits don't
crowd better candidates out of the top N. The model still makes the final per-block
keep/skip call when it renders — a block it declines to render still entered its
cooldown, which is deliberate (a non-fit isn't worth resurfacing tomorrow).

**Failure behavior**, same as lead-outreach filtering: if the exclusion lookup fails,
the report falls back to the *unfiltered* prospect list rather than hiding everything,
with a distinct `data_gaps` note. Every prospect DB call in the daily job path is
non-blocking — the email always sends. `scripts/mark_prospect.py` fails loud, like the
other human-run CLIs.

Review/resolve via `scripts/mark_prospect.py` (setup step 10) — `list` shows
still-`surfaced` prospects; `<domain> contacted|skipped [note]` resolves one (the CLI
normalizes the argument, so a full `https://…` URL pasted from a browser also works).
`mark_prospect()` upserts (like `mark_lead()`), since a domain is printed on each
prospect's `Domain:` line in the report and might be marked before that day's
surfaced-write ran.

## Brief format and prioritization

The daily brief follows a fixed template (`app/brief.py`'s `SYSTEM_PROMPT`): 📊 AT A
GLANCE → 📆 YESTERDAY → 🎯 #1 PRIORITY → 🔥 QUICK WINS (up to 3) → 📣 CONTENT
OPPORTUNITIES (LinkedIn/Facebook/Instagram, each with a structured Hook/Post/CTA-style
draft) → 🔎 SEO OPPORTUNITIES (each with its own displayed Impact/Confidence/Ease/Score)
→ 🤖 GEO/AI SEARCH OPPORTUNITIES → 👤 LEAD OUTREACH (up to 3, each with a full draft
message) → 💬 COMMUNITY OPPORTUNITIES (Reddit only - see below) → 🧪 EXPERIMENT (at most
one) → 🧠 WHAT WE'RE LEARNING (FACT/SIGNAL/HYPOTHESIS-labeled) → ⚠️ WATCH → 🏁 BOTTOM
LINE, each section (and named platform/subsection within one, e.g. one of the three
Content Opportunities platforms) omitted entirely when there's nothing meaningful. The
prompt targets ~500-800 words for the whole email (a return to an explicit target, after
an earlier version of this brief dropped it as incompatible with the bigger template -
see [[growth-agent-brief-format-rewrite]] in project memory); `WORD_COUNT_WARN_THRESHOLD`
(1200 words) in `generate_brief()` is a loose ceiling above that target, not the target
itself - it only warns on a genuinely runaway/looping completion, never blocks the send.

**The brief is explicitly told not to default to SEO as the #1 Priority** just because a
search-console number looks low - low traffic is very often not DataLayer's actual
bottleneck at its current volume. Before picking SEO as #1, the prompt requires weighing
it against every other channel with real evidence that run (leads, activation,
conversion, free-tool optimization, GEO, social, Reddit, direct outreach,
pricing/onboarding). A `FINAL QUALITY CHECK` block (a checklist the LLM is told to run
silently against its own draft before responding, never rendered in the output) reinforces
this and the internal-exclusion rules right before generation.

**Impact x Confidence x Ease scores ARE displayed** — for 🎯 #1 PRIORITY (`Impact: N/5` /
`Confidence: N/5` / `Ease: N/5` / `Score: N/125`, colon-separated) and for each 🔥 QUICK
WIN (`Impact N | Confidence N | Ease N | Score N`, pipe-separated, one line per item).
This is a return to displaying scores after an earlier version of this brief moved them
to internal-only reasoning - see [[growth-agent-brief-format-rewrite]] in project memory
for that intermediate step. **The QUICK WINS list is still sorted in code, not trusted
to the LLM**: this codebase already confirmed twice (original "Recommended actions" ICE
work) that gpt-4o-mini computes scores correctly but doesn't reliably self-sort a list by
them. `app/brief.py`'s `_sort_quick_wins_by_score()` re-sorts after generation, tolerant
of `Ease N | Score N` vs. `Ease: N | Score: N` formatting drift, no-oping safely (leaving
the brief unchanged, with a warning logged) if the section or a score can't be parsed.
`_cap_trackable_items()` separately caps the *count* of items the LLM hands back for
tracking (1 #1 Priority + ≤3 Quick Wins = ≤4 "action", ≤1 "experiment") regardless of
JSON over-production - a count cap, not a sort, same "verify a mechanical constraint in
code" philosophy.

## AT A GLANCE metrics

`app/metrics.py`'s `collect_metrics()` precomputes a nested `at_a_glance` dict, grouped
to match the email's own subheadings, rather than leaving the LLM to pull numbers out of
the larger `ga4`/DB JSON itself or decide which subheading they belong under - same
reasoning as `low_signal`/`NO_CONTROL_GROUP_CAVEAT` below: numbers this prominent
shouldn't depend on the model copying/placing them correctly. A `null` field means that
source was unavailable this run; the prompt omits that line (or the whole subheading, if
every field in it is null) rather than showing "null".

- **📈 Acquisition** — `sessions` (`ga4.sessions_and_users`) / `tools`
  (`ga4.tool_page_funnel_events_last_30_days.csv_uploaded`, actual free-tool usage
  events, not raw pageviews) — both `null` together if the GA4 fetch failed.
- **🧩 Product** — `signups`/`uploads` (DB-backed 30-day totals, so these stay available
  even when GA4 is down) / `activated` — new `_activated_customers()`: count of
  signed-up users (`users.user_id`) with ≥1 row in `uploads` (in-app upload after
  signup), an all-time snapshot, not a 30-day window — activation is a milestone a user
  either has or hasn't reached. Distinct from `uploads` (a 30-day event count) and from
  `lead_research`'s free-tool leads (pre-signup, keyed on email, no `user_id` at all).
- **💰 Commercial** — `free_users` (`plan_tier_distribution['free']`) / `paid_customers`
  (`paying_customers`) - both all-time snapshots, EXTERNAL users only (see "Internal/
  founder user exclusion" below) / `internal_premium_accounts` - a founder/team account
  that happens to carry a paid `plan_tier` for testing, shown separately, never folded
  into `paid_customers`.
- **👥 Users** — `external` / `internal`, all-time headcounts - the same internal/founder
  exclusion applied everywhere else in this block, but shown explicitly here rather than
  silently baked in, so nobody has to wonder whether "signups: 3" secretly includes a
  founder's own test account.

Note: the email's own "Reporting period: Last 30 days" line describes the Acquisition/
Product flow metrics (sessions/tools/signups/uploads); `activated`/`free_users`/
`paid_customers`/`users` are current-state snapshots shown in the same block, per Amir's
own requested template.

## Internal/founder user exclusion

**Every customer-facing metric in this app excludes founder/team accounts by
construction** - `app/metrics.py`'s `_internal_users(cur)` identifies them and every
counting function (`_period_metric`, `_plan_tier_distribution`, `_activated_customers`,
`_lead_research`, `_resolved_action_outcomes`, and `collect_weekly_datalayer_metrics()`'s
own queries) threads a `NOT IN` exclusion through at the SQL level via the shared
`_not_in_clause()` helper. This is deliberately NOT a prompt-only instruction the LLM has
to remember to apply each run - same "precompute the correct number in Python, don't
trust the model to get a mechanical constraint right every time" philosophy as
`low_signal`/`at_a_glance` elsewhere in this file.

**Identification, in priority order:**
1. **Role flags** (primary) - `users.is_admin` / `users.is_super_admin` (real Postgres
   booleans already used by the main app for its own admin/permissions system - see
   `datalayer-ecommerce`'s alembic `0013`/`0014`). Needs zero configuration here; any
   account flagged either way in the main app is automatically excluded.
2. **`GROWTH_AGENT_INTERNAL_EMAILS`** (fallback, comma-separated, case-insensitive, empty
   by default) - covers what role flags structurally can't: a founder/team member who
   used a free tool with their own personal email but never created a DataLayer account
   at all (`csv_tool_leads` has no role column - there's nothing to flag), or a team
   member whose `users` row isn't flagged admin for some other reason.

**What's excluded, and how:**
- `signups`/`uploads`/`csv_tool_leads` (and everything derived from them:
  `at_a_glance.product.*`, the weekly report's own signup/upload/lead counts) - internal
  rows never counted in the first place, not filtered after the fact.
- `plan_tier_distribution`/`paying_customers`/`free_users`/`activated_customers` - same,
  via `_plan_tier_distribution(cur, internal_user_ids)` /
  `_activated_customers(cur, internal_user_ids)`. A mirror function,
  `_internal_plan_tier_distribution()`, computes the internal-only counterpart so a
  founder's own test Premium account is still visible somewhere
  (`internal_premium_accounts`), never silently dropped - it's tracked, just never
  reported as a paying customer.
- `lead_research` - internal emails excluded unconditionally at the SQL level in
  `_lead_research()` itself, before the separate (optional, can fail into a data_gaps
  note) already-contacted/skipped filtering layer runs. This exclusion has no failure
  mode of its own to degrade out of.
- `resolved_action_outcomes` - the daily brief's own conversion-attribution before/after
  counts (see "Conversion attribution" below) also exclude internal signups/uploads, so a
  founder testing something around the time an action was marked done can't masquerade
  as evidence the action worked.

**What's NOT excluded, and why:** GA4 `sessions`/`tools` (`at_a_glance.acquisition`).
There's no reliable way to separate founder/team pageviews from real visitor sessions
with the current GA4 property setup (no User-ID tracking configured) - rather than
silently treat all GA4 traffic as customer traffic, `at_a_glance.acquisition.note` carries
a fixed, precomputed caveat string (`GA4_INTERNAL_TRAFFIC_NOTE`) that the prompt always
renders verbatim under 📈 Acquisition: "Internal traffic cannot currently be separated
from external traffic in this metric." Search Console and Reddit data need no such
caveat - both only reflect real public-internet activity by construction.

## Conversion attribution

For items marked `done`, the brief's 🧠 WHAT WE'RE LEARNING section (only when there's
an eligible entry — most runs there isn't, which is normal) shows DataLayer's own
signup/upload counts in the days immediately before vs. after the item was resolved,
always labeled SIGNAL (never FACT - the sample is always too small; see the
FACT/SIGNAL/HYPOTHESIS evidence discipline the whole brief uses), prefixed with a
directional badge - 🟡 INCONCLUSIVE when `low_signal` is true, 🟢 WORKING when
signups/uploads moved up, 🔴 NOT WORKING when neither did. The badge is a prioritization
aid ("try this again" vs. "drop it"), not a causal claim - the prompt requires it always
be immediately followed by the no-control-group caveat sentence, never shown alone
(`app/metrics.py`'s `_resolved_action_outcomes`/`_anchored_window_counts`/
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
threshold), and the prompt requires a fixed SIGNAL-labeled sentence template
(`low_signal` ⇒ "still collecting data" + the pre-written note — otherwise the raw
before/after counts plus the no-control-group caveat) rather than open-ended "be
cautious" language.

**Known, accepted limitation**: `resolved_at` is when Amir *ran `mark_action.py`*, not
necessarily when the action actually took effect in the world — if something ships and
gets marked done a few days later, the before/after windows shift by that much. Not
solved here, same category as the exact-string-match de-dupe limitation above.

Like `pending_from_prior_briefs`, this degrades gracefully (`data_gaps`) if
`GROWTH_AGENT_TRACKING_DATABASE_URL` is unset or unreachable — no new env var, no new
debug route (it's an internal DB-to-DB computation, no external credential to
pre-verify).

## Automated customer acquisition reporting

Every day (default 08:30 UTC, 30 minutes after the growth brief's hour, configurable via
`ACQUISITION_REPORT_SEND_HOUR_UTC`), a second, separate daily email sends the Customer
Acquisition Report (`app/acquisition_report.py`) — its own LLM call, its own email, sent
*in addition to* that day's growth brief, not instead of it.

Why a separate report rather than a new section in the growth brief: the growth brief's
job is broad ("analyze all the metrics, find the biggest bottleneck" — traffic, SEO, GEO,
content, ICE-scored quick wins). This report has one narrow, harder-nosed job: DataLayer
has 0 external paying customers, so every day it only asks "what gets us closer to the
first 10 real ones, today." It deliberately excludes AT A GLANCE, Quick Wins, SEO/GEO, and
Content Opportunities — those stay exclusively in the growth brief — so the two emails
don't repeat each other in different words.

Sections (each omitted when there's nothing real to report, except Lead-Based Outreach
and Prospecting, which always show — see `acquisition_report.py`'s SYSTEM_PROMPT for the
exact fallback lines):
- **Today's prioritized actions** — up to 3, ranked existing leads > community >
  outreach > partnerships > content > SEO, each tied to real evidence in this run's data.
- **Lead-based outreach** — the same `lead_research` candidates the growth brief's Lead
  Outreach section draws from (DataLayer currently has no other named-individual,
  external-intent data source), with draft messages for human review only.
- **Prospecting** — up to `PROSPECT_DISPLAY_LIMIT` (default 5) real, live Shopify
  storefronts via the Apify "Shopify Store Finder" Actor (`app/apify_prospecting.py`,
  `igolaizola/shopify-store-finder`), each with
  business/website/contact-channel/why-they-fit/evidence and a draft outreach message.
  Optional — degrades gracefully (see `data_gaps`) if `APIFY_API_TOKEN` is unset or the
  Apify run fails, same philosophy as GA4/Search Console/Reddit. Unlike `lead_research`,
  a prospect's contact email is public information the merchant already publishes on
  their own storefront, so (unlike Lead-Based Outreach) it's fine for it to appear in the
  report. Prospects have their **own** dedup table (`growth_agent_prospects`) and script
  (`scripts/mark_prospect.py`), keyed on normalized storefront domain — a ~30-day
  cooldown keeps a business shown today from reappearing, and marking one
  contacted/skipped excludes it permanently. See "Prospect tracking" below.
- **Community opportunities** — Reddit only, same data source and draft-for-review
  convention as the growth brief's Community Opportunities section.
- **Customer experiment** — at most one, same shape as the growth brief's Experiment
  section, shares the same `growth_agent_tracked_items` table (see below).
- **Customer acquisition target** — a short, concrete daily target derived only from what
  this run's data actually supports (never an invented headline number).

Shares the growth brief's tracking table on purpose: both reports write their action/
experiment items to the same `growth_agent_tracked_items` table via `tracking.py`, and
both read `pending_from_prior_briefs` from it. This means an action recommended by one
report is visible to the other, so the two don't independently recommend the same
unresolved thing forever.

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
  data the daily brief's 🧠 WHAT WE'RE LEARNING section computes (`app/metrics.py`'s
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
