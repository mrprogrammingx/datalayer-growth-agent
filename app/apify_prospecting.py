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
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import requests

LOGGER = logging.getLogger(__name__)

# owner~name, not owner/name - Apify's REST API requires the tilde form
# when addressing an Actor by owner+name instead of its opaque ID (see
# https://docs.apify.com/api/v2/act-run-sync-get-dataset-items-post).
ACTOR_ID = 'igolaizola~shopify-store-finder'
APIFY_API_URL = f'https://api.apify.com/v2/actors/{ACTOR_ID}/run-sync-get-dataset-items'

# Up to 4 separate Apify accounts (APIFY_API_TOKEN plus _2/_3/_4), tried in
# random order each call, falling through to the next only when the one
# tried is specifically out of monthly budget - not on any other failure,
# so a real bug (bad actor input, network error) still surfaces immediately
# instead of being masked by 3 more silent retries. Each account's own free
# tier is a hard monthly USD cap (see /v2/users/me/limits) - once
# exhausted, Apify rejects every maxItems request with this same 400
# regardless of the value requested, since it pre-computes affordable
# charged results as 0 before starting the run.
BUDGET_EXHAUSTED_ERROR_TYPES = {'max-items-must-be-greater-than-zero'}
BUDGET_EXHAUSTED_MESSAGE_SNIPPETS = ('charged results must be greater than zero', 'usage limit', 'monthly usage')


def _api_tokens() -> List[str]:
    tokens = [os.environ.get('APIFY_API_TOKEN')]
    tokens += [os.environ.get(f'APIFY_API_TOKEN_{n}') for n in (2, 3, 4)]
    tokens = [t for t in tokens if t]
    if not tokens:
        raise RuntimeError('APIFY_API_TOKEN is not set')
    random.shuffle(tokens)
    return tokens


def _is_budget_exhausted(response: requests.Response) -> bool:
    """True only for Apify's specific "this account can't afford any
    charged results this run" rejection - never for an actor-input error,
    auth failure, or anything else, so those still raise immediately rather
    than silently burning through the other 3 tokens for no reason.
    """
    if response.status_code not in (400, 402):
        return False
    try:
        error = response.json().get('error', {})
    except ValueError:
        return False
    error_type = (error.get('type') or '').lower()
    message = (error.get('message') or '').lower()
    return (
        error_type in BUDGET_EXHAUSTED_ERROR_TYPES
        or any(snippet in message for snippet in BUDGET_EXHAUSTED_MESSAGE_SNIPPETS)
    )

# run-sync-get-dataset-items blocks for the actor's whole run and hard-caps
# at 300s server-side (returns 408 past that) - keep our own request timeout
# a little above that ceiling so we see the real 408 rather than a client-side
# timeout with no explanation.
REQUEST_TIMEOUT_SECONDS = 320

# A wide-but-bounded fetch, same "fetch wider than what's shown" shape as
# metrics.py's LEAD_RESEARCH_FETCH_LIMIT/DISPLAY_LIMIT split - gives the LLM
# real candidates to choose the best-fit 5 from, without an unbounded (and
# unbounded-cost) Apify run every day.
DEFAULT_MAX_ITEMS = 50

# Rotated when APIFY_PROSPECT_QUERY isn't pinned to a fixed value (see
# _todays_query) - a generic, SMB-relevant spread so consecutive daily runs
# search different slices of Shopify's storefronts instead of the same
# broad/unfiltered top results every time (which is what quickly exhausted
# PROSPECT_COOLDOWN_DAYS - see acquisition_report.py). Override with
# APIFY_PROSPECT_QUERIES (comma-separated) to use your own list.
DEFAULT_PROSPECT_QUERIES = [
    'organic skincare',
    'handmade jewelry',
    'pet supplies',
    'home decor',
    'fitness apparel',
    'specialty coffee',
    'eco-friendly products',
    'outdoor gear',
    'candles and home fragrance',
    'art prints and posters',
]


def _todays_query() -> str:
    """Deterministically picks one niche keyword per UTC calendar day - the
    same day always yields the same query (so a retry within the same run
    doesn't switch niches mid-way), and the full list is cycled through
    before repeating. Reads APIFY_PROSPECT_QUERIES (comma-separated) if set,
    else falls back to DEFAULT_PROSPECT_QUERIES.
    """
    raw = os.environ.get('APIFY_PROSPECT_QUERIES', '')
    queries = [q.strip() for q in raw.split(',') if q.strip()] or DEFAULT_PROSPECT_QUERIES
    today = datetime.now(timezone.utc).date()
    return queries[today.toordinal() % len(queries)]


