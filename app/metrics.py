"""Pulls DataLayer's own signup/upload/lead metrics for the growth brief.

Every function returns plain JSON-serializable dicts/lists only - no raw
cursor rows or DB objects - since the output of collect_metrics() is fed
straight into the LLM prompt in brief.py.

There is no page-visit or tool-usage tracking table in this database
(that only exists in GA4, which is out of scope for V0) - this module
must never claim to have traffic/funnel data it doesn't have.
"""
from datetime import date, timedelta
from typing import Any, Dict, List

from .db import connect_to_db

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

    return {
        'generated_at': today.isoformat(),
        'window_days': WINDOW_DAYS,
        'signups': signups,
        'uploads': uploads,
        'csv_tool_leads': leads,
        'plan_tier_distribution': plan_tiers,
        'paying_customers': paying_customers,
        'data_gaps': [
            'No website traffic or per-page/per-tool visit tracking is available in this '
            'database (GA4/Search Console not yet wired in - Phase 1). Only signup, upload, '
            'and free-tool-lead events below are real, measured data.'
        ],
    }
