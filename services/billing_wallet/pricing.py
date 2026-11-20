"""Cálculo de tarifas (lógica pura, sin I/O => fácil de probar unitariamente)."""

import os
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

BASE_PRICE_PER_KWH = {"FAST": Decimal("0.30"), "ULTRA": Decimal("0.45")}
SURGE_MULTIPLIER = Decimal("1.5")
TZ = ZoneInfo(os.environ.get("PRICING_TZ", "America/Bogota"))


def parse_peak_hours(spec: str) -> tuple[int, int]:
    """'17-21' => (17, 21): horas pico [inicio, fin). '0-24' => todo el día."""
    start, end = (int(x) for x in spec.split("-"))
    if not (0 <= start < end <= 24):
        raise ValueError(f"Ventana pico inválida: {spec}")
    return start, end


def is_peak(at: datetime, peak: tuple[int, int]) -> bool:
    hour = at.astimezone(TZ).hour
    return peak[0] <= hour < peak[1]


@dataclass(frozen=True)
class Quote:
    connector_type: str
    energy_kwh: Decimal
    base_price_per_kwh: Decimal
    surge_enabled: bool
    peak_hour: bool
    multiplier: Decimal
    price_per_kwh: Decimal
    amount: Decimal


def quote(connector_type: str, energy_kwh: Decimal, surge_enabled: bool, at: datetime, peak: tuple[int, int]) -> Quote:
    if connector_type not in BASE_PRICE_PER_KWH:
        raise ValueError(f"Tipo de conector desconocido: {connector_type}")
    if energy_kwh <= 0:
        raise ValueError("La energía solicitada debe ser positiva")
    base = BASE_PRICE_PER_KWH[connector_type]
    peak_now = is_peak(at, peak)
    multiplier = SURGE_MULTIPLIER if (surge_enabled and peak_now) else Decimal("1")
    price = (base * multiplier).quantize(Decimal("0.0001"))
    amount = (price * energy_kwh).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return Quote(connector_type, energy_kwh, base, surge_enabled, peak_now, multiplier, price, amount)
