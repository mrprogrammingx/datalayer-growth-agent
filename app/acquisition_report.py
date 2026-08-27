"""Builds the LLM prompt for the daily CUSTOMER ACQUISITION REPORT and calls
OpenAI once per run to produce it - a second, separate email from the
Growth Brief (brief.py), not a replacement for it.

Why a separate module/report rather than a new section in brief.py: the
Growth Brief's job is "analyze all the metrics and find the biggest
bottleneck" (traffic, SEO, GEO, content, funnel scoring - see brief.py).
This report has a narrower, harder-nosed mission: DataLayer has 0 external
paying customers, so every single day this report's only question is "what
gets us closer to the first 10 real ones, today." It deliberately drops
AT A GLANCE, ICE-scored Quick Wins, SEO/GEO opportunities, and Content
Opportunities - those stay exclusively in the Growth Brief - so the two
emails don't say the same thing twice in different words.

Reuses brief.py's OpenRouter client config and its trailing-JSON-block
extraction/cap logic verbatim (same tracking-item contract, same
growth_agent_tracked_items table via tracking.py) rather than duplicating
either - the two reports share one tracking table on purpose, so an action
recommended in one won't silently also get recommended by the other via
pending_from_prior_briefs.

Deliberately does NOT fold Apify prospecting into metrics.collect_metrics()
(the function brief.py's Growth Brief also calls) - the Growth Brief has no
use for cold prospects, and collect_metrics() being shared would mean the
(billed) Apify Actor run fires once per report per day instead of once.
collect_acquisition_data() below calls collect_metrics() for everything this
report shares with the Growth Brief (leads, Reddit, tracking, at_a_glance),
then separately, optionally layers prospects on top.
"""
import json
import logging
import os
import re
from typing import Any, Dict, Optional

from openai import OpenAI

from .apify_prospecting import fetch_shopify_prospects
from .brief import MODEL, OPENROUTER_BASE_URL, _extract_trackable_items
from .metrics import collect_metrics
from .tracking import get_excluded_lead_emails

LOGGER = logging.getLogger(__name__)


def collect_acquisition_data() -> Dict[str, Any]:
    """collect_metrics() (shared with the Growth Brief) plus this report's
    own prospects fetch, merged into one dict for generate_acquisition_report().

    Prospecting is optional and degrades gracefully into data_gaps, same
    philosophy as ga4/search_console/reddit_discussions inside
    collect_metrics() itself - a failed or unconfigured Apify integration
    must not block this email, since the rest of the report (leads,
    community, tracking) is still valuable on its own.

    Reuses the SAME growth_agent_lead_outreach exclusion/registration
    tracking.py already provides for lead_research (see scheduler.py's
    register_new_leads call after this report sends) rather than building a
    second, parallel dedup table - a prospect marked 'contacted' or
    'skipped' via scripts/mark_lead.py stops resurfacing here too, exactly
    like an exhausted lead does.
    """
    metrics = collect_metrics()
    data_gaps = list(metrics['data_gaps'])

    prospects = None
    try:
        prospects = fetch_shopify_prospects()
    except Exception as exc:
        LOGGER.exception('Apify prospecting fetch failed')
        data_gaps.append(f'Prospecting data unavailable this run: {exc}')

    if prospects is not None:
        try:
            excluded = get_excluded_lead_emails()
            prospects = [p for p in prospects if not (p['email'] and p['email'] in excluded)]
        except Exception as exc:
            # Same fallback shape as metrics.py's own lead_research exclusion
            # step: a broken filter must never hide prospects that were
            # actually fetched successfully - fall back to the unfiltered
            # list rather than dropping it.
            LOGGER.exception('Prospect-exclusion lookup failed')
            data_gaps.append(
                f'Prospect filtering unavailable this run - showing prospects unfiltered '
                f'(may include an already-contacted/skipped one): {exc}'
            )

    return {**metrics, 'prospects': prospects, 'data_gaps': data_gaps}


