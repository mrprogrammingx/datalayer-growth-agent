# datalayer-growth-agent

Internal growth system for DataLayer (usedatalayer.com). Once a day it:

1. Reads DataLayer's own Postgres metrics (signups, uploads, free-tool leads) via a
   read-only DB role.
2. Reads real GA4 traffic/funnel data (sessions, per-tool-page views, funnel event
   counts) and Search Console data (top queries/pages) via Google APIs, authenticated
   with a personal OAuth refresh token (not a service account).
3. Sends all of that to an LLM (`gpt-4o-mini`) to identify the single biggest growth
   problem, compute real funnel drop-off, surface SEO opportunities, and rank
   recommended actions.
4. Emails the resulting brief once a day via Resend.

Deliberately minimal: Flask + Postgres + OpenAI API + GA4/Search Console APIs +
APScheduler + Docker only. No scraping, no outreach drafting, no experiment tracking, no
multi-agent frameworks, no LangChain/vector DB/Redis/Celery/Kubernetes.

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
as the host (see network wiring in step 3) — no SSL needed on the private compose
network, same as datalayer-ecommerce's own internal `web` → `db` connection.

## 2. Google Analytics / Search Console setup

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
3. **Configure the OAuth consent screen.** Go to **APIs & Services > OAuth consent
   screen**. Set **User type** to **Internal** (available because this project is under
   the `usedatalayer.com` Workspace domain) — this is important: an **External** app left
   in Testing mode has refresh tokens that silently expire after 7 days, which would
   break this a week after setup with no obvious error. Internal apps have no such expiry
   and need no Google verification review. Fill in the minimal required fields (app name,
   support email) and save.
4. **Create an OAuth Client ID.** Go to **APIs & Services > Credentials > Create
   Credentials > OAuth client ID**. Application type: **Desktop app**. Name it something
   like `growth-agent-oauth`. Note the **Client ID** and **Client Secret** shown.
5. **Run the one-time authorization script**, locally, on a machine with a browser (your
   laptop — not the VPS):

   ```bash
   pip install google-auth-oauthlib
   python scripts/authorize_google.py <client_id> <client_secret>
   ```

   A browser window opens — log in with the Google account that has access to
   DataLayer's GA4 property and Search Console property, and approve. The script prints a
   **refresh token**. Paste it into `.env` as `GOOGLE_OAUTH_REFRESH_TOKEN` — this same
   value (along with the client ID/secret) works on **every host**, unlike a
   service-account key file, so you only need to run this once and copy the three values
   to both local dev's `.env` and the VPS's `.env`.
6. **Record the GA4 Property ID.** In GA4, go to **Admin > Property Settings** and copy
   the numeric **Property ID** (NOT the `G-XXXXXXX` measurement ID used in the site's
   tracking snippet — that's a different identifier). This goes in `.env` as
   `GA4_PROPERTY_ID`.
7. **Confirm the Search Console property type.** In the Search Console UI, check whether
   the verified property is a **domain property** (shown as `sc-domain:usedatalayer.com`)
   or a **URL-prefix property** (shown as `https://usedatalayer.com/`). Copy the exact
   value shown — this goes in `.env` as `SEARCH_CONSOLE_SITE_URL` and must match exactly
   (including the trailing slash for URL-prefix properties).

## 3. Configure environment

```bash
cp .env.example .env
```

Fill in:

- `GROWTH_AGENT_DATABASE_URL` — from step 1 (`db` host, `growth_agent_ro` user)
- `OPENROUTER_API_KEY` — reuses the same OpenRouter account already used elsewhere in
  datalayer-ecommerce (see its `.env`'s `Openrouter_API_KEY`); `brief.py` points the
  OpenAI SDK's `base_url` at `https://openrouter.ai/api/v1` since OpenRouter is
  OpenAI-API-compatible, so no separate OpenAI account is needed
- `RESEND_API_KEY`, `EMAIL_FROM` — reuses datalayer-ecommerce's existing verified Resend
  sender
- `GROWTH_BRIEF_RECIPIENT`
- `BRIEF_SEND_HOUR_UTC` (default `8`)
- `GROWTH_AGENT_NETWORK` — see step 4
- `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REFRESH_TOKEN`,
  `GA4_PROPERTY_ID`, `SEARCH_CONSOLE_SITE_URL` — from step 2

## 4. Confirm the Docker network name

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

## 5. Run it

```bash
docker compose up -d --build
```

This boots the Flask app (internal-only, no published port) and starts the daily
APScheduler job (default 08:00 UTC, configurable via `BRIEF_SEND_HOUR_UTC`).

## 6. Verify Google API credentials before testing the full brief

Before triggering a real (billed) `/run-now`, confirm the GA4 and Search Console
credentials/permissions work using the two debug-only routes — these call the fetch
functions directly and return raw JSON, without touching the LLM or sending any email:

```bash
docker compose exec growth-agent python -c "
import urllib.request
print(urllib.request.urlopen('http://localhost:8080/debug/ga4').read().decode())
"
docker compose exec growth-agent python -c "
import urllib.request
print(urllib.request.urlopen('http://localhost:8080/debug/search-console').read().decode())
"
```

If either returns an error, double check: `GOOGLE_OAUTH_CLIENT_ID`/
`GOOGLE_OAUTH_CLIENT_SECRET`/`GOOGLE_OAUTH_REFRESH_TOKEN` are all set in `.env` (from
step 2.5), the OAuth consent screen is set to **Internal** (an External app in Testing
mode has refresh tokens that expire after 7 days), `GA4_PROPERTY_ID` is the numeric
Property ID (not the `G-XXXXXXX` measurement ID), and `SEARCH_CONSOLE_SITE_URL` exactly
matches the verified property's format (`sc-domain:...` vs. `https://.../`).

Only once both debug routes return real, clean data should you move on to a full
end-to-end test.

## 7. Manually test end-to-end

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

## Repo layout

```
docker-compose.yml
Dockerfile
requirements.txt
.env.example
scripts/
  authorize_google.py One-time local OAuth authorization helper (not in the Docker image)
app/
  main.py             Flask app: GET /health, GET /debug/ga4, GET /debug/search-console,
                       POST /run-now
  scheduler.py        APScheduler daily job (default 08:00 UTC)
  db.py               Read-only Postgres connection
  metrics.py          Signup/upload/lead queries + GA4/Search Console merge -> plain JSON
  google_auth.py      Shared OAuth credential helper (GA4 + Search Console)
  ga4.py              GA4 Data API: sessions, tool-page views, funnel event counts
  search_console.py   Search Console API: top queries/pages by clicks
  brief.py            Builds LLM prompt, calls OpenAI, returns markdown brief
  email_sender.py     Sends the brief via Resend
```

## Data sources

DataLayer's own Postgres (signups, uploads, free-tool leads), GA4 (traffic, per-tool-page
sessions, funnel event counts), and Search Console (top search queries/pages) are all
wired in. If a GA4 or Search Console fetch fails on a given run, that run's brief still
sends — the failure is reported under `data_gaps` in the metrics JSON instead of crashing
the whole pipeline, since the DB-only brief is still valuable on its own. DB, LLM, and
email failures are NOT handled this way — they still fail loudly, since there's no
meaningful fallback for those.
