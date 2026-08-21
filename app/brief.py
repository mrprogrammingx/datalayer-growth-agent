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
    "Note: this brief is based on DataLayer's own signup/upload/lead data only. "
    "Website traffic and per-tool visit funnels (GA4) are not yet wired in - add in Phase 1."
)

SYSTEM_PROMPT = f"""You are the growth analyst for DataLayer (usedatalayer.com), a small
bootstrapped e-commerce analytics SaaS for small businesses. The team is very small with
limited time and money. DataLayer already has ~20 free tools (CSV Cleaner, Shopify CSV
Cleaner, Customer Segmentation, etc.) - the bottleneck is distribution, discoverability,
and conversion, NOT a lack of features. Do not recommend building new tools/features.

You will be given a JSON object of DataLayer's own product metrics (signups, uploads,
free-tool lead captures, plan tier mix) covering the last 30 days vs. the prior 30 days.

CRITICAL RULES:
- Use ONLY the numbers in the provided metrics JSON. Never invent, estimate, or assume
  any traffic, funnel, visitor, or conversion-rate number that is not explicitly present
  in the JSON.
- The JSON has no website traffic or per-tool usage data (that's GA4, not yet available).
  Do not fabricate funnel or traffic figures - if relevant, say the data isn't available yet.
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
