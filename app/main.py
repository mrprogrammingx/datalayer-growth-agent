"""Internal-only Flask app: boots the scheduler, serves /health and manual
trigger routes.

Not routed through nginx and has no published port in docker-compose.yml -
/run-now, /acquisition-report-now, and /weekly-report-now all send real
emails (/run-now and /acquisition-report-now also make a real, billed
OpenAI call). /social-commerce-discovery-now sends NO email but DOES write
to growth_agent_prospects and may make a real, billed OpenRouter call - see
its own docstring. This must never be publicly reachable. Use `docker exec`
or curl from inside the Docker network to hit it for manual testing.
"""
import logging
import threading

from flask import Flask, jsonify

from .apify_prospecting import fetch_shopify_prospects
from .ga4 import fetch_ga4_metrics
from .reddit_discovery import fetch_reddit_discussions
from .scheduler import (
    start_scheduler,
    run_acquisition_report_job,
    run_growth_brief_job,
    run_social_commerce_discovery_job,
    run_weekly_report_job,
)
from .search_console import fetch_search_console_metrics
from .social_commerce_prospecting import fetch_social_commerce_candidates

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
start_scheduler()


@app.get('/health')
def health():
    return jsonify({'status': 'ok'})


@app.get('/debug/ga4')
def debug_ga4():
    """Calls fetch_ga4_metrics() directly and returns the raw JSON - no LLM
    call, no email send. Use this to verify GA4 OAuth credentials and
    property ID without triggering a real (billed) /run-now.
    """
    try:
        return jsonify(fetch_ga4_metrics())
    except Exception as exc:
        logging.exception('debug/ga4 failed')
        return jsonify({'status': 'error', 'error': str(exc)}), 500


@app.get('/debug/search-console')
def debug_search_console():
    """Calls fetch_search_console_metrics() directly and returns the raw
    JSON - no LLM call, no email send. Use this to verify Search Console
    OAuth credentials and site URL without triggering a real (billed)
    /run-now.
    """
    try:
        return jsonify(fetch_search_console_metrics())
    except Exception as exc:
        logging.exception('debug/search-console failed')
        return jsonify({'status': 'error', 'error': str(exc)}), 500


@app.get('/debug/reddit')
def debug_reddit():
    """Calls fetch_reddit_discussions() directly and returns the raw JSON -
    no LLM call, no email send. Use this to verify Reddit API credentials
    without triggering a real (billed) /run-now.
    """
    try:
        return jsonify(fetch_reddit_discussions())
    except Exception as exc:
        logging.exception('debug/reddit failed')
        return jsonify({'status': 'error', 'error': str(exc)}), 500


@app.get('/debug/apify')
def debug_apify():
    """Calls fetch_shopify_prospects() directly and returns the raw JSON -
    no LLM call, no email send. Uses a small max_items (3) - this is a
    credential/wiring check, not a real prospecting run, and Apify Actor
    runs are billed by compute usage. Use this to verify APIFY_API_TOKEN
    without triggering a real (billed, on both OpenRouter and Apify)
    /acquisition-report-now.
    """
    try:
        return jsonify(fetch_shopify_prospects(max_items=3))
    except Exception as exc:
        logging.exception('debug/apify failed')
        return jsonify({'status': 'error', 'error': str(exc)}), 500


@app.get('/debug/spyur')
def debug_spyur():
    """Calls fetch_social_commerce_candidates() with a tiny cap and returns
    raw JSON - no LLM call, no DB write. Unlike /debug/apify, this is a
    free, credential-free public scrape (spyur.am), so this is purely a
    wiring/reachability check, not a cost check.
    """
    try:
        return jsonify(fetch_social_commerce_candidates(max_candidates=3))
    except Exception as exc:
        logging.exception('debug/spyur failed')
        return jsonify({'status': 'error', 'error': str(exc)}), 500


@app.post('/run-now')
def run_now():
    """Manual trigger for testing. Makes a real OpenAI API call and sends a
    real email via Resend - internal-only, no nginx route, not for
    unattended/public use.
    """
    try:
        brief_markdown = run_growth_brief_job()
        return jsonify({'status': 'sent', 'brief': brief_markdown})
    except Exception as exc:
        logging.exception('run-now failed')
        return jsonify({'status': 'error', 'error': str(exc)}), 500


@app.post('/acquisition-report-now')
def acquisition_report_now():
    """Manual trigger for testing. Makes a real OpenAI API call and sends a
    real email via Resend - internal-only, no nginx route, not for
    unattended/public use. Same caution as /run-now.
    """
    try:
        report_markdown = run_acquisition_report_job()
        return jsonify({'status': 'sent', 'report': report_markdown})
    except Exception as exc:
        logging.exception('acquisition-report-now failed')
        return jsonify({'status': 'error', 'error': str(exc)}), 500


@app.post('/weekly-report-now')
def weekly_report_now():
    """Manual trigger for testing. Deterministic, no LLM call and no
    OpenRouter cost (unlike /run-now) - still sends a real email via
    Resend, so still confirm before triggering it, same as /run-now.
    """
    try:
        report_text = run_weekly_report_job()
        return jsonify({'status': 'sent', 'report': report_text})
    except Exception as exc:
        logging.exception('weekly-report-now failed')
        return jsonify({'status': 'error', 'error': str(exc)}), 500


@app.post('/social-commerce-discovery-now')
def social_commerce_discovery_now():
    """Manual trigger for testing. Unlike every other manual trigger in this
    file, does NOT run synchronously and does NOT return a summary in the
    response - it fires run_social_commerce_discovery_job() in a background
    thread and returns immediately with status 'started'.

    This is deliberate, not a shortcut: gunicorn's own --timeout 120
    (Dockerfile) - already raised once, see its own comment, for the
    LLM-call-bound jobs above - is nowhere near enough for THIS job's true
    runtime (multiple minutes, paced by spyur.am's own Crawl-delay: 10 on
    every request to it). Running it synchronously was tried first and
    confirmed live to hit gunicorn's WORKER TIMEOUT well before completion,
    killing the worker mid-fetch and silently losing every candidate found
    so far - nothing had been persisted yet, since mark_prospects_surfaced()
    is the job's last step. A background thread sidesteps gunicorn's
    per-request timeout entirely (the actual DAILY run has the same
    property already, for free - APScheduler runs jobs in its own thread,
    never through a gunicorn request/worker at all, so it was never at risk
    here).

    Check results via `docker logs` for the
    'Social commerce discovery complete: ...' line, or query
    growth_agent_prospects directly - not this response, which only
    confirms the job started.
    """
    try:
        thread = threading.Thread(target=run_social_commerce_discovery_job, daemon=True)
        thread.start()
        return jsonify({'status': 'started'})
    except Exception as exc:
        logging.exception('social-commerce-discovery-now failed to start')
        return jsonify({'status': 'error', 'error': str(exc)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
