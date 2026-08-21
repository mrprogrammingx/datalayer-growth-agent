"""Pulls DataLayer's own signup/upload/lead metrics, plus GA4 traffic/funnel
data, Search Console query/page data, Reddit discussion data, pending
tracked items from prior briefs, and outcomes of already-resolved tracked
items, for the growth brief.

Every function returns plain JSON-serializable dicts/lists only - no raw
cursor rows or DB objects - since the output of collect_metrics() is fed
straight into the LLM prompt in brief.py.

GA4, Search Console, Reddit, pending-tracking, and resolved-action-outcome
fetches are each wrapped in their own try/except: a failure there must not
crash the whole brief or block the email, since the DB-only brief is still
valuable on its own - it just means data_gaps gets a specific note about
what failed and why. This is different from the DB/LLM/email failures in
the rest of the pipeline, which correctly stay fail-loud since there's no
fallback for those. `lead_research`'s raw fetch is a DB query like the
other DB-only metrics (uses the always-required read-only connection), not
a separate optional fetch, so it stays fail-loud along with them - unlike
`pending_from_prior_briefs` and `resolved_action_outcomes`, which depend on
the separate, optional tracking connection (see tracking.py) and so degrade
gracefully like GA4/Search Console/Reddit. `resolved_action_outcomes`
additionally opens its own short-lived read-only connection (once it knows,
via the tracking connection, that there's at least one eligible item) to
compute before/after signup/upload counts - two separate connections joined
in Python, not a SQL join, so neither the read-only role's nor the tracking
role's existing grants need to change. `lead_research` gets a FILTERING
step layered on top of its fail-loud raw fetch (excluding leads already
marked contacted/skipped) that DOES depend on the optional tracking
connection - unlike every other optional field here, a failure in that
filtering step does not null out lead_research, it falls back to the
unfiltered (but still capped) list, recorded in data_gaps as a distinct
"filtering unavailable" note rather than "field unavailable".
"""
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List

from .db import connect_to_db
from .ga4 import fetch_ga4_metrics
from .reddit_discovery import fetch_reddit_discussions
from .search_console import fetch_search_console_metrics
from .tracking import get_excluded_lead_emails, get_pending_items, get_resolved_items_for_attribution

LOGGER = logging.getLogger(__name__)

WINDOW_DAYS = 30

ATTRIBUTION_WINDOW_DAYS = 7
ATTRIBUTION_MIN_DAYS_SINCE_RESOLVED = 7
# Given DataLayer's real traffic (~3-14 signups per rolling 30-day window, i.e.
# roughly 0.1-0.5/day), a 7-day before+after window will almost always land
# under this - that's expected, not a mis-tuned threshold: it means "too small
# to interpret" fires most runs, which is the honest answer at this traffic
# volume. See brief.py's SYSTEM_PROMPT for how this is used.
LOW_SIGNAL_TOTAL_THRESHOLD = 5

# Shared with weekly_report.py's plain-code formatter, and imported by brief.py
# for the LLM prompt's required sentence template - kept as ONE constant so the
# caution wording can't silently drift between the LLM-narrated daily brief and
# the deterministic weekly report.
NO_CONTROL_GROUP_CAVEAT = (
    'One data point, no control group - directional at most, not proof of effect.'
)

WEEKLY_WINDOW_DAYS = 7

# The weekly report's "each done item appears in exactly one weekly report"
# guarantee depends on this window's width equaling WEEKLY_WINDOW_DAYS -
# expressed here as a derived value, not two independently-set literals
# that happen to match, so changing the report cadence can't silently
# desync from the attribution window without also changing this line.
ATTRIBUTION_MAX_DAYS_SINCE_RESOLVED = ATTRIBUTION_MIN_DAYS_SINCE_RESOLVED + WEEKLY_WINDOW_DAYS

