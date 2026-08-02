"""ATM strike selection and the profit-target check."""
import datetime as dt

from src.config import Config
from src.execution import OpenPosition
from src.instruments import Instrument, atm_strike
from src.main import _target_hit

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _config(underlying: float | None = 30.0, premium: float | None = 15.0) -> Config:
    return Config(
        consumer_key="k", environment="prod", mobile_number="m", ucc="u", mpin="p", totp_secret="s",
        live_trading=False, lot_size=1, max_daily_loss=None,
        target_underlying_points=underlying, target_premium_points=premium, log_level="INFO",
    )


def _position(option_type: str, entry_price: float, underlying: float | None) -> OpenPosition:
    return OpenPosition(
        option_type=option_type,
        instrument=Instrument("1", "nse_fo", "NIFTY24250CE", 65, dt.date(2026, 8, 4)),
        quantity=65,
        entry_price=entry_price,
        entry_time=dt.datetime(2026, 7, 30, 12, 30, tzinfo=IST),
        trade_id=1,
        underlying_entry_price=underlying,
    )


def test_atm_strike_uses_the_spot_index_rounded_to_the_nearest_fifty():
    # 30-Jul-2026 12:25: futures 24315, spot 24264.05. Only the spot rounds to
    # the 24250 strike the strategy calls for.
    assert atm_strike(24264.05) == 24250
    assert atm_strike(24315.0) == 24300
    assert atm_strike(24326.0) == 24350


def test_ce_target_hit_when_underlying_rises_thirty_points():
    position = _position("CE", entry_price=80.0, underlying=24250.0)
    assert _target_hit(position, _config(), spot=24279.0, premium=None) is None
    assert "underlying moved" in _target_hit(position, _config(), spot=24280.0, premium=None)


def test_pe_target_hit_when_underlying_falls_thirty_points():
    position = _position("PE", entry_price=80.0, underlying=24250.0)
    assert _target_hit(position, _config(), spot=24280.0, premium=None) is None
    assert "underlying moved" in _target_hit(position, _config(), spot=24220.0, premium=None)


def test_premium_target_hit_independently_of_the_underlying_leg():
    position = _position("CE", entry_price=80.0, underlying=24250.0)
    assert _target_hit(position, _config(), spot=24250.0, premium=94.0) is None
    assert "premium gained" in _target_hit(position, _config(), spot=24250.0, premium=95.0)


def test_targets_can_be_disabled():
    position = _position("CE", entry_price=80.0, underlying=24250.0)
    disabled = _config(underlying=None, premium=None)
    assert _target_hit(position, disabled, spot=24999.0, premium=999.0) is None


def test_no_underlying_entry_price_falls_back_to_the_premium_leg():
    position = _position("CE", entry_price=80.0, underlying=None)
    assert _target_hit(position, _config(), spot=24999.0, premium=None) is None
    assert "premium gained" in _target_hit(position, _config(), spot=24999.0, premium=100.0)
