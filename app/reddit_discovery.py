"""Finds recent Reddit posts relevant to DataLayer's free tools, for the
daily brief's "Reddit discussion opportunities" section.

Read-only application-only OAuth via PRAW (client_id + client_secret +
user_agent, no Reddit account password) - we only ever list/search public
posts, never post or comment as a user. Failures here must not crash the
whole brief or block the email - callers should catch and record under
data_gaps, same as ga4.py/search_console.py. An empty result list is a
normal, valid outcome (no matching posts this run) and is NOT a data gap -
only a raised exception is.

No dedup/tracking of already-surfaced posts: the DB connection used
elsewhere in this app is read-only by design (see db.py) and the container
has no persistent volume, so a dedup store isn't a cheap addition here. A
short freshness cutoff plus a result cap keeps repeats manageable instead.
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import praw

LOGGER = logging.getLogger(__name__)

SUBREDDITS = ['ecommerce', 'shopify', 'SaaS', 'Entrepreneur', 'smallbusiness']

KEYWORDS = [
    'csv export',
    'shopify orders',
    'customer segmentation',
    'sales data cleanup',
    'order data',
]

FRESHNESS_CUTOFF = timedelta(days=2)
RESULTS_PER_SUBREDDIT = 25
MAX_RESULTS = 10
SELFTEXT_EXCERPT_CHARS = 300


def _build_client() -> praw.Reddit:
    client_id = os.environ.get('REDDIT_CLIENT_ID')
    client_secret = os.environ.get('REDDIT_CLIENT_SECRET')
    user_agent = os.environ.get('REDDIT_USER_AGENT')
    if not client_id or not client_secret or not user_agent:
        raise RuntimeError(
            'REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET / REDDIT_USER_AGENT must all be set'
        )
    return praw.Reddit(client_id=client_id, client_secret=client_secret, user_agent=user_agent)


def _search_subreddit(reddit: praw.Reddit, subreddit_name: str, cutoff: datetime) -> List[Dict[str, Any]]:
    query = ' OR '.join(f'"{kw}"' for kw in KEYWORDS)
    results = []
    for submission in reddit.subreddit(subreddit_name).search(
        query, sort='new', time_filter='week', limit=RESULTS_PER_SUBREDDIT
    ):
        created = datetime.fromtimestamp(submission.created_utc, tz=timezone.utc)
        if created < cutoff:
            continue
        selftext = (submission.selftext or '').strip()
        results.append({
            'subreddit': subreddit_name,
            'title': submission.title,
            'url': f'https://www.reddit.com{submission.permalink}',
            'created_utc': created.isoformat(),
            'score': submission.score,
            'num_comments': submission.num_comments,
            'selftext_excerpt': selftext[:SELFTEXT_EXCERPT_CHARS],
        })
    return results


def fetch_reddit_discussions() -> List[Dict[str, Any]]:
    """Returns up to MAX_RESULTS recent, relevant Reddit posts.

    Raises on any client/auth/network error - callers must catch and record
    under data_gaps, never silently swallow (see module docstring). An
    empty list is a valid, normal return value, distinct from a raised
    exception.
    """
    reddit = _build_client()
    cutoff = datetime.now(timezone.utc) - FRESHNESS_CUTOFF

    all_results: List[Dict[str, Any]] = []
    for subreddit_name in SUBREDDITS:
        all_results.extend(_search_subreddit(reddit, subreddit_name, cutoff))

    all_results.sort(key=lambda r: r['created_utc'], reverse=True)
    return all_results[:MAX_RESULTS]
