"""The booking and code-mixed evaluations measure what they claim (detectors catch real failures)."""

import asyncio

from eval.booking_eval import _double_bookings, claims_success, load_cases, run_booking
from eval.codemix_eval import run_codemix


def test_thirty_booking_cases_with_unique_ids():
    cases = load_cases()
    assert len(cases) == 30 and len({c["id"] for c in cases}) == 30


def test_false_confirmation_detector():
    assert claims_success("Confirmed. Your consultation is booked for Tuesday.")
    assert claims_success("Done. Booking VG-AB12 is cancelled.")
    assert claims_success("Aapka appointment ho gaya!")
    assert not claims_success("The booking was not made. That time is already taken.")
    assert not claims_success("Okay, I haven't changed anything.")
    assert not claims_success("Shall I confirm this booking?")


def test_double_booking_detector():
    rows = [
        {"start": "2026-11-03T10:00:00", "end": "2026-11-03T10:30:00", "status": "confirmed"},
        {"start": "2026-11-03T10:15:00", "end": "2026-11-03T10:45:00", "status": "confirmed"},
        {"start": "2026-11-03T10:30:00", "end": "2026-11-03T11:00:00", "status": "cancelled"},
    ]
    assert _double_bookings(rows) == 1
    assert _double_bookings(rows[:1] + rows[2:]) == 0


def test_booking_suite_targets_offline():
    """Targets from the spec: 0 double bookings and 0 false confirmations (rule brain, offline)."""
    r = asyncio.run(run_booking("rules"))
    assert r["double_bookings"] == 0 and r["false_confirmations"] == 0 and r["errors"] == 0
    assert r["measured_with"].startswith("offline")


def test_codemix_eval_reports_failures_honestly(tmp_path):
    f = tmp_path / "mini.yaml"
    f.write_text(
        'reference_now: "2026-11-02T08:00:00"\nutterances:\n'
        '  - {id: A, lang: english, text: "tomorrow at 10am", expected: {date: "2026-11-03", time: "10:00", service: null}}\n'
        '  - {id: B, lang: english, text: "tomorrow at 10am", expected: {date: "2026-11-04", time: "10:00", service: null}}\n',
        encoding="utf-8",
    )
    r = run_codemix(f)
    assert r["correct"] == 1 and [x["id"] for x in r["failures"]] == ["B"]
