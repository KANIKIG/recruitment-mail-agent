from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class MailMessage:
    uid: int
    message_id: str
    subject: str
    sender_name: str
    sender_address: str
    received_at: datetime
    body: str


@dataclass(frozen=True)
class Classification:
    relevant: bool
    status: str
    company: str
    role: str
    confidence: float
    reason: str
    source_key: str
    deadline: str | None = None
    company_type: str | None = None
