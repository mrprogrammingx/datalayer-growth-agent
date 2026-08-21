# datalayer-growth-agent

Internal growth system for DataLayer (usedatalayer.com). Once a day it:

1. Reads DataLayer's own Postgres metrics (signups, uploads, free-tool leads) via a
   read-only DB role.
2. Reads real GA4 traffic/funnel data (sessions, per-tool-page views, funnel event
   counts) and Search Console data (top queries/pages) via Google service-account APIs.
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

The growth brief pulls real GA4 and Search Console data via a dedicated read-only Google
service account. This reuses the existing GCP project already tied to
`datalayer-ecommerce` — do not create a new project.

1. **Find the existing GCP project.** Check `GOOGLE_CLOUD_PROJECT_NUMBER` in
   `../datalayer-ecommerce/.env` and open that project in the
   [Cloud Console](https://console.cloud.google.com/).
2. **Enable the two APIs.** In that project, go to **APIs & Services > Library** and
   enable:
   - `Google Analytics Data API`
   - `Google Search Console API`

   Both are free — no billing required beyond normal quota limits.
3. **Create a service account.** Go to **APIs & Services > Credentials > Create
   Credentials > Service account**. Name it something like
   `growth-agent-readonly` (resulting email:
   `growth-agent-readonly@<project-id>.iam.gserviceaccount.com`).
4. **Generate a JSON key.** Open the new service account > **Keys > Add Key > Create new
   key > JSON**, and download it.
5. **Grant GA4 access.** In [GA4](https://analytics.google.com/), go to **Admin > Property
   Access Management > Add users**, paste the service account's email, and grant it the
   **Viewer** role.
6. **Grant Search Console access.** In
   [Search Console](https://search.google.com/search-console), go to **Settings > Users
   and permissions > Add user**, paste the service account's email, and grant it
   **Restricted** permission (sufficient for read-only Search Analytics queries).
7. **Record the GA4 Property ID.** In GA4, go to **Admin > Property Settings** and copy
   the numeric **Property ID** (NOT the `G-XXXXXXX` measurement ID used in the site's
   tracking snippet — that's a different identifier). This goes in `.env` as
   `GA4_PROPERTY_ID`.
8. **Confirm the Search Console property type.** In the Search Console UI, check whether
   the verified property is a **domain property** (shown as `sc-domain:usedatalayer.com`)
   or a **URL-prefix property** (shown as `https://usedatalayer.com/`). Copy the exact
   value shown — this goes in `.env` as `SEARCH_CONSOLE_SITE_URL` and must match exactly
   (including the trailing slash for URL-prefix properties).
9. **Place the JSON key on each host.** The downloaded key can't travel through git
   (`secrets/` is gitignored). Copy it out-of-band (e.g. `scp`) to
   `./secrets/ga-service-account.json` on **each host separately** — local dev and the
   prod VPS each need their own copy of the same file, even though it's the same
   credentials.

```bash
mkdir -p secrets
# scp the downloaded key to secrets/ga-service-account.json on this host
```

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
- `GOOGLE_APPLICATION_CREDENTIALS`, `GA4_PROPERTY_ID`, `SEARCH_CONSOLE_SITE_URL` — from
  step 2

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

If either returns an error, double check: the service account has Viewer/Restricted
access granted in step 2.5/2.6, `GA4_PROPERTY_ID` is the numeric Property ID (not the
`G-XXXXXXX` measurement ID), `SEARCH_CONSOLE_SITE_URL` exactly matches the verified
property's format (`sc-domain:...` vs. `https://.../`), and
`./secrets/ga-service-account.json` exists on this host and is the mounted path.

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
secrets/                    Per-host service-account JSON key (gitignored, not in git)
app/
  main.py             Flask app: GET /health, GET /debug/ga4, GET /debug/search-console,
                       POST /run-now
  scheduler.py        APScheduler daily job (default 08:00 UTC)
  db.py               Read-only Postgres connection
  metrics.py          Signup/upload/lead queries + GA4/Search Console merge -> plain JSON
  google_auth.py      Shared service-account credential helper (GA4 + Search Console)
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
