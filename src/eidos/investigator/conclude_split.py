"""Pure timing policy for a conclude-only decision (ADR 0042)."""
from dataclasses import dataclass

DURABLE_WRITE_MARGIN_SECONDS = 2.0
MIN_FINAL_ATTEMPT_SECONDS = 60.0


@dataclass(frozen=True)
class ConcludeSplit:
    window_s: float

    @property
    def read_slot_s(self) -> float:
        return min(30.0, max(10.0, 0.12 * self.window_s))

    @property
    def fallback_reserve_s(self) -> float:
        return min(120.0, max(45.0, 0.30 * self.window_s))

    def reads_allowed(self, elapsed: float) -> bool:
        return elapsed < self.read_slot_s

    def final_attempt(self, left: float, request_timeout: float) -> tuple[float, bool]:
        attempt = left - self.fallback_reserve_s - DURABLE_WRITE_MARGIN_SECONDS
        armed = attempt >= MIN_FINAL_ATTEMPT_SECONDS
        usable = attempt if armed else left - DURABLE_WRITE_MARGIN_SECONDS
        return min(request_timeout, usable), armed
