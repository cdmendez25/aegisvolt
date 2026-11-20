from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from billing_wallet.pricing import is_peak, parse_peak_hours, quote
from energy_dispatch.telemetry import reading

# 2026-11-20 23:00 UTC = 18:00 en Bogotá (hora pico); 15:00 UTC = 10:00 Bogotá
PEAK_TIME = datetime(2026, 11, 20, 23, 0, tzinfo=UTC)
OFF_PEAK_TIME = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)


def test_parse_peak_hours():
    assert parse_peak_hours("17-21") == (17, 21)
    assert parse_peak_hours("0-24") == (0, 24)
    with pytest.raises(ValueError):
        parse_peak_hours("21-17")


def test_is_peak_uses_bogota_timezone():
    assert is_peak(PEAK_TIME, (17, 21))
    assert not is_peak(OFF_PEAK_TIME, (17, 21))


def test_surge_applies_only_with_flag_and_peak():
    on_peak = quote("FAST", Decimal("40"), True, PEAK_TIME, (17, 21))
    assert on_peak.multiplier == Decimal("1.5")
    assert on_peak.amount == Decimal("18.00")  # 0.30 * 1.5 * 40

    flag_off = quote("FAST", Decimal("40"), False, PEAK_TIME, (17, 21))
    assert flag_off.multiplier == 1 and flag_off.amount == Decimal("12.00")

    off_peak = quote("FAST", Decimal("40"), True, OFF_PEAK_TIME, (17, 21))
    assert off_peak.multiplier == 1 and not off_peak.peak_hour


def test_ultra_is_more_expensive():
    assert quote("ULTRA", Decimal("10"), False, OFF_PEAK_TIME, (17, 21)).amount == Decimal("4.50")


@pytest.mark.parametrize("ctype,kwh", [("NUCLEAR", "10"), ("FAST", "0"), ("FAST", "-5")])
def test_invalid_quotes(ctype, kwh):
    with pytest.raises(ValueError):
        quote(ctype, Decimal(kwh), False, OFF_PEAK_TIME, (17, 21))


def test_telemetry_progresses_and_completes():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    early = reading(start, start + timedelta(seconds=10), 50, 40, speedup=60)
    assert 0 < early.energy_kwh < 40 and early.power_kw > 0 and not early.completed

    done = reading(start, start + timedelta(hours=2), 50, 40, speedup=60)
    assert done.completed and done.energy_kwh == 40 and done.power_kw == 0 and done.progress_pct == 100


def test_telemetry_tapers_after_80_percent():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    # 0.95*50 kW llega a 32 kWh (80% de 40) en ~40.4 min simulados => 40.4 s reales
    before = reading(start, start + timedelta(seconds=30), 50, 40, speedup=60)
    after = reading(start, start + timedelta(seconds=45), 50, 40, speedup=60)
    assert before.power_kw > 40 and after.power_kw < 25
