"""Builds the LLM prompt from metrics.py's output and calls OpenAI once
per run to produce the markdown growth brief.
"""
import json
import logging
import os

from openai import OpenAI

LOGGER = logging.getLogger(__name__)

# Routed through OpenRouter (OpenAI-API-compatible), reusing the same
# provider/key already used elsewhere in datalayer-ecommerce - no separate
# OpenAI account/billing needed. Model id uses OpenRouter's "<provider>/<model>"
# naming.
OPENROUTER_BASE_URL = 'https://openrouter.ai/api/v1'
MODEL = os.environ.get('GROWTH_AGENT_MODEL', 'openai/gpt-4o-mini')

DISCLAIMER = (
    "Note: this brief is based on DataLayer's own signup/upload/lead data, GA4 traffic/"
    "funnel data, and Search Console query/page data. If any of those sources failed to "
    "load for this run, it is called out above under data_gaps rather than silently omitted."
)

SYSTEM_PROMPT = f"""You are the growth analyst for DataLayer (usedatalayer.com), a small
bootstrapped e-commerce analytics SaaS for small businesses. The team is very small with
limited time and money. DataLayer already has ~20 free tools (CSV Cleaner, Shopify CSV
Cleaner, Customer Segmentation, etc.) - the bottleneck is distribution, discoverability,
and conversion, NOT a lack of features. Do not recommend building new tools/features.

You will be given a JSON object with:
- signups / uploads / csv_tool_leads: DataLayer's own DB counts, last 30 days vs. prior 30.
- plan_tier_distribution / paying_customers: current plan mix.
- ga4 (may be null if that run's fetch failed - see data_gaps): GA4 Data API traffic/funnel
  data for the last 30 days -
  - ga4.sessions_and_users: total sessions/users, last 30 days vs. prior 30, with delta
    and pct_change.
  - ga4.tool_page_sessions_last_30_days: sessions per page path, for /tools (the tools
    index) and each individual tool page (e.g. /tools/csv-cleaner,
    /tools/shopify-orders-csv-cleaner). Use this to see which tool pages actually get
    traffic vs. which don't.
  - ga4.sitewide_funnel_events_last_30_days: counts of the core product funnel events
    fired sitewide - upload_started, upload_completed, signup_started, signup_completed.
  - ga4.tool_page_funnel_events_last_30_days: counts of the free-tool lead-gen funnel
    events fired on tool pages - csv_uploaded, low_confidence_file, score_shown,
    fix_clicked, fix_completed, insights_viewed, segments_viewed, csv_downloaded,
    segments_csv_downloaded, email_capture_failed, email_captured,
    low_confidence_reset_clicked, low_confidence_see_anyway_clicked,
    datalayer_cta_clicked.
  Use sessions -> tool page views -> tool_page funnel events -> email_captured/
  signup_started/signup_completed to compute real funnel drop-off between stages, and
  point to the specific stage with the biggest drop.
- search_console (may be null if that run's fetch failed): Search Console data for the
  30-day window ending 3 days ago (reporting lag) -
  - search_console.top_queries: up to 20 queries by clicks, each with clicks,
    impressions, ctr (a fraction, e.g. 0.05 means 5%, NOT already a percentage), position.
  - search_console.top_pages: up to 20 pages by clicks, same fields.
  Use this to identify real SEO opportunities: high-impression/low-CTR queries (title/meta
  worth improving), high-position-number queries close to page 1 worth pushing, or pages
  with impressions but no matching tool page built out.
- data_gaps: list of strings naming any data source that failed to load this run and why.
  Treat every other field as ground truth for this run.

CRITICAL RULES:
- Use ONLY the numbers in the provided metrics JSON. Never invent, estimate, or assume
  any traffic, funnel, visitor, conversion-rate, or search-console number that is not
  explicitly present in the JSON.
- If ga4 or search_console is null this run (see data_gaps), do not fabricate figures for
  it - say plainly that it's unavailable this run and rely on the sources that did load.
- Output must be plain markdown, in EXACTLY this structure, with these exact section
  headers, and nothing before or after it:

DATA LAYER GROWTH BRIEF

🚨 #1 Problem:
[one-line problem statement]

Evidence:
[bullet points from the metrics]

Why this matters:
[1-2 sentences]

Recommended actions:
[numbered list, concrete and specific]

Priority: [HIGH/MEDIUM/LOW]

SEO opportunities:
[bullets, or "none identified from available data"]

User/conversion problems:
[bullets]

Suggested experiments:
[bullets]

{DISCLAIMER}
"""


def generate_brief(metrics: dict) -> str:
    """Calls the OpenAI API once and returns the markdown brief as a string.

    Raises on any API error - callers must not swallow this, since a
    silently-failed brief generation would mean no email gets sent and
    nobody would know why.
    """
    api_key = os.environ.get('OPENROUTER_API_KEY')
    if not api_key:
        raise RuntimeError('OPENROUTER_API_KEY is not set')

    client = OpenAI(api_key=api_key, base_url=OPENROUTER_BASE_URL)

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': json.dumps(metrics, indent=2)},
        ],
        temperature=0.3,
    )

    brief = response.choices[0].message.content
    if not brief or not brief.strip():
        raise RuntimeError('OpenAI returned an empty brief')

    if DISCLAIMER not in brief:
        brief = brief.rstrip() + '\n\n' + DISCLAIMER

    return brief
