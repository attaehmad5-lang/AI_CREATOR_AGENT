"""Normalized error hierarchy."""
from __future__ import annotations


class AdvpsError(Exception):
    code = "error"
    retryable = False


class ValidationError(AdvpsError):
    code = "validation_error"


class SecurityError(AdvpsError):
    code = "security_error"


class CredentialUnavailableError(AdvpsError):
    code = "credential_unavailable"


class CredentialExpiredError(AdvpsError):
    code = "credential_expired"


class StoreCorruptError(AdvpsError):
    code = "store_corrupt"


class JobCancelledError(AdvpsError):
    code = "job_cancelled"


class NotFoundError(AdvpsError):
    code = "not_found"


class PriceUnknownError(AdvpsError):
    code = "price_unknown"


class PaidUsageNotApprovedError(AdvpsError):
    code = "paid_usage_not_approved"


class QuotaExceededError(AdvpsError):
    code = "quota_exceeded"


class BudgetExceededError(AdvpsError):
    code = "budget_exceeded"


class CircuitOpenError(AdvpsError):
    code = "circuit_open"
    retryable = True


class NoProviderError(AdvpsError):
    code = "no_provider"


class ProviderError(AdvpsError):
    """Error raised by a provider call. `retryable` says whether a retry is safe."""

    code = "provider_error"

    def __init__(self, message: str, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class RateLimitError(ProviderError):
    code = "rate_limited"

    def __init__(self, message: str = "rate limited", retry_after: float | None = None):
        super().__init__(message, retryable=True, status=429)
        self.retry_after = retry_after


class AmbiguousOutcomeError(ProviderError):
    """Request may have reached the provider (and been billed). Never retried blindly."""

    code = "ambiguous_outcome"

    def __init__(self, message: str = "ambiguous outcome"):
        super().__init__(message, retryable=False)
