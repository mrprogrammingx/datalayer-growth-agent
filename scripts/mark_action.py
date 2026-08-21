"""One-off CLI to review and resolve tracked action/experiment items.

Runs INSIDE the container (needs Postgres reachable by the `db` hostname on
the private Docker network, and .env for GROWTH_AGENT_TRACKING_DATABASE_URL)
- unlike scripts/authorize_google.py, which deliberately runs on a laptop.

Usage:
    docker compose exec growth-agent python scripts/mark_action.py list
    docker compose exec growth-agent python scripts/mark_action.py 7 done "shipped the popup, +12 signups"
    docker compose exec growth-agent python scripts/mark_action.py 8 skipped "not worth it, low traffic page"

`list` is necessary, not optional: tracking rows are written only after the
day's email has already been sent, so the email itself never contains an
item's DB-assigned id - `list` is the only way to look one up.

Deliberately not argparse: a free-text `note` that happens to start with a
hyphen and contain no spaces (e.g. "-skip", "-duplicate") is a very natural
thing to type, and argparse's positional/option heuristics reject exactly
that shape as an "unrecognized argument" - confirmed live, the whole
command fails and the item never gets marked. sys.argv is parsed by hand
instead so a leading '-' in the note is never ambiguous with an option.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.tracking import get_pending_items, mark_item  # noqa: E402

USAGE = (
    'Usage:\n'
    '  mark_action.py list\n'
    '  mark_action.py <id> <done|skipped> [note]'
)


def _cmd_list():
    items = get_pending_items(older_than_days=0, limit=50)
    if not items:
        print('No pending items.')
        return
    for item in items:
        print(f"[{item['id']}] ({item['category']}, {item['brief_date']}) {item['description']}")


def _cmd_mark(item_id_str: str, status: str, note: str):
    try:
        item_id = int(item_id_str)
    except ValueError:
        print(f'Invalid id: {item_id_str!r}\n\n{USAGE}', file=sys.stderr)
        sys.exit(1)

    if status not in ('done', 'skipped'):
        print(f"status must be 'done' or 'skipped', got {status!r}\n\n{USAGE}", file=sys.stderr)
        sys.exit(1)

    updated = mark_item(item_id, status, note or None)
    if not updated:
        print(f'No pending item with id {item_id} found.')
        sys.exit(1)
    print(f'Marked item {item_id} as {status}.')


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

    item_id_str, status, *note_words = argv
    _cmd_mark(item_id_str, status, ' '.join(note_words))


if __name__ == '__main__':
    main()
