"""One-off CLI to review and resolve lead-outreach status.

Runs INSIDE the container (needs Postgres reachable by the `db` hostname on
the private Docker network, and .env for GROWTH_AGENT_TRACKING_DATABASE_URL)
- same as scripts/mark_action.py.

Usage:
    docker compose exec growth-agent python scripts/mark_lead.py list
    docker compose exec growth-agent python scripts/mark_lead.py someone@example.com contacted "sent outreach email Aug 20"
    docker compose exec growth-agent python scripts/mark_lead.py someone@example.com skipped "bounced, not a fit"

Kept SEPARATE from scripts/mark_action.py: the two key on genuinely
different identity types (integer DB id vs. email string).

Unlike mark_action.py, `list` here is a convenience, not a hard
requirement: the email is already printed directly in the brief itself,
so a lead can be marked without looking anything up first.

Deliberately not argparse - same reason as mark_action.py: a free-text
`note` starting with a hyphen and containing no spaces (e.g. "-skip") is a
natural thing to type and argparse rejects exactly that shape as an
unrecognized option.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.tracking import get_pending_leads, mark_lead  # noqa: E402

USAGE = (
    'Usage:\n'
    '  mark_lead.py list\n'
    '  mark_lead.py <email> <contacted|skipped> [note]'
)


def _cmd_list():
    leads = get_pending_leads(limit=50)
    if not leads:
        print('No pending leads.')
        return
    for lead in leads:
        print(f"{lead['email']} (first seen {lead['first_seen_at']})")


def _cmd_mark(email: str, status: str, note: str):
    if status not in ('contacted', 'skipped'):
        print(f"status must be 'contacted' or 'skipped', got {status!r}\n\n{USAGE}", file=sys.stderr)
        sys.exit(1)

    mark_lead(email, status, note or None)
    print(f'Marked {email} as {status}.')


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

    email, status, *note_words = argv
    _cmd_mark(email, status, ' '.join(note_words))


if __name__ == '__main__':
    main()
