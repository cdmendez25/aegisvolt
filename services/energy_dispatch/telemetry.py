import math
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Reading:
    timestamp: str
    elapsed_min: float
    power_kw: float
    energy_kwh: float
    voltage_v: float
    current_a: float
    temperature_c: float
    progress_pct: float
    completed: bool


def reading(started_at: datetime, now: datetime, max_power_kw: float, target_kwh: float, speedup: float) -> Reading:
    elapsed_h = max((now - started_at).total_seconds(), 0) * speedup / 3600
    full_phase_kwh = 0.8 * target_kwh
    p_full, p_taper = 0.95 * max_power_kw, 0.4 * max_power_kw
    t_full = full_phase_kwh / p_full
    if elapsed_h <= t_full:
        energy = p_full * elapsed_h
        power = p_full
    else:
        energy = min(target_kwh, full_phase_kwh + p_taper * (elapsed_h - t_full))
        power = p_taper if energy < target_kwh else 0.0
    completed = energy >= target_kwh - 1e-9
    wobble = 1 + 0.02 * math.sin(elapsed_h * 40)
    power = round(power * wobble, 2) if not completed else 0.0
    voltage = round(400 + 400 * min(energy / target_kwh, 1) * 0.5, 1)
    current = round(power * 1000 / voltage, 1) if voltage else 0.0
    temperature = round(25 + 18 * (power / max_power_kw if max_power_kw else 0), 1)
    return Reading(
        timestamp=now.isoformat(timespec="seconds"),
        elapsed_min=round(elapsed_h * 60, 1),
        power_kw=power,
        energy_kwh=round(energy, 3),
        voltage_v=voltage,
        current_a=current,
        temperature_c=temperature,
        progress_pct=round(min(energy / target_kwh, 1) * 100, 1),
        completed=completed,
    )