# The DAILY brief calls get_resolved_action_outcomes() every single day, not
# once a week - reusing the wide weekly window here would show the same
# resolved item with identical numbers in ~7 consecutive daily emails. A
# 1-day-wide window means an item becomes eligible on exactly one calendar
# day (the day it turns ATTRIBUTION_MIN_DAYS_SINCE_RESOLVED days old), so it
# appears in exactly one daily brief too, same "exactly once" guarantee the
# weekly report already had.
ATTRIBUTION_DAILY_WINDOW_DAYS = 1
ATTRIBUTION_DAILY_MAX_DAYS_SINCE_RESOLVED = (
    ATTRIBUTION_MIN_DAYS_SINCE_RESOLVED + ATTRIBUTION_DAILY_WINDOW_DAYS
)

# _lead_research() fetches a wider buffer than what's actually shown, so
# collect_metrics() has headroom to filter out already-contacted/skipped
# leads (see tracking.get_excluded_lead_emails) and still backfill up to
# LEAD_RESEARCH_DISPLAY_LIMIT candidates - filtering the already-capped
# top 10 would just shrink the shown list toward empty as leads get
# resolved, instead of surfacing the next-oldest untouched one.
LEAD_RESEARCH_FETCH_LIMIT = 50
LEAD_RESEARCH_DISPLAY_LIMIT = 10


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


def _totals_before_after(cur, table: str, ts_column: str, boundary, window_days: int) -> Dict[str, Any]:
    """Shared core for _week_over_week_metric() and _anchored_window_counts():
    two adjacent windows of `window_days` each, split at `boundary` -
    [boundary-window_days, boundary) ("before") and
    [boundary, boundary+window_days) ("after"). `boundary` may be a `date`
    or `datetime`; both support the same arithmetic/comparison used here.

    These two callers used to duplicate this exact summing logic with
    slightly different variable names - factored out so a future fix to
    the before/after totaling (e.g. a boundary off-by-one) only has one
    place to land instead of two, which could otherwise silently drift.
    """
    before_start = boundary - timedelta(days=window_days)
    after_end = boundary + timedelta(days=window_days)
    before_total = sum(d['count'] for d in _daily_counts(cur, table, ts_column, before_start, boundary))
    after_total = sum(d['count'] for d in _daily_counts(cur, table, ts_column, boundary, after_end))
    return {'before_total': before_total, 'after_total': after_total, 'delta': after_total - before_total}


def _week_over_week_metric(cur, table: str, ts_column: str, today: date) -> Dict[str, Any]:
    """Week-over-week sibling of _period_metric(), for the weekly report
    only. Not a window_days param on _period_metric() itself - that
    function's return keys (last_30_days/prior_30_days) are hardcoded to
    30-day semantics and it sits on collect_metrics()'s fail-loud,
    always-required DB block; changing its signature risks that path for
    no reason.

    Deliberately no pct_change - same reasoning as _anchored_window_counts():
    a percentage off DataLayer's small weekly bases (e.g. 1->3 as "+200%")
    would overstate significance, and unlike the LLM-narrated daily brief,
    the weekly report has no LLM present to soften a scary-looking number.
    """
    totals = _totals_before_after(
        cur, table, ts_column, today - timedelta(days=WEEKLY_WINDOW_DAYS), WEEKLY_WINDOW_DAYS
    )
    return {
        'last_7_days': {'total': totals['after_total']},
        'prior_7_days': {'total': totals['before_total']},
        'delta': totals['delta'],
    }


