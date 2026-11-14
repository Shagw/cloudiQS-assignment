"""Input/output schemas for the Risk Monitor agent."""

from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class Transaction(BaseModel):
    transaction_id: str = Field(min_length=1, max_length=64)
    customer_id: str = Field(min_length=1, max_length=64)
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    merchant: str = Field(min_length=1, max_length=200)
    timestamp: datetime
    currency: str = Field(default="USD", min_length=3, max_length=3)

    @field_validator("timestamp")
    @classmethod
    def require_timezone(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp must include a timezone offset (e.g. 2026-11-14T09:00:00Z)")
        return v


class RiskRequest(BaseModel):
    transactions: list[Transaction] = Field(min_length=1, max_length=500)

    @field_validator("transactions")
    @classmethod
    def unique_ids(cls, v: list[Transaction]) -> list[Transaction]:
        ids = [t.transaction_id for t in v]
        if len(ids) != len(set(ids)):
            raise ValueError("transaction_id values must be unique")
        return v


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Alert(BaseModel):
    transaction_id: str
    customer_id: str
    risk_level: RiskLevel
    reasons: list[str]
    webhook_delivered: bool
    webhook_status: int | None = None
    webhook_error: str | None = None


class RiskReport(BaseModel):
    transactions_reviewed: int
    flagged_count: int
    alerts: list[Alert]
    summary: str
