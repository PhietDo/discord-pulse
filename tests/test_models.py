from datetime import datetime, timedelta, timezone

import pytest

from pulse.links import jump_link
from pulse.models import from_iso, parse_timestamp, to_iso


def test_to_iso_is_fixed_width_utc():
    a = to_iso(datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc))
    b = to_iso(datetime(2026, 9, 20, 10, 0, 0, 123456, tzinfo=timezone.utc))
    assert a == "2026-09-20T10:00:00.000000Z"
    assert len(a) == len(b)
    assert a < b


def test_to_iso_converts_offsets_to_utc():
    eastern = timezone(timedelta(hours=-4))
    assert to_iso(datetime(2026, 9, 20, 10, 0, tzinfo=eastern)) == "2026-09-20T14:00:00.000000Z"


def test_to_iso_rejects_naive():
    with pytest.raises(ValueError):
        to_iso(datetime(2026, 9, 20, 10, 0))


def test_from_iso_roundtrip():
    dt = datetime(2026, 9, 20, 10, 0, 0, 500, tzinfo=timezone.utc)
    assert from_iso(to_iso(dt)) == dt


def test_parse_timestamp_normalizes_offsets_and_long_fractions():
    dt = parse_timestamp("2026-09-20T10:00:00.1234567-04:00")
    assert to_iso(dt) == "2026-09-20T14:00:00.123456Z"
    assert to_iso(parse_timestamp("2026-09-21T09:00:00Z")) == "2026-09-21T09:00:00.000000Z"


def test_parse_timestamp_treats_naive_as_utc():
    assert to_iso(parse_timestamp("2026-09-21 09:00:00")) == "2026-09-21T09:00:00.000000Z"


def test_jump_link():
    assert jump_link("900", "300", "3001") == "https://discord.com/channels/900/300/3001"
