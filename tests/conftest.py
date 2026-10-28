from datetime import datetime

import pytest

from booking.service import BookingService

# Wednesday. Thu 29 Oct is a normal weekday, Sat 31 Oct is short hours, Sun 1 Nov is closed.
NOW = datetime(2026, 10, 28, 8, 0)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def svc(db_path):
    return BookingService(db_path, clock=lambda: NOW)