def collect_weekly_datalayer_metrics() -> Dict[str, Any]:
    """DB metrics for the weekly report - fail-loud, same philosophy as
    collect_metrics()'s own DB block (no meaningful fallback for a DB
    failure).
    """
    today = date.today()
    conn = connect_to_db()
    try:
        cur = conn.cursor()
        signups = _week_over_week_metric(cur, 'users', 'created_at', today)
        uploads = _week_over_week_metric(cur, 'uploads', 'uploaded_at', today)
        leads = _week_over_week_metric(cur, 'csv_tool_leads', 'created_at', today)
    finally:
        conn.close()
    return {
        'window_days': WEEKLY_WINDOW_DAYS,
        'signups': signups,
        'uploads': uploads,
        'csv_tool_leads': leads,
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


def _lead_research(cur, limit: int = LEAD_RESEARCH_FETCH_LIMIT) -> List[Dict[str, Any]]:
    """Free-tool leads who gave an email but never signed up (no matching
    `users` row). Deliberately excludes `csv_content` - it can contain a
    lead's own customers' PII (their uploaded order/customer data) and must
    never reach the LLM prompt.

    `limit` defaults to LEAD_RESEARCH_FETCH_LIMIT (a wide buffer), not
    LEAD_RESEARCH_DISPLAY_LIMIT (what's actually shown) - collect_metrics()
    filters and slices down to the display limit afterward. This function
    itself is unchanged otherwise: still fail-loud, still the same
    always-required read-only connection as before lead-outreach tracking
    existed.
    """
    cur.execute(
        """
        SELECT l.email, l.score, l.row_count, l.file_name, l.created_at
        FROM csv_tool_leads l
        LEFT JOIN users u ON u.email = l.email
        WHERE u.email IS NULL
        ORDER BY l.created_at DESC NULLS LAST
        LIMIT %s
        """,
        (limit,),
    )
    return [
        {
            'email': row[0],
            'score': row[1],
            'row_count': row[2],
            'file_name': row[3],
            'created_at': row[4].isoformat() if row[4] else None,
        }
        for row in cur.fetchall()
    ]


def _anchored_window_counts(
    cur, table: str, ts_column: str, anchor: datetime, window_days: int = ATTRIBUTION_WINDOW_DAYS
) -> Dict[str, Any]:
    """Anchored-date sibling of _period_metric(): total counts in `table`
    for the `window_days` immediately before `anchor` vs. immediately
    after it, where `anchor` is an arbitrary past timestamp (a tracked
    item's resolved_at), not "today".

    Returns totals only - no daily breakdown, no pct_change (unlike
    _period_metric). Deliberate: at per-item scale (single-digit counts) a
    daily array adds prompt noise without signal, and a pct_change off
    totals this small (e.g. 0->1) actively overstates significance rather
    than just being uninformative - see LOW_SIGNAL_TOTAL_THRESHOLD/
    brief.py's SYSTEM_PROMPT instead.
    """
    return _totals_before_after(cur, table, ts_column, anchor, window_days)


def _resolved_action_outcomes(cur, resolved_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Computes before/after signup+upload counts for each already-fetched
    resolved ('done') item, for the "Past action outcomes" brief section.

    Split from get_resolved_items_for_attribution() deliberately - that
    function uses the tracking connection/role, this one uses the
    read-only connection/role; collect_metrics() is what bridges them via
    a Python merge, so neither role's grants need to change.
    """
    outcomes = []
    for item in resolved_items:
        anchor = datetime.fromisoformat(item['resolved_at'])
        signups = _anchored_window_counts(cur, 'users', 'created_at', anchor)
        uploads = _anchored_window_counts(cur, 'uploads', 'uploaded_at', anchor)
        combined_total = (
            signups['before_total'] + signups['after_total']
            + uploads['before_total'] + uploads['after_total']
        )
        low_signal = combined_total < LOW_SIGNAL_TOTAL_THRESHOLD
        outcomes.append({
            'id': item['id'],
            'category': item['category'],
            'description': item['description'],
            'outcome_note': item['outcome_note'],
            'resolved_at': item['resolved_at'],
            'window_days': ATTRIBUTION_WINDOW_DAYS,
            'signups': signups,
            'uploads': uploads,
            'low_signal': low_signal,
            'low_signal_note': (
                f'Only {combined_total} combined signups+uploads across both '
                f'{ATTRIBUTION_WINDOW_DAYS}-day windows - too small to support any '
                'claim about impact either way.'
                if low_signal else None
            ),
        })
    return outcomes


def get_resolved_action_outcomes(
    min_days_since_resolved: int = ATTRIBUTION_MIN_DAYS_SINCE_RESOLVED,
    max_days_since_resolved: int = ATTRIBUTION_MAX_DAYS_SINCE_RESOLVED,
) -> List[Dict[str, Any]]:
    """Fetches eligible resolved items (tracking connection) and computes
    their before/after outcomes (a fresh read-only connection), joined in
    Python. Shared by both collect_metrics() (daily brief) and
    weekly_report.py.

    The default window (7-14 days since resolved) is sized for
    weekly_report.py's cadence - its 7-day width exactly matches the
    weekly report's cadence, so each item lands in it during exactly one
    weekly run, no gaps, no overlap. collect_metrics() (the DAILY brief)
    explicitly overrides max_days_since_resolved to a 1-day-wide window
    instead (ATTRIBUTION_DAILY_MAX_DAYS_SINCE_RESOLVED) - reusing the wide
    default there would show the same item with identical numbers in ~7
    consecutive daily emails, since this runs once a day, not once a week.

    Raises on any error - callers must catch and degrade gracefully into
    data_gaps, same as get_pending_items.
    """
    resolved_items = get_resolved_items_for_attribution(
        min_days_since_resolved=min_days_since_resolved,
        max_days_since_resolved=max_days_since_resolved,
    )
    if not resolved_items:
        return []
    conn = connect_to_db()
    try:
        return _resolved_action_outcomes(conn.cursor(), resolved_items)
    finally:
        conn.close()


def collect_metrics() -> Dict[str, Any]:
    today = date.today()
    conn = connect_to_db()
    try:
        cur = conn.cursor()
        signups = _period_metric(cur, 'users', 'created_at', today)
        uploads = _period_metric(cur, 'uploads', 'uploaded_at', today)
        leads = _period_metric(cur, 'csv_tool_leads', 'created_at', today)
        plan_tiers = _plan_tier_distribution(cur)
        lead_research = _lead_research(cur)
    finally:
        conn.close()

    paying_customers = sum(n for tier, n in plan_tiers.items() if tier not in ('free', 'unknown'))

    data_gaps: List[str] = []

    try:
        excluded_emails = get_excluded_lead_emails()
        lead_research = [lead for lead in lead_research if lead['email'] not in excluded_emails]
    except Exception as exc:
        LOGGER.exception('Lead-outreach exclusion lookup failed')
        data_gaps.append(
            'Lead outreach filtering unavailable this run - showing unconverted leads '
            f'unfiltered (may include already-contacted/skipped ones): {exc}'
        )
    lead_research = lead_research[:LEAD_RESEARCH_DISPLAY_LIMIT]

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

    reddit_discussions = None
    try:
        reddit_discussions = fetch_reddit_discussions()
    except Exception as exc:
        LOGGER.exception('Reddit fetch failed')
        data_gaps.append(f'Reddit discussion data unavailable this run: {exc}')

    pending_from_prior_briefs = None
    try:
        pending_from_prior_briefs = get_pending_items(older_than_days=3)
    except Exception as exc:
        LOGGER.exception('Pending-action lookup failed')
        data_gaps.append(f'Pending-action tracking unavailable this run: {exc}')

    resolved_action_outcomes = None
    try:
        # Narrow 1-day window (not the weekly report's default 7-day one) -
        # this runs daily, so a wide window would show the same item with
        # identical numbers in ~7 consecutive daily briefs. See
        # ATTRIBUTION_DAILY_MAX_DAYS_SINCE_RESOLVED's comment above.
        resolved_action_outcomes = get_resolved_action_outcomes(
            max_days_since_resolved=ATTRIBUTION_DAILY_MAX_DAYS_SINCE_RESOLVED
        )
    except Exception as exc:
        LOGGER.exception('Resolved-action attribution failed')
        data_gaps.append(f'Resolved-action attribution unavailable this run: {exc}')

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
        'reddit_discussions': reddit_discussions,
        'lead_research': lead_research,
        'pending_from_prior_briefs': pending_from_prior_briefs,
        'resolved_action_outcomes': resolved_action_outcomes,
        'data_gaps': data_gaps,
    }
