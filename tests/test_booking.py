import re
import sqlite3
import threading
from datetime import date, datetime

import pytest

from booking.db import connect
from booking.models import BookingRequest, ErrorCode
from booking.service import BookingService, normalize_phone
from tests.conftest import NOW

THU = "2026-10-29"


def req(**kw) -> BookingRequest:
    base = dict(name="Ravi Kumar", phone="98765 43210", service="consultation", date=THU, time="10:00")
    return BookingRequest(**{**base, **kw})


def test_fixture_calendar_assumptions():
    assert NOW.date().weekday() == 2  # Wed
    assert date(2026, 11, 1).weekday() == 6  # Sun


# ---------- happy path and verified write ----------


def test_create_booking_returns_verified_row(svc, db_path):
    r = svc.create_booking(req())
    assert r.ok and r.error is None
    b = r.booking
    assert b.start == datetime(2026, 10, 29, 10, 0) and b.end == datetime(2026, 10, 29, 10, 30)
    assert b.phone_last4 == "3210" and b.code.startswith("VG-") and b.status == "confirmed"
    row = connect(db_path).execute("SELECT * FROM bookings WHERE id = ?", (b.id,)).fetchone()
    assert row["code"] == b.code


def test_full_phone_number_is_never_stored(svc, db_path):
    svc.create_booking(req())
    dump = "\n".join(connect(db_path).iterdump())
    assert "9876543210" not in dump and "98765 43210" not in dump


def test_service_alias_resolves(svc):
    r = svc.create_booking(req(service="Full Body", time="11:00"))
    assert r.ok and r.booking.purpose == "full_checkup"
    assert r.booking.end == datetime(2026, 10, 29, 12, 0)


# ---------- conflict detection ----------


def test_exact_overlap_is_rejected_with_alternatives(svc):
    assert svc.create_booking(req()).ok
    r = svc.create_booking(req(name="Sita"))
    assert not r.ok and r.error.code == ErrorCode.CONFLICT
    alts = r.error.alternatives
    assert len(alts) == 3
    assert all(a.start != datetime(2026, 10, 29, 10, 0) for a in alts)
    # nearest first: 09:30 and 10:30 are each 30 min away
    assert {a.start for a in alts} >= {datetime(2026, 10, 29, 9, 30), datetime(2026, 10, 29, 10, 30)}


def test_partial_overlap_is_rejected(svc):
    assert svc.create_booking(req(service="full_checkup", time="10:00")).ok  # 10:00-11:00
    assert svc.create_booking(req(time="10:30")).error.code == ErrorCode.CONFLICT


def test_touching_slots_are_allowed(svc):
    assert svc.create_booking(req(time="10:00")).ok
    assert svc.create_booking(req(time="10:30")).ok
    assert svc.create_booking(req(time="09:30")).ok


def test_free_slots_exclude_booked_and_respect_duration(svc):
    svc.create_booking(req(time="10:00"))
    starts = [s.start.strftime("%H:%M") for s in svc.get_free_slots(date(2026, 10, 29))]
    assert "10:00" not in starts and "09:30" in starts and "10:30" in starts
    assert starts[0] == "09:00" and starts[-1] == "17:30"
    # 60-min service cannot start at 09:30 (would overlap 10:00)
    long_starts = [s.start.strftime("%H:%M") for s in svc.get_free_slots(date(2026, 10, 29), "full_checkup")]
    assert "09:30" not in long_starts and "17:00" in long_starts and "17:30" not in long_starts


def test_free_slots_today_skip_past_times(db_path):
    svc = BookingService(db_path, clock=lambda: datetime(2026, 10, 28, 12, 10))
    starts = [s.start.strftime("%H:%M") for s in svc.get_free_slots(date(2026, 10, 28))]
    assert starts[0] == "12:30"


def test_concurrent_bookings_for_same_slot_only_one_wins(db_path):
    BookingService(db_path, clock=lambda: NOW)  # init schema
    barrier = threading.Barrier(8)
    results = []

    def worker(i):
        s = BookingService(db_path, clock=lambda: NOW)
        barrier.wait()
        results.append(s.create_booking(req(name=f"Caller {chr(65 + i)}")))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r.ok for r in results) == 1
    assert all(r.error.code == ErrorCode.CONFLICT for r in results if not r.ok)
    assert connect(db_path).execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 1


# ---------- invalid input ----------


@pytest.mark.parametrize(
    "kw, code",
    [
        (dict(date="2026-10-27"), ErrorCode.PAST_DATE),
        (dict(date="2026-10-28", time="07:30"), ErrorCode.PAST_DATE),
        (dict(date="2026-12-31"), ErrorCode.TOO_FAR_AHEAD),
        (dict(date="2026-11-01"), ErrorCode.CLOSED),
        (dict(time="08:30"), ErrorCode.OUTSIDE_HOURS),
        (dict(time="17:45"), ErrorCode.OUTSIDE_HOURS),
        (dict(service="full_checkup", time="17:30"), ErrorCode.OUTSIDE_HOURS),
        (dict(date="2026-10-31", time="14:00"), ErrorCode.OUTSIDE_HOURS),
        (dict(time="10:15"), ErrorCode.OFF_GRID),
        (dict(service="haircut"), ErrorCode.UNKNOWN_SERVICE),
        (dict(phone="12345"), ErrorCode.INVALID_INPUT),
        (dict(name="1234"), ErrorCode.INVALID_INPUT),
        (dict(date="tomorrow"), ErrorCode.INVALID_INPUT),
        (dict(time="25:00"), ErrorCode.INVALID_INPUT),
        (dict(name=""), ErrorCode.MISSING_FIELD),
        (dict(phone=None, time=None), ErrorCode.MISSING_FIELD),
    ],
)
def test_invalid_requests_are_rejected(svc, kw, code):
    r = svc.create_booking(req(**kw))
    assert not r.ok and r.booking is None
    assert r.error.code == code and r.error.message


