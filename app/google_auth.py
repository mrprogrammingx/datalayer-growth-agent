"""Shared Google service-account credential helper for GA4 and Search Console.

Both APIs are read-only and use the same downloaded service-account JSON key
(see README for setup) - only the OAuth scope differs per API.
"""
import os

from google.oauth2 import service_account

GA4_SCOPES = ['https://www.googleapis.com/auth/analytics.readonly']
SEARCH_CONSOLE_SCOPES = ['https://www.googleapis.com/auth/webmasters.readonly']


def _credentials_path() -> str:
    path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', '')
    if not path:
        raise RuntimeError('GOOGLE_APPLICATION_CREDENTIALS is not set')
    return path


def get_ga4_credentials():
    return service_account.Credentials.from_service_account_file(
        _credentials_path(), scopes=GA4_SCOPES
    )


def get_search_console_credentials():
    return service_account.Credentials.from_service_account_file(
        _credentials_path(), scopes=SEARCH_CONSOLE_SCOPES
    )
