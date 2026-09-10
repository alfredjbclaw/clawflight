"""Shared fixtures. Everything here is synthetic; see ``fixtures/README.md``."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from clawflight.models import FlightLeg, FlightRecord, PersonRef
from clawflight.people import PersonTable
from clawflight.recipients import RecipientConfig


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture
def people() -> PersonTable:
    payload = json.loads((FIXTURES / "people.json").read_text(encoding="utf-8"))
    return PersonTable.from_entries(payload["people"])


@pytest.fixture
def calendar_text() -> str:
    return (FIXTURES / "calendar_events.txt").read_text(encoding="utf-8")


@pytest.fixture
def recipients() -> RecipientConfig:
    return RecipientConfig.from_entries(
        [
            {
                "key": "alex",
                "name": "Alex",
                "follow_all": True,
                "channel": {"channel": "telegram", "to": "-1009876543210:topic:42"},
            },
            {
                "key": "sam",
                "name": "Sam",
                "channel": {"channel": "whatsapp", "to": "+15550000000"},
            },
        ]
    )


def make_leg(**overrides) -> FlightLeg:
    defaults = dict(
        carrier="AA",
        number=4912,
        date="2026-07-11",
        origin="ASE",
        dest="DFW",
        sched_dep_iso="2026-07-11T12:51:00-06:00",
        sched_arr_iso="2026-07-11T16:10:00-05:00",
        conf_code="FAKE01",
        seat="10C",
    )
    defaults.update(overrides)
    return FlightLeg(**defaults)


def make_record(**overrides) -> FlightRecord:
    leg = overrides.pop("leg", None) or make_leg()
    defaults = dict(
        flight_id="AA4912-2026-07-11",
        leg=leg,
        person=PersonRef(key="alex", name="Alex"),
        sources=("cal:cal-0001",),
        backup_group=None,
        status="scheduled",
        notes=(),
    )
    defaults.update(overrides)
    return FlightRecord(**defaults)
