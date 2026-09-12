"""Three APScheduler jobs on one BackgroundScheduler instance: the daily
metrics -> LLM -> email growth brief, a daily metrics -> LLM -> email
customer acquisition report (see acquisition_report.py), and a weekly
deterministic rollup report (no LLM call - see weekly_report.py). All three
are additive - none replaces another, they're different content sent as
separate emails.
"""
import logging
import os
from datetime import date

from apscheduler.schedulers.background import BackgroundScheduler

LOGGER = logging.getLogger(__name__)

_scheduler = None


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
    _scheduler.start()
    LOGGER.info('Scheduler started: daily growth brief at %02d:00 UTC', hour)
    LOGGER.info('Scheduler started: daily customer acquisition report at %02d:30 UTC', acquisition_hour)
    LOGGER.info('Scheduler started: weekly growth report on %s at %02d:00 UTC', weekly_day, weekly_hour)
    return _scheduler
