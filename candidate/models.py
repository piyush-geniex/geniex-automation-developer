"""
Domain models for the price intelligence platform.

Abbreviations used in this module:
  - ttl: time-to-live (seconds)
  - cf:  Cloudflare
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ProxyStatus(str, Enum):
    HEALTHY = "HEALTHY"
    COOLING_DOWN = "COOLING_DOWN"
    BANNED = "BANNED"
    RETIRED = "RETIRED"


class ProxyType(str, Enum):
    RESIDENTIAL = "residential"
    DATACENTER = "datacenter"
    MOBILE = "mobile"


class CampaignStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PAUSED = "PAUSED"


class JobStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"    # terminal: parse succeeded
    FAILED = "FAILED"          # terminal: unrecoverable fetch or classification error
    EXHAUSTED = "EXHAUSTED"    # terminal: retry budget depleted
    RETRYING = "RETRYING"      # transient: scheduled for retry


class ErrorType(str, Enum):
    """
    Classification of errors observed during job execution.

    PROXY_BANNED covers all cases where the request was rejected at the
    network or identity layer and the remediation is proxy rotation.
    This includes 403 responses from the target site regardless of the
    specific reason encoded in the response body.
    """
    PROXY_BANNED = "PROXY_BANNED"      # 403 — proxy or identity rejected
    RATE_LIMITED = "RATE_LIMITED"      # 429 — too many requests from this IP
    NOT_FOUND = "NOT_FOUND"            # 404 — URL no longer valid
    SERVER_ERROR = "SERVER_ERROR"      # 5xx — target site error
    NETWORK_ERROR = "NETWORK_ERROR"    # connection-level failure
    PROVIDER_ERROR = "PROVIDER_ERROR"  # CAPTCHA provider failure
    UNKNOWN = "UNKNOWN"


class RemediationAction(str, Enum):
    ROTATE_PROXY = "rotate_proxy"
    RESOLVE_CAPTCHA = "resolve_captcha"
    BACKOFF = "backoff"
    SKIP = "skip"
    RETRY = "retry"
    ABORT = "abort"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass
class ParseResult:
    """
    Output from PriceParser.parse().

    success=True with price=None indicates a structurally valid page
    where the SKU has no currently listed price (unlisted, pre-release,
    or temporarily out of range). This is valid business data, not a failure.

    success=False indicates a structural parse failure (malformed HTML,
    response too small, or an exception during parsing).
    """
    success: bool
    price: Optional[float]
    available: Optional[bool]
    currency: Optional[str] = None
    raw_html_size: int = 0
    parse_duration_ms: float = 0.0


@dataclass
class JobResult:
    job_id: str
    status: JobStatus
    parse_result: Optional[ParseResult]
    error_type: Optional[ErrorType]
    proxy_id: Optional[str]
    completed_at: str


@dataclass
class ErrorEvent:
    job_id: str
    error_type: ErrorType
    http_status: Optional[int]
    remediation: RemediationAction
    occurred_at: str
    detail: str = ""


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


@dataclass
class Proxy:
    id: str
    host: str
    port: int
    username: str
    password: str
    proxy_type: ProxyType
    country: str
    status: ProxyStatus
    sticky_until: Optional[str]            # ISO 8601 UTC — None if unassigned
    assigned_campaign_id: Optional[str]
    last_used_at: Optional[str]
    ban_count: int = 0
    success_count: int = 0
    # Tracks back-to-back failures within the active session window.
    # Used by the pool to skip proxies that are repeatedly failing.
    consecutive_failures: int = 0

    def is_sticky_active(self) -> bool:
        """Return True if this proxy is still within its stickiness window."""
        if self.sticky_until is None:
            return False
        expiry = datetime.fromisoformat(self.sticky_until.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) < expiry


@dataclass
class Job:
    id: str
    campaign_id: str
    url: str
    status: JobStatus
    retry_count: int
    # Counts every solve attempt regardless of outcome — provider errors,
    # invalid tokens, and successful solves all consume from this budget.
    captcha_solves_used: int
    assigned_proxy_id: Optional[str]
    result: Optional[JobResult]
    created_at: str
    updated_at: str
    error_log: list = field(default_factory=list)

    def mark_completed(self, parse_result: ParseResult) -> None:
        self.status = JobStatus.COMPLETED
        self.result = JobResult(
            job_id=self.id,
            status=JobStatus.COMPLETED,
            parse_result=parse_result,
            error_type=None,
            proxy_id=self.assigned_proxy_id,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def mark_failed(self, error_type: ErrorType) -> None:
        self.status = JobStatus.FAILED
        self.result = JobResult(
            job_id=self.id,
            status=JobStatus.FAILED,
            parse_result=None,
            error_type=error_type,
            proxy_id=self.assigned_proxy_id,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def mark_exhausted(self) -> None:
        self.status = JobStatus.EXHAUSTED
        self.updated_at = datetime.now(timezone.utc).isoformat()


@dataclass
class Campaign:
    id: str
    name: str
    status: CampaignStatus
    target_urls: list
    jobs: list = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    completed_at: Optional[str] = None

    def success_rate(self) -> float:
        """
        Returns the fraction of jobs that reached COMPLETED status.
        EXHAUSTED and FAILED jobs count as failures.
        """
        if not self.jobs:
            return 0.0
        completed = [j for j in self.jobs if j.status == JobStatus.COMPLETED]
        return len(completed) / len(self.jobs)

    def price_coverage(self) -> float:
        """
        Returns the fraction of COMPLETED jobs that have a non-null price.
        """
        completed = [j for j in self.jobs if j.status == JobStatus.COMPLETED]
        if not completed:
            return 0.0
        with_price = [
            j for j in completed
            if j.result and j.result.parse_result and j.result.parse_result.price is not None
        ]
        return len(with_price) / len(completed)
