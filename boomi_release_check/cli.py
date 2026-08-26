"""Command line entry point for verifying Boomi integration pack releases."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List, Optional, Sequence

from .checker import ReleaseVerifier
from .client import DEFAULT_BASE_URL, REGIONS, BoomiClient
from .discovery import resolve_sub_accounts
from .errors import BoomiError, ConfigError, ReleaseNotReady
from .report import render

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_ERROR = 2

LOGGER = logging.getLogger("boomi_release_check")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="boomi-release-check",
        description=(
            "Verify that the version released from a Boomi master account actually "
            "reached every OEM sub-account."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--request-id",
        default=os.environ.get("BOOMI_RELEASE_REQUEST_ID"),
        help="Release requestId returned by ReleaseIntegrationPack (env: BOOMI_RELEASE_REQUEST_ID)",
    )

    auth = parser.add_argument_group("authentication")
    auth.add_argument(
        "--account-id",
        default=os.environ.get("BOOMI_ACCOUNT_ID"),
        help="Master/parent Boomi account ID (env: BOOMI_ACCOUNT_ID)",
    )
    auth.add_argument(
        "--username",
        default=os.environ.get("BOOMI_USERNAME"),
        help="API username, e.g. BOOMI_TOKEN.user@example.com (env: BOOMI_USERNAME)",
    )
    auth.add_argument(
        "--token",
        default=os.environ.get("BOOMI_API_TOKEN") or os.environ.get("BOOMI_PASSWORD"),
        help="API token or password (env: BOOMI_API_TOKEN or BOOMI_PASSWORD)",
    )
    auth.add_argument(
        "--base-url",
        default=os.environ.get("BOOMI_BASE_URL", DEFAULT_BASE_URL),
        help="Platform API host (env: BOOMI_BASE_URL)",
    )
    auth.add_argument(
        "--region",
        choices=sorted(REGIONS),
        help="Shortcut for --base-url using a documented Boomi region host",
    )
    auth.add_argument(
        "--partner-api",
        action="store_true",
        help="Use /partner/api with ?overrideAccount= instead of the sub-account in the URL path",
    )

    accounts = parser.add_argument_group("sub-account selection")
    accounts.add_argument(
        "--sub-account",
        action="append",
        default=[],
        metavar="ACCOUNT_ID",
        help="Sub-account to check (repeatable)",
    )
    accounts.add_argument(
        "--sub-accounts-file",
        help="File of sub-accounts (.json, .csv or one account ID per line)",
    )
    accounts.add_argument(
        "--discover",
        action="store_true",
        help="Discover sub-accounts with the Account query API",
    )
    accounts.add_argument(
        "--include-inactive-accounts",
        action="store_true",
        help="Keep suspended/expired accounts found by --discover",
    )
    accounts.add_argument(
        "--exclude-account",
        action="append",
        default=[],
        metavar="ACCOUNT_ID",
        help="Sub-account to skip (repeatable)",
    )

    checks = parser.add_argument_group("comparison")
    checks.add_argument(
        "--component",
        action="append",
        default=[],
        metavar="COMPONENT_ID",
        help="Only verify these released component IDs (repeatable)",
    )
    checks.add_argument(
        "--strict-version",
        action="store_true",
        help='Require an exact version string match ("1.0" will not equal "1.00")',
    )
    checks.add_argument(
        "--check-instances",
        action="store_true",
        help="Query IntegrationPackInstance first to distinguish not-installed from not-deployed",
    )
    checks.add_argument(
        "--include-inactive-deployments",
        action="store_true",
        help="Include deployments where active is false",
    )

    polling = parser.add_argument_group("release polling")
    polling.add_argument(
        "--wait",
        action="store_true",
        help="Poll ReleaseIntegrationPackStatus while it returns HTTP 202",
    )
    polling.add_argument("--poll-interval", type=float, default=15.0, help="Seconds between polls")
    polling.add_argument("--poll-timeout", type=float, default=900.0, help="Maximum seconds to poll")

    output = parser.add_argument_group("output")
    output.add_argument(
        "--format",
        dest="output_format",
        choices=["table", "json", "csv", "markdown"],
        default="table",
        help="Report format",
    )
    output.add_argument("--output", help="Write the report to a file instead of stdout")
    output.add_argument(
        "--detailed",
        action="store_true",
        help="Include the per-component and per-deployment breakdown",
    )
    output.add_argument(
        "--no-fail-on-drift",
        action="store_true",
        help="Always exit 0 when the run completes, even if sub-accounts are behind",
    )

    runtime = parser.add_argument_group("runtime")
    runtime.add_argument("--max-workers", type=int, default=8, help="Parallel sub-account checks")
    runtime.add_argument("--timeout", type=float, default=60.0, help="Per-request timeout in seconds")
    runtime.add_argument("--max-retries", type=int, default=4, help="Retries for 429/5xx responses")
    runtime.add_argument("-v", "--verbose", action="count", default=0, help="Increase log verbosity")

    return parser


def _configure_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)


def _validate(args: argparse.Namespace) -> None:
    missing: List[str] = []
    if not args.request_id:
        missing.append("--request-id")
    if not args.account_id:
        missing.append("--account-id")
    if not args.username:
        missing.append("--username")
    if not args.token:
        missing.append("--token")
    if missing:
        raise ConfigError("Missing required options: " + ", ".join(missing))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    try:
        _validate(args)
        base_url = REGIONS[args.region] if args.region else args.base_url
        client = BoomiClient(
            account_id=args.account_id,
            username=args.username,
            password=args.token,
            base_url=base_url,
            partner_api=args.partner_api,
            timeout=args.timeout,
            max_retries=args.max_retries,
        )
        accounts = resolve_sub_accounts(
            client,
            explicit=args.sub_account,
            file_path=args.sub_accounts_file,
            discover=args.discover,
            exclude=args.exclude_account,
            only_active=not args.include_inactive_accounts,
        )
        LOGGER.info("Verifying %d sub-account(s)", len(accounts))

        verifier = ReleaseVerifier(
            client,
            strict_version=args.strict_version,
            check_instances=args.check_instances,
            include_inactive=args.include_inactive_deployments,
            max_workers=args.max_workers,
            poll_interval=args.poll_interval,
            poll_timeout=args.poll_timeout,
        )
        report = verifier.verify(
            args.request_id,
            accounts,
            wait=args.wait,
            component_ids=args.component,
        )
    except ReleaseNotReady as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("hint: pass --wait to poll until the release finishes", file=sys.stderr)
        return EXIT_ERROR
    except (BoomiError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    rendered = render(report, args.output_format, detailed=args.detailed)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(rendered if rendered.endswith("\n") else rendered + "\n")
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(rendered)

    if report.has_errors:
        return EXIT_ERROR
    if report.has_drift and not args.no_fail_on_drift:
        return EXIT_DRIFT
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
