#!/usr/bin/env python3
"""Board updates for a Claude run working a ticket.

Identify the task with --task (Vikunja's immutable task id, preferred) or
--ticket (the editable #NN title prefix).

Usage:
    vkctl.py show    --task 11
    vkctl.py comment --task 11 "<text>"
    vkctl.py move    --task 11 Done          # Backlog|Ready|In Progress|Waiting|Done
    vkctl.py show    --ticket 35

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

    args = parser.parse_args(argv)

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    client = VikunjaClient(config.api_url, config.token)
    try:
        project_id = client.project_id(config.project_title, config.project_id)
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
        elif args.command == "comment":
            client.add_comment(ticket.task_id, args.text)
            print(f"commented on {ticket.reference} (task {ticket.task_id})")
        elif args.command == "move":
            client.move_to_bucket(project_id, view_id, ticket.task_id, args.bucket)
            print(f"moved {ticket.reference} to {args.bucket}")
    except VikunjaError as exc:
        print(f"vikunja error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