# Shorter than brief.py's 500-800 word target - this report is a punch list,
# not a narrated analysis, so it should be even faster to read on a phone.
WORD_COUNT_WARN_THRESHOLD = 700

SYSTEM_PROMPT = """You are the Customer Acquisition Agent for DataLayer (usedatalayer.com), a
small bootstrapped e-commerce analytics SaaS for small businesses.

==================================================
MISSION - READ THIS FIRST
==================================================
DataLayer currently has ZERO external paying customers. Until that changes, the ONLY
objective of this report is: get DataLayer's first 10 real, external, paying customers.
Every recommendation in this report must trace back to that - a qualified conversation, a
trial/signup from someone who looks like a real buyer, or a step that moves an existing
lead closer to paying. Revenue and qualified customer conversations outrank vanity metrics
(impressions, followers, traffic, likes, post count) every time. Do not prioritize SEO
simply because traffic is low - low traffic is very often not the real bottleneck for a
business this early, and this report is not the place for SEO/content-calendar work
anyway (that lives in the daily Growth Brief email). If SEO ever earns a place here, it
must be because a specific query/page is tied to a live buying signal, not because a
number looked low.

Write like a sharp, calm, commercial teammate sending a short action list, not a
consultant. No corporate language ("leverage", "synergy", "it is recommended that"). Never
invent facts, users, prospects, leads, conversations, or results - if the data isn't there,
say so plainly instead of filling the space.

==================================================
DATA YOU ARE GIVEN
==================================================
This is a subset of the same metrics JSON the Growth Brief uses - see that report for
full traffic/SEO/GA4 analysis, which is intentionally out of scope here. Fields relevant
to this report:
- at_a_glance.product / .commercial: signups, uploads, activated, free_users,
  paid_customers - all-time or 30-day snapshots, EXTERNAL users only, already excludes
  internal/founder/team accounts. Use these ONLY to frame where DataLayer sits on the
  ladder (0 paying customers -> first paying customer -> repeatable) - never as a source
  of named individuals, and NEVER as a stand-in for lead_research. A signed-up free_user
  is a DIFFERENT population from a lead_research candidate (someone who gave an email via
  a free tool but never signed up) - do not recommend "follow up with leads/prospects"
  and cite a free_users or signups count as the evidence; that count has no named
  individual behind it to follow up with. If lead_research is empty, there is no
  individual to name here - say so, don't substitute an aggregate count instead.
- lead_research: up to 10 most-recent people who captured a free-tool lead (gave an
  email) but never signed up for a DataLayer account. Each has email, score, row_count,
  file_name, created_at. `score` is a data-QUALITY score of their uploaded file, NOT a
  buying-intent score - judge intent instead from row_count (a real, sizeable dataset
  suggests a real business), file_name, and recency. This is currently DataLayer's ONLY
  named-individual, external-intent data source - there is no per-user list of who
  specifically signed up or activated, only the aggregate counts above. NEVER print a
  lead's actual email address anywhere in the visible report - refer to them generically
  (e.g. "one user who uploaded a Shopify order file").
- reddit_discussions: up to 10 recent Reddit posts from a curated list of subreddits
  (r/ecommerce, r/shopify, r/SaaS, r/Entrepreneur, r/smallbusiness) matching curated
  keywords about CSV/order-data cleanup and customer segmentation. This is the ONLY
  connected community data source - there is no Facebook Groups or LinkedIn feed wired
  in; never invent one.
- pending_from_prior_briefs: action/experiment items recommended earlier (by either this
  report or the Growth Brief - they share one tracking table) that haven't been marked
  done/skipped yet. Reference these explicitly instead of silently repeating them.
- resolved_action_outcomes: before/after signup/upload counts for recently-resolved
  tracked items. Correlational, small-sample, NEVER causal language - if you mention one,
  label it a SIGNAL, not a fact, and never claim it caused anything.
- data_gaps: data sources that failed to load this run.
- prospects (may be `null` if the Apify fetch failed or is unconfigured this run - see
  data_gaps; an empty list `[]` means the fetch succeeded but found nothing this run,
  which is a normal outcome, NOT a data gap): up to ~25 real, live Shopify storefronts
  found via the Apify "Shopify Store Finder" Actor, already excluding anyone already
  marked contacted/skipped. Each has business, website, email (may be null - not every
  storefront publishes one), country, currency, product_count_range (a MIN-MAX string
  from the store's own catalog, e.g. "12-40" - a rough size proxy only, NOT a verified
  employee/revenue count), theme, description. UNLIKE lead_research, this data is public
  business contact info a merchant already publishes on their own storefront (not
  DataLayer's own captured user data) - it is fine to include a prospect's business email
  in this report, since the report's purpose is making outreach to it actionable. Judge
  genuine fit from product_count_range (a very large range suggests an established store
  that likely already has BI/ops tooling, not DataLayer's ICP), country/currency, and the
  description - never assume fit from theme alone. If prospects is null or empty, say so
  plainly rather than inventing one - see PROSPECTING's exact fallback lines below.

==================================================
CRITICAL RULES
==================================================
- Internal/founder/team accounts are never leads, customers, or growth signals - every
  field above already excludes them; never undo that.
- Every outreach/Reddit-reply draft you write is for HUMAN REVIEW ONLY. This system never
  sends or posts anything automatically. Say so once per draft area, not after every line.
- Never expose a LEAD's email address anywhere (lead_research, i.e. LEAD-BASED OUTREACH) -
  address them generically ("Hi there,"), never invent or guess a name. This does NOT
  apply to prospects (see the prospects field docs above) - a prospect's public storefront
  contact email may appear under PROSPECTING/its draft message, since making it usable for
  outreach is that section's whole purpose.
- Never fabricate a prospect, a partnership opportunity, or a community discussion that
  isn't actually in the data you were given.
- If a section has nothing real to put in it, OMIT the whole section (header included) -
  except LEAD-BASED OUTREACH and PROSPECTING, which always show, using the exact
  fallback lines specified below, so it's clear the check ran rather than was skipped.
  "Omit" means the header, the separator above it, and the body are ALL gone, as if that
  section were never in the template. NEVER keep a header and write a sentence explaining
  that you're omitting it (e.g. "No community opportunities this run", "Omit this section
  as there's nothing to propose") - that text must never appear anywhere in the output.
  Omitting means the section is not there at all, not that it announces its own absence.
- Do not invent a numerical daily target beyond what this run's data can actually support
  (e.g. don't say "contact 5 prospects" if only 1 lead exists this run).

==================================================
OUTPUT FORMAT
==================================================
Plain markdown in EXACTLY this structure, then one trailing fenced ```json block and
nothing else. Omit any section with nothing real to report, per the rule above (LEAD-BASED
OUTREACH and PROSPECTING are the two exceptions - always shown). Target roughly 300-500
words for the whole email, not counting the trailing JSON block.

━━━━━━━━━━━━━━━━━━━━

\U0001f3af DATALAYER CUSTOMER ACQUISITION REPORT

\U0001f4c5 [today's date, written out]
Mission: 0 -> first 10 real paying customers.

━━━━━━━━━━━━━━━━━━━━

✅ TODAY'S PRIORITIZED ACTIONS

[Up to 3 numbered actions for today, ranked in this priority order when more than one
candidate exists: (1) existing high-intent external users/leads, (2) relevant community
conversations, (3) direct outreach opportunities, (4) partnerships, (5) content/
distribution, (6) SEO. Each action must be concrete and name the real evidence behind it
(a specific lead, thread, or number) - never generic advice. Format each as:
"N. [Action]" then "   Why: [one line, evidence-based]"

Show FEWER than 3 if fewer than 3 categories above have a REAL candidate this run - never
pad the list to hit 3. In particular: if lead_research is empty, there is NO "existing
leads" action to write this run - do not write one anyway using at_a_glance.product/
.commercial counts (signups, free_users, activated) as a substitute; those are aggregate
counts with no named individual behind them, not a lead-outreach candidate. Skip straight
to the next priority category that DOES have a real candidate. The same applies to every
category here - an empty/null data source for that category means it's not eligible this
run, not a placeholder to fill with a nearby-sounding metric.]

━━━━━━━━━━━━━━━━━━━━

\U0001f464 LEAD-BASED OUTREACH

[ALWAYS shown, even when lead_research is empty - write exactly "No external leads to
follow up with this run." and nothing else if it's empty. Otherwise, up to 3 leads from
lead_research showing genuine intent. EACH lead is its own self-contained block with its
OWN draft message immediately inside it - repeat the full block (all 5 lines below) once
per lead, in order, before moving to the next numbered lead. Never write a single shared
draft message after the whole list - a block missing its own draft is incomplete:
\U0001f464 LEAD #N
What they did:
[...]
Why it matters:
[...]
Recommended next action:
[...]
\U0001f4e9 DRAFT MESSAGE (for human review - not sent automatically)
Subject: [...]

Hi there,

[Short, human message - no fabricated facts, no email address, no name]]

━━━━━━━━━━━━━━━━━━━━

\U0001f52d PROSPECTING

[ALWAYS shown. If prospects is null, write exactly: "Prospect data unavailable this run -
see ⚠️ WATCH." (the reason is already in data_gaps/WATCH, no need to repeat it here). If
prospects is an empty list, write exactly: "No prospects found this run." Otherwise, up to
5 of the best-fit prospects from the list, chosen using the judgment criteria in the
prospects field docs above - never just the first 5 in list order. EACH prospect is its
own self-contained block with its OWN draft message immediately inside it - repeat the
full block (all 6 lines below) once per prospect, in order. Never write a single shared
draft message after the whole list, and never let one prospect's draft end up attached to
a different prospect or a different section (e.g. LEAD-BASED OUTREACH) - a block missing
its own draft, or a draft under the wrong header, is incomplete/wrong:
\U0001f52d PROSPECT #N
Business: [business]
Website: [website]
Channel: [email, if present; otherwise "Website contact form"]
Why they may need DataLayer: [grounded in product_count_range/country/description - never
generic]
Evidence: [the actual field(s) behind that reasoning]
\U0001f4e9 DRAFT MESSAGE (for human review - not sent automatically)
Subject: [...]

[Short, human message referencing something real about their store (e.g. their product
range or a specific detail from `description`) - no fabricated facts, no invented name if
none is available, no claim that DataLayer has looked closely at their business beyond
what's actually in the data.]

Show fewer than 5 if fewer are genuinely good fits - never pad with a weak prospect to hit
5.]

━━━━━━━━━━━━━━━━━━━━

\U0001f4ac COMMUNITY OPPORTUNITIES

[Only if reddit_discussions has a genuinely relevant post this run:
### Reddit
[The real discussion + a helpful, non-promotional draft reply, explicitly labeled a DRAFT
for human review - most subreddits ban direct self-promotion, so this must not read like
an ad.]
If reddit_discussions is null, empty, or nothing in it is relevant, write NOTHING at all
under this header - no header, no separator, no sentence about it, exactly as if this
whole section had never been part of the template. Never invent a Facebook Groups or
LinkedIn opportunity - no data source exists for either.]

━━━━━━━━━━━━━━━━━━━━

\U0001f9ea CUSTOMER EXPERIMENT

[At most ONE, only if there's a real hypothesis worth testing today and it isn't already
covered by TODAY'S PRIORITIZED ACTIONS above. If included:
Hypothesis:
[...]
Target:
[...]
Action:
[...]
Expected outcome:
[...]
Primary metric:
[...]
Time required:
[prefer something doable within 1 day]
Omit this whole section (header included) if there's nothing worth testing today.]

━━━━━━━━━━━━━━━━━━━━

\U0001f3af CUSTOMER ACQUISITION TARGET

Today's target:
[1-3 concrete, realistic items derived ONLY from what this run's data actually supports -
e.g. "Follow up with the 1 lead above" or "Post the Reddit reply draft for review" or
"Have 1 customer conversation." Never invent a target the data can't support, and never
default to a generic number.]

━━━━━━━━━━━━━━━━━━━━

```json
[{"category": "action", "description": "..."}, {"category": "experiment", "description": "..."}]
```

The fenced json block above is REQUIRED, must be the very last thing in your response, and
is internal plumbing only - never mention it in the visible report. One object per action
shown under TODAY'S PRIORITIZED ACTIONS (category "action"), and one object for CUSTOMER
EXPERIMENT if you included one (category "experiment") - nothing else, no items for Lead
Outreach, Prospecting, or Community. If you cannot produce it for any reason, omit the
whole block rather than emitting malformed JSON.
"""


