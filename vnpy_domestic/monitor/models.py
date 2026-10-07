"""SQLAlchemy models for the monitor database (monitor.db).

Writer side (trading process, via MonitorWriter). The read-only web backend
keeps an IDENTICAL copy at web-backend/web_backend/models.py — change table
definitions in BOTH files together.

All time fields store Beijing-time strings "YYYY-MM-DD HH:MM:SS"; the
frontend renders them with zero conversion.
"""

from sqlalchemy import JSON, Float, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    vt_orderid: Mapped[str] = mapped_column(String(64), index=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    account: Mapped[str] = mapped_column(String(16), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    exchange: Mapped[str] = mapped_column(String(16))
    direction: Mapped[str] = mapped_column(String(8))
    offset: Mapped[str] = mapped_column(String(8))
    price: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    traded: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(16))
    submit_time: Mapped[str] = mapped_column(String(32))
    delay_ms: Mapped[float] = mapped_column(Float, default=0)


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    vt_tradeid: Mapped[str] = mapped_column(String(64), index=True)
    vt_orderid: Mapped[str] = mapped_column(String(64))
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    account: Mapped[str] = mapped_column(String(16), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    exchange: Mapped[str] = mapped_column(String(16))
    price: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    slippage: Mapped[float] = mapped_column(Float, default=0)
    delay_ms: Mapped[float] = mapped_column(Float, default=0)
    trade_time: Mapped[str] = mapped_column(String(32))


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    account: Mapped[str] = mapped_column(String(16), index=True)
    vt_symbol: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(8))
    volume: Mapped[float] = mapped_column(Float)
    avg_price: Mapped[float] = mapped_column(Float)
    pnl: Mapped[float] = mapped_column(Float, default=0)
    trading_day: Mapped[str] = mapped_column(String(16))
    snapshot_time: Mapped[str] = mapped_column(String(32))


class AccountPosition(Base):
    """CTP account-level real positions (separate from strategy positions)."""

    __tablename__ = "account_positions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account: Mapped[str] = mapped_column(String(16), index=True)
    accountid: Mapped[str] = mapped_column(String(32), default="")
    vt_symbol: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(8))
    volume: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    pnl: Mapped[float] = mapped_column(Float, default=0)
    snapshot_time: Mapped[str] = mapped_column(String(32))


class AccountDailyPnl(Base):
    """Account-level intraday P&L (realized + floating, sourced from CTP)."""

    __tablename__ = "account_daily_pnl"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account: Mapped[str] = mapped_column(String(16), index=True)
    accountid: Mapped[str] = mapped_column(String(32), default="")
    trading_day: Mapped[str] = mapped_column(String(16))
    realized_pl: Mapped[float] = mapped_column(Float, default=0)
    floating_pl: Mapped[float] = mapped_column(Float, default=0)
    balance: Mapped[float] = mapped_column(Float, default=0)
    snapshot_time: Mapped[str] = mapped_column(String(32))


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    account: Mapped[str] = mapped_column(String(16), index=True)
    accountid: Mapped[str] = mapped_column(String(32), default="")
    balance: Mapped[float] = mapped_column(Float)
    available: Mapped[float] = mapped_column(Float)
    frozen: Mapped[float] = mapped_column(Float, default=0)
    margin: Mapped[float] = mapped_column(Float, default=0)
    pnl: Mapped[float] = mapped_column(Float, default=0)
    snapshot_time: Mapped[str] = mapped_column(String(32))


class DailyPnl(Base):
    __tablename__ = "daily_pnl"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    trading_day: Mapped[str] = mapped_column(String(16), index=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    account: Mapped[str] = mapped_column(String(16), index=True)
    realized_pl: Mapped[float] = mapped_column(Float)
    cumulative_pl: Mapped[float] = mapped_column(Float)


class StrategyStatus(Base):
    __tablename__ = "strategy_status"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    account: Mapped[str] = mapped_column(String(16), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    pos: Mapped[float] = mapped_column(Float, default=0)
    long_pos: Mapped[float] = mapped_column(Float, default=0)
    short_pos: Mapped[float] = mapped_column(Float, default=0)
    pnl: Mapped[float] = mapped_column(Float, default=0)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    variables: Mapped[dict] = mapped_column(JSON, default=dict)
    update_time: Mapped[str] = mapped_column(String(32))


class StrategyIntraday(Base):
    """Strategy intraday equity snapshot (realized + floating), 15min."""

    __tablename__ = "strategy_intraday"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    account: Mapped[str] = mapped_column(String(16), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    realized_pl: Mapped[float] = mapped_column(Float, default=0)
    floating_pl: Mapped[float] = mapped_column(Float, default=0)
    equity: Mapped[float] = mapped_column(Float, default=0)
    snapshot_time: Mapped[str] = mapped_column(String(32))


class Log(Base):
    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(32), index=True)
    level: Mapped[str] = mapped_column(String(16))
    strategy: Mapped[str] = mapped_column(String(64), default="")
    account: Mapped[str] = mapped_column(String(16), index=True)
    message: Mapped[str] = mapped_column(String(512))


class SystemMetric(Base):
    __tablename__ = "system_metrics"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(32))
    cpu: Mapped[float] = mapped_column(Float)
    mem: Mapped[float] = mapped_column(Float)
    disk: Mapped[float] = mapped_column(Float)
    uptime: Mapped[str] = mapped_column(String(32))


class Kline(Base):
    __tablename__ = "klines"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    exchange: Mapped[str] = mapped_column(String(16))
    account: Mapped[str] = mapped_column(String(16), index=True)
    interval: Mapped[str] = mapped_column(String(8))
    datetime: Mapped[str] = mapped_column(String(32), index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)


class TradeRound(Base):
    __tablename__ = "trade_rounds"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    exchange: Mapped[str] = mapped_column(String(16))
    account: Mapped[str] = mapped_column(String(16), index=True)
    direction: Mapped[str] = mapped_column(String(8))
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    pnl: Mapped[float] = mapped_column(Float)
    holding_seconds: Mapped[float] = mapped_column(Float, default=0)
    open_time: Mapped[str] = mapped_column(String(32))
    close_time: Mapped[str] = mapped_column(String(32))
    trading_day: Mapped[str] = mapped_column(String(16))


class Contract(Base):
    """One row per contract; upserted by the trading process on every monitor
    cycle so rollover/new contracts refresh automatically (symbol is the PK)."""

    __tablename__ = "contracts"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(16), default="")
    name: Mapped[str] = mapped_column(String(64), default="")
    product: Mapped[str] = mapped_column(String(32), default="")
    size: Mapped[float] = mapped_column(Float, default=0)
    pricetick: Mapped[float] = mapped_column(Float, default=0)
    update_time: Mapped[str] = mapped_column(String(32))