def test_missing_field_message_lists_fields(svc):
    assert svc.create_booking(req(phone=None, time="")).error.message == "Missing: phone, time."


def test_closed_day_suggests_open_alternatives(svc):
    alts = svc.create_booking(req(date="2026-11-01")).error.alternatives
    assert alts and all(a.start.weekday() != 6 for a in alts)


@pytest.mark.parametrize(
    "raw, out",
    [("98765 43210", "9876543210"), ("+91-98765-43210", "9876543210"), ("098765 43210", "9876543210"), ("1234", None)],
)
def test_normalize_phone(raw, out):
    assert normalize_phone(raw) == out


# ---------- verified write failure modes ----------


def test_read_back_failure_is_not_reported_as_success(svc, monkeypatch):
    monkeypatch.setattr(svc, "_read_back", lambda conn, booking_id: None)
    r = svc.create_booking(req())
    assert not r.ok and r.error.code == ErrorCode.WRITE_NOT_VERIFIED


def test_read_back_mismatch_is_not_reported_as_success(svc, monkeypatch):
    real = svc._read_back

    def tampered(conn, booking_id):
        b = real(conn, booking_id)
        return b.model_copy(update={"start": b.start.replace(hour=11)})

    monkeypatch.setattr(svc, "_read_back", tampered)
    assert svc.create_booking(req()).error.code == ErrorCode.WRITE_NOT_VERIFIED


def test_db_error_during_write_is_reported(svc, monkeypatch):
    def broken(self):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(BookingService, "_new_code", broken)
    r = svc.create_booking(req())
    assert not r.ok and r.error.code == ErrorCode.WRITE_FAILED


# ---------- opaque public booking_id ----------


def _fill_day(svc, day="2026-10-29"):
    out = []
    for h in range(9, 18):
        for m in ("00", "30"):
            r = svc.create_booking(req(name=f"Caller {h}{m}", date=day, time=f"{h:02d}:{m}"))
            assert r.ok, r.error
            out.append(r.booking)
    return out


def test_public_booking_ids_are_unique_opaque_and_not_sequential(svc):
    made = _fill_day(svc) + _fill_day(svc, "2026-10-30")
    ids = [b.booking_id for b in made]
    assert len(ids) == len(set(ids)) == 36
    assert all(re.fullmatch(r"bk_[a-z2-9]{10}", i) for i in ids)
    assert ids != sorted(ids)  # random, not monotonic


def test_booking_id_collision_is_retried(svc, monkeypatch):
    first = svc.create_booking(req()).booking
    seq = iter([first.booking_id, first.booking_id, "bk_freshvalue"])
    monkeypatch.setattr(svc, "_new_booking_id", lambda: next(seq))
    second = svc.create_booking(req(time="11:00"))
    assert second.ok and second.booking.booking_id == "bk_freshvalue"


def test_booking_id_is_unique_in_schema(svc, db_path):
    b = svc.create_booking(req()).booking
    with pytest.raises(sqlite3.IntegrityError):
        connect(db_path).execute(
            "INSERT INTO bookings (booking_id, customer_name, phone_last4, code, start, \"end\", purpose)"
            " VALUES (?, 'X', '1111', 'VG-ZZZZ', 'a', 'b', 'consultation')",
            (b.booking_id,),
        )


def test_read_back_checks_booking_id(svc, monkeypatch):
    real = svc._read_back
    monkeypatch.setattr(svc, "_read_back", lambda conn, rid: real(conn, rid).model_copy(update={"booking_id": "bk_other"}))
    assert svc.create_booking(req()).error.code == ErrorCode.WRITE_NOT_VERIFIED


def test_old_database_is_migrated_with_random_booking_ids(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE bookings (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_name TEXT NOT NULL,"
        " phone_last4 TEXT NOT NULL, code TEXT NOT NULL UNIQUE, start TEXT NOT NULL, \"end\" TEXT NOT NULL,"
        " purpose TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'confirmed');"
        "INSERT INTO bookings (customer_name, phone_last4, code, start, \"end\", purpose)"
        " VALUES ('Old One', '1111', 'VG-OLD1', '2026-10-29T09:00:00', '2026-10-29T09:30:00', 'consultation'),"
        "        ('Old Two', '2222', 'VG-OLD2', '2026-10-29T09:30:00', '2026-10-29T10:00:00', 'consultation');"
    )
    conn.commit()
    conn.close()
    svc = BookingService(db, clock=lambda: NOW)  # runs init_db -> migration
    ids = [r[0] for r in connect(db).execute("SELECT booking_id FROM bookings")]
    assert len(set(ids)) == 2 and all(i.startswith("bk_") for i in ids)
    assert svc.create_booking(req(time="10:00")).ok  # old DB keeps working
