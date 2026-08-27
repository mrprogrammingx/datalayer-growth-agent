"""Finds real, external Shopify-store prospects via the Apify REST API, for
the customer acquisition report's PROSPECTING section (see
acquisition_report.py) - the one section that report previously always had
to report as "no data source connected" (see git history), since DataLayer
had no enrichment/contact-database integration wired in.

Actor: igolaizola/shopify-store-finder (https://apify.com/igolaizola/shopify-store-finder)
- takes a niche `query` keyword (or empty for broad discovery) and returns
  live, public Shopify storefronts with business data (URL, contact emails,
  catalog size, theme, country, currency) scraped from each store's own
  public site - this is public business contact info a merchant already
  publishes on their own storefront, not personal user data, so (unlike
  lead_research's csv_tool_leads) it is NOT redacted before reaching the LLM
  prompt - the report's whole point is making it actionable for outreach.

Optional feature, same graceful-degradation contract as ga4.py/
search_console.py/reddit_discovery.py: every failure here (missing token,
HTTP error, timeout, malformed response) raises, and callers must catch it
and record data_gaps rather than block the report/email. An empty list is a
normal, valid outcome (the actor found nothing this run), not a data gap -
only a raised exception is.
"""
import logging
import os
from typing import Any, Dict, List, Optional

import requests

LOGGER = logging.getLogger(__name__)

# owner~name, not owner/name - Apify's REST API requires the tilde form
# when addressing an Actor by owner+name instead of its opaque ID (see
# https://docs.apify.com/api/v2/act-run-sync-get-dataset-items-post).
ACTOR_ID = 'igolaizola~shopify-store-finder'
APIFY_API_URL = f'https://api.apify.com/v2/actors/{ACTOR_ID}/run-sync-get-dataset-items'

# run-sync-get-dataset-items blocks for the actor's whole run and hard-caps
# at 300s server-side (returns 408 past that) - keep our own request timeout
# a little above that ceiling so we see the real 408 rather than a client-side
# timeout with no explanation.
REQUEST_TIMEOUT_SECONDS = 320

# A wide-but-bounded fetch, same "fetch wider than what's shown" shape as
# metrics.py's LEAD_RESEARCH_FETCH_LIMIT/DISPLAY_LIMIT split - gives the LLM
# real candidates to choose the best-fit 5 from, without an unbounded (and
# unbounded-cost) Apify run every day.
DEFAULT_MAX_ITEMS = 25


def fetch_shopify_prospects(
    query: Optional[str] = None, max_items: int = DEFAULT_MAX_ITEMS,
) -> List[Dict[str, Any]]:
    """Runs the Shopify Store Finder Actor and returns normalized prospect
    dicts. `query` is a niche/keyword filter (e.g. "organic skincare") -
    None or empty runs broad discovery, per the Actor's own documented
    behavior. Defaults to APIFY_PROSPECT_QUERY from the environment when not
    passed explicitly, so the niche can be tuned without a code change.

    Raises RuntimeError on a missing token or any request/response failure -
    callers must catch this and degrade gracefully (see module docstring).
    """
    api_token = os.environ.get('APIFY_API_TOKEN')
    if not api_token:
        raise RuntimeError('APIFY_API_TOKEN is not set')

    if query is None:
        query = os.environ.get('APIFY_PROSPECT_QUERY', '')

    response = requests.post(
        APIFY_API_URL,
        headers={
            'Authorization': f'Bearer {api_token}',
            'Content-Type': 'application/json',
        },
        json={'query': query, 'maxItems': max_items},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    if response.status_code >= 400:
        LOGGER.error('Apify prospecting run failed: %s %s', response.status_code, response.text)
        raise RuntimeError(f'Apify API error {response.status_code}: {response.text[:500]}')

    try:
        items = response.json()
    except ValueError as exc:
        raise RuntimeError(f'Apify response was not valid JSON: {exc}') from exc

    if not isinstance(items, list):
        raise RuntimeError(f'Apify response was not a list of dataset items (got {type(items).__name__})')

    return [_normalize_prospect(item) for item in items]


def _normalize_prospect(item: Dict[str, Any]) -> Dict[str, Any]:
    """Maps the Actor's raw output fields onto a small, stable shape - so a
    change in the Actor's own schema only needs a fix here, not in the LLM
    prompt or anywhere else this data flows.

    Deliberately keeps `email` (the store's own public contact address) and
    `website` - see module docstring for why this is NOT redacted the way
    lead_research's csv_tool_leads emails are. `product_count_range` is a
    lightweight, honest size proxy (min/max from the Actor, not a real
    employee/revenue count DataLayer has no way to know) - the LLM prompt is
    told explicitly what this is and is not evidence of.
    """
    theme = item.get('theme') or {}
    emails = item.get('emails') or []
    min_products = item.get('minProductCount')
    max_products = item.get('maxProductCount')

    return {
        'business': item.get('title') or item.get('shop'),
        'website': item.get('url') or item.get('myShopifyUrl'),
        'email': emails[0] if emails else None,
        'country': item.get('country'),
        'currency': item.get('currency'),
        'product_count_range': (
            f'{min_products}-{max_products}' if min_products is not None or max_products is not None else None
        ),
        'theme': theme.get('name'),
        'description': item.get('info'),
    }