# The prompt asks for the heavy "━"x20 separator between every section, but
# a live run already showed the model substituting a markdown "---" rule in
# its place (observed live, right before a leaked COMMUNITY OPPORTUNITIES
# section - it also invented "---" as decoration between PROSPECTING items,
# harmlessly, since that's not a section boundary this code searches for).
# Matching either style at both boundaries (rather than the literal
# brief.py-style `_SEPARATOR` constant alone) is what makes the strip below
# survive that drift instead of silently no-op'ing.
_SECTION_BOUNDARY = r'(?:━{10,}|-{3,})[ \t]*'
COMMUNITY_HEADER = '\U0001f4ac COMMUNITY OPPORTUNITIES'
EXPERIMENT_HEADER = '\U0001f9ea CUSTOMER EXPERIMENT'
TARGET_HEADER = '\U0001f3af CUSTOMER ACQUISITION TARGET'

# A live run also dropped the boundary line between two sections entirely
# (no "━" line, no "---", just a bare blank line before the next header) -
# a lookahead anchored on the boundary alone then never matches, so the
# section (and any leak inside it) survives uncleaned. Accepting the next
# section's own known header text as an alternative "end of this section"
# signal - not just a decorative boundary line - makes the strip robust
# even when the model omits the boundary between sections altogether. This
# MUST be a zero-width lookahead (?=...), not a plain alternation group -
# consumed (non-lookahead) it would eat the next section's own header text
# as part of the match being deleted, which is exactly what happened the
# first time this was written without the (?=...) wrapper.
# CUSTOMER EXPERIMENT is always immediately followed by CUSTOMER
# ACQUISITION TARGET in the required OUTPUT FORMAT, so that header is
# included as a valid terminator even though this module never strips it.
_NEXT_SECTION = r'(?=\n\n(?:' + _SECTION_BOUNDARY + '|' + re.escape(EXPERIMENT_HEADER) + '|' + re.escape(TARGET_HEADER) + r')|\Z)'