def fetch_shopify_prospects(
    query: Optional[str] = None, max_items: int = DEFAULT_MAX_ITEMS,
) -> List[Dict[str, Any]]:
    """Runs the Shopify Store Finder Actor and returns normalized prospect
    dicts. `query` is a niche/keyword filter (e.g. "organic skincare") - if
    not passed explicitly, None picks it from the environment: a non-empty
    APIFY_PROSPECT_QUERY always wins if set (pins every run to that one
    niche); otherwise (unset, or set but empty - the .env.example default)
    rotates daily through APIFY_PROSPECT_QUERIES/DEFAULT_PROSPECT_QUERIES
    (see _todays_query) so consecutive runs search different slices instead
    of re-scraping the same top results every day.

    Tries each configured Apify token (APIFY_API_TOKEN, APIFY_API_TOKEN_2/3/4
    - see _api_tokens) in random order, falling through to the next one only
    when the one just tried is specifically out of monthly budget (see
    _is_budget_exhausted). Any other failure - bad actor input, auth error,
    network/timeout - raises immediately on that token rather than masking
    a real bug behind 3 more silent retries.

    Raises RuntimeError on a missing token, every token being budget-
    exhausted, or any other request/response failure - callers must catch
    this and degrade gracefully (see module docstring).
    """
    if query is None:
        query = os.environ.get('APIFY_PROSPECT_QUERY') or _todays_query()

    tokens = _api_tokens()
    last_budget_error = None

    for token in tokens:
        response = requests.post(
            APIFY_API_URL,
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
            },
            json={'query': query, 'maxItems': max_items},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

        if response.status_code >= 400:
            if _is_budget_exhausted(response) and len(tokens) > 1:
                LOGGER.warning(
                    'Apify token ...%s out of monthly budget, trying next token', token[-4:]
                )
                last_budget_error = RuntimeError(
                    f'Apify API error {response.status_code}: {response.text[:500]}'
                )
                continue
            LOGGER.error('Apify prospecting run failed: %s %s', response.status_code, response.text)
            raise RuntimeError(f'Apify API error {response.status_code}: {response.text[:500]}')

        try:
            items = response.json()
        except ValueError as exc:
            raise RuntimeError(f'Apify response was not valid JSON: {exc}') from exc

        if not isinstance(items, list):
            raise RuntimeError(f'Apify response was not a list of dataset items (got {type(items).__name__})')

        return [_normalize_prospect(item) for item in items]

    # Every configured token was tried and every one was budget-exhausted.
    raise last_budget_error


def normalize_prospect_domain(raw: Optional[str]) -> Optional[str]:
    """Pure. Reduces a storefront URL/host to a stable dedup key: the bare
    hostname, lowercased, with a leading "www.", any ":port", and a trailing
    "." stripped. Returns None for anything without a usable host (None,
    empty/whitespace-only, or a value urlsplit can't pull a netloc from), or
    for a value with no "." or embedded whitespace (not a real domain).

    Used both by the acquisition report's cooldown filter (see
    acquisition_report._select_prospects_to_surface) and by
    scripts/mark_prospect.py, so the exact string a report is built around
    is the exact key the CLI marks.
    """
    if not raw or not raw.strip():
        return None
    value = raw.strip()
    if '://' not in value:
        value = '//' + value
    host = urlsplit(value).netloc.lower()
    host = host.split(':', 1)[0]          # strip :port
    if host.startswith('www.'):
        host = host[len('www.'):]
    host = host.rstrip('.')
    if '.' not in host or any(c.isspace() for c in host):
        return None
    return host


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
        # The *.myshopify.com handle is immutable per store; a custom domain
        # can lapse and change, so this is the more stable dedup identity and
        # is preferred as the cooldown key (see normalize_prospect_domain).
        'myshopify_url': item.get('myShopifyUrl'),
        'email': emails[0] if emails else None,
        'country': item.get('country'),
        'currency': item.get('currency'),
        'product_count_range': (
            f'{min_products}-{max_products}' if min_products is not None or max_products is not None else None
        ),
        'theme': theme.get('name'),
        'description': item.get('info'),
    }
