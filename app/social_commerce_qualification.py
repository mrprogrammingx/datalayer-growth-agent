"""Calls OpenRouter once per run to judge which social_commerce candidates
(see app/social_commerce_prospecting.py) are genuine DataLayer prospects and
draft their outreach message - the automated counterpart to the manual
judgment calls made in the original social_commerce research (deciding a
164K-follower celebrity brand was too large, a "the store is closed" bio
meant dormant, etc.).

Mirrors app/acquisition_report.py's OpenRouter client/prompt pattern (same
MODEL/OPENROUTER_BASE_URL from app/brief.py, same TRAILING_JSON_BLOCK_RE
fenced-JSON extraction), but produces NO markdown report - there is no
social_commerce email (the user explicitly ruled that out), so the entire
model response is one fenced JSON array of qualification objects.

Every candidate handed to this module already passed the two hard gates
computable without an LLM (real resolvable email, non-myshopify.com domain -
see social_commerce_prospecting.fetch_social_commerce_candidates). This
module's job is the harder, judgment-based gates: real/active business,
genuine online-sales evidence, and SMB-sized rather than enterprise - using
only what was actually scraped (a social link existing, a category match, a
live website), never inventing evidence like "recently posted" that the
pipeline has no way to know (this pipeline never fetched Instagram/Facebook
feed content itself, only the spyur.am directory listing and the
candidate's own website).
"""
import json
import logging
import os
from typing import Any, Dict, List, Optional

from openai import OpenAI

from .brief import MODEL, OPENROUTER_BASE_URL, TRAILING_JSON_BLOCK_RE

LOGGER = logging.getLogger(__name__)

# The subset of RESEARCH_FIELDS the LLM is asked to produce - contact_name,
# instagram_url, facebook_url, country, other_contact, platform are already
# known from scraping (social_commerce_prospecting) and must never be
# re-guessed by the model; only these 6 genuinely require judgment.
_LLM_OUTPUT_FIELDS = ('sells', 'sales_evidence', 'activity_notes', 'fit_reason', 'personalization_note', 'lead_quality')

# Explicit bound, not left to the provider's own default - observed live to
# matter: a call with no max_tokens set defaulted to requesting 16384,
# tripping a 402 "requires more credits" error on an account that could
# afford 15977 (a gap of just 407 tokens). This module's whole response is
# a bounded JSON array (at most SOCIAL_COMMERCE_MAX_CANDIDATES_PER_RUN
# objects, each a handful of short fields plus one draft message) - 6000 is
# comfortably enough for a realistic qualified count (the prompt itself
# biases toward omitting weak candidates rather than including everyone),
# while keeping this call's worst-case cost predictable.
_MAX_OUTPUT_TOKENS = 6000

SYSTEM_PROMPT = """You are qualifying automated candidates for DataLayer (usedatalayer.com), a
small bootstrapped e-commerce analytics SaaS for small businesses, for its social_commerce
acquisition segment - small Armenian businesses that sell online but are NOT small Shopify
stores (a separate, already-covered segment).

==================================================
WHAT YOU ARE GIVEN
==================================================
A JSON array of candidates. Each already passed two hard gates in code before reaching you:
a real, resolvable email address (never fabricated) and a non-myshopify.com domain. Each has:
business, website (may be null), instagram_url (may be null), facebook_url (may be null),
email, other_contact (phone, may be null), country ("Armenia"), platform (mechanically
derived from which links exist - e.g. "Website & Instagram"), category_hint (the directory
category this business was listed under, e.g. "Flowers" - a HINT for what they sell, not
confirmed fact), domain (a pre-computed dedup key - copy it back verbatim, never alter it).

==================================================
CRITICAL LIMITATION - READ THIS FIRST
==================================================
You were NOT given any Instagram/Facebook feed content, post history, or follower counts -
only a business directory listing and (if reachable) the candidate's own website. You have
NO way to know if a business is actively posting, how many followers it has, or what its
recent activity looks like. NEVER claim to have "seen" recent posts, engagement, or activity
- ground every claim only in what's actually in the data (a social link existing, a category
match, a live website with a real contact page). When in doubt about a specific claim, either
omit it or phrase it as what's structurally implied (e.g. "lists an Instagram profile" is
fine; "actively posts daily" is not, since you cannot know that).

==================================================
QUALIFICATION GATES (all must pass)
==================================================
1. Real, plausibly active business - a real company page a real business listed itself
   under, not obviously defunct (if the business name or category_hint suggests something
   closed/discontinued, exclude it).
2. Genuine online-sales plausibility - having a live website and/or Instagram/Facebook
   presence alongside a real category (retail-shaped: flowers, jewelry, bakery, clothing,
   cosmetics, gifts) is sufficient signal here; you cannot verify an actual purchase path
   the way live browsing could, so ground sales_evidence honestly in what's structurally
   present (e.g. "has its own website" / "lists both Instagram and Facebook" / "listed under
   Flowers in the directory") rather than claiming to have confirmed a checkout flow you
   never saw.
3. Real email - already guaranteed structurally; never second-guess or invent a different
   one.
4. Plausibly small/medium-sized, not a large enterprise with its own analytics/BI function.
   A directory listing gives no follower/revenue signal, so default toward including a
   candidate unless something specific suggests otherwise (a name implying a large chain,
   multiple explicit branch locations, etc.).
5. NOT a Shopify store (already excluded structurally) and not obviously a large
   professional agency/wholesaler rather than a small retail-facing business.

Because you cannot verify activity/evidence with the confidence live browsing would give,
DEFAULT your lead_quality toward "Medium" or "Low" - reserve "High" only when multiple
independent signals align (e.g. both a live website AND an Instagram/Facebook presence,
plus a clearly retail-shaped category). When a candidate's data is too thin to judge
confidently either way, OMIT it entirely rather than guessing - a false negative just gets
re-evaluated next run; a false positive risks a human acting on a weak, ungrounded message.

==================================================
DATALAYER POSITIONING (for draft_message only)
==================================================
Use DataLayer's actual capabilities only - never invent features: bringing sales/business
data into one simple dashboard, revenue and sales trends, best-selling products, product
performance, customer analytics, repeat vs. one-time customers, RFM/customer segmentation,
reports, AI-powered questions/analysis. Lead with the business problem, not a feature list.
Every draft is for HUMAN REVIEW ONLY - this system never sends anything automatically.

==================================================
OUTPUT FORMAT
==================================================
Respond with ONLY one fenced ```json block containing a JSON array, nothing else - no prose
before or after. One object per candidate you judge a genuine fit (a SUBSET of the input -
omitting a candidate is how you say "not a fit"; do not include a rejected candidate with a
null/empty verdict). Each object:

{"domain": "<copied verbatim from input>",
 "sells": "<grounded description of what they likely sell>",
 "sales_evidence": "<grounded in what was actually scraped - see gate 2>",
 "activity_notes": "<grounded size/activity notes - see gate 4>",
 "fit_reason": "<why this business is a good fit for DataLayer>",
 "personalization_note": "<one specific, real, structurally-grounded observation usable for outreach>",
 "lead_quality": "High|Medium|Low",
 "draft_message": "Subject: ...\\n\\n<short personalized body>"}

Never include a field you cannot ground in the actual input data. Never include a candidate
whose domain does not appear in the input list.
"""


