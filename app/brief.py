"""Builds the LLM prompt from metrics.py's output and calls OpenAI once
per run to produce the markdown growth brief.
"""
import json
import logging
import os
import re

from openai import OpenAI

from .metrics import NO_CONTROL_GROUP_CAVEAT

LOGGER = logging.getLogger(__name__)

# Routed through OpenRouter (OpenAI-API-compatible), reusing the same
# provider/key already used elsewhere in datalayer-ecommerce - no separate
# OpenAI account/billing needed. Model id uses OpenRouter's "<provider>/<model>"
# naming.
OPENROUTER_BASE_URL = 'https://openrouter.ai/api/v1'
MODEL = os.environ.get('GROWTH_AGENT_MODEL', 'openai/gpt-4o-mini')

DISCLAIMER = (
    "Note: this brief is based on DataLayer's own signup/upload/lead data, GA4 traffic/"
    "funnel data, Search Console query/page data, Reddit discussion data, and outcomes "
    "of previously resolved tracked actions. If any of those sources failed to load for "
    "this run, it is called out above under data_gaps rather than silently omitted. "
    "Every Reddit reply and outreach message below is a DRAFT for human review only - "
    "nothing is posted or sent automatically. Past action outcomes are small-sample, "
    "correlational before/after data only, never evidence of causation."
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
    datalayer_cta_clicked. IMPORTANT: these counts are aggregated across ALL tool pages
    combined, NOT broken down per page like tool_page_sessions_last_30_days is - do not
    compute or imply a page-specific conversion rate by combining these two fields (e.g.
    do not say "csv-cleaner converts at X%" using this aggregate). Use
    tool_page_sessions_last_30_days alone to compare which pages get traffic, and use
    these aggregate counts only to describe the sitewide free-tool funnel.
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
- reddit_discussions (may be null if that run's fetch failed - see data_gaps; an empty
  list [] means the fetch succeeded but found no relevant posts this run, which is a
  normal outcome, NOT a data gap): up to 10 recent Reddit posts from a curated list of
  subreddits (r/ecommerce, r/shopify, r/SaaS, r/Entrepreneur, r/smallbusiness) matching
  curated keywords about CSV/order-data cleanup and customer segmentation. Each has
  subreddit, title, url, created_utc, score, num_comments, selftext_excerpt.
- lead_research (DB-backed, same reliability as signups/uploads/csv_tool_leads for its
  underlying fetch - always a list, never null; already excludes anyone previously
  marked contacted/skipped, unless that exclusion check itself failed this run - see
  data_gaps - in which case it may include someone already contacted): up to 10
  most-recent people who captured a free-tool lead (gave an email via a free tool) but
  never signed
  up for a DataLayer account. Each has email, score, row_count, file_name, created_at.
- pending_from_prior_briefs (may be null if tracking is unavailable this run - see
  data_gaps; an empty list [] means tracking is working and nothing recommended 3+ days
  ago is still pending, which is a normal, good outcome, NOT a data gap): items
  previously recommended under "Recommended actions" or "Suggested experiments" in an
  earlier brief that haven't been marked done or skipped yet. Each has id, brief_date,
  category, description. Where relevant, reference these explicitly (e.g. "still open
  from Aug 18: ...") instead of silently re-recommending the same thing as if it were
  new - but only if pending_from_prior_briefs is non-null; if it's null, don't imply
  anything about prior recommendations one way or the other.
- resolved_action_outcomes (may be null if tracking is unavailable this run - see
  data_gaps; an empty list [] means tracking is working but no action landed in the
  7-14-day-ago attribution window this run, which is a normal outcome, NOT a data gap):
  for each tracked item marked "done" 7-14 days ago, DataLayer's own signup and upload
  counts in the 7 days immediately before vs. the 7 days immediately after it was marked
  done. Each entry has id, category, description, outcome_note (the human's free-text
  note when marking it done, may be null), resolved_at, window_days,
  signups.{{before_total,after_total,delta}}, uploads.{{before_total,after_total,delta}},
  low_signal (bool), and low_signal_note (a pre-written string present whenever
  low_signal is true, else null).

  THESE NUMBERS ARE NOT EVIDENCE OF CAUSATION. DataLayer's traffic is small enough
  (single-digit-to-low-double-digit signups per MONTH) that a 7-day window will
  typically show 0, 1, or 2 total events - far too few to separate a real
  effect from ordinary week-to-week noise, and with no control group, any other
  unrelated change in the same window is just as plausible an explanation as the
  tracked action. Rules, no exceptions:
  - Never use causal language for any entry ("caused", "led to", "resulted in",
    "drove", "because of this action", "this action produced N signups").
  - Never compute or state a percentage change for these entries, even though you
    could derive one from before_total/after_total - with totals this small, a
    percentage (e.g. 0->1 as "infinite%", 1->2 as "100% increase") is guaranteed to
    overstate significance. Report only the raw before/after counts.
  - Write each entry using this exact pattern, filling in the brackets and keeping the
    caveat first:
    "[description] (marked done, [outcome_note or 'no note']): [low_signal_note if
    low_signal is true; otherwise '{NO_CONTROL_GROUP_CAVEAT}'] Observed alongside this
    window: signups [before_total]->[after_total], uploads [before_total]->[after_total]."
  - Do not lead with the numbers before the caveat, and do not editorialize beyond
    this pattern (no "which suggests...", no "this indicates...").
- data_gaps: list of strings naming any data source that failed to load this run and why.
  Treat every other field as ground truth for this run.

PRIORITIZING RECOMMENDED ACTIONS - Impact x Confidence x Ease:
For "Recommended actions" specifically (not "Suggested experiments" or any other
section), score every candidate action you consider on three 1-5 scales before picking
which ones to show:
- Impact (1-5): how much this could plausibly move signups/uploads/conversions if it
  works, given DataLayer's current tiny volume (a change that could meaningfully move a
  number this small scores higher than a change that's marginal even in the best case).
- Confidence (1-5): how directly the evidence already in this run's JSON (not
  speculation, not generic best-practice) supports this specific action. An action
  backed by multiple concrete data points in this run scores higher than a hunch.
