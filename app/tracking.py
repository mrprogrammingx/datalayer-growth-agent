"""Persists action/experiment items from the daily growth brief so they get
a stable identity and can be marked done/skipped with an outcome note.

Uses a SEPARATE DB role/connection (GROWTH_AGENT_TRACKING_DATABASE_URL,
growth_agent_tracking role - see README for the CREATE ROLE/GRANT script)
from db.py's read-only GROWTH_AGENT_DATABASE_URL / growth_agent_ro. This is
the only module in the app that ever writes to DataLayer's Postgres, and it
must never share a connection or role with the read-only path so
growth_agent_ro's read-only guarantee is never put at risk.

Optional feature: if GROWTH_AGENT_TRACKING_DATABASE_URL is unset, every
function here raises RuntimeError - callers must catch and degrade
gracefully (metrics.py -> data_gaps, scheduler.py -> log and skip), same
philosophy as ga4.py / search_console.py / reddit_discovery.py.
"""
import logging
import os
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import psycopg2

from .db import psycopg2_connect_kwargs

LOGGER = logging.getLogger(__name__)

VALID_CATEGORIES = ('action', 'experiment')


def _get_tracking_database_url() -> str:
    url = os.environ.get('GROWTH_AGENT_TRACKING_DATABASE_URL', '')
    if not url:
        raise RuntimeError('GROWTH_AGENT_TRACKING_DATABASE_URL is not set')
    return url


def _connect():
    """New read-write connection via the dedicated growth_agent_tracking
    role. NOT session-readonly, unlike db.py's connect_to_db() - this is
    the one connection in the app allowed to write, scoped by GRANT to
    SELECT/INSERT/UPDATE on only growth_agent_tracked_items/
    growth_agent_lead_outreach. Caller must close it - prefer the _cursor()
    context manager below over calling this directly.
    """
    conn = psycopg2.connect(**psycopg2_connect_kwargs(_get_tracking_database_url()))
    conn.autocommit = False
    return conn


@contextmanager
def _cursor(commit: bool = False):
    """Shared connection/cursor lifecycle for every function in this
    module - opens a connection, yields a cursor, always closes the
    connection, and commits only on success when commit=True.

    Every function used to hand-roll `conn = _connect(); try: ...; finally:
    conn.close()` independently (9 near-identical copies) with an
    inconsistent commit convention (some called conn.commit() before the
    finally, some never did, since read-only functions don't need to).
    Factoring it here makes "always closes, commits only when asked and
    only on success" true by construction instead of by 9-way convention -
    a future change to connection handling (retry, timeout, etc.) now has
    one place to land instead of nine.
    """
    conn = _connect()
    try:
        yield conn.cursor()
        if commit:
            conn.commit()
    finally:
        conn.close()


def record_new_items(brief_date: date, items: List[Dict[str, str]]) -> int:
    """Inserts one row per trackable item from today's brief. Skips any
    item whose description exactly matches an already-pending item's
    description - a cheap, honest de-dupe that only catches exact string
    matches (a reworded repeat isn't caught; true de-dupe would be the
    kind of automation deliberately out of scope for this slice). This
    check also catches duplicates WITHIN the same `items` list (e.g. the
    LLM listing the same idea twice in one response), not just against
    rows already in the DB from a prior run - the in-memory de-dupe set is
    updated as each row is inserted, not just seeded once from the DB.

    Raises on any error - callers (scheduler.py) must catch this and never
    let it block/delay the email. Returns the number of rows inserted.
    """
    if not items:
        return 0
    with _cursor(commit=True) as cur:
        cur.execute(
            "SELECT description FROM growth_agent_tracked_items WHERE status = 'pending'"
        )
        already_pending = {row[0] for row in cur.fetchall()}

        inserted = 0
        for item in items:
            category = item.get('category')
            description = (item.get('description') or '').strip()
            if category not in VALID_CATEGORIES or not description:
                LOGGER.warning('Skipping malformed trackable item: %r', item)
                continue
            if description in already_pending:
                continue
            cur.execute(
                """
                INSERT INTO growth_agent_tracked_items (brief_date, category, description)
                VALUES (%s, %s, %s)
                """,
                (brief_date, category, description),
            )
            already_pending.add(description)
            inserted += 1
        return inserted


