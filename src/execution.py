"""Order execution with a single LIVE_TRADING switch.

Both paper and live modes go through the exact same state transitions and
logging; only the actual place_order() call is gated, so flipping the switch
in .env doesn't change any other behavior. Every entry/exit is also persisted
to storage.py so the dashboard can show today's trades and historical
analytics.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Literal

from src import storage
from src.auth import KotakSession
from src.config import Config
from src.instruments import Instrument
from src.risk import RiskManager

logger = logging.getLogger(__name__)

OptionType = Literal["CE", "PE"]


@dataclass
class OpenPosition:
    option_type: OptionType
    instrument: Instrument
    quantity: int
    entry_price: float
    entry_time: dt.datetime
    trade_id: int
    # NIFTY 50 spot at entry, so the "30 points of the underlying" target can
    # be measured. None when the index feed hadn't printed yet at entry.
    underlying_entry_price: float | None = None


class OrderExecutor:
    def __init__(self, session: KotakSession, config: Config, risk: RiskManager):
        self.session = session
        self.config = config
        self.risk = risk
        self.position: OpenPosition | None = None

    @property
    def open_option_type(self) -> OptionType | None:
        return self.position.option_type if self.position else None

    def enter(
        self,
        option_type: OptionType,
        instrument: Instrument,
        ltp: float,
        underlying_ltp: float | None = None,
    ) -> None:
        can_enter, reason = self.risk.can_enter(has_open_position=self.position is not None)
        if not can_enter:
            logger.info("Skipping %s entry: %s", option_type, reason)
            return

        quantity = self.risk.lot_size * instrument.lot_size

        if self.config.live_trading:
            result = self.session.call_with_retry(
                "place_order",
                exchange_segment=instrument.exchange_segment,
                product="MIS",
                price="0",
                order_type="MKT",
                quantity=str(quantity),
                validity="DAY",
                trading_symbol=instrument.trading_symbol,
                transaction_type="B",
            )
            logger.info(
                "LIVE order placed: BUY %s x%d %s -> %s",
                option_type, quantity, instrument.trading_symbol, result,
            )
        else:
            logger.info(
                "[PAPER] Would BUY %s x%d %s @ ~%.2f",
                option_type, quantity, instrument.trading_symbol, ltp,
            )

        entry_time = dt.datetime.now(dt.timezone.utc)
        trade_id = storage.record_trade_entry(
            option_type=option_type,
            trading_symbol=instrument.trading_symbol,
            quantity=quantity,
            entry_time=entry_time,
            entry_price=ltp,
            live_trading=self.config.live_trading,
        )

        self.position = OpenPosition(
            option_type=option_type,
            instrument=instrument,
            quantity=quantity,
            entry_price=ltp,
            entry_time=entry_time,
            trade_id=trade_id,
            underlying_entry_price=underlying_ltp,
        )

    def exit(self, ltp: float, reason: str) -> None:
        if self.position is None:
            return
        position = self.position

        if self.config.live_trading:
            result = self.session.call_with_retry(
                "place_order",
                exchange_segment=position.instrument.exchange_segment,
                product="MIS",
                price="0",
                order_type="MKT",
                quantity=str(position.quantity),
                validity="DAY",
                trading_symbol=position.instrument.trading_symbol,
                transaction_type="S",
            )
            logger.info(
                "LIVE order placed: SELL %s x%d %s -> %s (%s)",
                position.option_type, position.quantity, position.instrument.trading_symbol, result, reason,
            )
        else:
            logger.info(
                "[PAPER] Would SELL %s x%d %s @ ~%.2f (%s)",
                position.option_type, position.quantity, position.instrument.trading_symbol, ltp, reason,
            )

        realized_pnl = (ltp - position.entry_price) * position.quantity
        self.risk.record_realized_pnl(realized_pnl)
        storage.record_trade_exit(
            trade_id=position.trade_id,
            exit_time=dt.datetime.now(dt.timezone.utc),
            exit_price=ltp,
            pnl=realized_pnl,
            reason=reason,
        )
        self.position = None