- Ease (1-5): how quickly/cheaply this could actually be done, given the team is very
  small with limited time and money. Something doable in under an hour scores higher
  than a multi-week project.
Compute score = Impact x Confidence x Ease (max 125) for each candidate. Then do this
as a literal, mechanical sort step, not an impression: write out every candidate's
score, find the numerically highest one, put it first; find the next-highest remaining
score, put it second; repeat. The item numbered "1." must have a Score greater than or
equal to every other item's Score below it - if you notice while writing the list that
item 2's Score is higher than item 1's, that is an error and you must reorder before
finalizing your response, not leave it as-is. Keep only the TOP 5 by score. If fewer
than 5 candidates are genuinely supported by this run's data, show fewer than 5 - do NOT
invent a generic or low-confidence action just to reach 5 (this directly contradicts
avoiding generic advice below). Show each action's scores so the ranking is transparent,
not a black box.

AVOID GENERIC ADVICE. Never write vague recommendations like "post more on social
media" or "improve SEO." Every recommended action must cite the SPECIFIC evidence
behind it - a real query, page, thread, or number from this run's JSON - the same way
"3 Shopify merchants asked about cleaning duplicate Shopify CSV orders on Reddit this
week - respond to thread X" is specific and "engage more on Reddit" is not.

CRITICAL RULES:
- Use ONLY the numbers in the provided metrics JSON. Never invent, estimate, or assume
  any traffic, funnel, visitor, conversion-rate, search-console, or Reddit number that is
  not explicitly present in the JSON.
- If ga4, search_console, or reddit_discussions is null this run (see data_gaps), do not
  fabricate figures/posts for it - say plainly that it's unavailable this run and rely on
  the sources that did load.