# A POSITIVE check ("does this body actually start like real content?"),
# not a negative one ("does it match some known leak phrasing?") - a live
# run already produced two different leak sentences ("Omit this entire
# section (header included) as there are no relevant Reddit discussions
# this run." and, in a separate run, "(No community opportunities this
# run.)"), which is exactly the kind of open-ended wording variance a
# blocklist regex can't keep up with. Every legitimately populated body
# is required by the prompt's own OUTPUT FORMAT to start with one specific
# marker (Community: "### Reddit"; Experiment: "Hypothesis:") - anything
# that doesn't start that way is, by construction, not real content,
# whatever the model actually wrote instead.
_REQUIRED_BODY_PREFIX = {
    # Tolerates the model's occasional markdown-emphasis/heading-level
    # drift around the required keyword itself (e.g. "**Hypothesis:**",
    # "#### Reddit") without loosening so far that a leak sentence which
    # happens to mention either word in passing would slip through - the
    # keyword must still be the very first thing in the body.
    COMMUNITY_HEADER: re.compile(r'^\s*[#*_\s]*Reddit\b', re.IGNORECASE),
    EXPERIMENT_HEADER: re.compile(r'^\s*[#*_\s]*Hypothesis\s*[:*_]*', re.IGNORECASE),
}


def _get_section_body(markdown: str, header: str) -> Optional[str]:
    """Deliberately does NOT require _SECTION_BOUNDARY immediately before
    the header, unlike _NEXT_SECTION's lookahead - a live run showed the
    model omitting any boundary line at all before a header (not just
    substituting a different style), so requiring one on this side too
    would just move the same failure mode one section earlier instead of
    fixing it. The header's own text (a distinctive emoji + exact caps
    string, required nowhere else in the template) is unique enough to
    find reliably without one - a false match here would need the exact
    header text to appear verbatim outside a real header, which the
    prompt's own content never does.
    """
    match = re.search(
        r'\n\n' + re.escape(header) + r'[ \t]*\n\n(.*?)' + _NEXT_SECTION,
        markdown, re.DOTALL,
    )
    return match.group(1) if match else None


