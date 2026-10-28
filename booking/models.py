"""Typed models for the booking core. Nothing here knows about LLMs or tiers."""

from __future__ import annotations

from datetime import date, datetime, time
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

CONFIG_PATH = Path(__file__).with_name("config.yaml")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class ErrorCode(StrEnum):
    MISSING_FIELD = "MISSING_FIELD"
    INVALID_INPUT = "INVALID_INPUT"
    UNKNOWN_SERVICE = "UNKNOWN_SERVICE"
    PAST_DATE = "PAST_DATE"
    TOO_FAR_AHEAD = "TOO_FAR_AHEAD"
    CLOSED = "CLOSED"
    OUTSIDE_HOURS = "OUTSIDE_HOURS"
    OFF_GRID = "OFF_GRID"
    CONFLICT = "CONFLICT"
    WRITE_FAILED = "WRITE_FAILED"
    WRITE_NOT_VERIFIED = "WRITE_NOT_VERIFIED"
    NOT_CONFIRMED = "NOT_CONFIRMED"
    CONFIRMATION_INVALID = "CONFIRMATION_INVALID"
    CONFIRMATION_EXPIRED = "CONFIRMATION_EXPIRED"
    CONFIRMATION_USED = "CONFIRMATION_USED"
    CONFIRMATION_MISMATCH = "CONFIRMATION_MISMATCH"


class ServiceConfig(BaseModel):
    label: str
    duration_minutes: int = Field(gt=0)
    aliases: list[str] = []


class BusinessConfig(BaseModel):
    business_name: str
    timezone: str
    slot_minutes: int = Field(gt=0)
    booking_window_days: int = Field(gt=0)
    hours: dict[str, tuple[time, time] | None]
    services: dict[str, ServiceConfig]
    faq: dict[str, str] = {}

    def hours_for(self, d: date) -> tuple[time, time] | None:
        return self.hours.get(WEEKDAYS[d.weekday()])


def load_config(path: Path = CONFIG_PATH) -> BusinessConfig:
    with open(path, encoding="utf-8") as f:
        return BusinessConfig.model_validate(yaml.safe_load(f))


class BookingRequest(BaseModel):
    """Raw caller input. Fields are optional so we can report MISSING_FIELD clearly."""

    name: str | None = None
    phone: str | None = None
    service: str | None = None
    date: str | None = None  # YYYY-MM-DD
    time: str | None = None  # HH:MM (24h)


class Slot(BaseModel):
    start: datetime
    end: datetime


class Booking(BaseModel):
    """A row of the bookings table. Internal: never handed to the model directly."""

    id: int  # internal
    booking_id: str  # opaque public reference
    customer_name: str
    phone_last4: str
    code: str
    start: datetime
    end: datetime
    purpose: str
    status: str


class BookingError(BaseModel):
    code: ErrorCode
    message: str
    alternatives: list[Slot] = []


class ValidatedRequest(BaseModel):
    name: str
    phone_last4: str
    service: str
    start: datetime
    end: datetime


class BookingResult(BaseModel):
    ok: bool
    booking: Booking | None = None
    error: BookingError | None = None
