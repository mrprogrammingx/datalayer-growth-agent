"""Builds the deterministic weekly growth report - no LLM call, unlike
brief.py. Deliberately a separate module rather than folded into brief.py,
whose purpose ("builds the LLM prompt... calls OpenAI") is explicitly
LLM-shaped.

Why no LLM here: this report is pure counts/aggregates over a 7-day window.
At DataLayer's real traffic volume, a narrated summary of numbers this
small risks the same overclaiming problem conversion attribution had to
solve carefully (see metrics.py's LOW_SIGNAL_TOTAL_THRESHOLD /
NO_CONTROL_GROUP_CAVEAT) - a deterministic formatter sidesteps that
entirely and avoids a recurring LLM cost for something that doesn't need
one.
"""
import logging
from datetime import date
from typing import Any, Dict, List

from .metrics import (
    NO_CONTROL_GROUP_CAVEAT,
    collect_weekly_datalayer_metrics,
    get_resolved_action_outcomes,
)
from .tracking import get_weekly_tracking_summary

LOGGER = logging.getLogger(__name__)


def collect_weekly_data() -> Dict[str, Any]:
    """DataLayer metrics are fail-loud (no meaningful fallback for a DB
    failure, same as collect_metrics()'s own DB block). Tracking summary
    and resolved-action outcomes each degrade gracefully into data_gaps,
    same graceful-degradation contract as collect_metrics() uses for
    everything depending on the optional tracking connection.
    """
    today = date.today()
    datalayer_metrics = collect_weekly_datalayer_metrics()

    data_gaps: List[str] = []

    tracking_summary = None
    try:
        tracking_summary = get_weekly_tracking_summary()
    except Exception as exc:
        LOGGER.exception('Weekly tracking summary failed')
        data_gaps.append(f'Weekly tracking summary unavailable this run: {exc}')

    resolved_action_outcomes = None
    try:
        resolved_action_outcomes = get_resolved_action_outcomes()
    except Exception as exc:
        LOGGER.exception('Resolved-action attribution failed')
        data_gaps.append(f'Resolved-action attribution unavailable this run: {exc}')

    return {
        'generated_at': today.isoformat(),
        'datalayer_metrics': datalayer_metrics,
        'tracking_summary': tracking_summary,
        'resolved_action_outcomes': resolved_action_outcomes,
        'data_gaps': data_gaps,
    }


def _format_outcome_line(item: Dict[str, Any]) -> str:
    """Mechanically executes the same required sentence pattern brief.py's
    SYSTEM_PROMPT asks the LLM to follow for "Past action outcomes" -
    deterministic code following a fixed template is at least as reliable
    as an LLM instructed to follow one, and needs no prompt at all.
    """
    note = item['outcome_note'] or 'no note'
    caveat = item['low_signal_note'] if item['low_signal'] else NO_CONTROL_GROUP_CAVEAT
    signups, uploads = item['signups'], item['uploads']
    return (
        f"- {item['description']} (marked done, {note}): {caveat} "
        f"Observed alongside this window: signups {signups['before_total']}->{signups['after_total']}, "
        f"uploads {uploads['before_total']}->{uploads['after_total']}."
    )


def render_weekly_report(data: Dict[str, Any]) -> str:
    lines = [
        'DATALAYER WEEKLY REPORT',
        f"Generated {data['generated_at']} (UTC)",
        '',
        'This is a deterministic weekly rollup - no LLM-generated analysis. See the '
        'daily Growth Brief email for the full narrated brief, SEO opportunities, '
        'Reddit drafts, and lead outreach drafts.',
        '',
        'DataLayer metrics (last 7 days vs. prior 7 days):',
    ]

    for label, key in [('Signups', 'signups'), ('Uploads', 'uploads'), ('CSV tool leads', 'csv_tool_leads')]:
        metric = data['datalayer_metrics'][key]
        lines.append(
            f"- {label}: {metric['last_7_days']['total']} "
            f"(prior 7 days: {metric['prior_7_days']['total']}, delta {metric['delta']:+d})"
        )
    lines.append(
        "Note: DataLayer's traffic volume is small - week-over-week counts vary "
        'naturally; no percentage change is shown here, since a small base would make '
        'one misleading.'
    )
    lines.append('')

    lines.append('Tracking summary:')
    tracking_summary = data['tracking_summary']
    if tracking_summary is None:
        gap = next((g for g in data['data_gaps'] if 'tracking summary' in g.lower()), 'reason unknown')
        lines.append(f'- Unavailable this week: {gap}')
    else:
        created = tracking_summary['created_last_7_days']
        resolved = tracking_summary['resolved_last_7_days']
        lines.append(
            f"- Items created this week: {created['action']} action, "
            f"{created['experiment']} experiment"
        )
        lines.append(
            f"- Items resolved this week: {resolved['done']} done, {resolved['skipped']} skipped"
        )
        lines.append(f"- Currently pending (all-time): {tracking_summary['pending_total']}")
    lines.append('')

    lines.append('Recent action outcomes (marked done 7-14 days ago):')
    outcomes = data['resolved_action_outcomes']
    if outcomes is None:
        gap = next((g for g in data['data_gaps'] if 'resolved-action' in g.lower()), 'reason unknown')
        lines.append(f'- Unavailable this week: {gap}')
    elif not outcomes:
        lines.append('- No actions in the 7-14-day attribution window this week.')
    else:
        lines.extend(_format_outcome_line(item) for item in outcomes)

    return '\n'.join(lines)