def get_pending_items(older_than_days: int = 0, limit: int = 10) -> List[Dict[str, Any]]:
    """Returns up to `limit` pending items first recommended at least
    `older_than_days` days ago, oldest first. older_than_days=0 (the CLI's
    "show me everything" case) applies no age filter.

    Raises on any error; metrics.py must catch and record under
    data_gaps, same as ga4.py/search_console.py/reddit_discovery.py.
    """
    cutoff = date.today() - timedelta(days=older_than_days)
    with _cursor() as cur:
        cur.execute(
            """
            SELECT id, brief_date, category, description
            FROM growth_agent_tracked_items
            WHERE status = 'pending' AND brief_date <= %s
            ORDER BY brief_date ASC
            LIMIT %s
            """,
            (cutoff, limit),
        )
        return [
            {'id': r[0], 'brief_date': r[1].isoformat(), 'category': r[2], 'description': r[3]}
            for r in cur.fetchall()
        ]


def get_resolved_items_for_attribution(
    min_days_since_resolved: int = 7,
    max_days_since_resolved: int = 14,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Returns up to `limit` 'done' items whose resolved_at is between
    max_days_since_resolved and min_days_since_resolved days ago - old
    enough that a full 'after' window has elapsed, not so old that the
    same item keeps reappearing in every brief for weeks. Ages out on its
    own; no "already reported" column/write path needed. Callers
    (metrics.py) size this window per their own cadence - see
    ATTRIBUTION_MAX_DAYS_SINCE_RESOLVED vs.
    ATTRIBUTION_DAILY_MAX_DAYS_SINCE_RESOLVED in metrics.py.

    Only 'done' items - 'skipped' ones had nothing executed, so a
    before/after delta wouldn't be meaningful (see metrics.py).

    Window boundaries are computed in SQL via now() - make_interval(), not
    in Python via date.today()/datetime.now(), to stay consistent with how
    resolved_at itself is set (mark_item's `resolved_at = now()`) and
    avoid app-server/DB clock skew.

    Known limitation, not fixed here: resolved_at is when a human ran
    mark_action.py, not necessarily when the action took effect in the
    world - if it's marked done days after actually shipping, the
    before/after windows shift by that much. Same category as the
    exact-string-match de-dupe limitation on record_new_items().

    Raises on any error; metrics.py must catch and record under
    data_gaps, same as get_pending_items.
    """
    with _cursor() as cur:
        cur.execute(
            """
            SELECT id, category, description, outcome_note, resolved_at
            FROM growth_agent_tracked_items
            WHERE status = 'done'
              AND resolved_at IS NOT NULL
              AND resolved_at <= now() - make_interval(days => %s)
              AND resolved_at >  now() - make_interval(days => %s)
            ORDER BY resolved_at ASC
            LIMIT %s
            """,
            (min_days_since_resolved, max_days_since_resolved, limit),
        )
        return [
            {
                'id': r[0],
                'category': r[1],
                'description': r[2],
                'outcome_note': r[3],
                'resolved_at': r[4].isoformat(),
            }
            for r in cur.fetchall()
        ]


def get_weekly_tracking_summary() -> Dict[str, Any]:
    """Counts, not rows, for the weekly report: items created in the last
    7 days (by category), items resolved in the last 7 days (by status),
    and the current pending total (an all-time snapshot, not week-scoped -
    "how much is backed up right now" rather than "how much became pending
    this week").

    Raises on any error; weekly_report.py must catch and record under
    data_gaps, same as get_pending_items.
    """
    with _cursor() as cur:
        cur.execute(
            """
            SELECT category, COUNT(*)
            FROM growth_agent_tracked_items
            WHERE created_at >= now() - interval '7 days'
            GROUP BY category
            """
        )
        created = {row[0]: row[1] for row in cur.fetchall()}

        cur.execute(
            """
            SELECT status, COUNT(*)
            FROM growth_agent_tracked_items
            WHERE resolved_at >= now() - interval '7 days'
            GROUP BY status
            """
        )
        resolved = {row[0]: row[1] for row in cur.fetchall()}

        cur.execute("SELECT COUNT(*) FROM growth_agent_tracked_items WHERE status = 'pending'")
        pending_total = cur.fetchone()[0]

        return {
            'created_last_7_days': {
                'action': created.get('action', 0),
                'experiment': created.get('experiment', 0),
            },
            'resolved_last_7_days': {
                'done': resolved.get('done', 0),
                'skipped': resolved.get('skipped', 0),
            },
            'pending_total': pending_total,
        }


def mark_item(item_id: int, status: str, outcome_note: Optional[str] = None) -> bool:
    """Marks a tracked item 'done' or 'skipped'. Returns False if no row
    with that id exists. Raises on any DB error - used directly by
    scripts/mark_action.py, a human-run one-off where a loud stack trace
    is the correct failure mode, unlike the daily pipeline.
    """
    if status not in ('done', 'skipped'):
        raise ValueError(f"status must be 'done' or 'skipped', got {status!r}")
    with _cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE growth_agent_tracked_items
            SET status = %s, outcome_note = %s, resolved_at = now()
            WHERE id = %s
            """,
            (status, outcome_note, item_id),
        )
        return cur.rowcount > 0


def get_excluded_lead_emails() -> set:
    """Emails already marked 'contacted' or 'skipped' in
    growth_agent_lead_outreach - 'pending' emails are NOT excluded
    (known but not yet acted on, should keep showing in the brief).

    Raises on any error; metrics.py must catch and fall back to the
    UNFILTERED lead list (never hide all leads because filtering itself
    broke) - a different fallback than get_pending_items/
    get_resolved_items_for_attribution, whose callers fall back to null.
    """
    with _cursor() as cur:
        cur.execute(
            "SELECT email FROM growth_agent_lead_outreach WHERE status IN ('contacted', 'skipped')"
        )
        return {row[0] for row in cur.fetchall()}


def register_new_leads(emails: List[str]) -> int:
    """Inserts a new 'pending' row per email not already present (any
    status) - ON CONFLICT DO NOTHING, not an upsert, so an email already
    pending/contacted/skipped is never silently reset.

    Raises on any error - callers (scheduler.py) must catch this and never
    let it block/delay the email, same as record_new_items. Returns the
    number of rows actually inserted.
    """
    if not emails:
        return 0
    with _cursor(commit=True) as cur:
        inserted = 0
        for email in emails:
            cur.execute(
                "INSERT INTO growth_agent_lead_outreach (email) VALUES (%s) "
                "ON CONFLICT (email) DO NOTHING",
                (email,),
            )
            inserted += cur.rowcount
        return inserted


def get_pending_leads(limit: int = 50) -> List[Dict[str, Any]]:
    """For scripts/mark_lead.py's `list` command - pending lead emails,
    oldest-first. A convenience, not a hard requirement like
    get_pending_items is for action ids - the email itself is already
    visible directly in the brief.
    """
    with _cursor() as cur:
        cur.execute(
            """
            SELECT email, first_seen_at
            FROM growth_agent_lead_outreach
            WHERE status = 'pending'
            ORDER BY first_seen_at ASC
            LIMIT %s
            """,
            (limit,),
        )
        return [{'email': r[0], 'first_seen_at': r[1].isoformat()} for r in cur.fetchall()]


def mark_lead(email: str, status: str, outcome_note: Optional[str] = None) -> None:
    """Marks a lead 'contacted' or 'skipped'. UPSERTs, deliberately unlike
    mark_item's UPDATE-only: a lead's email is printed directly in the
    brief (unlike a tracked item's id, only visible via `list`), so Amir
    can reasonably mark one before that day's register_new_leads() has
    run (or if it silently failed - same post-send swallow-and-log
    pattern as the rest of the pipeline). Safe to upsert since `email` is
    a real UNIQUE DB constraint, unlike growth_agent_tracked_items'
    accepted-limitation string-matching de-dupe.

    Raises on any DB error - used directly by scripts/mark_lead.py, a
    human-run one-off where a loud stack trace is the correct failure mode.
    """
    if status not in ('contacted', 'skipped'):
        raise ValueError(f"status must be 'contacted' or 'skipped', got {status!r}")
    with _cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO growth_agent_lead_outreach (email, status, outcome_note, resolved_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (email) DO UPDATE
            SET status = EXCLUDED.status, outcome_note = EXCLUDED.outcome_note, resolved_at = now()
            """,
            (email, status, outcome_note),
        )
