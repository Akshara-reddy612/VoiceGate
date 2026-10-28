from datetime import date, datetime, time
from pathlib import Path

import pytest
import yaml

from agent.normalizer import coerce_date, coerce_time, normalize
from booking.models import load_config

DATA = yaml.safe_load((Path(__file__).resolve().parents[1] / "eval" / "codemix.yaml").read_text(encoding="utf-8"))
TODAY = datetime.fromisoformat(DATA["reference_now"]).date()  # Mon 2026-11-02
CFG = load_config()


@pytest.mark.parametrize("u", DATA["utterances"], ids=[u["id"] for u in DATA["utterances"]])
def test_codemix_utterance(u):
    n = normalize(u["text"], TODAY, CFG)
    got = {
        "date": n.date.isoformat() if n.date else None,
        "time": n.time.strftime("%H:%M") if n.time else None,
        "service": n.service,
    }
    assert got == u["expected"]
    if u.get("issue"):
        assert u["issue"] in n.issues


def test_set_size_and_coverage():
    us = DATA["utterances"]
    assert len(us) >= 40
    assert {u["lang"] for u in us} == {"tenglish", "hinglish", "english"}
    assert {u["issue"] for u in us if "issue" in u} >= {"past_date", "invalid_date", "invalid_time", "needs_hour", "ambiguous_time"}


# ---------- does not over-match ----------


@pytest.mark.parametrize(
    "text",
    ["I do want to come", "das minute mein aata hoon", "my son is ten", "call me at the clinic", "ki baat hai"],
)
def test_no_spurious_time(text):
    assert normalize(text, TODAY).time is None


def test_no_date_from_plain_numbers():
    assert normalize("I have 2 kids and 3 dogs", TODAY).date is None


def test_same_weekday_means_next_week():
    assert normalize("monday 10 am", TODAY).date == date(2026, 11, 9)


def test_past_month_day_rolls_to_next_year():
    assert normalize("1st january", TODAY).date == date(2027, 1, 1)


def test_clean_text_is_untouched_when_nothing_found():
    n = normalize("hello", TODAY)
    assert (n.date, n.time, n.service, n.issues) == (None, None, None, [])


# ---------- tool-layer helpers ----------


@pytest.mark.parametrize("value, out", [("2026-11-05", "2026-11-05"), ("repu", "2026-11-03"), ("kal", "2026-11-03"),
                                        ("parso", "2026-11-04"), ("someday", "someday")])
def test_coerce_date(value, out):
    assert coerce_date(value, TODAY) == out


@pytest.mark.parametrize("value, out", [("10:30", "10:30"), ("sayantram 5", "17:00"), ("saadhe das", "10:30"),
                                        ("5 pm", "17:00"), ("whenever", "whenever")])
def test_coerce_time(value, out):
    assert coerce_time(value, TODAY) == out


def test_morning_keeps_small_hours_and_evening_lifts_them():
    assert normalize("subah 7 baje", TODAY).time == time(7, 0)
    assert normalize("shaam 7 baje", TODAY).time == time(19, 0)
    assert normalize("7 baje", TODAY).time == time(19, 0)  # clinic default: 1-7 without a part of day = PM


# ---------- ambiguity must ask, never guess (phrasings deliberately differ from the held-out file) ----------


@pytest.mark.parametrize(
    "text, expected_date",
    [
        ("next week wednesday at 10am", date(2026, 11, 11)),
        ("agle hafte shukravar 11 baje", date(2026, 11, 13)),
        ("vacche vaaram guruvaram 9 ki", date(2026, 11, 12)),
        ("next week monday", date(2026, 11, 9)),
    ],
)
def test_next_week_weekday_is_in_the_following_week(text, expected_date):
    assert normalize(text, TODAY).date == expected_date


def test_next_week_without_day_asks_for_day():
    n = normalize("sometime next week at 10am", TODAY)
    assert n.date is None and "needs_day" in n.issues


@pytest.mark.parametrize(
    "text",
    ["kal 4 ya 5 baje", "11am or 12 noon tomorrow", "repu 10 leda 11 ki", "between 3 and 4 on friday",
     "tomorrow at 10:30 or 11:30", "kal subah 9 baje, nahi toh shaam 5 baje"],
)
def test_several_times_are_ambiguous_not_guessed(text):
    n = normalize(text, TODAY)
    assert n.time is None and "ambiguous_time" in n.issues


def test_ambiguous_candidates_listed_when_known():
    n = normalize("tomorrow 11am or 12 noon", TODAY)
    assert n.candidates == [time(11, 0), time(12, 0)]


def test_one_time_said_twice_is_not_ambiguous():
    assert normalize("10am, yes 10am tomorrow", TODAY).time == time(10, 0)


def test_minutes_with_meridiem_read_once():
    n = normalize("4:30 pm", TODAY)
    assert n.time == time(16, 30) and n.issues == []
