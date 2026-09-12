"""One-off CLI to review and resolve prospect status.

Runs INSIDE the container (needs Postgres reachable by the `db` hostname on
the private Docker network, and .env for GROWTH_AGENT_TRACKING_DATABASE_URL)
- same as scripts/mark_action.py and scripts/mark_lead.py.

Usage:
    docker compose exec growth-agent python scripts/mark_prospect.py list
    docker compose exec growth-agent python scripts/mark_prospect.py examplestore.myshopify.com contacted "sent outreach Sep 10"
    docker compose exec growth-agent python scripts/mark_prospect.py examplestore.com skipped "already on Triple Whale, not a fit"

Kept SEPARATE from scripts/mark_action.py and scripts/mark_lead.py: the
three key on genuinely different identity types (integer DB id, email
string, and here a normalized storefront domain).

Like mark_lead.py, `list` is a convenience, not a hard requirement: the
domain is printed on each prospect's Domain: line in the customer acquisition
report, so a prospect can be marked without looking anything up first. The
CLI normalizes whatever host/URL form you pass (a full https://... URL is
fine), so it matches the stored key.

Deliberately not argparse - same reason as mark_action.py/mark_lead.py: a
free-text `note` starting with a hyphen and containing no spaces (e.g.
"-skip") is a natural thing to type and argparse rejects exactly that shape
as an unrecognized option.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.apify_prospecting import normalize_prospect_domain  # noqa: E402
from app.tracking import get_reviewable_prospects, mark_prospect  # noqa: E402

USAGE = (
    'Usage:\n'
    '  mark_prospect.py list\n'
    '  mark_prospect.py <domain> <contacted|skipped> [note]'
)


def _cmd_list():
    prospects = get_reviewable_prospects(limit=50)
    if not prospects:
        print('No prospects to review.')
        return
    for p in prospects:
        last = (p['last_surfaced_at'] or 'never')[:10]
        business = p['business'] or '(no business name)'
        print(
            f"{p['domain']} — {business} "
            f"({p['status']}, surfaced {p['times_surfaced']}x, last {last})"
        )
        if p.get('draft_message'):
            indented = '\n'.join(f'    {line}' for line in p['draft_message'].splitlines())
            print(f'  Draft message:\n{indented}')
        else:
            print('  Draft message: (none captured)')


def _cmd_mark(domain_arg: str, status: str, note: str):
    if status not in ('contacted', 'skipped'):
        print(f"status must be 'contacted' or 'skipped', got {status!r}\n\n{USAGE}", file=sys.stderr)
        sys.exit(1)

    domain = normalize_prospect_domain(domain_arg)
    if not domain:
        print(f'not a usable domain: {domain_arg!r}\n\n{USAGE}', file=sys.stderr)
        sys.exit(1)

    mark_prospect(domain, status, note or None)
    msg = f'Marked {domain} as {status}.'
    if domain != domain_arg:
        msg += f' (normalized from {domain_arg!r})'
    print(msg)


def main():
    argv = sys.argv[1:]

    if not argv or argv[0] in ('-h', '--help'):
        print(USAGE)
        return

    if argv[0] == 'list':
        _cmd_list()
        return

    if len(argv) < 2:
        print(USAGE, file=sys.stderr)
        sys.exit(1)

    domain_arg, status, *note_words = argv
    _cmd_mark(domain_arg, status, ' '.join(note_words))


if __name__ == '__main__':
    main()
