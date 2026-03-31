from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List


@dataclass(frozen=True)
class SessionPolicy:
    name: str
    start_hour: int
    end_hour: int
    strategy_id: str


# Each session owns exactly one strategy running at full capacity.
# No percentage splits — the active strategy uses pure risk-based sizing.
SESSIONS: List[SessionPolicy] = [
    SessionPolicy(name="ASIA",   start_hour=0,  end_hour=8,  strategy_id="candle3"),
    SessionPolicy(name="LONDON", start_hour=8,  end_hour=16, strategy_id="scalp"),
    SessionPolicy(name="NY",     start_hour=16, end_hour=24, strategy_id="trend"),
]


class SessionManager:
    def __init__(self) -> None:
        self._sessions = SESSIONS

    def current_session(self, timestamp: datetime | None = None) -> SessionPolicy:
        ts = timestamp or datetime.now(timezone.utc)
        hour = ts.hour
        for session in self._sessions:
            if session.start_hour <= hour < session.end_hour:
                return session
        return self._sessions[-1]

    def is_strategy_allowed(self, strategy_id: str, timestamp: datetime) -> bool:
        return self.current_session(timestamp).strategy_id == strategy_id
