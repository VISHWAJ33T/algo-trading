"""Position sizing, daily loss cap, and single-position-at-a-time guard.

Numbers here are config-driven placeholders (see .env.example) -- the
strategy spec doesn't define position sizing or a daily loss limit, so
defaults are conservative (1 lot, cap disabled) until the user sets real
values.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, lot_size: int, max_daily_loss: float | None):
        self.lot_size = lot_size
        self.max_daily_loss = max_daily_loss
        self.realized_pnl_today = 0.0

    def can_enter(self, has_open_position: bool) -> tuple[bool, str]:
        if has_open_position:
            return False, "a position is already open"
        if self.max_daily_loss is not None and self.realized_pnl_today <= -abs(self.max_daily_loss):
            return False, (
                f"daily loss cap of {self.max_daily_loss} breached "
                f"(realized today: {self.realized_pnl_today:.2f})"
            )
        return True, ""

    def record_realized_pnl(self, pnl: float) -> None:
        self.realized_pnl_today += pnl
        logger.info("Realized P&L %.2f recorded (cumulative today: %.2f)", pnl, self.realized_pnl_today)

    def reset_daily(self) -> None:
        self.realized_pnl_today = 0.0
