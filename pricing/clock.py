# pricing/clock.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo


@dataclass(frozen=True)
class Clock:
    tick_time_utc: datetime
    date_mt5: str
    time_mt5_hhmm: str


def tick_time_to_clock(tick_epoch: int) -> Clock:
    dt = datetime.fromtimestamp(int(tick_epoch), tz=timezone.utc)
    date_mt5 = dt.strftime("%Y-%m-%d")
    hhmm = dt.strftime("%H:%M")
    return Clock(tick_time_utc=dt, date_mt5=date_mt5, time_mt5_hhmm=hhmm)


def to_server_time(dt_utc: datetime, server_tz: tzinfo) -> datetime:
    return dt_utc.astimezone(server_tz)

def to_local_time(dt_utc: datetime, local_tz: tzinfo) -> datetime:
    return dt_utc.astimezone(local_tz)

def to_ist_time(dt_utc: datetime, ist_tz: tzinfo) -> datetime:
    return dt_utc.astimezone(ist_tz)


def iso_z(dt_utc: datetime) -> str:
    return dt_utc.isoformat().replace("+00:00", "Z")