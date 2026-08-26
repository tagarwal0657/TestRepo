"""Render a :class:`VerificationReport` as text, JSON, CSV or Markdown."""

from __future__ import annotations

import csv
import io
import json
from typing import Callable, Dict, List, Sequence

from .models import AccountStatus, VerificationReport

STATUS_ORDER = [
    AccountStatus.UP_TO_DATE,
    AccountStatus.PARTIAL,
    AccountStatus.OUT_OF_DATE,
    AccountStatus.NOT_DEPLOYED,
    AccountStatus.NOT_INSTALLED,
    AccountStatus.ERROR,
]


def _render_rows(rows: Sequence[Sequence[str]], headers: Sequence[str]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    divider = "  ".join("-" * width for width in widths)
    lines = ["  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)), divider]
    for row in rows:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return "\n".join(lines)


def render_table(report: VerificationReport, *, detailed: bool = False) -> str:
    release = report.release
    summary = report.summary()
    header_lines = [
        "Boomi integration pack release verification",
        "=" * 44,
        f"Request ID       : {release.request_id}",
        f"Integration pack : {release.name or '-'} ({release.integration_pack_id or '-'})",
        f"Release status   : {release.release_status}"
        + (f" ({release.release_progress}%)" if release.release_progress else ""),
        f"Installation type: {release.installation_type or '-'}",
        f"Generated at     : {report.generated_at or '-'}",
        "",
        "Released components",
        "-" * 44,
    ]
    component_rows = [
        [component.component_id, component.released_version or "-", component.component_version or "-"]
        for component in release.components
    ]
    if component_rows:
        header_lines.append(
            _render_rows(component_rows, ["COMPONENT ID", "RELEASED VERSION", "COMPONENT VERSION"])
        )
    else:
        header_lines.append("(none reported by ReleaseIntegrationPackStatus)")

    account_rows: List[List[str]] = []
    for result in report.accounts:
        counts = result.counts()
        detail = ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))
        if result.error:
            detail = result.error
        account_rows.append(
            [
                result.account.account_id,
                result.account.name or "-",
                result.status,
                detail or "-",
            ]
        )

    body = [
        "",
        "Sub-account results",
        "-" * 44,
        _render_rows(account_rows, ["ACCOUNT ID", "NAME", "STATUS", "DETAIL"])
        if account_rows
        else "(no sub-accounts checked)",
    ]

    if detailed:
        body.extend(["", "Per-component detail", "-" * 44])
        detail_rows: List[List[str]] = []
        for result in report.accounts:
            for check in result.checks:
                detail_rows.append(
                    [
                        result.account.account_id,
                        check.component_id,
                        check.expected_version or "-",
                        check.deployed_version or "-",
                        check.status,
                        check.drift or "-",
                        ", ".join(check.environment_ids) or "-",
                    ]
                )
        body.append(
            _render_rows(
                detail_rows,
                ["ACCOUNT ID", "COMPONENT ID", "EXPECTED", "DEPLOYED", "STATUS", "DRIFT", "ENVIRONMENTS"],
            )
            if detail_rows
            else "(no component checks ran)"
        )

    summary_line = "  ".join(
        f"{status}={summary.get(status, 0)}" for status in STATUS_ORDER if summary.get(status, 0)
    )
    body.extend(
        [
            "",
            "Summary",
            "-" * 44,
            f"Sub-accounts checked: {summary.get('TOTAL', 0)}",
            summary_line or "no results",
        ]
    )
    return "\n".join(header_lines + body)


def render_json(report: VerificationReport, *, detailed: bool = False) -> str:
    payload = report.to_dict()
    if not detailed:
        for account in payload.get("accounts", []):
            for check in account.get("checks", []):
                check.pop("deployments", None)
    return json.dumps(payload, indent=2, sort_keys=False)


def render_csv(report: VerificationReport, *, detailed: bool = False) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "requestId",
            "integrationPackId",
            "releaseStatus",
            "accountId",
            "accountName",
            "accountStatus",
            "componentId",
            "expectedVersion",
            "deployedVersion",
            "deployedComponentVersion",
            "componentStatus",
            "drift",
            "environmentIds",
            "detail",
        ]
    )
    release = report.release
    for result in report.accounts:
        if not result.checks:
            writer.writerow(
                [
                    release.request_id,
                    release.integration_pack_id or "",
                    release.release_status,
                    result.account.account_id,
                    result.account.name or "",
                    result.status,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    result.error or "",
                ]
            )
            continue
        for check in result.checks:
            writer.writerow(
                [
                    release.request_id,
                    release.integration_pack_id or "",
                    release.release_status,
                    result.account.account_id,
                    result.account.name or "",
                    result.status,
                    check.component_id,
                    check.expected_version or "",
                    check.deployed_version or "",
                    check.deployed_component_version or "",
                    check.status,
                    check.drift or "",
                    ";".join(check.environment_ids),
                    check.detail or "",
                ]
            )
    return buffer.getvalue()


def render_markdown(report: VerificationReport, *, detailed: bool = False) -> str:
    release = report.release
    summary = report.summary()
    lines = [
        f"# Release verification - {release.name or release.integration_pack_id or release.request_id}",
        "",
        f"- **Request ID:** `{release.request_id}`",
        f"- **Integration pack ID:** `{release.integration_pack_id or '-'}`",
        f"- **Release status:** `{release.release_status}`",
        f"- **Installation type:** `{release.installation_type or '-'}`",
        f"- **Generated at:** {report.generated_at or '-'}",
        "",
        "## Released components",
        "",
        "| Component ID | Released version | Component version |",
        "| --- | --- | --- |",
    ]
    for component in release.components:
        lines.append(
            f"| `{component.component_id}` | `{component.released_version or '-'}` | "
            f"`{component.component_version or '-'}` |"
        )

    lines.extend(
        [
            "",
            "## Sub-accounts",
            "",
            "| Account ID | Name | Status | Detail |",
            "| --- | --- | --- | --- |",
        ]
    )
    for result in report.accounts:
        counts = result.counts()
        detail = result.error or ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "-"
        lines.append(
            f"| `{result.account.account_id}` | {result.account.name or '-'} | "
            f"**{result.status}** | {detail} |"
        )

    if detailed:
        lines.extend(
            [
                "",
                "## Component detail",
                "",
                "| Account ID | Component ID | Expected | Deployed | Status | Drift |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for result in report.accounts:
            for check in result.checks:
                lines.append(
                    f"| `{result.account.account_id}` | `{check.component_id}` | "
                    f"`{check.expected_version or '-'}` | `{check.deployed_version or '-'}` | "
                    f"{check.status} | {check.drift or '-'} |"
                )

    lines.extend(["", "## Summary", ""])
    lines.append(f"- Sub-accounts checked: **{summary.get('TOTAL', 0)}**")
    for status in STATUS_ORDER:
        count = summary.get(status, 0)
        if count:
            lines.append(f"- {status}: **{count}**")
    return "\n".join(lines) + "\n"


RENDERERS: Dict[str, Callable[..., str]] = {
    "table": render_table,
    "json": render_json,
    "csv": render_csv,
    "markdown": render_markdown,
}


def render(report: VerificationReport, output_format: str, *, detailed: bool = False) -> str:
    try:
        renderer = RENDERERS[output_format]
    except KeyError:
        raise ValueError(f"Unsupported output format: {output_format}") from None
    return renderer(report, detailed=detailed)
