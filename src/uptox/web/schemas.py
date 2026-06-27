"""
Pydantic models used only for OpenAPI schema documentation (Swagger/ReDoc).
Routes in app.py do NOT declare these as `response_model` — they keep
returning plain dict/list as before. These are wired in via
`responses={200: {"model": ...}}`, which documents the real shape without
forcing FastAPI to validate/coerce the actual response through it.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class Trade(BaseModel):
    id: str = Field(description="Short unique trade id")
    date: str = Field(description="YYYY-MM-DD")
    time_entry: str
    entry_ts: float = Field(description="Unix timestamp at entry")
    time_exit: str | None = Field(default=None, description="null while the trade is open")
    exit_ts: float | None = Field(default=None, description="Unix timestamp at exit, null while open")
    strike: int
    option: str = Field(description="CE or PE")
    expiry: str = Field(description="Option expiry date, YYYY-MM-DD")
    option_key: str
    entry_price: float
    exit_price: float | None = Field(default=None, description="null while open")
    lots: int
    invested: float
    sl: float
    target: float
    delta: float | None = None
    spot_entry: float = Field(description="Nifty 50 level at entry")
    spot_current: float | None = Field(default=None, description="Nifty 50 level, last seen tick")
    spot_peak: float | None = Field(default=None, description="Best Nifty 50 level reached in the trade's favor")
    spot_exit: float | None = Field(default=None, description="Nifty 50 level at exit, set only once closed")
    spot_move_pts: float | None = Field(default=None, description="Raw signed Nifty move, entry to exit (closed trades only)")
    spot_directional_pts: float | None = Field(default=None, description="Signed so + always means Nifty moved in the trade's favor (closed trades only)")
    spot_peak_move_pts: float | None = Field(default=None, description="Best favorable Nifty move reached before exit (closed trades only)")
    current_price: float | None = Field(default=None, description="Live option price, only present while open")
    pnl_pts: float
    pnl_rs: float
    outcome: str = Field(description="OPEN, WIN, LOSS, TARGET_HIT, SL_HIT, or BREAKEVEN")
    exit_reason: str | None = None
    entry_reason: str
    score: int
    sma_score: int
    flow_score: int
    atr_at_entry: float
    expiry_day: bool
    peak_price: float | None = Field(default=None, description="Best option price reached (closed trades only)")
    max_pnl_pts: float | None = Field(default=None, description="Max unrealised P&L pts before close (closed trades only)")
    max_pnl_rs: float | None = None


class TradeStats(BaseModel):
    total: int
    wins: int
    losses: int
    win_rate: float = Field(description="Percent, 0-100")
    total_pnl_pts: float
    total_pnl_rs: float
    avg_win: float = Field(description="Average winning trade, in option pts")
    avg_loss: float = Field(description="Average losing trade, in option pts (negative)")
    best: float = Field(description="Best single trade, option pts")
    worst: float = Field(description="Worst single trade, option pts")
    expectancy: float = Field(description="Expected pts per trade given current win rate/avg win/loss")
    avg_rr: float = Field(description="Average win / average loss ratio")


class TodayStats(TradeStats):
    date: str
    pnl_pct_today: float = Field(description="Today's P&L as percent of current capital")


class DailyBreakdownItem(TradeStats):
    date: str


class CapitalInfo(BaseModel):
    base: float = Field(description="Starting capital")
    current: float = Field(description="Base + lifetime realised P&L")
    realised_lifetime: float
    daily_loss_pct: float | None = Field(default=None, description="% of capital that blocks new trades for the day")
    max_trades_per_day: int | None = None
    max_consec_losses: int | None = None


class NotificationSetting(BaseModel):
    enabled: bool
    interval_min: float | None = Field(default=None, description="Only present for trade_update / market_update")
    threshold_pct: float | None = Field(default=None, description="Only present for approaching_limit")
    weekday: str | None = Field(default=None, description="Only present for weekly_report, e.g. 'saturday'")
    time: str | None = Field(default=None, description="Only present for weekly_report, 24h HH:MM")


class NotificationEnabledBody(BaseModel):
    enabled: bool


class NotificationSubBody(BaseModel):
    sub_key: str = Field(description="e.g. interval_min, threshold_pct, weekday, time")
    value: float | str