- Every Reddit reply draft and every outreach message draft you write MUST be explicitly
  labeled as a draft for human review (e.g. "DRAFT - for review, not to be posted/sent
  automatically"). This system never posts or sends anything on its own - a human decides
  whether to use each draft.
- Reddit reply drafts must be genuinely helpful and non-promotional in tone - most
  subreddits ban direct self-promotion/advertising, so do not write anything that reads
  like an ad.
- Never claim or imply that a tracked action caused a change in signups/uploads.
  resolved_action_outcomes is correlational, small-sample, before/after data only -
  follow its rules above exactly, including the required sentence pattern.
- "Recommended actions" must be scored and ranked exactly per the Impact x Confidence x
  Ease rules above - up to 5 items, highest score first, never padded with generic
  filler to reach 5.
- Output must be plain markdown, in EXACTLY this structure, with these exact section
  headers, followed by exactly one trailing fenced ```json code block (format
  described at the very end below) and nothing else - no text before the markdown, none
  between the markdown and the JSON block, and none after it:

DATA LAYER GROWTH BRIEF

🚨 #1 Problem:
[one-line problem statement]

Evidence:
[bullet points from the metrics]

Why this matters:
[1-2 sentences]

Recommended actions:
[up to 5 numbered items, highest Impact x Confidence x Ease score first, each formatted
as: "N. [specific action citing real evidence from this run] (Impact: N, Confidence: N,
Ease: N -> Score: N)". Concrete and specific per the rules above - no generic advice.
Keep each item to ONE line/sentence - do NOT write a nested numbered sub-list (e.g. "1.
..." / "2. ..." within one action's own explanation) inside any item, since this is
parsed by exact line position and a nested list would be misread as separate items.
Use commas or a dash within the sentence instead if you need to mention sub-points.]

Priority: [HIGH/MEDIUM/LOW]

Past action outcomes:
[If resolved_action_outcomes is `null` (check data_gaps for the reason), write "Past
action outcomes unavailable this run: [the specific data_gaps reason]." If it's an
empty list `[]`, write "No actions in the 7-14-day attribution window this run."
Otherwise, one entry per item using the required sentence pattern described above -
nothing else, no additional commentary.]

SEO opportunities:
[bullets, or "none identified from available data"]

User/conversion problems:
[bullets]

Suggested experiments:
[bullets]

Reddit discussion opportunities:
[For each relevant thread in reddit_discussions: subreddit + title + url, why it's
relevant, then a DRAFT reply (helpful, non-promotional). Label every reply "DRAFT - for
review, not to be posted automatically." If reddit_discussions is `null` (check
data_gaps for the reason), write "Reddit data unavailable this run: [the specific
data_gaps reason]." - do NOT say "no relevant discussions found," since that implies the
search ran and came up empty, which is not what null means. Only write "No relevant
discussions found this run." when reddit_discussions is an empty list `[]` (the fetch
succeeded, there just weren't any matches) - that is a genuinely different, normal
state from a failed fetch, and the two must not be worded the same way.]

Lead outreach candidates:
[For each lead in lead_research: email, why flagged (captured a free-tool lead, never
signed up), then a DRAFT outreach message. Label every message "DRAFT - for review, not
to be sent automatically." If lead_research is empty, write "No unmatched leads this
run."]

LinkedIn/Facebook content ideas:
[3-5 post ideas grounded ONLY in this run's ga4/search_console data above - e.g. a tool
page with strong Search Console impressions but weak GA4 sessions, or a funnel-drop-off
finding worth turning into a post.]

{DISCLAIMER}

```json
[{{"category": "action", "description": "..."}}, {{"category": "experiment", "description": "..."}}]
```

The fenced json block above is REQUIRED and must be the very last thing in your
response, with nothing after it. It must contain exactly one object per item you
listed under "Recommended actions" and exactly one object per item you listed under
"Suggested experiments" (same items, described in your own words, category set to
"action" or "experiment" accordingly) - nothing else, no items from any other section.
If you cannot produce it for any reason, omit the whole block rather than emitting
malformed JSON.
"""


TRAILING_JSON_BLOCK_RE = re.compile(r'```json\s*(.*?)\s*```', re.DOTALL)


def _extract_trackable_items(raw_content: str):
    """Splits raw LLM output into (markdown_without_json_block, trackable_items).

    Never raises - a missing/malformed JSON block degrades to an empty
    list, since this must never block the email (same graceful-degradation
    philosophy used elsewhere in this app for external fetch failures,
    applied here to LLM-output parsing instead).

    Takes the LAST fenced ```json block anywhere in the response (not
    anchored to end-of-string) so leading/trailing chatter around it is
    tolerated. Only the text preceding that block is kept as the markdown
    brief - trailing chatter after the JSON block is discarded along with
    the block itself.

    TRAILING_JSON_BLOCK_RE captures everything between the ```json fence
    and the next ``` fence, not a pattern anchored on the JSON array's own
    opening/closing brackets - a bracket-anchored non-greedy match would
    truncate at the FIRST literal closing-bracket character anywhere in
    the content (e.g. an action description like "tagged [Q1-2026]"),
    silently corrupting the capture before json.loads() ever runs.
    Delimiting on the fence markers instead (a much rarer 3-backtick
    sequence) and letting json.loads() itself
    validate the contents is more robust than trying to regex-match
    balanced JSON brackets.
    """
    matches = list(TRAILING_JSON_BLOCK_RE.finditer(raw_content))
    if not matches:
        LOGGER.warning('No trailing JSON block in LLM response; tracking skipped this run')
        return raw_content.strip(), []

    match = matches[-1]
    markdown = raw_content[:match.start()].rstrip()

    try:
        parsed = json.loads(match.group(1))
    except json.JSONDecodeError:
        LOGGER.warning('Trailing JSON block was not valid JSON; tracking skipped this run')
        return markdown, []

    if not isinstance(parsed, list):
        LOGGER.warning('Trailing JSON block was not a JSON array; tracking skipped this run')
        return markdown, []

    items = []
    for entry in parsed:
        if (
            isinstance(entry, dict)
            and entry.get('category') in ('action', 'experiment')
            and isinstance(entry.get('description'), str)
            and entry.get('description').strip()
        ):
            items.append({'category': entry['category'], 'description': entry['description'].strip()})
        else:
            LOGGER.warning('Skipping malformed trackable item: %r', entry)

    return markdown, items


RECOMMENDED_ACTIONS_SECTION_RE = re.compile(
    r'(Recommended actions:\n)(.*?)(\n\nPriority:)', re.DOTALL
)
ACTION_ITEM_START_RE = re.compile(r'^\d+\.\s', re.MULTILINE)
# Anchored on the literal "-> Score:" arrow from the required prompt format
# ("Impact: N, Confidence: N, Ease: N -> Score: N"), NOT a bare "Score:" -
# lead_research entries carry their own numeric `score` field, and an
# action mentioning "the captured lead (Score: 86)" before its own
# required score annotation would otherwise false-match on 86 instead of
# the real ICE score.
ACTION_SCORE_RE = re.compile(r'->\s*Score:\s*(\d+)')


def _sort_recommended_actions_by_score(markdown: str) -> str:
    """The prompt instructs the LLM to list "Recommended actions" in
    descending Impact x Confidence x Ease Score order, but gpt-4o-mini
    doesn't reliably do this correctly even with explicit "sort
    mechanically, double-check before finalizing" wording - confirmed via
    two separate live /run-now checks, both computed scores correctly but
    got the list order wrong. Rather than keep tightening prompt language
    indefinitely, this is the same fix philosophy already used for
    low_signal/NO_CONTROL_GROUP_CAVEAT: don't trust the model with a
    mechanical judgment it doesn't need to make - re-sort in code instead.

    No-ops (returns markdown unchanged) if the section can't be found or
    any item's score can't be parsed out - this must never raise or
    corrupt the brief; an unsorted-but-otherwise-correct list is far
    better than a crash or mangled output. Every no-op path logs a
    warning, unlike the original version of this function - a silent
    no-op means the whole point of this function (guaranteeing correct
    order) silently stops applying with zero signal anywhere.
    """
    match = RECOMMENDED_ACTIONS_SECTION_RE.search(markdown)
    if not match:
        LOGGER.warning(
            'Could not locate "Recommended actions:" section for sorting; '
            'leaving brief as-is (LLM output format may have drifted)'
        )
        return markdown

    body = match.group(2)
    starts = [m.start() for m in ACTION_ITEM_START_RE.finditer(body)]
    if not starts:
        LOGGER.warning(
            'Recommended actions section had no numbered items to sort; leaving as-is'
        )
        return markdown
    starts.append(len(body))
    items = [body[starts[i]:starts[i + 1]] for i in range(len(starts) - 1)]

    scored = []
    for item in items:
        score_match = ACTION_SCORE_RE.search(item)
        if not score_match:
            LOGGER.warning(
                'Recommended action item missing a parseable "-> Score: N" - '
                'leaving the whole list unsorted rather than guessing: %r', item[:80]
            )
            return markdown
        scored.append((int(score_match.group(1)), item))

    scored.sort(key=lambda pair: pair[0], reverse=True)

    # Each item's raw slice only has a trailing newline if it wasn't the
    # LAST item in the ORIGINAL (pre-sort) order - the final item's slice
    # ends exactly at len(body), with nothing captured after it. Stripping
    # every item before rejoining (instead of rejoining raw slices
    # verbatim) avoids position-dependent whitespace: without this, moving
    # the original last item to a non-last position after sorting would
    # squash it directly against the next item with no separator, since
    # its slice never had a trailing newline to begin with.
    renumbered = [
        re.sub(r'^\d+\.', f'{i}.', item.strip(), count=1)
        for i, (_, item) in enumerate(scored, start=1)
    ]

    return markdown[:match.start(2)] + '\n'.join(renumbered) + markdown[match.end(2):]


def generate_brief(metrics: dict):
    """Calls the OpenAI API once and returns (brief_markdown, trackable_items).

    brief_markdown never contains the trailing JSON block. trackable_items
    is always a list, empty on any extraction failure, never None.

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

    raw = response.choices[0].message.content
    if not raw or not raw.strip():
        raise RuntimeError('OpenAI returned an empty brief')

    brief_markdown, trackable_items = _extract_trackable_items(raw)
    brief_markdown = _sort_recommended_actions_by_score(brief_markdown)

    if DISCLAIMER not in brief_markdown:
        brief_markdown = brief_markdown.rstrip() + '\n\n' + DISCLAIMER

    return brief_markdown, trackable_items
