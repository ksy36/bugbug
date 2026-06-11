"""Command-line entry point for the bug_fix agent.

A larrey.py-style local runner: builds an in-process Bugzilla MCP server from
a Bugzilla API key and drives BugFixTool directly, with live output. Intended
for local experimentation (no broker, no Docker).

Example (web-compat reproduction):

    BZ_API_KEY=<key> python -m bugbug.tools.bug_fix \\
        --bugs 1899999 \\
        --webcompat-tools --firefox-path /usr/bin/firefox \\
        --task "Reproduce this web-compat bug on the live site." \\
        --dry-run --verbose --log webcompat.log
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import bugsy

from bugbug.tools.bug_fix.agent import BugFixTool
from bugbug.tools.bug_fix.bugzilla_mcp import BugzillaContext
from bugbug.tools.bug_fix.bugzilla_mcp import build_server as build_bugzilla_server

_EFFORT_CHOICES = ("low", "medium", "high", "max")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="bug_fix",
        description="Bugzilla bug-fix / triage agent (local runner).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    p.add_argument(
        "--bugs",
        required=True,
        help="Comma-separated list of bug IDs to process.",
    )
    p.add_argument(
        "--base-url",
        default="https://bugzilla.mozilla.org/rest",
        help="Bugzilla REST base URL.",
    )
    p.add_argument(
        "--api-key",
        default=os.environ.get("BZ_API_KEY"),
        help="Bugzilla API key (sent as X-BUGZILLA-API-KEY). "
        "Defaults to the BZ_API_KEY environment variable.",
    )
    p.add_argument(
        "--source-repo",
        type=Path,
        default=Path.cwd(),
        help="Source tree used as the agent's working directory. For web-compat "
        "runs the tree is unused, but it must be a real directory.",
    )

    p.add_argument(
        "--task",
        default=None,
        help="Replace the default triage/fix workflow with this directive. The "
        "rules dir and all tools stay available, but this becomes the goal.",
    )
    p.add_argument(
        "--instructions",
        default="",
        help="Extra free-text guidance layered on top of the normal workflow.",
    )
    p.add_argument(
        "--rules-dir",
        type=Path,
        default=None,
        help="Directory holding triage ruleset .md files (defaults to the "
        "packaged rules/).",
    )

    p.add_argument(
        "--webcompat-tools",
        action="store_true",
        default=False,
        help="Enable the firefox-devtools MCP tools to drive a live Firefox for "
        "web-compatibility reproduction.",
    )
    p.add_argument(
        "--firefox-path",
        type=Path,
        default=None,
        help="Firefox binary for the devtools MCP to drive. Implies "
        "--webcompat-tools. Auto-detected if omitted.",
    )
    p.add_argument(
        "--chrome-mask-profile",
        type=Path,
        default=None,
        help="A pre-built Firefox profile with the Chrome Mask extension "
        "installed (see scripts/setup_chrome_mask_profile.py). Used as a "
        "template so the agent can enable UA-spoofing per site. Implies "
        "--webcompat-tools.",
    )

    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate all Bugzilla writes; print what would happen.",
    )
    p.add_argument(
        "--newest-first",
        action="store_true",
        help="Process bugs newest-first (highest ID first).",
    )

    p.add_argument("--model", default=None, help="Claude model to use.")
    p.add_argument(
        "--max-turns",
        type=int,
        default=None,
        help="Cap on agent turns (runaway-loop safety valve).",
    )
    p.add_argument(
        "--effort",
        choices=_EFFORT_CHOICES,
        default=None,
        help="Adaptive-thinking effort level for models that require it.",
    )

    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Stream turn-by-turn agent activity (thinking, tool calls) to stdout.",
    )
    p.add_argument(
        "--log",
        type=Path,
        default=None,
        metavar="FILE",
        help="Write a full untruncated transcript to this file.",
    )

    args = p.parse_args()

    if args.firefox_path is not None or args.chrome_mask_profile is not None:
        args.webcompat_tools = True

    if not args.api_key:
        p.error("no Bugzilla API key: pass --api-key or set BZ_API_KEY")

    try:
        args.bug_ids = sorted({int(x) for x in args.bugs.split(",") if x.strip()})
    except ValueError:
        p.error(f"--bugs must be comma-separated integers, got: {args.bugs!r}")
    if not args.bug_ids:
        p.error("--bugs resolved to no bug IDs")

    return args


async def run(args: argparse.Namespace) -> int:
    bz = bugsy.Bugsy(api_key=args.api_key, bugzilla_url=args.base_url)
    bugzilla_server = build_bugzilla_server(
        BugzillaContext(client=bz, dry_run=args.dry_run)
    )

    tool = BugFixTool.create()
    result = await tool.run(
        bugzilla_mcp_server=bugzilla_server,
        source_repo=args.source_repo,
        bugs=args.bug_ids,
        instructions=args.instructions,
        task=args.task,
        rules_dir=args.rules_dir,
        newest_first=args.newest_first,
        model=args.model,
        max_turns=args.max_turns,
        effort=args.effort,
        webcompat_tools=args.webcompat_tools,
        firefox_path=args.firefox_path,
        chrome_mask_profile=args.chrome_mask_profile,
        verbose=args.verbose,
        log=args.log,
    )

    print(
        f"\n[bug_fix] done: exit_code={result.exit_code} "
        f"bugs_processed={result.bugs_processed}",
        file=sys.stderr,
    )
    return result.exit_code


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
