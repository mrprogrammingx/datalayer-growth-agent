"""Four APScheduler jobs on one BackgroundScheduler instance: the daily
metrics -> LLM -> email growth brief, a daily metrics -> LLM -> email
customer acquisition report for the shopify_smb segment (see
acquisition_report.py), a weekly deterministic rollup report (no LLM call -
see weekly_report.py), and a daily silent discovery pipeline for the
social_commerce segment (see social_commerce_prospecting.py /
social_commerce_qualification.py) - unlike the other three, this one sends
no email; it only persists to growth_agent_prospects, reviewed via
scripts/mark_prospect.py list --segment social_commerce. All four are
additive - none replaces another.
"""
import logging
import os
from datetime import date

from apscheduler.schedulers.background import BackgroundScheduler

LOGGER = logging.getLogger(__name__)

_scheduler = None

# Own constant, deliberately not reusing acquisition_report.py's
# PROSPECT_COOLDOWN_DAYS - tracking.py already documents the two segments'
# cooldowns as independent (get_excluded_prospect_domains is segment-scoped).
SOCIAL_COMMERCE_COOLDOWN_DAYS = int(os.environ.get('SOCIAL_COMMERCE_COOLDOWN_DAYS', '30'))


def run_growth_brief_job():
    from .metrics import collect_metrics
    from .brief import generate_brief
    from .email_sender import send_brief_email
    from .tracking import record_new_items, register_new_leads

    LOGGER.info('Running growth brief job')
    metrics = collect_metrics()
    brief_markdown, trackable_items = generate_brief(metrics)

    send_brief_email(brief_markdown)
    LOGGER.info('Growth brief job complete')

    try:
        record_new_items(date.fromisoformat(metrics['generated_at']), trackable_items)
    except Exception:
        # ALERT-WORTHY: greppable prefix, since this failure has NO other
        # signal - it runs after data_gaps is already computed (see
        # metrics.py), so a misconfigured (not just unset)
        # GROWTH_AGENT_TRACKING_DATABASE_URL fails here silently and
        # permanently with only this log line as evidence. See README's
        # "Known limitation" note on this gap.
        LOGGER.exception(
            'TRACKING WRITE FAILED: could not persist tracking rows (email already sent - not blocked)'
        )

    try:
        register_new_leads([lead['email'] for lead in metrics['lead_research']])
    except Exception:
        LOGGER.exception(
            'TRACKING WRITE FAILED: could not register new lead-outreach rows '
            '(email already sent - not blocked)'
        )

    return brief_markdown


def run_acquisition_report_job():
    from .acquisition_report import collect_acquisition_data, generate_acquisition_report
    from .email_sender import send_brief_email
    from .tracking import mark_prospects_surfaced, record_new_items, register_new_leads

    LOGGER.info('Running customer acquisition report job')
    metrics = collect_acquisition_data()
    report_markdown, trackable_items = generate_acquisition_report(metrics)

    send_brief_email(report_markdown, subject='DataLayer Customer Acquisition Report')
    LOGGER.info('Customer acquisition report job complete')

    try:
        record_new_items(date.fromisoformat(metrics['generated_at']), trackable_items)
    except Exception:
        # Same tracked_items table the growth brief writes to (see
        # acquisition_report.py's module docstring) - a write failure here
        # has the same "no other signal" property run_growth_brief_job's
        # own try/except calls out.
        LOGGER.exception(
            'TRACKING WRITE FAILED: could not persist tracking rows (email already sent - not blocked)'
        )

    try:
        # lead_research emails only - prospects are tracked in their own
        # growth_agent_prospects table now (see mark_prospects_surfaced
        # below), not folded into growth_agent_lead_outreach.
        register_new_leads([lead['email'] for lead in metrics['lead_research']])
    except Exception:
        LOGGER.exception(
            'TRACKING WRITE FAILED: could not register new lead-outreach rows '
            '(email already sent - not blocked)'
        )

    try:
        # metrics['prospects'] is already the exact set the report was built
        # from - collect_acquisition_data() selects it deterministically, so
        # there is nothing to parse back out of the LLM output.
        mark_prospects_surfaced(metrics.get('prospects') or [])
    except Exception:
        LOGGER.exception(
            'TRACKING WRITE FAILED: could not bump surfaced-prospect rows '
            '(email already sent - not blocked)'
        )

    return report_markdown


