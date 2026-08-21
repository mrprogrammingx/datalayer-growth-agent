"""One daily APScheduler job that runs the full metrics -> LLM -> email pipeline."""
import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler

LOGGER = logging.getLogger(__name__)

_scheduler = None


def run_growth_brief_job():
    from .metrics import collect_metrics
    from .brief import generate_brief
    from .email_sender import send_brief_email

    LOGGER.info('Running growth brief job')
    metrics = collect_metrics()
    brief_markdown = generate_brief(metrics)
    send_brief_email(brief_markdown)
    LOGGER.info('Growth brief job complete')
    return brief_markdown


def start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    hour = int(os.environ.get('BRIEF_SEND_HOUR_UTC', '8'))

    _scheduler = BackgroundScheduler(timezone='UTC')
    _scheduler.add_job(
        run_growth_brief_job,
        trigger='cron',
        hour=hour,
        minute=0,
        id='daily_growth_brief',
        replace_existing=True,
    )
    _scheduler.start()
    LOGGER.info('Scheduler started: daily growth brief at %02d:00 UTC', hour)
    return _scheduler
