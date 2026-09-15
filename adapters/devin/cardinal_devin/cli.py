"""`cardinal-devin` command line."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from cardinal_core.paths import AgentPaths

from . import __version__, ingest
from .client import (
    API_VERSIONS, DEFAULT_DEVIN_BASE_URL, DEFAULT_GITHUB_API_URL, ApiError, DevinClient, GitHubClient,
)
from .connect import DEFAULT_HOST, run_connect, run_status
from .poller import PollOptions, Poller
from .state import STATE_FILE, PollState, StateError

log = logging.getLogger("cardinal_devin")

ADAPTER_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ADAPTER_ROOT / "playbook" / "decision-output.schema.json"
PLAYBOOK_PATH = ADAPTER_ROOT / "playbook" / "cardinal-decisions.md"


def default_state_dir() -> Path:
    env = os.environ.get("CARDINAL_DEVIN_HOME")
    return Path(env).expanduser() if env else Path.home() / ".cardinal-devin"


def _env_number(name: str, default: Any, kind: Callable[[str], Any]) -> Any:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return kind(raw)
    except ValueError:
        raise SystemExit(f"error: {name}={raw!r} is not a number")


def _devin_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--devin-key-file", help="File holding the Devin service-user key (default: $DEVIN_API_KEY).")
    parser.add_argument("--devin-org-id", default=os.environ.get("DEVIN_ORG_ID"),
                        help="Devin org id; selects the v3 API when --api-version=auto (default: $DEVIN_ORG_ID).")
    parser.add_argument("--devin-base-url", default=os.environ.get("DEVIN_API_BASE_URL") or DEFAULT_DEVIN_BASE_URL,
                        help=f"Devin API base URL (default: $DEVIN_API_BASE_URL or {DEFAULT_DEVIN_BASE_URL}).")
    parser.add_argument("--api-version", choices=API_VERSIONS, default="auto",
                        help="auto = v3 when an org id is set, else v1.")
    parser.add_argument("--github-api-url", default=os.environ.get("GITHUB_API_URL") or DEFAULT_GITHUB_API_URL,
                        help=f"GitHub REST base URL (default: $GITHUB_API_URL or {DEFAULT_GITHUB_API_URL}).")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state-dir", type=Path, default=None,
                        help="Where credentials and poll state live (default: $CARDINAL_DEVIN_HOME or ~/.cardinal-devin).")
    common.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")

    parser = argparse.ArgumentParser(
        prog="cardinal-devin",
        description="Cardinal telemetry for Devin: polls the Devin API and emits Cardinal OTLP logs.",
    )
    parser.add_argument("--version", action="version", version=f"cardinal-devin {__version__}")
    sub = parser.add_subparsers(dest="command")

    connect = sub.add_parser("connect", parents=[common], help="Connect to Cardinal (device flow; mints an ingest key).")
    connect.add_argument("--host", default=DEFAULT_HOST, help=f"Cardinal app host (default: {DEFAULT_HOST}).")
    connect.add_argument("--rotate", action="store_true", help="Overwrite an existing connection.")
    connect.add_argument("--deployment-env", default=None, help="Override deployment.environment.")
    connect.add_argument("--dry-run", action="store_true", help="Run the flow and print what would be written.")

    status = sub.add_parser("status", parents=[common], help="Show connection, Devin config and poll state.")
    _devin_options(status)

    poll = sub.add_parser("poll", parents=[common], help="Poll Devin and emit telemetry.")
    _devin_options(poll)
    poll.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    poll.add_argument("--interval", type=int, default=300, help="Seconds between cycles (default 300).")
    poll.add_argument("--lookback-days", type=float, default=_env_number("CARDINAL_DEVIN_LOOKBACK_DAYS", 7.0, float),
                      help="Ignore sessions last updated before this window. Downtime longer than this "
                           "skips sessions that finished in the gap (default: $CARDINAL_DEVIN_LOOKBACK_DAYS or 7).")
    poll.add_argument("--active-ttl-days", type=float, default=14.0,
                      help="Stop polling an active session with no update for this long (default 14).")
    poll.add_argument("--state-retention-days", type=float, default=90.0,
                      help="Keep finished session state this long so a resumed session re-sends nothing (default 90).")
    poll.add_argument("--max-state-sessions", type=int, default=20_000,
                      help="Cap on tracked sessions; the oldest finished ones outside the lookback window "
                           "are evicted first (default 20000).")
    poll.add_argument("--retry-updated-after-filter", action="store_true",
                      help="Re-enable the v3 updated_after filter after it was disabled for a mismatch.")
    poll.add_argument("--page-size", type=int, default=100, help="Sessions per list page (v3 max 200).")
    poll.add_argument("--max-pages", type=int, default=_env_number("CARDINAL_DEVIN_MAX_PAGES", 50, int),
                      help="List pages per cycle; v1 has no documented order or filter, so sessions past "
                           "the cap are not seen (default: $CARDINAL_DEVIN_MAX_PAGES or 50).")
    poll.add_argument("--no-decisions", action="store_true", help="Do not emit cardinal.decision.")
    poll.add_argument("--no-usage", action="store_true",
                      default=bool(os.environ.get("CARDINAL_DEVIN_USAGE_DISABLED", "").strip()),
                      help="Do not fetch or emit ACU usage (cardinal.turn_usage). "
                           "Default: off unless CARDINAL_DEVIN_USAGE_DISABLED is set.")
    poll.add_argument("--dry-run", action="store_true",
                      help="Print OTLP bodies to stdout; send nothing and leave poll state untouched.")

    sub.add_parser("decision-schema", help="Print the structured-output JSON schema for Devin sessions.")
    return parser


def resolve_devin_key(key_file: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """(key, source description)."""
    if key_file:
        try:
            key = Path(key_file).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise SystemExit(f"error: cannot read --devin-key-file: {exc}")
        return (key, key_file) if key else (None, None)
    key = os.environ.get("DEVIN_API_KEY", "").strip()
    return (key, "$DEVIN_API_KEY") if key else (None, None)


def _paths(args: argparse.Namespace) -> AgentPaths:
    return AgentPaths(home=(args.state_dir or default_state_dir()).expanduser())


def _raise_interrupt(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def cmd_poll(args: argparse.Namespace) -> int:
    """SIGTERM stops the poller like Ctrl-C. As PID 1 in a container Python
    has no default SIGTERM action, so without this `docker stop` and pod
    termination wait out the grace period and SIGKILL."""
    try:
        previous = signal.signal(signal.SIGTERM, _raise_interrupt)
    except ValueError:  # not the main thread
        previous = None
    try:
        return _poll(args)
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def _poll(args: argparse.Namespace) -> int:
    paths = _paths(args)
    try:
        ingest_config = ingest.resolve(paths)
    except ingest.IngestConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    connection = ingest_config.connection
    if connection is None and not args.dry_run:
        print(f"Not connected. Run: cardinal-devin connect, or set {ingest.ENV_ENDPOINT} and {ingest.ENV_API_KEY}",
              file=sys.stderr)
        return 1
    if ingest_config.source == ingest.SOURCE_ENV:
        log.info("Cardinal ingest: credentials from environment (%s, endpoint %s)",
                 ingest.ENV_API_KEY, connection.endpoint if connection else "?")
    else:
        ignored = ingest.resource_env_ignored()
        if ignored:
            log.warning("%s ignored: only used with %s and %s", ", ".join(ignored),
                        ingest.ENV_ENDPOINT, ingest.ENV_API_KEY)
    key, _ = resolve_devin_key(args.devin_key_file)
    if not key:
        print("error: no Devin key (set DEVIN_API_KEY or pass --devin-key-file)", file=sys.stderr)
        return 2
    try:
        devin = DevinClient(
            key, base_url=args.devin_base_url, org_id=args.devin_org_id, api_version=args.api_version,
            page_size=args.page_size, max_pages=args.max_pages,
        )
        state = PollState(paths.runtime_dir / STATE_FILE, readonly=args.dry_run)
    except (ValueError, StateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    github = GitHubClient(token, base_url=args.github_api_url) if token else None
    if github is None:
        log.info("GITHUB_TOKEN not set: git_state carries repo and PR only (no branch, head sha, initiative)")
    poller = Poller(
        devin=devin, state=state, connection=connection, connection_state=ingest_config.connection_state,
        github=github,
        options=PollOptions(
            lookback_days=args.lookback_days, active_ttl_days=args.active_ttl_days,
            state_retention_days=args.state_retention_days, max_state_sessions=args.max_state_sessions,
            retry_updated_after_filter=args.retry_updated_after_filter,
            emit_decisions=not args.no_decisions, emit_usage=not args.no_usage,
            dry_run=args.dry_run,
        ),
    )
    log.info("polling Devin %s API at %s", devin.api_version, devin.base_url)
    while True:
        failed = False
        try:
            result = poller.poll_once()
            log.info("poll: %s", result.summary())
            failed = bool(result.errors)
            for body in result.bodies:
                print(json.dumps(body, indent=2))
        except ApiError as exc:
            log.error("poll failed: %s", exc)
            failed = True
        if args.once:
            return 1 if failed else 0
        try:
            time.sleep(max(1, args.interval))
        except KeyboardInterrupt:
            return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    if args.command == "decision-schema":
        print(SCHEMA_PATH.read_text(encoding="utf-8"), end="")
        return 0
    if args.command == "connect":
        return run_connect(
            _paths(args), host=args.host, rotate=args.rotate, dry_run=args.dry_run,
            deployment_env=args.deployment_env,
        )
    if args.command == "status":
        _, source = resolve_devin_key(args.devin_key_file)
        return run_status(
            _paths(args), devin_key_source=source, devin_org_id=args.devin_org_id,
            devin_base_url=args.devin_base_url, api_version=args.api_version,
            github_token_set=bool(os.environ.get("GITHUB_TOKEN", "").strip()),
        )
    if args.command == "poll":
        try:
            return cmd_poll(args)
        except KeyboardInterrupt:
            return 0
    parser.print_help()
    return 2
