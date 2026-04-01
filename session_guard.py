from __future__ import annotations

# =============================================================================
# SESSION GUARD — time-based entry gates
# All times pulled from config.
# =============================================================================

from datetime import datetime, timezone, timedelta
from config import cfg, StrategyConfig


def _utc_now(now_utc: datetime | None = None) -> datetime:
    return now_utc or datetime.now(timezone.utc)


def _hhmm(now_utc: datetime | None = None) -> str:
    return _utc_now(now_utc).strftime("%H:%M")


def is_session_allowed(
    strategy_cfg: StrategyConfig = cfg,
    now_utc: datetime | None = None,
) -> bool:
    hhmm = _hhmm(now_utc)
    return strategy_cfg.session_start_hhmm <= hhmm <= strategy_cfg.session_end_hhmm


def is_force_close_time(
    strategy_cfg: StrategyConfig = cfg,
    now_utc: datetime | None = None,
) -> bool:
    return _hhmm(now_utc) >= strategy_cfg.force_close_hhmm


def is_news_blackout(
    event_times_utc: list[str],
    strategy_cfg: StrategyConfig = cfg,
    now_utc: datetime | None = None,
) -> bool:
    now    = _utc_now(now_utc)
    window = timedelta(minutes=strategy_cfg.news_blackout_minutes)
    for t in event_times_utc:
        try:
            h, m   = map(int, t.split(":"))
            ev     = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if abs((now - ev).total_seconds()) <= window.total_seconds():
                return True
        except ValueError:
            continue
    return False


def session_status(
    strategy_cfg: StrategyConfig = cfg,
    now_utc: datetime | None = None,
) -> str:
    hhmm = _hhmm(now_utc)
    if is_force_close_time(strategy_cfg, now_utc):
        return f"FORCE_CLOSE ({hhmm})"
    if is_session_allowed(strategy_cfg, now_utc):
        return f"ACTIVE ({hhmm})"
    return f"OUT_OF_SESSION ({hhmm})"


if __name__ == "__main__":
    print(session_status())