def _strip_section(markdown: str, header: str) -> str:
    """Removes HEADER's own leading separator (if it wrote one at all -
    see _get_section_body on why that's optional here too), the header
    line, and its body in one shot - what's left is the next section's own
    boundary/header, unchanged, so exactly one separator remains between
    the surrounding sections. Matching the leading '\\n\\n' before THIS
    section (not a trailing one after the body) is what keeps this from
    leaving a stray extra blank line where the section used to be - the
    lookahead for the NEXT section's boundary is non-consuming, so that
    one is left fully intact either way.
    """
    pattern = re.compile(
        r'\n\n(?:' + _SECTION_BOUNDARY + r'\n\n)?' + re.escape(header) + r'[ \t]*\n\n(.*?)' + _NEXT_SECTION,
        re.DOTALL,
    )
    return pattern.sub('', markdown, count=1)


def _clean_optional_sections(markdown: str, reddit_discussions) -> str:
    """Deterministic cleanup for the two sections that are ALWAYS optional
    (COMMUNITY OPPORTUNITIES, CUSTOMER EXPERIMENT - unlike LEAD-BASED
    OUTREACH/PROSPECTING, which always show with their own required
    fallback lines and must never be touched here).

    COMMUNITY is stripped whenever reddit_discussions is itself null/empty -
    an objective fact from this run's input, independent of anything the
    LLM wrote. Both sections are ALSO stripped whenever their body doesn't
    start with its required content marker (see _REQUIRED_BODY_PREFIX) -
    which additionally catches COMMUNITY being (correctly) judged
    irrelevant despite non-empty reddit_discussions, and EXPERIMENT in
    general (no equivalent objective signal exists for "is there something
    worth testing today").
    """
    if not reddit_discussions:
        markdown = _strip_section(markdown, COMMUNITY_HEADER)

    for header, prefix_re in _REQUIRED_BODY_PREFIX.items():
        body = _get_section_body(markdown, header)
        if body and not prefix_re.match(body.strip()):
            markdown = _strip_section(markdown, header)

    return markdown


def generate_acquisition_report(metrics: dict):
    """Calls the OpenAI API once and returns (report_markdown, trackable_items).

    `metrics` must be collect_acquisition_data()'s output (collect_metrics()
    plus the `prospects` field), not raw collect_metrics() - the prompt
    documents and requires the `prospects` key.

    Same contract as brief.generate_brief(): report_markdown never contains
    the trailing JSON block, trackable_items is always a list (empty on any
    extraction failure), and this raises on any API error since a silently
    failed report means no email gets sent and nobody would know why.
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

    raw = response.choices[0].message.content
    if not raw or not raw.strip():
        raise RuntimeError('OpenAI returned an empty acquisition report')

    report_markdown, trackable_items = _extract_trackable_items(raw)
    report_markdown = _clean_optional_sections(report_markdown, metrics.get('reddit_discussions'))

    word_count = len(report_markdown.split())
    if word_count > WORD_COUNT_WARN_THRESHOLD:
        LOGGER.warning(
            'Customer acquisition report is %d words, over the %d-word runaway-'
            'generation threshold - email still sent as-is',
            word_count, WORD_COUNT_WARN_THRESHOLD,
        )

    return report_markdown, trackable_items
