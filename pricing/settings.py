from dataclasses import dataclass
from datetime import timezone, timedelta, datetime

@dataclass(frozen=True)
class PriceSettings:
    base_dir: str = "data"
    pretty_json: bool = True
    poll_seconds: float = 0.1
    status_print_seconds: float = 5.0
    lock_hhmm_mt5: str = "00:00"
    allow_bootstrap_lock: bool = True
    mt5_ui_tz = timezone.utc
    server_tz = timezone(timedelta(hours=3))
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    local_tz = timezone(timedelta(hours=3, minutes=30))
    midnight_grace_minutes: int = 10
    stale_after_seconds: int = 20