def _build_candidate_payload(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Only the fields the LLM should see/use - never business/email/phone
    beyond what's needed for judgment, and never anything already decided
    in code (times_surfaced, status, etc.) that would just be noise.
    """
    fields = ('domain', 'business', 'website', 'instagram_url', 'facebook_url',
              'email', 'other_contact', 'country', 'platform', 'category_hint')
    return [{field: c.get(field) for field in fields} for c in candidates]


def qualify_and_draft_candidates(candidates: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Calls OpenRouter once with `candidates` (each already gate-3/gate-5
    passed - see social_commerce_prospecting.fetch_social_commerce_candidates)
    and returns {domain: {sells, sales_evidence, activity_notes, fit_reason,
    personalization_note, lead_quality, draft_message}} for the subset the
    model judged a genuine fit.

    Defensive matching, same philosophy as
    acquisition_report._extract_prospect_drafts: an output object whose
    'domain' doesn't match a real input candidate's domain is dropped, never
    paired with the wrong row. A missing/malformed JSON block degrades to an
    empty dict rather than raising - the caller (app/scheduler.py) still has
    the full hard-gated candidate list to persist even if qualification
    produced nothing usable this run.

    Raises RuntimeError only for a missing OPENROUTER_API_KEY or an outright
    API error - callers must catch and degrade to "persist scraped
    candidates without qualification detail", same non-blocking contract as
    the rest of this app.
    """
    if not candidates:
        return {}

    api_key = os.environ.get('OPENROUTER_API_KEY')
    if not api_key:
        raise RuntimeError('OPENROUTER_API_KEY is not set')

    valid_domains = {c['domain'] for c in candidates if c.get('domain')}
    client = OpenAI(api_key=api_key, base_url=OPENROUTER_BASE_URL)

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': json.dumps(_build_candidate_payload(candidates), indent=2)},
        ],
        temperature=0.3,
        max_tokens=_MAX_OUTPUT_TOKENS,
    )

    raw = response.choices[0].message.content
    if not raw or not raw.strip():
        raise RuntimeError('OpenRouter returned an empty social commerce qualification response')

    matches = list(TRAILING_JSON_BLOCK_RE.finditer(raw))
    if not matches:
        LOGGER.warning('No fenced JSON block found in social commerce qualification response')
        return {}
    try:
        parsed = json.loads(matches[-1].group(1))
    except json.JSONDecodeError:
        LOGGER.warning('Social commerce qualification JSON block failed to parse')
        return {}
    if not isinstance(parsed, list):
        LOGGER.warning('Social commerce qualification JSON block was not a list')
        return {}

    qualified: Dict[str, Dict[str, Any]] = {}
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        domain = entry.get('domain')
        if not isinstance(domain, str) or domain not in valid_domains:
            LOGGER.warning('Skipping social commerce qualification entry with unmatched domain: %r', domain)
            continue
        qualified[domain] = {
            field: entry.get(field) for field in (*_LLM_OUTPUT_FIELDS, 'draft_message')
            if isinstance(entry.get(field), str) and entry.get(field).strip()
        }

    return qualified
