# datalayer-growth-agent

Internal V0 growth system for DataLayer (usedatalayer.com). Once a day it:

1. Reads DataLayer's own Postgres metrics (signups, uploads, free-tool leads) via a
   read-only DB role — no GA4/Search Console yet, that's Phase 1.
2. Sends the metrics to an LLM (`gpt-4o-mini`) to identify the single biggest growth
   problem and rank recommended actions.
3. Emails the resulting brief once a day via Resend.

Deliberately minimal: Flask + Postgres + OpenAI API + APScheduler + Docker only. No
GA4/GSC, no scraping, no outreach drafting, no experiment tracking, no multi-agent
frameworks, no LangChain/vector DB/Redis/Celery/Kubernetes.

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

## 2. Configure environment

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

## 3. Wire up the Docker network

This service has no published port and is not routed through nginx — it must not be
publicly reachable. It joins the same Docker network as `../datalayer-ecommerce`'s `db`
service so it can reach Postgres by hostname (`db`).

`docker-compose.yml` here declares that network as `external: true` with
`name: datalayer-ecommerce_default` — Compose's default network name for a project with
no `COMPOSE_PROJECT_NAME` override, derived from the sibling repo's directory name.
**Confirm this matches before first run** (on both your local machine and, separately,
on the VPS — the clone directory name could differ there):

```bash
docker network ls | grep datalayer
```

If it differs, update the `name:` field in `docker-compose.yml` to match.

## 4. Run it

```bash
docker compose up -d --build
```

This boots the Flask app (internal-only, no published port) and starts the daily
APScheduler job (default 08:00 UTC, configurable via `BRIEF_SEND_HOUR_UTC`).

## 5. Manually test end-to-end

`/run-now` triggers a real OpenAI API call (billed) and a real email send via Resend.
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
app/
  main.py        Flask app: GET /health, POST /run-now
  scheduler.py    APScheduler daily job (default 08:00 UTC)
  db.py            Read-only Postgres connection
  metrics.py       Signup/upload/lead queries -> plain JSON dict
  brief.py          Builds LLM prompt, calls OpenAI, returns markdown brief
  email_sender.py   Sends the brief via Resend
```

## Known gap (by design for V0)

There is no page-visit or tool-usage tracking table in DataLayer's Postgres — that data
only exists in GA4, which is out of scope for V0. Every generated brief includes a fixed
disclaimer noting this, so the LLM never fabricates traffic/funnel numbers it wasn't
given. Wiring in GA4/Search Console is Phase 1.
