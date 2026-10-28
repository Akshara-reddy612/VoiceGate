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
