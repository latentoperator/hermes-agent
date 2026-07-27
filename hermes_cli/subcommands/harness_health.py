"""Parser for ``hermes harness-health``."""

from __future__ import annotations

from typing import Callable


def build_harness_health_parser(subparsers, *, cmd_harness_health: Callable) -> None:
    parser = subparsers.add_parser(
        "harness-health",
        help="Audit configured harness inputs and persisted run evidence",
        description=(
            "Collect a deterministic, local, read-only Harness Health v0.1 report."
        ),
    )
    parser.add_argument(
        "--platform", default="cli", help="Platform to inspect (default: cli)"
    )
    parser.add_argument("--cwd", default=None, help="Working directory to inspect")
    parser.add_argument(
        "--session", default=None, help="Persisted session ID to inspect"
    )
    parser.add_argument("--json", action="store_true", help="Emit structured JSON")
    parser.add_argument(
        "--output", default=None, help="Exclusively create a JSON report file"
    )
    parser.set_defaults(func=cmd_harness_health)
