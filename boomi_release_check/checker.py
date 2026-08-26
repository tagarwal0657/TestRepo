"""Compare a parent-account release against what each sub-account runs.

The flow mirrors the Boomi Platform API contract:

1. ``GET /ReleaseIntegrationPackStatus/{requestId}`` on the parent account gives
   the overall ``releaseStatus`` and the ``componentId``/``releasedVersion``
   pairs that the release pushed.
2. ``POST /DeployedPackage/query`` runs in each sub-account (account override)
   filtered by those component IDs with ``active = true``.
3. The sub-account's ``packageVersion`` is compared against ``releasedVersion``.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence

from .client import BoomiClient
from .errors import BoomiError, ReleaseNotReady
from .models import (
    AccountResult,
    AccountStatus,
    CheckStatus,
    ComponentCheck,
    DeployedPackage,
    ReleaseInfo,
    ReleaseStatus,
    ReleasedComponent,
    SubAccount,
    VerificationReport,
)
from .versions import describe_drift, versions_equal

LOGGER = logging.getLogger("boomi_release_check.checker")


def _newest_first(deployment: DeployedPackage) -> str:
    return deployment.deployed_date or ""


class ReleaseVerifier:
    """Runs the parent release lookup and the per-sub-account comparison."""

    def __init__(
        self,
        client: BoomiClient,
        *,
        strict_version: bool = False,
        check_instances: bool = False,
        include_inactive: bool = False,
        max_workers: int = 8,
        poll_interval: float = 15.0,
        poll_timeout: float = 900.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.strict_version = strict_version
        self.check_instances = check_instances
        self.include_inactive = include_inactive
        self.max_workers = max(1, max_workers)
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self._sleep = sleep
        self._clock = clock

    # ------------------------------------------------------------------
    # Step 1 - parent side
    # ------------------------------------------------------------------
    def fetch_release(self, request_id: str, *, wait: bool = False) -> ReleaseInfo:
        """Read the release status, optionally polling while Boomi returns 202."""
        deadline = self._clock() + self.poll_timeout
        while True:
            status_code, payload = self.client.get_release_status_raw(request_id)
            release = (
                ReleaseInfo.from_api(payload, request_id)
                if payload
                else ReleaseInfo(request_id=request_id, release_status=ReleaseStatus.IN_PROGRESS)
            )
            if status_code == 200 and not release.is_pending:
                return release

            # A scheduled release has not pushed anything yet; waiting for a
            # future calendar date is not something this run should block on.
            if release.release_status == ReleaseStatus.SCHEDULED:
                raise ReleaseNotReady(request_id, ReleaseStatus.SCHEDULED, release.release_on_date or "")
            if not wait:
                raise ReleaseNotReady(
                    request_id, release.release_status, release.release_progress or ""
                )
            if self._clock() >= deadline:
                raise ReleaseNotReady(
                    request_id, release.release_status, release.release_progress or ""
                )
            LOGGER.info(
                "Release %s still %s (%s); polling again in %.0fs",
                request_id,
                release.release_status,
                release.release_progress or "no progress reported",
                self.poll_interval,
            )
            self._sleep(self.poll_interval)

    # ------------------------------------------------------------------
    # Step 2 and 3 - sub-account side
    # ------------------------------------------------------------------
    def check_account(
        self,
        account: SubAccount,
        components: Sequence[ReleasedComponent],
        *,
        integration_pack_id: Optional[str] = None,
    ) -> AccountResult:
        """Compare one sub-account's deployed packages against the release."""
        instance_ids: List[str] = []
        try:
            if self.check_instances and integration_pack_id:
                instances = self.client.query_integration_pack_instances(
                    integration_pack_id, account=account.account_id
                )
                instance_ids = [str(entry.get("id")) for entry in instances if entry.get("id")]
                if not instances:
                    return AccountResult(
                        account=account,
                        status=AccountStatus.NOT_INSTALLED,
                        checks=[
                            ComponentCheck(
                                component_id=component.component_id,
                                expected_version=component.released_version,
                                status=CheckStatus.NOT_DEPLOYED,
                                detail="Integration pack instance not found in this sub-account",
                            )
                            for component in components
                        ],
                        instance_ids=(),
                    )

            checks = [self._check_component(account, component) for component in components]
        except BoomiError as exc:
            LOGGER.warning("Sub-account %s failed: %s", account.account_id, exc)
            return AccountResult(
                account=account,
                status=AccountStatus.ERROR,
                checks=[],
                instance_ids=tuple(instance_ids),
                error=str(exc),
            )

        return AccountResult(
            account=account,
            status=self._roll_up(checks),
            checks=checks,
            instance_ids=tuple(instance_ids),
        )

    def _check_component(self, account: SubAccount, component: ReleasedComponent) -> ComponentCheck:
        raw = self.client.query_deployed_packages(
            component.component_id,
            account=account.account_id,
            active_only=not self.include_inactive,
        )
        deployments = [DeployedPackage.from_api(entry) for entry in raw]
        if not self.include_inactive:
            deployments = [d for d in deployments if d.active]
        deployments.sort(key=_newest_first, reverse=True)

        if not deployments:
            return ComponentCheck(
                component_id=component.component_id,
                expected_version=component.released_version,
                status=CheckStatus.NOT_DEPLOYED,
                detail="No active DeployedPackage found for this component",
                deployments=(),
            )

        matching = [
            d
            for d in deployments
            if versions_equal(component.released_version, d.package_version, self.strict_version)
        ]
        if matching:
            newest = matching[0]
            detail = None
            if len(deployments) > len(matching):
                other = sorted({d.package_version or "?" for d in deployments if d not in matching})
                detail = (
                    f"{len(matching)} of {len(deployments)} active deployments on the released "
                    f"version; also deployed: {', '.join(other)}"
                )
            return ComponentCheck(
                component_id=component.component_id,
                expected_version=component.released_version,
                deployed_version=newest.package_version,
                deployed_component_version=newest.component_version,
                status=CheckStatus.MATCH,
                detail=detail,
                deployments=tuple(deployments),
            )

        newest = deployments[0]
        return ComponentCheck(
            component_id=component.component_id,
            expected_version=component.released_version,
            deployed_version=newest.package_version,
            deployed_component_version=newest.component_version,
            status=CheckStatus.MISMATCH,
            drift=describe_drift(component.released_version, newest.package_version),
            detail=(
                f"Deployed packageVersion {newest.package_version!r} does not match released "
                f"version {component.released_version!r}"
            ),
            deployments=tuple(deployments),
        )

    @staticmethod
    def _roll_up(checks: Sequence[ComponentCheck]) -> str:
        if not checks:
            return AccountStatus.NOT_DEPLOYED
        statuses = {check.status for check in checks}
        if statuses == {CheckStatus.MATCH}:
            return AccountStatus.UP_TO_DATE
        if statuses == {CheckStatus.NOT_DEPLOYED}:
            return AccountStatus.NOT_DEPLOYED
        if CheckStatus.ERROR in statuses:
            return AccountStatus.ERROR
        if CheckStatus.MATCH in statuses:
            return AccountStatus.PARTIAL
        return AccountStatus.OUT_OF_DATE

    # ------------------------------------------------------------------
    # Full run
    # ------------------------------------------------------------------
    def verify(
        self,
        request_id: str,
        accounts: Sequence[SubAccount],
        *,
        wait: bool = False,
        component_ids: Optional[Sequence[str]] = None,
    ) -> VerificationReport:
        release = self.fetch_release(request_id, wait=wait)
        components = list(release.components)
        if component_ids:
            wanted = {value.strip() for value in component_ids if value.strip()}
            components = [c for c in components if c.component_id in wanted]

        results: List[AccountResult] = []
        if components and accounts:
            worker_count = min(self.max_workers, len(accounts))
            if worker_count > 1:
                with ThreadPoolExecutor(max_workers=worker_count) as pool:
                    results = list(
                        pool.map(
                            lambda account: self.check_account(
                                account,
                                components,
                                integration_pack_id=release.integration_pack_id,
                            ),
                            accounts,
                        )
                    )
            else:
                results = [
                    self.check_account(
                        account, components, integration_pack_id=release.integration_pack_id
                    )
                    for account in accounts
                ]
        elif accounts:
            results = [
                AccountResult(
                    account=account,
                    status=AccountStatus.ERROR,
                    error="Release returned no packaged components to verify",
                )
                for account in accounts
            ]

        return VerificationReport(
            release=ReleaseInfo(
                request_id=release.request_id,
                release_status=release.release_status,
                integration_pack_id=release.integration_pack_id,
                name=release.name,
                installation_type=release.installation_type,
                release_schedule=release.release_schedule,
                release_on_date=release.release_on_date,
                release_progress=release.release_progress,
                components=tuple(components),
            ),
            accounts=results,
            generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        )


def summarize_counts(report: VerificationReport) -> Dict[str, int]:
    """Convenience wrapper used by the reporters and the CLI exit code."""
    return report.summary()
