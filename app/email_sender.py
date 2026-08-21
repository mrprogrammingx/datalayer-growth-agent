"""Sends the growth brief via the Resend HTTP API.

Internal-only path - no `emails` audit-log table, unlike
../datalayer-ecommerce/services/email.py's send_admin_email. Fails loudly:
raises on any error instead of swallowing it, since a silent failure here
means the daily brief just never arrives with no other record of it.
"""
import logging
import os

import requests

LOGGER = logging.getLogger(__name__)

RESEND_API_URL = 'https://api.resend.com/emails'


def _markdown_to_html(markdown_text: str) -> str:
    """Minimal, dependency-free markdown-ish -> HTML for the email body.
    The brief is simple (headers + bullets), so this doesn't need a full
    markdown parser - just preserve line breaks and wrap in <pre>-like styling.
    """
    escaped = (
        markdown_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    )
    return f'<pre style="font-family: monospace; white-space: pre-wrap; font-size: 14px;">{escaped}</pre>'


def send_brief_email(brief_markdown: str) -> dict:
    api_key = os.environ.get('RESEND_API_KEY')
    from_address = os.environ.get('EMAIL_FROM')
    recipient = os.environ.get('GROWTH_BRIEF_RECIPIENT')

    missing = [
        name
        for name, val in [
            ('RESEND_API_KEY', api_key),
            ('EMAIL_FROM', from_address),
            ('GROWTH_BRIEF_RECIPIENT', recipient),
        ]
        if not val
    ]
    if missing:
        raise RuntimeError(f'Missing required env vars: {", ".join(missing)}')

    payload = {
        'from': from_address,
        'to': [recipient],
        'subject': 'DataLayer Growth Brief',
        'text': brief_markdown,
        'html': _markdown_to_html(brief_markdown),
    }

    response = requests.post(
        RESEND_API_URL,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        json=payload,
        timeout=30,
    )

    if response.status_code >= 400:
        LOGGER.error('Resend send failed: %s %s', response.status_code, response.text)
        raise RuntimeError(f'Resend API error {response.status_code}: {response.text}')

    LOGGER.info('Growth brief email sent to %s', recipient)
    return response.json()
