#!/usr/bin/env python3
"""Board updates for a Claude run working a ticket.

Identify the task with --task (Vikunja's immutable task id, preferred) or
--ticket (the editable #NN title prefix).

Usage:
    vkctl.py show    --task 11
    vkctl.py comment --task 11 "<text>"
    vkctl.py move    --task 11 Done          # Backlog|Ready|In Progress|Waiting|Done
    vkctl.py show    --ticket 35
    vkctl.py close   --task 11 [--comment-file closing.html]
    vkctl.py create  "Title" --desc-file body.html
    vkctl.py edit    --task 11 --desc-file body.html

Descriptions are HTML and are passed as files, not as arguments: they are long,
and shell quoting is its own way to corrupt one.

Closing a ticket goes through the client's read-modify-write path, which checks
the description survived. Never close one with a bare
``curl -X POST /tasks/<id> -d '{"done":true}'`` -- that replaces the task and
blanks every field the body omits.

The Vikunja token is read from the VIKUNJA_API_TOKEN environment variable that
the launcher puts in this process's environment. It is never a CLI argument.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vikunja_claude.config import Config, ConfigError  # noqa: E402
from vikunja_claude.vikunja import VikunjaClient, VikunjaError  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vkctl", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def with_selector(sub_parser):
        group = sub_parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--task", type=int, help="Vikunja task id (preferred)")
        group.add_argument("--ticket", type=int, help="#NN title prefix")
        return sub_parser

    with_selector(sub.add_parser("show", help="print a ticket"))

    comment = with_selector(sub.add_parser("comment", help="comment on a ticket"))
    comment.add_argument("text")

    move = with_selector(sub.add_parser("move", help="move a ticket to a bucket"))
    move.add_argument("bucket")

    close = with_selector(sub.add_parser("close", help="mark a ticket done"))
    close.add_argument("--comment-file", help="HTML closing comment, posted first")

    create = sub.add_parser("create", help="create a ticket")
    create.add_argument("title")
    create.add_argument("--desc-file", help="HTML description")

    edit = with_selector(sub.add_parser("edit", help="replace a ticket's description"))
    edit.add_argument("--desc-file", required=True)

    args = parser.parse_args(argv)

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    client = VikunjaClient(config.api_url, config.token)
    try:
        project_id = client.project_id(config.project_title, config.project_id)

        if args.command == "create":
            description = _read_html(args.desc_file) if args.desc_file else ""
            created = client.create_task(project_id, args.title, description)
            print(f"created task {created['id']}: {created['title']}")
            return 0

        view_id = client.kanban_view_id(project_id)
        if args.task is not None:
            ticket = client.find_by_task_id(args.task, project_id, view_id)
        else:
            ticket = client.find_ticket(args.ticket, project_id, view_id)

        if args.command == "show":
            print(f"{ticket.reference} {ticket.summary}")
            print(f"bucket: {ticket.bucket_title}  task id: {ticket.task_id}")
            print()
            print(ticket.description)
            _print_comments(client.comment_views(ticket.task_id))
        elif args.command == "comment":
            client.add_comment(ticket.task_id, args.text)
            print(f"commented on {ticket.reference} (task {ticket.task_id})")
        elif args.command == "move":
            client.move_to_bucket(project_id, view_id, ticket.task_id, args.bucket)
            print(f"moved {ticket.reference} to {args.bucket}")
        elif args.command == "close":
            if args.comment_file:
                client.add_comment(ticket.task_id, _read_html(args.comment_file))
                print(f"commented on {ticket.reference}")
            client.close_task(ticket.task_id)
            print(f"closed {ticket.reference} (task {ticket.task_id})")
            print(f"description intact: {len(ticket.description_html)} chars")
        elif args.command == "edit":
            client.set_description(ticket.task_id, _read_html(args.desc_file))
            print(f"updated the description of {ticket.reference}")
    except VikunjaError as exc:
        print(f"vikunja error: {exc}", file=sys.stderr)
        return 1
    return 0


def _print_comments(comments: list[dict]) -> None:
    """Print a task's comments under `show`, oldest first.

    "(no comments)" is printed rather than nothing. Printing nothing is exactly
    what this command did while it never read comments at all, so a silent tail
    would leave a reader unable to tell an uncommented ticket from a stale
    build of this script -- which is how the gap survived long enough to become
    lore ("check comments via the API, the page renders none").
    """
    print()
    print(f"comments ({len(comments)}):" if comments else "comments: (none)")
    for comment in comments:
        who = comment.get("author") or "unknown"
        print()
        print(f"--- {comment.get('created')} by {who} (comment {comment.get('id')})")
        print(comment.get("text") or "")


def _read_html(path: str) -> str:
    """Read an HTML body from a file, refusing an empty one.

    A blank file would otherwise be an ordinary-looking way to erase a
    description -- the same damage as a partial POST, just slower.
    """
    text = Path(path).read_text(encoding="utf-8")
    if not text.strip():
        raise VikunjaError(f"{path} is empty -- refusing to write a blank body")
    return text


if __name__ == "__main__":
    raise SystemExit(main())
