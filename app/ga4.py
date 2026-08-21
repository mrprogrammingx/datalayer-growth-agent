"""Pulls real website traffic/funnel data from the GA4 Data API.

Every function returns plain JSON-serializable dicts/lists only, same
convention as metrics.py, since the output is fed straight into the LLM
prompt in brief.py.
"""
import os
from datetime import date, timedelta
from typing import Any, Dict, List

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,
    RunReportRequest,
)

from .google_auth import get_google_credentials

WINDOW_DAYS = 30

# Real sitewide custom events (templates/_analytics.html's dlTrackEvent wrapper).
SITEWIDE_EVENTS = [
    'upload_started',
    'upload_completed',
    'signup_started',
    'signup_completed',
]

# Real free-tool lead-gen events (static/js/csv-tool.js, gtag() directly).
TOOL_PAGE_EVENTS = [
    'csv_uploaded',
    'low_confidence_file',
    'score_shown',
    'fix_clicked',
    'fix_completed',
    'insights_viewed',
    'segments_viewed',
    'csv_downloaded',
    'segments_csv_downloaded',
    'email_capture_failed',
    'email_captured',
    'low_confidence_reset_clicked',
    'low_confidence_see_anyway_clicked',
    'datalayer_cta_clicked',
]

TOOL_PAGE_PATHS = [
    '/tools',
    '/tools/csv-cleaner',
    '/tools/customer-segments',
    '/tools/shopify-orders-csv-cleaner',
    '/tools/woocommerce-orders-csv-cleaner',
    '/tools/instagram-dm-orders-csv-cleaner',
    '/tools/etsy-orders-csv-cleaner',
    '/tools/amazon-seller-orders-csv-cleaner',
    '/tools/ebay-orders-csv-cleaner',
    '/tools/tiktok-shop-orders-csv-cleaner',
    '/tools/squarespace-orders-csv-cleaner',
    '/tools/square-orders-csv-cleaner',
]


def _property_id() -> str:
    property_id = os.environ.get('GA4_PROPERTY_ID', '')
    if not property_id:
        raise RuntimeError('GA4_PROPERTY_ID is not set')
    return property_id


def _client() -> BetaAnalyticsDataClient:
    return BetaAnalyticsDataClient(credentials=get_google_credentials())


def _date_range_pair(today: date):
    last_30_start = today - timedelta(days=WINDOW_DAYS)
    prior_30_start = today - timedelta(days=2 * WINDOW_DAYS)
    prior_30_end = last_30_start - timedelta(days=1)
    last_30_end = today - timedelta(days=1)
    return (
        DateRange(start_date=last_30_start.isoformat(), end_date=last_30_end.isoformat()),
        DateRange(start_date=prior_30_start.isoformat(), end_date=prior_30_end.isoformat()),
    )


def _totals(client: BetaAnalyticsDataClient, today: date) -> Dict[str, Any]:
    last_30_range, prior_30_range = _date_range_pair(today)
    request = RunReportRequest(
        property=f'properties/{_property_id()}',
        date_ranges=[last_30_range, prior_30_range],
        metrics=[Metric(name='sessions'), Metric(name='totalUsers')],
    )
    response = client.run_report(request)

    last_30 = {'sessions': 0, 'users': 0}
    prior_30 = {'sessions': 0, 'users': 0}
    # GA4 returns one row per requested date range, in the same order as
    # date_ranges (no dimension_values entry needed to tell them apart).
    if len(response.rows) >= 1:
        last_30['sessions'] = int(response.rows[0].metric_values[0].value)
        last_30['users'] = int(response.rows[0].metric_values[1].value)
    if len(response.rows) >= 2:
        prior_30['sessions'] = int(response.rows[1].metric_values[0].value)
        prior_30['users'] = int(response.rows[1].metric_values[1].value)

    delta = last_30['sessions'] - prior_30['sessions']
    pct_change = round((delta / prior_30['sessions']) * 100, 1) if prior_30['sessions'] else None

    return {
        'last_30_days': last_30,
        'prior_30_days': prior_30,
        'sessions_delta': delta,
        'sessions_pct_change': pct_change,
    }


def _page_views(client: BetaAnalyticsDataClient, today: date) -> List[Dict[str, Any]]:
    last_30_range, _ = _date_range_pair(today)
    request = RunReportRequest(
        property=f'properties/{_property_id()}',
        date_ranges=[last_30_range],
        dimensions=[Dimension(name='pagePath')],
        metrics=[Metric(name='sessions')],
        limit=200,
    )
    response = client.run_report(request)

    counts: Dict[str, int] = {}
    for row in response.rows:
        counts[row.dimension_values[0].value] = int(row.metric_values[0].value)

    return [
        {'page_path': path, 'sessions_last_30_days': counts.get(path, 0)}
        for path in TOOL_PAGE_PATHS
    ]


def _event_counts(client: BetaAnalyticsDataClient, today: date, event_names: List[str]) -> Dict[str, int]:
    last_30_range, _ = _date_range_pair(today)
    request = RunReportRequest(
        property=f'properties/{_property_id()}',
        date_ranges=[last_30_range],
        dimensions=[Dimension(name='eventName')],
        metrics=[Metric(name='eventCount')],
        limit=200,
    )
    response = client.run_report(request)

    counts: Dict[str, int] = {row.dimension_values[0].value: int(row.metric_values[0].value) for row in response.rows}
    return {name: counts.get(name, 0) for name in event_names}


def fetch_ga4_metrics() -> Dict[str, Any]:
    """Returns sessions/users totals, per-tool-page sessions, and funnel
    event counts for the last 30 days. Raises on any API/auth error -
    callers (metrics.py) must catch this and append a data_gaps note
    instead of crashing the whole brief.
    """
    today = date.today()
    client = _client()

    return {
        'window_days': WINDOW_DAYS,
        'sessions_and_users': _totals(client, today),
        'tool_page_sessions_last_30_days': _page_views(client, today),
        'sitewide_funnel_events_last_30_days': _event_counts(client, today, SITEWIDE_EVENTS),
        'tool_page_funnel_events_last_30_days': _event_counts(client, today, TOOL_PAGE_EVENTS),
    }
