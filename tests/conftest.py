import os
import tempfile
from datetime import datetime

# Importing api.main builds the app against VOICEGATE_DB_PATH: keep it out of the repo during tests.
os.environ.setdefault("VOICEGATE_DB_PATH", os.path.join(tempfile.gettempdir(), "voicegate-pytest-default.db"))

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
