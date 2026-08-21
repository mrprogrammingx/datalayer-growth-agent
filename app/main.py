"""Internal-only Flask app: boots the scheduler, serves /health and /run-now.

Not routed through nginx and has no published port in docker-compose.yml -
/run-now triggers a real OpenAI call and a real email send, so it must
never be publicly reachable. Use `docker exec` or curl from inside the
Docker network to hit it for manual testing.
"""
import logging

from flask import Flask, jsonify

from .ga4 import fetch_ga4_metrics
from .scheduler import start_scheduler, run_growth_brief_job
from .search_console import fetch_search_console_metrics

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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