def run_social_commerce_discovery_job():
    """Silent discovery pipeline for the social_commerce segment - no email
    is sent (unlike the other three jobs on this scheduler); it only
    discovers, qualifies, and persists to growth_agent_prospects. Reviewed
    via `scripts/mark_prospect.py list --segment social_commerce`, not a
    daily inbox item.

    Every stage is independently wrapped: a discovery failure means nothing
    to qualify or persist this run (logged, not raised further - there's no
    already-sent email this could ever "block"), a cooldown-lookup failure
    falls back to the unfiltered candidate list (same fallback shape as
    acquisition_report.py's own prospect-exclusion lookup), and a
    qualification failure just means this run's candidates get persisted
    without research-field/draft_message detail rather than not persisted
    at all - each hard-gated candidate should still start its cooldown so
    it isn't re-scraped and re-billed tomorrow regardless of whether the
    LLM step succeeded.

    Returns a small summary dict (eligible/qualified/persisted counts) -
    logged at the end either way, since this job has no email to carry that
    signal to a human instead.
    """
    from .social_commerce_prospecting import fetch_social_commerce_candidates
    from .social_commerce_qualification import qualify_and_draft_candidates
    from .tracking import get_excluded_prospect_domains, mark_prospects_surfaced

    LOGGER.info('Running social commerce discovery job')

    candidates = []
    try:
        candidates = fetch_social_commerce_candidates()
    except Exception:
        LOGGER.exception('Social commerce discovery failed - nothing to persist this run')

    if candidates:
        try:
            excluded = get_excluded_prospect_domains(SOCIAL_COMMERCE_COOLDOWN_DAYS, segment='social_commerce')
            candidates = [c for c in candidates if c.get('domain') not in excluded]
        except Exception:
            LOGGER.exception(
                'Social commerce cooldown lookup failed - continuing with the unfiltered '
                'candidate list (may re-surface an already-contacted/skipped/recent prospect)'
            )

    qualified_by_domain = {}
    if candidates:
        try:
            qualified_by_domain = qualify_and_draft_candidates(candidates)
        except Exception:
            LOGGER.exception(
                'Social commerce LLM qualification failed - persisting scraped candidates '
                'without qualification detail'
            )
        for candidate in candidates:
            candidate.update(qualified_by_domain.get(candidate.get('domain'), {}))

    persisted = 0
    try:
        persisted = mark_prospects_surfaced(candidates, segment='social_commerce')
    except Exception:
        LOGGER.exception(
            'TRACKING WRITE FAILED: could not persist social commerce prospects '
            '(no email was sent this run - nothing else to protect)'
        )

    summary = {'eligible': len(candidates), 'qualified': len(qualified_by_domain), 'persisted': persisted}
    LOGGER.info(
        'Social commerce discovery complete: eligible %d, LLM-qualified %d, persisted %d',
        summary['eligible'], summary['qualified'], summary['persisted'],
    )
    return summary


def run_weekly_report_job():
    from .weekly_report import collect_weekly_data, render_weekly_report
    from .email_sender import send_brief_email

    LOGGER.info('Running weekly growth report job')
    report_text = render_weekly_report(collect_weekly_data())
    send_brief_email(report_text, subject='DataLayer Weekly Report')
    LOGGER.info('Weekly growth report job complete')
    return report_text


def start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    hour = int(os.environ.get('BRIEF_SEND_HOUR_UTC', '8'))
    acquisition_hour = int(os.environ.get('ACQUISITION_REPORT_SEND_HOUR_UTC', '8'))
    weekly_day = os.environ.get('WEEKLY_REPORT_SEND_DAY_UTC', 'mon')
    weekly_hour = int(os.environ.get('WEEKLY_REPORT_SEND_HOUR_UTC', '9'))
    # Default 14 -> 14:30 UTC = 18:30 Yerevan time (Armenia is fixed UTC+4,
    # no DST observed).
    social_commerce_hour = int(os.environ.get('SOCIAL_COMMERCE_DISCOVERY_HOUR_UTC', '14'))

    _scheduler = BackgroundScheduler(timezone='UTC')
    _scheduler.add_job(
        run_growth_brief_job,
        trigger='cron',
        hour=hour,
        minute=0,
        id='daily_growth_brief',
        replace_existing=True,
    )
    _scheduler.add_job(
        run_acquisition_report_job,
        trigger='cron',
        hour=acquisition_hour,
        # :30, not :00 - avoids firing at the exact same instant as the
        # growth brief job (default same hour), so the two OpenRouter calls
        # don't race each other for no reason.
        minute=30,
        id='daily_acquisition_report',
        replace_existing=True,
    )
    _scheduler.add_job(
        run_weekly_report_job,
        trigger='cron',
        day_of_week=weekly_day,
        hour=weekly_hour,
        minute=0,
        id='weekly_growth_report',
        replace_existing=True,
    )
    _scheduler.add_job(
        run_social_commerce_discovery_job,
        trigger='cron',
        hour=social_commerce_hour,
        minute=30,
        id='social_commerce_discovery',
        replace_existing=True,
    )
    _scheduler.start()
    LOGGER.info('Scheduler started: daily growth brief at %02d:00 UTC', hour)
    LOGGER.info('Scheduler started: daily customer acquisition report at %02d:30 UTC', acquisition_hour)
    LOGGER.info('Scheduler started: weekly growth report on %s at %02d:00 UTC', weekly_day, weekly_hour)
    LOGGER.info('Scheduler started: social commerce discovery at %02d:30 UTC', social_commerce_hour)
    return _scheduler
