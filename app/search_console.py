"""Pulls top search queries/pages from the Search Console API.

Search Console data has a reporting lag, so this queries the 30-day window
ending 3 days ago rather than up to today - querying more recent days
returns incomplete/missing data.
"""
import os
from datetime import date, timedelta
from typing import Any, Dict, List

from googleapiclient.discovery import build

from .google_auth import get_search_console_credentials

WINDOW_DAYS = 30
REPORTING_LAG_DAYS = 3
ROW_LIMIT = 20


def _site_url() -> str:
    site_url = os.environ.get('SEARCH_CONSOLE_SITE_URL', '')
    if not site_url:
        raise RuntimeError('SEARCH_CONSOLE_SITE_URL is not set')
    return site_url


def _query(service, start: date, end: date, dimension: str) -> List[Dict[str, Any]]:
    response = service.searchanalytics().query(
        siteUrl=_site_url(),
        body={
            'startDate': start.isoformat(),
            'endDate': end.isoformat(),
            'dimensions': [dimension],
            'rowLimit': ROW_LIMIT,
        },
    ).execute()

    rows = []
    for row in response.get('rows', []):
        rows.append({
            dimension: row['keys'][0],
            'clicks': row['clicks'],
            'impressions': row['impressions'],
            'ctr': round(row['ctr'], 4),
            'position': round(row['position'], 1),
        })
    return rows


def fetch_search_console_metrics() -> Dict[str, Any]:
    """Returns top queries and top pages by clicks for the last 30 days
    ending 3 days ago. Raises on any API/auth error - callers (metrics.py)
    must catch this and append a data_gaps note instead of crashing the
    whole brief.
    """
    today = date.today()
    end = today - timedelta(days=REPORTING_LAG_DAYS)
    start = end - timedelta(days=WINDOW_DAYS)

    service = build(
        'searchconsole', 'v1', credentials=get_search_console_credentials(), cache_discovery=False
    )

    return {
        'window_days': WINDOW_DAYS,
        'start_date': start.isoformat(),
        'end_date': end.isoformat(),
        'top_queries': _query(service, start, end, 'query'),
        'top_pages': _query(service, start, end, 'page'),
    }
