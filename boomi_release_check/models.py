"""Dataclasses describing the Boomi objects and the verification outcome."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip()
    return text or None


def as_list(value: Any) -> List[Any]:
    """Boomi returns a bare object when a collection holds a single entry."""
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


class ReleaseStatus:
    IN_PROGRESS = "IN_PROGRESS"
    SUCCESS = "SUCCESS"
    SCHEDULED = "SCHEDULED"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"

    PENDING = frozenset({IN_PROGRESS, SCHEDULED})


class CheckStatus:
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    NOT_DEPLOYED = "NOT_DEPLOYED"
    ERROR = "ERROR"


class AccountStatus:
    UP_TO_DATE = "UP_TO_DATE"
    OUT_OF_DATE = "OUT_OF_DATE"
    PARTIAL = "PARTIAL"
    NOT_DEPLOYED = "NOT_DEPLOYED"
    NOT_INSTALLED = "NOT_INSTALLED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class SubAccount:
    """A sub-account (OEM customer tenant) to verify."""

    account_id: str
    name: Optional[str] = None

    @property
    def label(self) -> str:
        return f"{self.name} ({self.account_id})" if self.name else self.account_id

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> "SubAccount":
        return cls(
            account_id=_text(payload.get("accountId")) or "",
            name=_text(payload.get("name")),
        )


@dataclass(frozen=True)
class ReleasedComponent:
    """One entry of ReleasePackagedComponents.ReleasePackagedComponent."""

    component_id: str
    released_version: Optional[str]
    component_version: Optional[str] = None

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> "ReleasedComponent":
        return cls(
            component_id=_text(payload.get("componentId")) or "",
            released_version=_text(payload.get("releasedVersion")),
            component_version=_text(payload.get("version")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "componentId": self.component_id,
            "releasedVersion": self.released_version,
            "componentVersion": self.component_version,
        }


@dataclass(frozen=True)
class ReleaseInfo:
    """Parsed GET /ReleaseIntegrationPackStatus/{requestId} response."""

    request_id: str
    release_status: str
    integration_pack_id: Optional[str] = None
    name: Optional[str] = None
    installation_type: Optional[str] = None
    release_schedule: Optional[str] = None
    release_on_date: Optional[str] = None
    release_progress: Optional[str] = None
    components: Sequence[ReleasedComponent] = field(default_factory=tuple)

    @property
    def is_pending(self) -> bool:
        return self.release_status in ReleaseStatus.PENDING

    @classmethod
    def from_api(cls, payload: Mapping[str, Any], request_id: str = "") -> "ReleaseInfo":
        container = payload.get("ReleasePackagedComponents") or {}
        raw_components = as_list(container.get("ReleasePackagedComponent"))
        components = tuple(
            ReleasedComponent.from_api(entry)
            for entry in raw_components
            if isinstance(entry, Mapping) and _text(entry.get("componentId"))
        )
        return cls(
            request_id=_text(payload.get("requestId")) or request_id,
            release_status=_text(payload.get("releaseStatus")) or ReleaseStatus.UNKNOWN,
            integration_pack_id=_text(payload.get("integrationPackId")),
            name=_text(payload.get("name")),
            installation_type=_text(payload.get("installationType")),
            release_schedule=_text(payload.get("releaseSchedule")),
            release_on_date=_text(payload.get("releaseOnDate")),
            release_progress=_text(payload.get("releaseProgress")),
            components=components,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requestId": self.request_id,
            "releaseStatus": self.release_status,
            "integrationPackId": self.integration_pack_id,
            "name": self.name,
            "installationType": self.installation_type,
            "releaseSchedule": self.release_schedule,
            "releaseOnDate": self.release_on_date,
            "releaseProgress": self.release_progress,
            "components": [component.to_dict() for component in self.components],
        }


@dataclass(frozen=True)
class DeployedPackage:
    """One entry of a POST /DeployedPackage/query result."""

    component_id: str
    package_version: Optional[str] = None
    component_version: Optional[str] = None
    package_id: Optional[str] = None
    deployment_id: Optional[str] = None
    environment_id: Optional[str] = None
    component_type: Optional[str] = None
    deployed_date: Optional[str] = None
    deployed_by: Optional[str] = None
    branch_name: Optional[str] = None
    active: bool = False

    @classmethod
    def from_api(cls, payload: Mapping[str, Any]) -> "DeployedPackage":
        active = payload.get("active")
        if isinstance(active, str):
            active = active.strip().casefold() == "true"
        return cls(
            component_id=_text(payload.get("componentId")) or "",
            package_version=_text(payload.get("packageVersion")),
            component_version=_text(payload.get("componentVersion")),
            package_id=_text(payload.get("packageId")),
            deployment_id=_text(payload.get("deploymentId")),
            environment_id=_text(payload.get("environmentId")),
            component_type=_text(payload.get("componentType")),
            deployed_date=_text(payload.get("deployedDate")),
            deployed_by=_text(payload.get("deployedBy")),
            branch_name=_text(payload.get("branchName")),
            active=bool(active),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "componentId": self.component_id,
            "packageVersion": self.package_version,
            "componentVersion": self.component_version,
            "packageId": self.package_id,
            "deploymentId": self.deployment_id,
            "environmentId": self.environment_id,
            "componentType": self.component_type,
            "deployedDate": self.deployed_date,
            "deployedBy": self.deployed_by,
            "branchName": self.branch_name,
            "active": self.active,
        }


@dataclass
class ComponentCheck:
    """Comparison of one released component against one sub-account."""

    component_id: str
    expected_version: Optional[str]
    status: str
    deployed_version: Optional[str] = None
    deployed_component_version: Optional[str] = None
    drift: Optional[str] = None
    detail: Optional[str] = None
    deployments: Sequence[DeployedPackage] = field(default_factory=tuple)

    @property
    def environment_ids(self) -> List[str]:
        return [d.environment_id for d in self.deployments if d.environment_id]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "componentId": self.component_id,
            "expectedVersion": self.expected_version,
            "deployedVersion": self.deployed_version,
            "deployedComponentVersion": self.deployed_component_version,
            "status": self.status,
            "drift": self.drift,
            "detail": self.detail,
            "environmentIds": self.environment_ids,
            "deployments": [d.to_dict() for d in self.deployments],
        }


@dataclass
class AccountResult:
    """Verification outcome for a single sub-account."""

    account: SubAccount
    status: str
    checks: List[ComponentCheck] = field(default_factory=list)
    instance_ids: Sequence[str] = field(default_factory=tuple)
    error: Optional[str] = None

    @property
    def account_id(self) -> str:
        return self.account.account_id

    @property
    def is_up_to_date(self) -> bool:
        return self.status == AccountStatus.UP_TO_DATE

    def counts(self) -> Dict[str, int]:
        totals: Dict[str, int] = {}
        for check in self.checks:
            totals[check.status] = totals.get(check.status, 0) + 1
        return totals

    def to_dict(self) -> Dict[str, Any]:
        return {
            "accountId": self.account.account_id,
            "accountName": self.account.name,
            "status": self.status,
            "error": self.error,
            "instanceIds": list(self.instance_ids),
            "componentCounts": self.counts(),
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass
class VerificationReport:
    """Full parent-plus-sub-account verification result."""

    release: ReleaseInfo
    accounts: List[AccountResult] = field(default_factory=list)
    generated_at: Optional[str] = None

    def summary(self) -> Dict[str, int]:
        totals = {
            AccountStatus.UP_TO_DATE: 0,
            AccountStatus.PARTIAL: 0,
            AccountStatus.OUT_OF_DATE: 0,
            AccountStatus.NOT_DEPLOYED: 0,
            AccountStatus.NOT_INSTALLED: 0,
            AccountStatus.ERROR: 0,
        }
        for result in self.accounts:
            totals[result.status] = totals.get(result.status, 0) + 1
        totals["TOTAL"] = len(self.accounts)
        return totals

    @property
    def has_drift(self) -> bool:
        return any(not result.is_up_to_date for result in self.accounts)

    @property
    def has_errors(self) -> bool:
        return any(result.status == AccountStatus.ERROR for result in self.accounts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generatedAt": self.generated_at,
            "release": self.release.to_dict(),
            "summary": self.summary(),
            "accounts": [result.to_dict() for result in self.accounts],
        }
