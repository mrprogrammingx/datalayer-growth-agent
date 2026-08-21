"""Internal-only Flask app: boots the scheduler, serves /health and /run-now.

Not routed through nginx and has no published port in docker-compose.yml -
/run-now triggers a real OpenAI call and a real email send, so it must
never be publicly reachable. Use `docker exec` or curl from inside the
Docker network to hit it for manual testing.
"""
import logging

from flask import Flask, jsonify

from .scheduler import start_scheduler, run_growth_brief_job

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
start_scheduler()


@app.get('/health')
def health():
    return jsonify({'status': 'ok'})


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
