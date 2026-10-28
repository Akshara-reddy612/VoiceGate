"""Booking service: validation, conflict detection, verified writes.

All datetimes are naive local times in the business timezone, stored as ISO strings
("YYYY-MM-DDTHH:MM:SS") so lexical comparison in SQL equals chronological comparison.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from booking.db import connect, init_db
from booking.models import (
    Booking,
    BookingError,
    BookingRequest,
    BookingResult,
    BusinessConfig,
    ErrorCode,
    Slot,
    ValidatedRequest,
    load_config,
)

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
MAX_NAME_LEN = 60


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def normalize_phone(raw: str) -> str | None:
    """Return the 10-digit national number, or None if it doesn't look like an Indian mobile/landline."""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    return digits if len(digits) == 10 else None


class BookingService:
    def __init__(
        self,
        db_path: Path | str,
        config: BusinessConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = db_path
        self.config = config or load_config()
        tz = ZoneInfo(self.config.timezone)
        self._clock = clock or (lambda: datetime.now(tz).replace(tzinfo=None))
        init_db(db_path)

    def now(self) -> datetime:
        return self._clock().replace(second=0, microsecond=0)

    # ---------- validation ----------

    def resolve_service(self, raw: str) -> str | None:
        key = raw.strip().lower().replace(" ", "_").replace("-", "_")
        if key in self.config.services:
            return key
        text = raw.strip().lower()
        for k, svc in self.config.services.items():
            if text == svc.label.lower() or text in (a.lower() for a in svc.aliases):
                return k
        return None

    def validate(self, req: BookingRequest) -> ValidatedRequest | BookingError:
        missing = [f for f in ("name", "phone", "service", "date", "time") if not (getattr(req, f) or "").strip()]
        if missing:
            return BookingError(code=ErrorCode.MISSING_FIELD, message=f"Missing: {', '.join(missing)}.")

        name = " ".join(req.name.split())
        if len(name) > MAX_NAME_LEN or not re.fullmatch(r"[^\W\d_][\w .'-]*", name):
            return BookingError(code=ErrorCode.INVALID_INPUT, message="Please give a valid name.")

        phone = normalize_phone(req.phone)
        if phone is None:
            return BookingError(code=ErrorCode.INVALID_INPUT, message="Please give a 10-digit phone number.")

        service = self.resolve_service(req.service)
        if service is None:
            names = ", ".join(s.label for s in self.config.services.values())
            return BookingError(code=ErrorCode.UNKNOWN_SERVICE, message=f"We offer: {names}.")

        try:
            d = date.fromisoformat(req.date.strip())
            t = time.fromisoformat(req.time.strip())
        except ValueError:
            return BookingError(code=ErrorCode.INVALID_INPUT, message="Date must be YYYY-MM-DD and time HH:MM.")

        start = datetime.combine(d, t)
        end = start + timedelta(minutes=self.config.services[service].duration_minutes)
        err = self.check_time_rules(start, end)
        if err:
            return err
        return ValidatedRequest(name=name, phone_last4=phone[-4:], service=service, start=start, end=end)

    def check_time_rules(self, start: datetime, end: datetime) -> BookingError | None:
        now = self.now()
        if start <= now:
            return BookingError(code=ErrorCode.PAST_DATE, message="That time has already passed.")
        if start.date() > now.date() + timedelta(days=self.config.booking_window_days):
            return BookingError(
                code=ErrorCode.TOO_FAR_AHEAD,
                message=f"We only take bookings up to {self.config.booking_window_days} days ahead.",
            )
        hours = self.config.hours_for(start.date())
        if hours is None:
            return BookingError(
                code=ErrorCode.CLOSED,
                message=f"We are closed on {start.strftime('%A')}.",
                alternatives=self.find_alternatives(start, end - start),
            )
        open_t, close_t = hours
        if start.time() < open_t or end.time() > close_t or end.date() != start.date():
            return BookingError(
                code=ErrorCode.OUTSIDE_HOURS,
                message=f"On {start.strftime('%A')} we are open {open_t:%H:%M} to {close_t:%H:%M}.",
                alternatives=self.find_alternatives(start, end - start),
            )
        if (start.hour * 60 + start.minute) % self.config.slot_minutes:
            return BookingError(
                code=ErrorCode.OFF_GRID,
                message=f"Appointments start every {self.config.slot_minutes} minutes, e.g. 10:00 or 10:30.",
                alternatives=self.find_alternatives(start, end - start),
            )
        return None

    # ---------- availability ----------

    def _busy(self, conn: sqlite3.Connection, day: date) -> list[tuple[datetime, datetime]]:
        lo = _iso(datetime.combine(day, time.min))
        hi = _iso(datetime.combine(day + timedelta(days=1), time.min))
        rows = conn.execute(
            "SELECT start, \"end\" FROM bookings WHERE status = 'confirmed' AND start < ? AND \"end\" > ?",
            (hi, lo),
        ).fetchall()
        return [(datetime.fromisoformat(r["start"]), datetime.fromisoformat(r["end"])) for r in rows]

    def _free_starts(self, conn: sqlite3.Connection, day: date, length: timedelta) -> list[Slot]:
        hours = self.config.hours_for(day)
        if hours is None:
            return []
        busy = self._busy(conn, day)
        step = timedelta(minutes=self.config.slot_minutes)
        cur = datetime.combine(day, hours[0])
        close = datetime.combine(day, hours[1])
        now = self.now()
        out: list[Slot] = []
        while cur + length <= close:
            end = cur + length
            if cur > now and not any(b0 < end and b1 > cur for b0, b1 in busy):
                out.append(Slot(start=cur, end=end))
            cur += step
        return out

    def get_free_slots(self, day: date, service: str | None = None) -> list[Slot]:
        minutes = self.config.services[service].duration_minutes if service else self.config.slot_minutes
        conn = connect(self.db_path)
        try:
            return self._free_starts(conn, day, timedelta(minutes=minutes))
        finally:
            conn.close()

    def find_alternatives(self, wanted: datetime, length: timedelta, n: int = 3, days: int = 3) -> list[Slot]:
        """Nearest free slots to `wanted`, searching that day and the next `days` days."""
        conn = connect(self.db_path)
        try:
            candidates: list[Slot] = []
            for i in range(days + 1):
                candidates += self._free_starts(conn, wanted.date() + timedelta(days=i), length)
        finally:
            conn.close()
        candidates.sort(key=lambda s: (abs(s.start - wanted), s.start))
        return sorted(candidates[:n], key=lambda s: s.start)

    def check_conflict(self, start: datetime, end: datetime) -> BookingError | None:
        """Read-only pre-check used before read-back. create_booking re-checks inside its transaction."""
        conn = connect(self.db_path)
        try:
            clash = conn.execute(
                "SELECT 1 FROM bookings WHERE status = 'confirmed' AND start < ? AND \"end\" > ? LIMIT 1",
                (_iso(end), _iso(start)),
            ).fetchone()
        finally:
            conn.close()
        if not clash:
            return None
        return BookingError(
            code=ErrorCode.CONFLICT,
            message="That time is already taken.",
            alternatives=self.find_alternatives(start, end - start),
        )

    # ---------- writes ----------

    def _new_code(self) -> str:
        return "VG-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))

    def _read_back(self, conn: sqlite3.Connection, booking_id: int) -> Booking | None:
        row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        return Booking.model_validate(dict(row)) if row else None

    def create_booking(self, req: BookingRequest) -> BookingResult:
        """Validate, check conflicts and insert in one IMMEDIATE transaction, then verify by reading back."""
        v = self.validate(req)
        if isinstance(v, BookingError):
            return BookingResult(ok=False, error=v)

        conn = connect(self.db_path)
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")  # takes the write lock: no other writer can interleave
                clash = conn.execute(
                    "SELECT 1 FROM bookings WHERE status = 'confirmed' AND start < ? AND \"end\" > ? LIMIT 1",
                    (_iso(v.end), _iso(v.start)),
                ).fetchone()
                if clash:
                    conn.execute("ROLLBACK")
                    return BookingResult(
                        ok=False,
                        error=BookingError(
                            code=ErrorCode.CONFLICT,
                            message="That time is already taken.",
                            alternatives=self.find_alternatives(v.start, v.end - v.start),
                        ),
                    )
                for _ in range(5):
                    code = self._new_code()
                    if not conn.execute("SELECT 1 FROM bookings WHERE code = ?", (code,)).fetchone():
                        break
                cur = conn.execute(
                    "INSERT INTO bookings (customer_name, phone_last4, code, start, \"end\", purpose, status)"
                    " VALUES (?, ?, ?, ?, ?, ?, 'confirmed')",
                    (v.name, v.phone_last4, code, _iso(v.start), _iso(v.end), v.service),
                )
                booking_id = cur.lastrowid
                conn.execute("COMMIT")
            except sqlite3.Error:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                return BookingResult(
                    ok=False, error=BookingError(code=ErrorCode.WRITE_FAILED, message="Could not save the booking.")
                )

            saved = self._read_back(conn, booking_id)
            expected = (v.name, v.phone_last4, code, v.start, v.end, v.service, "confirmed")
            if saved is None or (
                saved.customer_name, saved.phone_last4, saved.code, saved.start, saved.end, saved.purpose, saved.status
            ) != expected:
                return BookingResult(
                    ok=False,
                    error=BookingError(
                        code=ErrorCode.WRITE_NOT_VERIFIED, message="The booking could not be verified. It is not confirmed."
                    ),
                )
            return BookingResult(ok=True, booking=saved)
        finally:
            conn.close()
