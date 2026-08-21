"""Shared Google OAuth credential helper for GA4 and Search Console.

Uses a refresh token tied to a regular OAuth client, obtained once via
scripts/authorize_google.py - NOT a service account. The usedatalayer.com
Google Workspace org enforces the iam.disableServiceAccountKeyCreation
policy, which blocks downloadable service-account JSON keys entirely.
A refresh token from a normal OAuth client isn't affected by that policy,
and since it's tied to a person's own Google account, there's no separate
"grant access" step needed for GA4/Search Console - access already exists.
"""
import os

from google.oauth2.credentials import Credentials

SCOPES = [
    'https://www.googleapis.com/auth/analytics.readonly',
    'https://www.googleapis.com/auth/webmasters.readonly',
]


def _required_env(name: str) -> str:
    value = os.environ.get(name, '')
    if not value:
        raise RuntimeError(f'{name} is not set')
    return value


def get_google_credentials() -> Credentials:
    """One shared Credentials object, valid for both GA4 and Search Console
    scopes - the refresh token was authorized for both at once, unlike
    service accounts which would need per-API grants.
    """
    return Credentials(
        token=None,
        refresh_token=_required_env('GOOGLE_OAUTH_REFRESH_TOKEN'),
        client_id=_required_env('GOOGLE_OAUTH_CLIENT_ID'),
        client_secret=_required_env('GOOGLE_OAUTH_CLIENT_SECRET'),
        token_uri='https://oauth2.googleapis.com/token',
        scopes=SCOPES,
    )
