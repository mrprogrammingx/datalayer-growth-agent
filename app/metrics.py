"""Pulls DataLayer's own signup/upload/lead metrics, plus GA4 traffic/funnel
data and Search Console query/page data, for the growth brief.

Every function returns plain JSON-serializable dicts/lists only - no raw
cursor rows or DB objects - since the output of collect_metrics() is fed
straight into the LLM prompt in brief.py.

GA4 and Search Console fetches are each wrapped in their own try/except:
a failure there must not crash the whole brief or block the email, since
the DB-only brief is still valuable on its own - it just means data_gaps
gets a specific note about what failed and why. This is different from
the DB/LLM/email failures in the rest of the pipeline, which correctly
stay fail-loud since there's no fallback for those.
"""
import logging
from datetime import date, timedelta
from typing import Any, Dict, List

from .db import connect_to_db
from .ga4 import fetch_ga4_metrics
from .search_console import fetch_search_console_metrics

LOGGER = logging.getLogger(__name__)

WINDOW_DAYS = 30


def _daily_counts(cur, table: str, ts_column: str, start: date, end: date) -> List[Dict[str, Any]]:
    """Daily counts of rows in `table` where `ts_column` falls in [start, end)."""
    cur.execute(
        f"""
        SELECT DATE({ts_column}) AS day, COUNT(*) AS n
        FROM {table}
        WHERE {ts_column} >= %s AND {ts_column} < %s
        GROUP BY DATE({ts_column})
        ORDER BY day
        """,
        (start, end),
    )
    return [{'date': row[0].isoformat(), 'count': row[1]} for row in cur.fetchall()]


def _period_metric(cur, table: str, ts_column: str, today: date) -> Dict[str, Any]:
    last_30_start = today - timedelta(days=WINDOW_DAYS)
    prior_30_start = today - timedelta(days=2 * WINDOW_DAYS)

    last_30_daily = _daily_counts(cur, table, ts_column, last_30_start, today)
    prior_30_daily = _daily_counts(cur, table, ts_column, prior_30_start, last_30_start)

    last_30_total = sum(d['count'] for d in last_30_daily)
    prior_30_total = sum(d['count'] for d in prior_30_daily)
    delta = last_30_total - prior_30_total
    pct_change = round((delta / prior_30_total) * 100, 1) if prior_30_total else None

    return {
        'last_30_days': {'total': last_30_total, 'daily': last_30_daily},
        'prior_30_days': {'total': prior_30_total, 'daily': prior_30_daily},
        'delta': delta,
        'pct_change': pct_change,
    }


def _plan_tier_distribution(cur) -> Dict[str, int]:
    cur.execute(
        """
        SELECT COALESCE(plan_tier, 'unknown') AS tier, COUNT(*) AS n
        FROM users
        GROUP BY tier
        ORDER BY n DESC
        """
    )
    return {row[0]: row[1] for row in cur.fetchall()}


def collect_metrics() -> Dict[str, Any]:
    today = date.today()
    conn = connect_to_db()
    try:
        cur = conn.cursor()
        signups = _period_metric(cur, 'users', 'created_at', today)
        uploads = _period_metric(cur, 'uploads', 'uploaded_at', today)
        leads = _period_metric(cur, 'csv_tool_leads', 'created_at', today)
        plan_tiers = _plan_tier_distribution(cur)
    finally:
        conn.close()

    paying_customers = sum(n for tier, n in plan_tiers.items() if tier not in ('free', 'unknown'))

    data_gaps: List[str] = []

    ga4_metrics = None
    try:
        ga4_metrics = fetch_ga4_metrics()
    except Exception as exc:
        LOGGER.exception('GA4 fetch failed')
        data_gaps.append(f'GA4 traffic/funnel data unavailable this run: {exc}')

    search_console_metrics = None
    try:
        search_console_metrics = fetch_search_console_metrics()
    except Exception as exc:
        LOGGER.exception('Search Console fetch failed')
        data_gaps.append(f'Search Console query/page data unavailable this run: {exc}')

    return {
        'generated_at': today.isoformat(),
        'window_days': WINDOW_DAYS,
        'signups': signups,
        'uploads': uploads,
        'csv_tool_leads': leads,
        'plan_tier_distribution': plan_tiers,
        'paying_customers': paying_customers,
        'ga4': ga4_metrics,
        'search_console': search_console_metrics,
        'data_gaps': data_gaps,
    }
