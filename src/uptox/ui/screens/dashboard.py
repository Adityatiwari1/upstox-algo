import logging
from textual.app import ComposeResult
from textual.containers import ScrollableContainer, Horizontal, Vertical
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static, Input
from uptox.services import feed_store, sma_service, option_chain_service, capital_service
from uptox.services import pattern_service, trailing_sl_service, trade_book, adaptive_params, bot_control
from uptox.services.sma_service import TimeFrame, ALL_TFS

log = logging.getLogger(__name__)

NIFTY = "NSE_INDEX|Nifty 50"
BANK  = "NSE_INDEX|Nifty Bank"


def _fmt(v: float, dec: int = 2) -> str:
    return f"{v:,.{dec}f}" if v else "—"


def _signal(delta: float, cum: float, oi_chg: float) -> tuple[str, str]:
    score = 0
    if delta > 0: score += 1
    if cum > 0:   score += 1
    if oi_chg > 0: score += 1
    if score >= 2:
        return "● BUY", "green"
    elif score == 0:
        return "● SELL", "red"
    return "◐ NEUTRAL", "yellow"


def _kv(rows: list[tuple[str, str, str]]) -> str:
    return "\n".join(
        f"[dim]{label:<12}[/] [{color}]{value}[/]"
        for label, value, color in rows
    )


def _affordable_strike(ltp: float, option: str) -> tuple[int, str | None, float]:
    """
    Return (strike, instrument_key, price) of best tradeable strike.

    Priority:
      1. ATM if price >= ₹50 and capital affordable
      2. ITM (1-4 steps) if ATM < ₹50 — cheap-ATM day, ITM has intrinsic value
      3. OTM (1-4 steps) if ATM > capital — price too high, go cheaper

    Returns (atm, None, 0.0) if no suitable strike found.
    """
    atm      = round(ltp / 50) * 50
    itm_step = -50 if option == "CE" else 50   # ITM: lower for CE, higher for PE
    otm_step = -itm_step                        # OTM: opposite direction

    def _get_price(strike: int) -> tuple[str | None, float]:
        key = option_chain_service.get_key(strike, option)
        if not key:
            return None, 0.0
        lo = feed_store.get(key)
        if lo and lo.asks:
            price = float(lo.asks[0].get("price", 0.0)) or lo.ltp
        elif lo:
            price = lo.ltp
        else:
            price = float(
                (option_chain_service.get_option(strike, option) or {}).get("ltp", 0.0)
            )
        return key, price

    atm_key, atm_price = _get_price(atm)

    # ATM < ₹50 → cheap-ATM day, try ITM for intrinsic value
    if 0 < atm_price < 50:
        for n in range(1, 5):
            strike      = atm + n * itm_step
            key, price  = _get_price(strike)
            if key and price >= 50 and capital_service.sizing(price)["lots"] > 0:
                return strike, key, price
        return atm, None, 0.0   # even ITM < ₹50, market too cheap — skip

    # Normal path: ATM first, OTM if ATM unaffordable
    for n in range(5):
        strike     = atm + n * otm_step
        key, price = _get_price(strike)
        if key and price and capital_service.sizing(price)["lots"] > 0:
            return strike, key, price
    return atm, None, 0.0


_swing_cache: tuple[int, list[tuple[str, float]]] = (-1, [])


def _swing_levels(n_bars: int = 36) -> list[tuple[str, float]]:
    """
    Recent 5-min swing highs/lows — fractal pivots (bar extreme vs 2 bars on
    each side) over the last ~3 hours. These are the intraday S/R levels
    traders actually watch; adding them to _near_sr means their BREAKS become
    entries through the existing fresh-break logic (S/R clears → flow
    confirms → fire; flow contradicts → retest watch).
    Returns the 2 most recent swing highs and lows, deduped by value.
    Result cached per 5-min slot — pivots only change on bar close, and this
    runs in the GUI thread on every entry check.
    """
    global _swing_cache
    import time as _time
    _slot = int(_time.time() // 300)
    if _swing_cache[0] == _slot:
        return _swing_cache[1]
    bars = sma_service.get_last_candles(NIFTY, TimeFrame.M5, n=n_bars)
    if len(bars) < 5:
        return []
    highs = [b[1] for b in bars]
    lows  = [b[2] for b in bars]
    sh: list[float] = []
    sl: list[float] = []
    for i in range(2, len(bars) - 2):
        if highs[i] == max(highs[i - 2:i + 3]) and highs[i] not in sh:
            sh.append(highs[i])
        if lows[i] == min(lows[i - 2:i + 3]) and lows[i] not in sl:
            sl.append(lows[i])
    out = ([("Swing High", v) for v in sh[-2:]]
           + [("Swing Low", v) for v in sl[-2:]])
    if out:   # don't cache the warm-up empty result — candles may still be loading
        _swing_cache = (_slot, out)
    return out


def _near_sr(ltp: float, option: str) -> tuple[str | None, float]:
    """
    Return (reason, level) if ltp is within SR_TOL points of a key level that
    would act as resistance (CE) or support (PE). Returns (None, 0.0) if clear.
    """
    SR_TOL = adaptive_params.get("sr_tolerance")
    levels: list[tuple[str, float]] = []

    # SMA levels on M5 and H1 are the most watched S/R by algo traders
    for tf, label in ((TimeFrame.M5, "5m"), (TimeFrame.H1, "1h")):
        smas = sma_service.get_smas(NIFTY, tf)
        for p in (20, 50, 100, 200):
            v = smas.get(p)
            if v:
                levels.append((f"SMA{p}/{label}", v))

    # Round hundred levels ±200pt around current price
    base = round(ltp / 100) * 100
    for off in (-200, -100, 0, 100, 200):
        levels.append((f"Round {int(base + off)}", float(base + off)))

    # 5-min swing highs/lows — freshest intraday S/R; breaks trade via the
    # fresh-break logic downstream
    levels.extend(_swing_levels())

    # Previous-day levels — static all session (unlike today's high/low),
    # and the most-watched intraday S/R by institutions and algos.
    from uptox.services import prev_day_service
    _pd = prev_day_service.get_ohlc()
    if _pd:
        levels.append(("PDH", _pd["high"]))
        levels.append(("PDL", _pd["low"]))
        _piv = prev_day_service.get_pivots()
        if _piv:
            levels.append(("Pivot PP", _piv["pp"]))
            levels.append(("Pivot R1", _piv["r1"]))
            levels.append(("Pivot S1", _piv["s1"]))

    # Day High/Low intentionally excluded — they update tick-by-tick so would
    # permanently block CE at new highs / PE at new lows. Handled instead by
    # the bounce/retreat filter in the main no_trade_reason chain.

    for label, lvl in levels:
        dist = ltp - lvl  # positive = ltp above level, negative = level above ltp
        if abs(dist) > SR_TOL:
            continue
        # CE bullish: block only when level is AT or ABOVE ltp (dist <= 0 = resistance overhead)
        # PE bearish: block only when level is AT or BELOW ltp (dist >= 0 = support underfoot)
        if option == "CE" and dist <= 0:
            return f"Resistance: {label} ({abs(dist):.0f}pt above)", lvl
        if option == "PE" and dist >= 0:
            return f"Support: {label} ({abs(dist):.0f}pt below)", lvl
    return None, 0.0


_blocked_log_ts: dict[str, float] = {}

def _log_blocked_entry(reason: str, option: str, ltp: float, score: int,
                       entry_type: str | None = None) -> None:
    """
    Append a blocked-entry snapshot to logs/blocked_entries.jsonl.
    Deduped to once per 5 min per (option, reason-prefix). Join later with
    logs/market_data/*.jsonl to measure each filter's false-negative rate —
    i.e. which filters block trades that would have won.
    """
    import json as _json
    import re as _re
    import time as _time
    # Window/pre-market/no-feed blocks are schedule facts, not filter decisions — skip
    if "trading window" in reason or "Pre-market" in reason or "No live data" in reason:
        return
    # Strip digits from the dedupe key — reasons embed live numbers ("74%",
    # "12pt") which defeated the 5-min dedupe and spammed hundreds of rows
    key = f"{option}|{_re.sub(r'[0-9.]+', 'N', reason.split('—')[0].strip())[:40]}"
    now = _time.time()
    if now - _blocked_log_ts.get(key, 0.0) < 300:
        return
    _blocked_log_ts[key] = now
    try:
        from pathlib import Path as _Path
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _zi
        p = _Path("logs/blocked_entries.jsonl")
        p.parent.mkdir(exist_ok=True)
        rec = {
            "ts":         _dt.now(_zi("Asia/Kolkata")).isoformat(timespec="seconds"),
            "option":     option,
            "ltp":        round(ltp, 1),
            "score":      score,
            "entry_type": entry_type,
            "reason":     reason,
        }
        with p.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # logging must never break the trading loop


class OverallAnalysisPanel(Static):
    _last_tradeable: bool | None = None
    _last_option: str | None = None

    # ── breakout-watch state ───────────────────────────────────────────────────
    _watch: str      = "IDLE"   # "IDLE" | "WATCH_CE" | "WATCH_PE"
    _bk_ltp: float   = 0.0     # Nifty LTP at the moment score crossed ±2
    _bk_t: float     = 0.0     # time.time() when watch started
    _bk_max_dist: float = 0.0  # max pts price traveled away from breakout level
    _RETEST_PTS:  int = 20     # must return within 20pts after departure
    _why_tag: str = ""         # human-readable reason why no trade entered yet

    # ── re-entry state (after SL hit) ─────────────────────────────────────────
    _last_sl_time: float   = 0.0   # time.time() when last SL/trail closed
    _last_sl_option: str   = ""    # direction of that closed trade
    _had_open_trade: bool  = False  # track trade→no-trade transition
    _REENTRY_COOLDOWN: int = 600   # 10 minutes

    # ── sustained-signal tracking (consecutive 5-min bars same sma_score ±2) ──
    _consec_sma_bars: int   = 0
    _last_sma_bar_slot: int = -1
    _last_sma_score: int    = 0
    _last_ce_iv: float      = 0.0
    _last_pe_iv: float      = 0.0
    _last_atm:   int        = 0

    # ── intraday high/low (reset daily, used for bounce/retreat filter) ────────
    _session_low:  float = float("inf")
    _session_high: float = 0.0
    _session_date: str   = ""
    _session_open: float = 0.0   # first live tick of the day (approx. open; restart mid-day = restart price)

    # ── S/R breakout quality + retest tracking ───────────────────────────────
    _sr_blocked_prev:   bool  = False  # was S/R blocking an entry attempt earlier
    _sr_block_level:    float = 0.0   # the level that blocked (breakout anchor for retest)
    _sr_block_option:   str   = ""    # direction the block applied to
    _sr_block_ts:       float = 0.0   # when the block was recorded — context expires
    _SR_BLOCK_TTL:      int   = 600   # seconds a block stays valid as "fresh break" context
    _SR_AWAY_CLEAR:     float = 25.0  # pts price must drift AWAY from a blocking level
                                      # before away-side entry is allowed (break not required)
    _sr_retest_mode:    bool  = False  # fake break → waiting for retest + 2 confirming bars
    _sr_retest_ts:      float = 0.0   # when retest watch started — expires via TTL
    _SR_RETEST_TTL:     int   = 1800  # 30 min; survives filter wobble but not forever
    _sr_retest_level:   float = 0.0   # the S/R level that was broken (to watch for retest)
    _sr_retest_option:  str   = ""    # CE or PE direction of the break
    _sr_retest_bars:    int   = 0     # consecutive confirming bars counted so far
    _sr_retest_last_bar: int  = -1   # bar_slot of the last counted bar (avoid double-count)

    # ── Opening Range Breakout (09:15–09:30 window) ───────────────────────────
    _orb_high:     float = 0.0
    _orb_low:      float = float("inf")
    _orb_ready:    bool  = False   # True once 09:30 passes and range is captured
    _orb_date:     str   = ""      # date when ORB was last set (reset daily)
    _orb_ce_done:  bool  = False   # CE ORB trade already taken today
    _orb_pe_done:  bool  = False   # PE ORB trade already taken today
    _orb_watching: bool  = False   # ORB break detected, waiting for departure confirmation

    # ── Prev-Day High/Low breakout (same mechanics as ORB, all-day window) ────
    _pdx_watching: bool = False    # PDH/PDL break detected, waiting for departure
    _pdx_ce_done:  bool = False    # PDH-break CE trade already taken today
    _pdx_pe_done:  bool = False    # PDL-break PE trade already taken today
    _pdx_date:     str  = ""       # daily reset marker


    def on_mount(self) -> None:
        self.border_title = "Overall Analysis"
        self._refresh()
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        import time
        t = feed_store.get(NIFTY)
        live_ltp = t.ltp if t else 0.0
        ltp = live_ltp or option_chain_service.get_spot()

        # Track intraday high/low (reset at midnight IST)
        from datetime import date as _date, datetime as _datetime
        from zoneinfo import ZoneInfo as _ZoneInfo
        _now_ist = _datetime.now(_ZoneInfo("Asia/Kolkata"))
        _today = _date.today().isoformat()
        _mkt_open_now = (_now_ist.hour, _now_ist.minute) >= (9, 15)
        if _today != self._session_date:
            self._session_date = _today
            self._session_low  = float("inf")
            self._session_high = 0.0
            self._session_open = 0.0
            # Restart-proof: rebuild today's open/high/low from M5 history —
            # primed via the intraday candle API, so it covers bars from before
            # this restart. Only trust it when the bar count matches the clock
            # (otherwise older bars would leak in and fake the session open).
            if _mkt_open_now:
                _n = max(0, ((_now_ist.hour * 60 + _now_ist.minute) - (9 * 60 + 15)) // 5)
                if _n:
                    _tb = sma_service.get_last_candles(NIFTY, TimeFrame.M5, n=_n)
                    if len(_tb) == _n:
                        self._session_open = _tb[0][0]
                        self._session_high = max(b[1] for b in _tb)
                        self._session_low  = min(b[2] for b in _tb)
        # Pre-9:15 ticks excluded — pre-open freak prints (24,614 print seen
        # 2026-07-03 09:00, 240pt off) poisoned session high and the gap calc.
        if live_ltp and _mkt_open_now:
            if not self._session_open:
                self._session_open = live_ltp
            if live_ltp < self._session_low:  self._session_low  = live_ltp
            if live_ltp > self._session_high: self._session_high = live_ltp

        # ── Opening Range Breakout range tracking (09:15–09:30) ──────────────
        if _today != self._orb_date:
            self._orb_date    = _today
            self._orb_high    = 0.0
            self._orb_low     = float("inf")
            self._orb_ready   = False
            # Restore once-per-day flags from today's trade log — in-memory
            # flags reset on restart, which allowed duplicate ORB/PDH trades
            # after a mid-session deploy.
            _taken = trade_book.entries_taken_today()
            self._orb_ce_done = ("orb", "CE") in _taken
            self._orb_pe_done = ("orb", "PE") in _taken
        if _today != self._pdx_date:
            self._pdx_date     = _today
            self._pdx_watching = False
            _taken = trade_book.entries_taken_today()
            self._pdx_ce_done  = ("pdh_break", "CE") in _taken
            self._pdx_pe_done  = ("pdl_break", "PE") in _taken
        if live_ltp:
            _h, _m = _now_ist.hour, _now_ist.minute
            _in_orb_window = (_h == 9 and 15 <= _m < 30)
            _after_orb     = (_h > 9 or (_h == 9 and _m >= 30))
            if _in_orb_window:
                if live_ltp > self._orb_high: self._orb_high = live_ltp
                if live_ltp < self._orb_low:  self._orb_low  = live_ltp
            elif _after_orb and not self._orb_ready and self._orb_high > 0 and self._orb_low < float("inf"):
                self._orb_ready = True
                log.info("ORB set: high=%.1f  low=%.1f", self._orb_high, self._orb_low)

        # ATM strike (nearest 50)
        atm = round(ltp / 50) * 50 if ltp else 0

        # SMA trend score from 5min (most responsive)
        # Min 8pt gap required — crosses within 8pts of SMA are noise in a ranging market
        _SMA_MIN_GAP = 8.0
        smas  = sma_service.get_smas(NIFTY, TimeFrame.M5)
        sma20 = smas.get(20)
        sma50 = smas.get(50)
        sma_ready = sma20 is not None and sma50 is not None
        sma_score = 0
        if ltp and sma20:
            if   ltp > sma20 + _SMA_MIN_GAP: sma_score += 1
            elif ltp < sma20 - _SMA_MIN_GAP: sma_score -= 1
        if ltp and sma50:
            if   ltp > sma50 + _SMA_MIN_GAP: sma_score += 1
            elif ltp < sma50 - _SMA_MIN_GAP: sma_score -= 1

        # Track consecutive 5-min bars where |sma_score| == 2 (for sustained-signal entry)
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        _bar_slot = _dt.now(_ZI("Asia/Kolkata")).hour * 12 + _dt.now(_ZI("Asia/Kolkata")).minute // 5
        if _bar_slot != self._last_sma_bar_slot:
            if self._last_sma_bar_slot != -1:
                if abs(sma_score) == 2 and sma_score == self._last_sma_score:
                    self._consec_sma_bars += 1
                elif abs(sma_score) == 2:
                    self._consec_sma_bars = 1
                else:
                    self._consec_sma_bars = 0
            else:
                self._consec_sma_bars = 1 if abs(sma_score) == 2 else 0
            self._last_sma_bar_slot = _bar_slot
            self._last_sma_score = sma_score

        # Order flow score — IndexFullFeed v3 has no tbq/tsq/oi; read from ATM options
        if atm:
            _ck  = option_chain_service.get_key(atm, "CE")
            _pk  = option_chain_service.get_key(atm, "PE")
            _tce = feed_store.get(_ck) if _ck else None
            _tpe = feed_store.get(_pk) if _pk else None
            _csn = option_chain_service.get_option(atm, "CE") or {}
            _psn = option_chain_service.get_option(atm, "PE") or {}
            _ce_buy, _ce_sell = feed_store.flow_qty(_tce)
            _pe_buy, _pe_sell = feed_store.flow_qty(_tpe)
            # CE net buy = bullish; PE net buy = bearish — subtract, don't add
            raw_delta = (_ce_buy - _ce_sell) - (_pe_buy - _pe_sell)
            # Rolling window, NOT session-cumulative: session delta goes stale —
            # 2026-07-07 morning buying (+70k) masked a 170pt afternoon
            # breakdown and flow stayed bullish through the whole crash.
            cum    = feed_store.rolling_delta(_ck, minutes=60) if _ck else 0.0
            # OI change from ±3 strikes (same data as OI Signal display — broader than ATM only)
            _ce_chg, _pe_chg = option_chain_service.get_oi_chg(live_ltp) if live_ltp else (0.0, 0.0)
            # Live tick IV preferred (per-tick, real value); REST snapshot as fallback
            ce_iv = (_tce.iv if _tce and _tce.iv else 0.0) or _csn.get("iv", 0.0)
            pe_iv = (_tpe.iv if _tpe and _tpe.iv else 0.0) or _psn.get("iv", 0.0)
            # Reset IV history when ATM changes — old strike's IV is not comparable
            if atm != self._last_atm:
                self._last_ce_iv = 0.0
                self._last_pe_iv = 0.0
                self._last_atm   = atm
            # OI rising + IV rising = fresh buying; OI rising + IV flat/falling = writing
            ce_iv_rising = bool(ce_iv and self._last_ce_iv and ce_iv > self._last_ce_iv)
            pe_iv_rising = bool(pe_iv and self._last_pe_iv and pe_iv > self._last_pe_iv)
            if ce_iv: self._last_ce_iv = ce_iv
            if pe_iv: self._last_pe_iv = pe_iv
            # delta_sc only valid when exchange provides actual trade flow (tbq/tsq)
            # depth-based raw_delta is always bid-heavy for index options — not directional
            _has_trade_flow = bool((_tce and (_tce.tbq or _tce.tsq)) or
                                   (_tpe and (_tpe.tbq or _tpe.tsq)))
        else:
            raw_delta = cum = 0.0
            _pe_chg = _ce_chg = 0.0
            ce_iv_rising = pe_iv_rising = False
            _has_trade_flow = False
        delta_sc = (1 if raw_delta > 0 else -1 if raw_delta < 0 else 0) if _has_trade_flow else 0
        cum_sc   = 1 if cum > 0 else -1 if cum < 0 else 0
        # IV-adjusted: OI↑+IV↑ = buying (directional); OI↑+IV↓ = writing (contrarian)
        if _pe_chg > _ce_chg:
            oi_sc = -1 if pe_iv_rising else 1   # PE buying=bear; PE writing=bull floor
        elif _ce_chg > _pe_chg:
            oi_sc = 1 if ce_iv_rising else -1   # CE buying=bull; CE writing=ceiling=bear
        else:
            oi_sc = 0
        flow_score = delta_sc + cum_sc + oi_sc

        total = sma_score + flow_score  # range: -5 … +5

        # ── trend label ───────────────────────────────────────────────────
        if total >= 4:
            trend, trend_color, option = "STRONG BULL", "green",  "CE"
        elif total >= 2:
            trend, trend_color, option = "BULLISH",     "green",  "CE"
        elif total <= -4:
            trend, trend_color, option = "STRONG BEAR", "red",    "PE"
        elif total <= -2:
            trend, trend_color, option = "BEARISH",     "red",    "PE"
        else:
            trend, trend_color, option = "NEUTRAL",     "yellow", "—"

        # ── external filters: VIX, IV rank, Bank Nifty, PCR ─────────────────
        from uptox.services import iv_rank_service

        # Bank Nifty alignment (must agree with Nifty direction)
        bn        = feed_store.get("NSE_INDEX|Nifty Bank")
        bn_ltp    = (bn.ltp if bn else 0.0) or sma_service.get_last_close("NSE_INDEX|Nifty Bank", TimeFrame.M5) \
                    or sma_service.get_last_close("NSE_INDEX|Nifty Bank", TimeFrame.H1)
        bn_s20    = sma_service.get_sma("NSE_INDEX|Nifty Bank", sma_service.get_current_tf(), 20)
        bn_s50    = sma_service.get_sma("NSE_INDEX|Nifty Bank", sma_service.get_current_tf(), 50)
        bn_score  = (1 if (bn_ltp and bn_s20 and bn_ltp > bn_s20) else -1 if bn_s20 else 0) + \
                    (1 if (bn_ltp and bn_s50 and bn_ltp > bn_s50) else -1 if bn_s50 else 0)
        # Skip BN conflict check when no live feed (avoid stale candle data blocking entries)
        bn_conflict = bool(bn_ltp and bn_s20 and bn_s50) and \
                      ((option == "CE" and bn_score < 0) or (option == "PE" and bn_score > 0))

        # IV Rank on ATM option
        atm_iv_key = option_chain_service.get_key(atm, option) if (atm and option != "—") else None
        iv_st      = iv_rank_service.get_status(atm_iv_key) if atm_iv_key else {}
        high_iv    = iv_st.get("high_iv", False)

        # PCR from ±3 strikes OI (filter check only — display PCR comes from option_chain_service)
        _ce_oi = _pe_oi = 0.0
        if atm and ltp:
            for off in range(-3, 4):
                s = atm + off * 50
                _ce_oi += float((option_chain_service.get_option(s, "CE") or {}).get("oi", 0))
                _pe_oi += float((option_chain_service.get_option(s, "PE") or {}).get("oi", 0))
        _pcr_filter    = round(_pe_oi / _ce_oi, 2) if _ce_oi else 0.0
        _pcr_bull_warn = _pcr_filter > 0 and _pcr_filter < adaptive_params.get("pcr_bull_block")
        _pcr_bear_warn = _pcr_filter > adaptive_params.get("pcr_bear_block")

        # Bid-ask spread on entry-side ATM option (hard block — objective liquidity check)
        _entry_tick = (_tce if option == "CE" else _tpe) if atm else None
        _spread = 0.0
        if _entry_tick and _entry_tick.asks and _entry_tick.bids:
            _spread = (_entry_tick.asks[0]["price"] - _entry_tick.bids[0]["price"])

        # ── trade validity ────────────────────────────────────────────────
        from uptox.services.market_calendar import is_trade_window, is_premarket, get_event_today, event_min_score
        today_event   = get_event_today()
        req_score     = max(int(adaptive_params.get("min_score")), event_min_score())

        # Consolidating regime: range-bound tape shakes out breakout entries
        # that would be fine in a trend — demand one extra score point.
        from uptox.services import atr_service as _atr_regime
        consol_regime = _atr_regime.get_regime()["regime"] == "CONSOLIDATING"
        if consol_regime:
            req_score += 1

        # Gap-day context: big overnight gap (>50pt) makes the first hour treacherous —
        # gap fills/fades trap breakout entries, so demand one extra score point till 10:30.
        _prev_close = t.close if t and t.close else 0.0
        _gap_pts    = (self._session_open - _prev_close) if (self._session_open and _prev_close) else 0.0
        _before_1030 = _now_ist.hour < 10 or (_now_ist.hour == 10 and _now_ist.minute < 30)
        gap_day = abs(_gap_pts) > 50 and _before_1030
        if gap_day:
            req_score += 1

        # Day-type bias from prev-day range: opening outside yesterday's range
        # signals a breakaway day — counter-direction entries need one extra point
        # all day (fading a breakaway open is the lower-probability side).
        from uptox.services import prev_day_service
        _pdo = prev_day_service.get_ohlc()
        day_bias = ""
        if _pdo and self._session_open:
            if self._session_open > _pdo["high"]:
                day_bias = "CE"   # opened above PDH — bullish breakaway
            elif self._session_open < _pdo["low"]:
                day_bias = "PE"   # opened below PDL — bearish breakdown
        counter_bias = day_bias and option not in ("—", day_bias)
        if counter_bias:
            req_score += 1

        no_trade_reason = None
        if bot_control.is_paused():
            no_trade_reason = "Paused via Telegram (/resume to re-enable)"
        elif not live_ltp:
            no_trade_reason = "No live data"
        elif not sma_ready:
            no_trade_reason = "SMA loading..."
        elif is_premarket():
            no_trade_reason = "Pre-market — trading starts 09:30"
        elif not is_trade_window():
            no_trade_reason = "Outside trading window (09:30–15:25)"
        elif option == "—":
            no_trade_reason = "Trend too weak"
        elif high_iv and not capital_service.is_expiry_afternoon():
            # Expiry afternoon exempted: premiums are cheap in rupees and gamma
            # is the trade — "options expensive" is the wrong lens there. The
            # block killed the 13:08 PE on 2026-07-07 (rank 88%) which then
            # ran +37%; June 30 expiry winners fired at similar IV.
            no_trade_reason = f"IV rank high ({iv_st.get('iv_rank', 0):.0f}%) — options expensive"
        elif _spread > 5:
            no_trade_reason = f"Bid-ask spread {_spread:.1f}pts — liquidity thin"
        elif (
            not capital_service.is_expiry_day()
            and (_now_ist.hour > 14 or (_now_ist.hour == 14 and _now_ist.minute >= 30))
            and iv_st.get("iv_rank", 100) < 30
        ):
            no_trade_reason = (
                f"Premium decay risk — IV rank {iv_st.get('iv_rank', 0):.0f}% after 14:30"
                f" (theta kills low-IV options in last hour)"
            )
        elif sma_score != 0 and flow_score != 0 and (sma_score > 0) != (flow_score > 0):
            no_trade_reason = "SMA vs Flow divergence"
        elif abs(total) < req_score:
            if today_event:
                no_trade_reason = f"Event day ({today_event}) — need score ≥{req_score}, got {total:+d}"
            elif counter_bias:
                no_trade_reason = (
                    f"Counter-bias — opened {'above PDH' if day_bias == 'CE' else 'below PDL'},"
                    f" {option} needs score ≥{req_score}, got {total:+d}"
                )
            elif gap_day:
                no_trade_reason = (
                    f"Gap day ({_gap_pts:+.0f}pt open) — need score ≥{req_score} till 10:30, got {total:+d}"
                )
            elif consol_regime:
                no_trade_reason = (
                    f"Consolidating regime — need score ≥{req_score}, got {total:+d}"
                )
            else:
                no_trade_reason = "Weak conviction"
        # Post-trade cooldown, bounce/retreat filter, consolidation check
        else:
            import time as _t
            _dir_locked, _lock_left = trade_book.direction_locked(option, live_ltp)
            if _dir_locked:
                no_trade_reason = (
                    f"{option} side locked — 2 consecutive {option} losses"
                    f" ({_lock_left:.0f}min left)"
                )
            _last_exit = trade_book.get_last_exit()
            if no_trade_reason is None and _last_exit:
                _exit_ts  = _last_exit.get("exit_ts") or 0.0
                _mins_ago = (_t.time() - _exit_ts) / 60.0
                _exit_opt = _last_exit.get("option", "")
                # Only a REAL loss earns the 15-min same-direction cooldown;
                # scratch exits get the normal 5-min any-exit cooldown below
                _was_loss = _last_exit.get("pnl_pts", 0) <= trade_book.REAL_LOSS_PTS
                if _was_loss and _exit_opt == option and _mins_ago < 15:
                    # Allow early re-entry if market resumed strongly past original entry level.
                    # Price > entry_spot+15 (CE) or < entry_spot-15 (PE) proves the SL
                    # was a shakeout, not a direction change.
                    _entry_spot = _last_exit.get("spot_entry", 0.0)
                    _resumed = (
                        (option == "CE" and live_ltp > _entry_spot + 15) or
                        (option == "PE" and live_ltp < _entry_spot - 15)
                    ) if _entry_spot else False
                    if not _resumed:
                        no_trade_reason = f"Cooldown after {option} loss — {15 - _mins_ago:.0f}min left"
                elif _mins_ago < 5:
                    no_trade_reason = f"Cooldown — {5 - _mins_ago:.0f}min after last exit"

            if no_trade_reason is None:
                _recent = sma_service.get_last_candles(NIFTY, TimeFrame.M5, n=2)
                _bounce_pts  = (live_ltp - self._session_low)  if self._session_low < float("inf") else 0.0
                _retreat_pts = (self._session_high - live_ltp) if self._session_high > 0 else 0.0
                _both_green = (len(_recent) == 2 and _recent[-1][3] > _recent[-1][0] and _recent[-2][3] > _recent[-2][0])
                _both_red   = (len(_recent) == 2 and _recent[-1][3] < _recent[-1][0] and _recent[-2][3] < _recent[-2][0])
                if option == "PE" and _bounce_pts > 25 and _both_green:
                    no_trade_reason = f"Bounce active +{_bounce_pts:.0f}pts from low — wait for red bar"
                elif option == "CE" and _retreat_pts > 25 and _both_red:
                    no_trade_reason = f"Retreat active -{_retreat_pts:.0f}pts from high — wait for green bar"
                else:
                    # Consolidation check: if last 6 bars (30min) sit in a range < 1.5×ATR the
                    # market is choppy/ranging — score can be high but there's no clean trend.
                    _congest = sma_service.get_last_candles(NIFTY, TimeFrame.M5, n=6)
                    if len(_congest) >= 4:
                        from uptox.services import atr_service as _atr_svc
                        _atr_m5   = _atr_svc.get_atr(NIFTY, TimeFrame.M5) or 30.0
                        _c_hi     = max(b[1] for b in _congest)
                        _c_lo     = min(b[2] for b in _congest)
                        _c_range  = _c_hi - _c_lo
                        # One-way grind is trend, not chop — a small range only
                        # means consolidation when price went nowhere inside it.
                        # Gap mornings inflate ATR so a steady +45pt climb read
                        # as "consolidating" (2026-07-10: blocked 09:50-10:25,
                        # entry finally at the top). Efficiency = net move/range.
                        _c_net = abs(_congest[-1][3] - _congest[0][0])
                        _c_eff = (_c_net / _c_range) if _c_range > 0 else 0.0
                        if _c_range < _atr_m5 * 1.5 and _c_eff < 0.6:
                            no_trade_reason = (
                                f"Consolidating — {_c_range:.0f}pt range in 30min "
                                f"(need >{_atr_m5 * 1.5:.0f}, drift {_c_eff:.0%})"
                            )
        # Bank Nifty conflict is advisory only — shown as warning but does NOT block entry

        tradeable = no_trade_reason is None

        # Track filter false-negatives: score was strong enough to trade but a
        # filter said no. Analyzed offline against market_data to tune filters.
        if not tradeable and option != "—" and abs(total) >= req_score:
            _log_blocked_entry(no_trade_reason, option, ltp, total)

        # ── alert on signal change ────────────────────────────────────────
        if self._last_tradeable is not None and self._last_tradeable != tradeable:
            self.app.bell()
            log.info("Signal flip: %s → %s  (score=%+d)",
                     "TRADE" if self._last_tradeable else "NO TRADE",
                     "TRADE" if tradeable else "NO TRADE", total)
        self._last_tradeable = tradeable

        # ── option key + live price ───────────────────────────────────────
        # ATM key (for general display/reference)
        key      = option_chain_service.get_key(atm, option) if (atm and option != "—") else None
        live_opt = feed_store.get(key) if key else None
        cur_price = live_opt.ltp if live_opt else 0.0

        expiry_pm = capital_service.is_expiry_afternoon()

        # Entry strike selection:
        #   Expiry afternoon → always ATM (binary bet, no OTM/ITM search)
        #   Normal           → ATM first, ITM if ATM cheap, OTM if ATM expensive
        if atm and option != "—":
            if expiry_pm:
                entry_strike = atm
                entry_key    = key
                entry_price  = cur_price
            else:
                entry_strike, entry_key, entry_price = _affordable_strike(ltp, option)
        else:
            entry_strike, entry_key, entry_price = atm, None, 0.0
        entry_live = feed_store.get(entry_key) if entry_key else None

        # ── detect SL close → arm re-entry window ─────────────────────────
        has_trade = trade_book.has_open_trade()
        if self._had_open_trade and not has_trade:
            all_t = trade_book.get_all_trades()
            if all_t:
                last_t = all_t[-1]
                if last_t.get("exit_reason") in ("sl_hit", "trail_sl"):
                    self._last_sl_time   = time.time()
                    self._last_sl_option = last_t.get("option", "")
                    log.info("Re-entry window armed: %s SL closed", self._last_sl_option)
        self._had_open_trade = has_trade

        # ── breakout watch + retest entry logic ───────────────────────────
        watch_tag = ""   # status line shown in panel
        entry_type: str | None = None  # "retest" | "momentum" | "reentry" | None
        self._why_tag = ""  # reset each tick; set below to explain no-trade reason

        bk_min_dist = adaptive_params.get("bk_min_dist")
        bk_timeout  = adaptive_params.get("bk_timeout")

        if self._sr_retest_mode and (time.time() - self._sr_retest_ts) > self._SR_RETEST_TTL:
            log.info("S/R retest watch expired (%.0fmin) — level %.1f abandoned",
                     self._SR_RETEST_TTL / 60, self._sr_retest_level)
            self._sr_retest_mode = False
            self._sr_retest_bars = 0

        if tradeable and cur_price:
            direction_flip = (self._last_option is not None and self._last_option != option)
            if direction_flip:
                if trade_book.has_open_trade():
                    # Exit at the HELD option's price — cur_price is the NEW
                    # direction's ATM premium, a different contract entirely
                    ot_flip = trade_book.get_open_trade()
                    _fk     = ot_flip.get("option_key", "")
                    _fl     = feed_store.get(_fk) if _fk else None
                    _fp     = _fl.ltp if (_fl and _fl.ltp) else ot_flip.get(
                        "current_price", ot_flip["entry_price"])
                    trade_book.close_trade(_fp, "direction_change")
                self._watch = "IDLE"
                self._bk_max_dist = 0.0
                self._sr_retest_mode = False
                self._sr_blocked_prev = False
                self._orb_watching   = False
                self._pdx_watching   = False

            if not trade_book.has_open_trade():
                watch_dir = f"WATCH_{option}"

                # ── re-entry check (after SL, same direction, cooled down) ──
                cooldown_done = (time.time() - self._last_sl_time) >= self._REENTRY_COOLDOWN
                same_dir      = self._last_sl_option == option
                reentry_armed = self._last_sl_time > 0 and cooldown_done and same_dir

                if reentry_armed and entry_key and entry_price and entry_live:
                    # Guard 1: move potential ≥ 10%
                    lvl_re     = capital_service.levels(
                        entry_price, entry_live.delta,
                        repeat_count=trade_book.same_direction_streak(option),
                    )
                    potential  = (lvl_re["target"] - entry_price) / entry_price if entry_price else 0
                    # ≥10pt absolute also qualifies — the %-only gate demanded
                    # 14pt+ on a ₹140 premium, silently vetoing small-target
                    # re-entries that are the whole game on consolidating days
                    potential_pts = lvl_re["target"] - entry_price
                    # Guard 2: premium velocity — delta × half-ATR / price ≥ 8%
                    from uptox.services import atr_service
                    atr_pts   = atr_service.get_atr(NIFTY, TimeFrame.M5) or 30.0
                    delta_val = abs(entry_live.delta) if entry_live.delta else 0.3
                    exp_move  = delta_val * (atr_pts * 0.4) / entry_price if entry_price else 0
                    if potential < 0.10 and potential_pts < 10.0:
                        watch_tag = f"  [dim]⟳ RE-ENTRY SKIP — target only {potential*100:.0f}% away[/]"
                    elif exp_move < 0.08:
                        watch_tag = f"  [dim]⟳ RE-ENTRY SKIP — slow premium ({exp_move*100:.0f}%/move)[/]"
                    else:
                        entry_type = "reentry"
                        self._last_sl_time = 0.0  # consume the re-entry window
                        log.info(
                            "Re-entry triggered: %s %s @ %.1f  potential=%.0f%%  vel=%.0f%%",
                            option, entry_key, entry_price, potential * 100, exp_move * 100,
                        )

                # ── Opening Range Breakout — arm watch on first break ─────────
                # On break: record ORB level as departure anchor, require bk_min_dist
                # before entering. Departure alone is enough (no retest/timeout).
                if (
                    self._orb_ready
                    and not self._orb_watching
                    and entry_key and entry_price
                ):
                    # Flow must strongly confirm (±2) — ORB breaks with weak flow
                    # are the most common failed breakouts of the day.
                    _orb_ce_break = (
                        option == "CE"
                        and live_ltp > self._orb_high
                        and flow_score >= 2
                        and not self._orb_ce_done
                    )
                    _orb_pe_break = (
                        option == "PE"
                        and live_ltp < self._orb_low
                        and flow_score <= -2
                        and not self._orb_pe_done
                    )
                    if _orb_ce_break or _orb_pe_break:
                        _orb_level = self._orb_high if option == "CE" else self._orb_low
                        self._orb_watching = True
                        self._watch        = watch_dir
                        self._bk_ltp       = _orb_level   # departure measured from ORB edge
                        self._bk_t         = time.time()
                        self._bk_max_dist  = 0.0
                        log.info(
                            "ORB break detected: %s @ %.1f  level=%.1f  range %.1f–%.1f",
                            option, live_ltp, _orb_level, self._orb_low, self._orb_high,
                        )

                # ── Prev-Day High/Low breakout — arm watch on first break ─────
                # Same mechanics as ORB (flow ±2 + 15pt departure) but the
                # trigger levels are yesterday's high/low, active all session.
                if (
                    not self._orb_watching
                    and not self._pdx_watching
                    and entry_key and entry_price
                ):
                    from uptox.services import prev_day_service as _pds
                    _pdd = _pds.get_ohlc()
                    if _pdd:
                        # A "break" requires the session to have OPENED on the other
                        # side of the level — a gap-open beyond PDH/PDL is not a
                        # breakout, the move was already priced overnight (seen
                        # 2026-07-03: 140pt gap-up, price above PDH from tick one).
                        _opened_below_pdh = bool(self._session_open) and self._session_open <= _pdd["high"]
                        _opened_above_pdl = bool(self._session_open) and self._session_open >= _pdd["low"]
                        _pdh_break = (
                            option == "CE"
                            and _opened_below_pdh
                            and live_ltp > _pdd["high"]
                            and flow_score >= 2
                            and not self._pdx_ce_done
                        )
                        _pdl_break = (
                            option == "PE"
                            and _opened_above_pdl
                            and live_ltp < _pdd["low"]
                            and flow_score <= -2
                            and not self._pdx_pe_done
                        )
                        if _pdh_break or _pdl_break:
                            _pdx_level = _pdd["high"] if option == "CE" else _pdd["low"]
                            self._pdx_watching = True
                            self._watch        = watch_dir
                            self._bk_ltp       = _pdx_level   # departure measured from PDH/PDL
                            self._bk_t         = time.time()
                            self._bk_max_dist  = 0.0
                            log.info(
                                "PD %s break detected: %s @ %.1f  level=%.1f",
                                "high" if option == "CE" else "low",
                                option, live_ltp, _pdx_level,
                            )

                if self._watch == "IDLE":
                    # Breakout just confirmed — start watching for retest
                    self._watch      = watch_dir
                    self._bk_ltp     = ltp
                    self._bk_t       = time.time()
                    self._bk_max_dist = 0.0
                    log.info("Breakout watch started: %s @ Nifty %.1f", option, ltp)

                elif self._watch == watch_dir:

                    # ── S/R retest confirmation mode ─────────────────────────
                    # Triggered when price breaks S/R but flow contradicts (fake break).
                    # Waits for pullback to the broken level + 2 confirming bars.
                    if self._sr_retest_mode and self._sr_retest_option == option:
                        _rl   = self._sr_retest_level
                        # Both thresholds carry a 5pt buffer over their original
                        # values (20/15) — checks run on raw ticks, and a single
                        # freak tick through the line was declaring BREAK FAILED
                        _zone = 25.0   # within this of level = "near enough" for retest
                        _fail = 20.0   # beyond this below (CE) / above (PE) = break failed

                        _near_level = abs(ltp - _rl) <= _zone
                        _break_failed = (
                            (option == "CE" and ltp < _rl - _fail) or
                            (option == "PE" and ltp > _rl + _fail)
                        )

                        if _break_failed:
                            # Price fell back through the level — break was fake, reset
                            self._sr_retest_mode = False
                            self._sr_retest_bars = 0
                            watch_tag = f"  [bold red]⚠ BREAK FAILED — price fell back through {_rl:.0f}[/]"
                            log.info("S/R retest failed: %s fell back through %.1f", option, _rl)

                        elif _near_level:
                            # Price pulled back to retest zone — count confirming bars
                            _recent_bars = sma_service.get_last_candles(NIFTY, TimeFrame.M5, n=1)
                            if _recent_bars:
                                _bo, _bc = _recent_bars[-1][0], _recent_bars[-1][3]
                                _confirms = (
                                    (option == "CE" and _bc > _bo) or   # green bar
                                    (option == "PE" and _bc < _bo)       # red bar
                                )
                                if _bar_slot != self._sr_retest_last_bar:
                                    if _confirms:
                                        self._sr_retest_bars    += 1
                                        self._sr_retest_last_bar = _bar_slot
                                        log.info("S/R retest bar %d/2: %s @ %.1f near %.1f",
                                                 self._sr_retest_bars, option, ltp, _rl)
                                    else:
                                        self._sr_retest_bars = 0  # non-confirming bar resets count

                            if self._sr_retest_bars >= 2:
                                entry_type = "sr_retest"
                                self._sr_retest_mode = False
                                log.info("S/R retest confirmed — entering %s @ %.1f after 2 bars near %.1f",
                                         option, ltp, _rl)
                            else:
                                _need = 2 - self._sr_retest_bars
                                watch_tag = (
                                    f"  [bold yellow]⟳ RETEST {option}[/]"
                                    f" [dim]{self._sr_retest_bars}/2 bars at {_rl:.0f}"
                                    f" ({abs(ltp-_rl):.0f}pt away)[/]"
                                )
                                self._why_tag = (
                                    f"Retest of {_rl:.0f} — {self._sr_retest_bars}/2 confirming bars"
                                    f" ({_need} more needed)"
                                )
                        else:
                            # Not yet pulled back to level (price still running away from it)
                            _dist = abs(ltp - _rl)
                            watch_tag = (
                                f"  [bold yellow]⟳ WAIT PULLBACK {option}[/]"
                                f" [dim]need return to {_rl:.0f} ({_dist:.0f}pt away)[/]"
                            )
                            self._why_tag = f"Waiting for pullback to {_rl:.0f} — {_dist:.0f}pt away"

                    elif self._orb_watching or self._pdx_watching:
                        # ── ORB / PDH-PDL departure confirmation ──────────────
                        # Entry fires as soon as price travels ≥ 15pt from the broken
                        # edge. Tighter than normal watch (30pt) — level breaks move fast.
                        _ORB_DEPART = 15.0
                        _wl = "ORB" if self._orb_watching else ("PDH" if option == "CE" else "PDL")
                        dist_now = abs(ltp - self._bk_ltp)
                        self._bk_max_dist = max(self._bk_max_dist, dist_now)
                        departed = self._bk_max_dist >= _ORB_DEPART

                        if departed:
                            if self._orb_watching:
                                entry_type = "orb"
                                self._orb_watching = False
                                if option == "CE":
                                    self._orb_ce_done = True
                                else:
                                    self._orb_pe_done = True
                            else:
                                entry_type = "pdh_break" if option == "CE" else "pdl_break"
                                self._pdx_watching = False
                                if option == "CE":
                                    self._pdx_ce_done = True
                                else:
                                    self._pdx_pe_done = True
                            log.info(
                                "%s departure confirmed: %s  depart=%.0f/%.0fpt",
                                _wl, option, self._bk_max_dist, _ORB_DEPART,
                            )
                        else:
                            _need = _ORB_DEPART - self._bk_max_dist
                            watch_tag = (
                                f"  [bold yellow]⟳ {_wl} WATCH {option}[/]"
                                f" [dim]depart {self._bk_max_dist:.0f}/{_ORB_DEPART:.0f}pt"
                                f"  (need {_need:.0f}pt more)[/]"
                            )
                            self._why_tag = (
                                f"{_wl} break at {self._bk_ltp:.0f} — "
                                f"need {_need:.0f}pt more departure ({self._bk_max_dist:.0f}/{_ORB_DEPART:.0f}pt)"
                            )

                    else:
                        # ── Normal departure / retest / momentum watch ────────
                        elapsed   = time.time() - self._bk_t
                        dist_now  = abs(ltp - self._bk_ltp)
                        self._bk_max_dist = max(self._bk_max_dist, dist_now)

                        # Price must travel ≥MIN_DEPART pts first; prevents instant
                        # entry on restart when price == breakout price
                        departed  = self._bk_max_dist >= bk_min_dist
                        retest    = departed and dist_now <= self._RETEST_PTS
                        timed_out = departed and elapsed >= bk_timeout

                        if retest:
                            entry_type = "retest"
                        elif timed_out:
                            entry_type = "momentum"
                        else:
                            remaining = int(bk_timeout - elapsed)
                            if not departed:
                                depart_str = f"depart {self._bk_max_dist:.0f}/{bk_min_dist:.0f}pt"
                                self._why_tag = (
                                    f"Needs {bk_min_dist - self._bk_max_dist:.0f}pt more departure "
                                    f"({self._bk_max_dist:.0f}/{bk_min_dist:.0f}pt so far)"
                                )
                            else:
                                depart_str = f"retest <{self._RETEST_PTS}pt ({dist_now:.0f}pt away)"
                                self._why_tag = (
                                    f"Departed ✓ — waiting for retest "
                                    f"({dist_now:.0f}pt from level, need <{self._RETEST_PTS}pt)"
                                )
                            watch_tag = (
                                f"  [bold yellow]⟳ WATCH {option}[/]"
                                f" [dim]{depart_str}  {remaining}s[/]"
                            )

                        # Sustained-signal entry: ≥4 bars same sma_score + OI not contradicting.
                        # OI gate replaces the retest quality-check that this entry type skips.
                        # OI rising on the opposite side only contradicts if it's fresh writing
                        # (IV flat/falling) — rising OI + rising IV is buying, which CONFIRMS
                        # the bearish/bullish move rather than fighting it.
                        _oi_ok = (
                            (sma_score < 0 and (oi_sc <= 0 or pe_iv_rising))
                            or (sma_score > 0 and (oi_sc >= 0 or ce_iv_rising))
                        )
                        # Flow must strongly confirm (±2), same as ORB — sustained
                        # skips the retest quality-check, so a lukewarm Flow+1 lets
                        # it churn the same idea into a stalling market all day.
                        _flow_ok = (
                            (option == "CE" and flow_score >= 2)
                            or (option == "PE" and flow_score <= -2)
                        )
                        if (not entry_type and self._consec_sma_bars >= 4
                                and abs(sma_score) == 2 and _oi_ok and _flow_ok):
                            entry_type = "sustained"
                            log.info(
                                "Sustained signal: %s sma_score=%+d for %d bars oi_sc=%+d "
                                "(ce_iv_rising=%s pe_iv_rising=%s) — entering without retest",
                                option, sma_score, self._consec_sma_bars, oi_sc,
                                ce_iv_rising, pe_iv_rising,
                            )

                    if entry_type:
                        # sr_retest entries already validated at the broken level — skip normal S/R check
                        if entry_type == "sr_retest":
                            sr_block, _broken_level = None, 0.0
                        else:
                            sr_block, _broken_level = _near_sr(ltp, option)
                        if sr_block:
                            watch_tag = f"  [bold red]⚠ BLOCKED — {sr_block}[/]"
                            log.info("Entry blocked by S/R: %s @ %.1f  (%s)", option, ltp, sr_block)
                            _log_blocked_entry(sr_block, option, ltp, total, entry_type)
                            entry_type = None
                            self._sr_blocked_prev = True
                            self._sr_block_level  = _broken_level
                            self._sr_block_option = option
                            self._sr_block_ts     = time.time()
                        else:
                            # Stored block context only counts if it matches this
                            # direction and hasn't gone stale — a block recorded for
                            # a signal that died must not fire a fake break event
                            # when an unrelated entry clears S/R much later.
                            fresh_break = False
                            _blk_ctx = (
                                self._sr_blocked_prev
                                and self._sr_block_option == option
                                and (time.time() - self._sr_block_ts) <= self._SR_BLOCK_TTL
                            )
                            _bl = self._sr_block_level
                            # A break means price crossed the level IN the trade's
                            # direction. Merely drifting out of tolerance AWAY from
                            # the wall leaves it intact between price and the move —
                            # 2026-07-09 both -10pt losses entered PE seconds after a
                            # support block, 15pt ABOVE the unbroken swing low.
                            _through = _blk_ctx and (
                                (option == "CE" and ltp > _bl) or
                                (option == "PE" and ltp < _bl)
                            )
                            if _blk_ctx and not _through and abs(ltp - _bl) < self._SR_AWAY_CLEAR:
                                watch_tag = (
                                    f"  [bold red]⚠ BLOCKED — S/R {_bl:.0f} intact"
                                    f" ({abs(ltp - _bl):.0f}pt away, need break or"
                                    f" {self._SR_AWAY_CLEAR:.0f}pt clearance)[/]"
                                )
                                _log_blocked_entry(
                                    f"S/R intact: {_bl:.0f} not broken ({abs(ltp - _bl):.0f}pt away)",
                                    option, ltp, total, entry_type,
                                )
                                entry_type = None
                                # keep _sr_blocked_prev armed; original ts stands so
                                # the TTL still expires genuinely stale context
                            else:
                                fresh_break = _through
                                self._sr_blocked_prev = False
                            if entry_type and fresh_break:
                                # On the clearing tick _near_sr returns level 0.0 —
                                # the broken level is the one stored at block time
                                _broken_level = _bl
                                # Just cleared S/R — classify: flow confirms → fire; flow contradicts → retest watch
                                _flow_bad = (
                                    (option == "CE" and flow_score < 0) or
                                    (option == "PE" and flow_score > 0)
                                )
                                if _flow_bad:
                                    _fd = "bearish" if option == "CE" else "bullish"
                                    watch_tag = (
                                        f"  [bold yellow]⟳ FAKE BREAK? — flow {_fd},"
                                        f" watching retest of {_broken_level:.0f}[/]"
                                    )
                                    log.info(
                                        "Fake break → retest watch: %s @ %.1f  level=%.1f  flow=%+d",
                                        option, ltp, _broken_level, flow_score,
                                    )
                                    entry_type = None
                                    self._sr_retest_mode    = True
                                    self._sr_retest_ts      = time.time()
                                    self._sr_retest_level   = _broken_level
                                    self._sr_retest_option  = option
                                    self._sr_retest_bars    = 0
                                    self._sr_retest_last_bar = -1
                                else:
                                    _confirm = "flow confirmed" if flow_score != 0 else "flow neutral"
                                    log.info("S/R break %s: %s @ %.1f  flow=%+d",
                                             _confirm, option, ltp, flow_score)

                        if entry_type is not None and (not entry_key or not entry_price):
                            # No suitable strike — ATM unaffordable or all strikes < ₹50 even after ITM search
                            atm_p = cur_price or 0.0
                            if 0 < atm_p < 50:
                                watch_tag = f"  [bold red]⚠ SKIP — market too cheap ₹{atm_p:.1f} (ITM also < ₹50)[/]"
                                log.info("Entry skipped: ATM %.1f < 50, no ITM strike viable", atm_p)
                            else:
                                watch_tag = (
                                    f"  [bold red]⚠ NO CAPITAL — ATM ₹{atm_p:.1f} "
                                    f"(cap ₹{capital_service.get_capital():.0f})[/]"
                                )
                                log.info("Entry skipped: ATM %.1f > capital %.0f", atm_p, capital_service.get_capital())
                            _log_blocked_entry(f"No viable strike — ATM ₹{atm_p:.1f}", option, ltp, total, entry_type)
                            entry_type = None
                        elif entry_type is not None:
                            # Daily limit check before opening
                            can_trade, block_reason = trade_book.can_trade_today()
                            if not can_trade:
                                watch_tag = f"  [bold red]⚠ BLOCKED — {block_reason}[/]"
                                _log_blocked_entry(block_reason or "daily limit", option, ltp, total, entry_type)
                                entry_type = None
                            else:
                              delta  = entry_live.delta if entry_live else None
                              iv     = entry_live.iv if entry_live else 0.0
                              lvl    = capital_service.levels(
                                  entry_price, delta, iv=iv,
                                  repeat_count=trade_book.same_direction_streak(option),
                              )
                              sz = (
                                  capital_service.sizing_expiry(entry_price)
                                  if expiry_pm
                                  else capital_service.sizing_with_risk(entry_price, lvl["sl"])
                              )
                              if sz["lots"] == 0:
                                  watch_tag = "  [bold red]⚠ RISK BLOCK — 0 lots (SL too wide for capital)[/]"
                                  _log_blocked_entry("Risk block — 0 lots (SL too wide)", option, ltp, total, entry_type)
                                  entry_type = None
                              else:
                                _itm = False
                                _strike_note = "[EXPIRY ATM]" if expiry_pm else ""
                                if not expiry_pm:
                                    _itm = (option == "CE" and entry_strike < atm) or (option == "PE" and entry_strike > atm)
                                    _strike_note = (
                                        f" ITM{entry_strike}" if (_itm and entry_strike != atm) else
                                        f" OTM{entry_strike}" if entry_strike != atm else ""
                                    )
                                reason = (
                                    f"{entry_type} | SMA{sma_score:+d} Flow{flow_score:+d}"
                                    f" | Nifty@{ltp:.0f} | {option}@{entry_strike}{_strike_note}"
                                    f" | {lvl['regime']} ATR={lvl['atr']:.0f}"
                                    f" | risk=₹{sz['risk_rs']:.0f} ({sz['risk_pct']:.1f}%)"
                                )
                                trade_book.open_trade(
                                    strike=entry_strike, option=option,
                                    expiry=option_chain_service.get_expiry() or "—",
                                    entry_price=entry_price,
                                    lots=sz["lots"], invested=sz["invested"],
                                    sl=lvl["sl"], target=lvl["target"],
                                    option_key=entry_key, delta=delta, spot=ltp,
                                    entry_reason=reason,
                                    score=total, sma_score=sma_score, flow_score=flow_score,
                                    atr_at_entry=lvl.get("atr", 0.0),
                                    expiry_day=capital_service.is_expiry_day(),
                                    regime=lvl.get("regime", "NORMAL"),
                                )
                                _loc = "ITM" if _itm else "OTM"
                                log.info(
                                    "Trade opened (%s): %s %s @ %.1f  lots=%d  risk=₹%.0f (%.1f%%)  cap=%s%s",
                                    entry_type, option, entry_strike, entry_price,
                                    sz["lots"], sz["risk_rs"], sz["risk_pct"], sz["capped_by"],
                                    f"  [{_loc} from ATM {atm}]" if entry_strike != atm else "",
                                )
                                self._watch = "IDLE"
                                self._why_tag = ""

        elif not tradeable:
            if self._watch != "IDLE":
                log.info("Breakout watch cancelled — signal dropped")
                self._watch = "IDLE"
                self._bk_max_dist = 0.0
                self._sr_blocked_prev = False
                self._orb_watching   = False
                self._pdx_watching   = False
                # _sr_retest_mode intentionally survives — the retest pullback
                # itself weakens score / trips the bounce filter, so resetting
                # here killed every retest setup mid-wait; a TTL expires it instead
            if trade_book.has_open_trade():
                # Only a genuine REVERSAL force-closes (score now ≥2 against the
                # held side). Filter wobble — score dip, IV rank, consolidation,
                # bounce — used to dump the position at market here, overriding
                # the trail SL that exists precisely to sit through such noise.
                # Trail floor / mae / stale / session-close manage everything else.
                ot_exit   = trade_book.get_open_trade()
                _held     = ot_exit.get("option", "")
                _reversed = (_held == "CE" and total <= -2) or (_held == "PE" and total >= 2)
                if _reversed:
                    exit_key  = ot_exit.get("option_key", "")
                    exit_live = feed_store.get(exit_key) if exit_key else None
                    exit_p    = exit_live.ltp if (exit_live and exit_live.ltp) else ot_exit.get(
                        "current_price", ot_exit["entry_price"])
                    log.info("Signal reversal against %s (score %+d) — closing", _held, total)
                    trade_book.close_trade(exit_p, "signal_flip")

        self._last_option = option if tradeable else self._last_option

        # ── live buy price (use affordable entry strike, not always ATM) ─────
        buy_price = entry_price if (tradeable and entry_key) else 0.0

        # ── PCR + Max Pain ────────────────────────────────────────────────
        pcr       = option_chain_service.get_pcr()
        max_pain  = option_chain_service.get_max_pain()
        pcr_str   = f"{pcr:.2f}" if pcr else "—"
        pcr_color = ("green" if pcr and pcr > 1.2 else "red" if pcr and pcr < 0.8 else "yellow")
        mp_str    = str(max_pain) if max_pain else "—"

        # ── render ────────────────────────────────────────────────────────
        opt_color = "green" if option == "CE" else "red" if option == "PE" else "dim white"
        ltp_str   = (f"{ltp:,.2f}" if live_ltp else f"{ltp:,.2f} [snap]") if ltp else "—"
        self.border_subtitle = ltp_str

        ot = trade_book.get_open_trade()
        if ot:
            # ── TRADE LIVE display ─────────────────────────────────────────
            cur_p    = ot.get("current_price", ot["entry_price"])
            pnl_pts  = ot.get("pnl_pts", 0.0)
            pnl_rs   = ot.get("pnl_rs", 0.0)
            elapsed  = int(time.time() - ot.get("entry_ts", time.time()))
            e_str    = f"{elapsed // 60}m {elapsed % 60}s"
            pnl_col  = "green" if pnl_pts >= 0 else "red"
            pnl_sign = "+" if pnl_pts >= 0 else ""
            ot_opt   = ot.get("option", "?")
            ot_col   = "green" if ot_opt == "CE" else "red"
            lines = [
                f"[bold {ot_col}]▶ TRADE LIVE — {ot_opt} {ot.get('strike','?')} @ ₹{ot['entry_price']:.1f}[/]",
                f"[dim]{'Spot':<12}[/] [white]{ltp:,.2f}[/]",
                f"[dim]{'Option ₹':<12}[/] [white]{cur_p:.1f}[/]",
                f"[dim]{'P&L':<12}[/] [bold {pnl_col}]{pnl_sign}{pnl_pts:.1f} pts  ₹{pnl_sign}{pnl_rs:,.0f}[/]",
                f"[dim]{'SL':<12}[/] [red]{ot['sl']:.1f}[/]",
                f"[dim]{'Target':<12}[/] [green]{ot['target']:.1f}[/]",
                f"[dim]{'Elapsed':<12}[/] [dim white]{e_str}[/]",
                f"[dim]{'Trend':<12}[/] [bold {trend_color}]{trend}[/]",
                f"[dim]{'Score':<12}[/] [dim white]{total:+d}/5  SMA{sma_score:+d}  Δ{delta_sc:+d} CumΔ{cum_sc:+d} OI{oi_sc:+d}[/]",
                f"[dim]{'PCR':<12}[/] [{pcr_color}]{pcr_str}[/]",
                f"[dim]{'Max Pain':<12}[/] [dim white]{mp_str}[/]",
            ]
            if bn_conflict:
                lines.append(f"[dim]{'BN':<12}[/] [yellow]⚠ BN diverges (score {bn_score:+d}) — advisory[/]")
            if today_event:
                lines.append(f"[dim]{'Event':<12}[/] [bold yellow]⚡ {today_event} — min score {req_score}[/]")
        else:
            if expiry_pm:
                otm_label = " [dim](EXPIRY ATM)[/]"
            elif entry_strike and entry_strike != atm:
                _is_itm   = (option == "CE" and entry_strike < atm) or (option == "PE" and entry_strike > atm)
                otm_label = f" [dim]({'ITM' if _is_itm else 'OTM'} from {atm})[/]"
            else:
                otm_label = ""
            disp_strike = entry_strike if (tradeable and entry_strike) else atm
            strike_str  = f"{disp_strike} {option}{otm_label}" if (disp_strike and tradeable) else (f"{atm}" if atm else "—")
            buy_str     = f"{buy_price:.1f}" if buy_price else "—"

            if self._watch != "IDLE" and not entry_type:
                action_line = (
                    f"[dim]{'Action':<12}[/] [bold {opt_color}]BUY {option} @ {disp_strike}[/]{watch_tag}"
                )
            elif tradeable:
                action_line = f"[dim]{'Action':<12}[/] [bold {opt_color}]BUY {option} @ {disp_strike}[/]"
            else:
                action_line = f"[dim]{'Action':<12}[/] [bold red]NO TRADE — {no_trade_reason}[/]"

            lines = [
                action_line,
            ]
            if self._why_tag:
                lines.append(f"[dim]{'Reason':<12}[/] [yellow]{self._why_tag}[/]")
            lines += [
                f"[dim]{'Strike':<12}[/] [yellow]{strike_str if tradeable else '—'}[/]",
                f"[dim]{'Option':<12}[/] [bold {opt_color}]{option if tradeable else '—'}[/]",
                f"[dim]{'Buy Price':<12}[/] [{opt_color}]{buy_str}[/]",
                f"[dim]{'Trend':<12}[/] [bold {trend_color}]{trend}[/]",
                f"[dim]{'Score':<12}[/] [dim white]{total:+d}/5  SMA{sma_score:+d}  Δ{delta_sc:+d} CumΔ{cum_sc:+d} OI{oi_sc:+d}[/]",
                f"[dim]{'PCR':<12}[/] [{pcr_color}]{pcr_str}[/]",
                f"[dim]{'Max Pain':<12}[/] [dim white]{mp_str}[/]",
            ]
            if bn_conflict:
                lines.append(f"[dim]{'BN':<12}[/] [yellow]⚠ BN diverges (score {bn_score:+d}) — advisory[/]")
            if (option == "CE" and _pcr_bull_warn) or (option == "PE" and _pcr_bear_warn):
                lines.append(f"[dim]{'PCR':<12}[/] [yellow]⚠ PCR {_pcr_filter:.2f} crowded — advisory[/]")
            if today_event:
                lines.append(f"[dim]{'Event':<12}[/] [bold yellow]⚡ {today_event} — min score {req_score}[/]")
            if self._orb_ready:
                _orb_lo = f"{self._orb_low:.0f}" if self._orb_low < float("inf") else "—"
                _orb_ce_tag = " [dim](done)[/]" if self._orb_ce_done else ""
                _orb_pe_tag = " [dim](done)[/]" if self._orb_pe_done else ""
                lines.append(
                    f"[dim]{'ORB':<12}[/] [cyan]{_orb_lo}–{self._orb_high:.0f}[/]"
                    f"  [green]CE>{self._orb_high:.0f}{_orb_ce_tag}[/]"
                    f"  [red]PE<{_orb_lo}{_orb_pe_tag}[/]"
                )
            elif self._orb_high > 0:
                lines.append(f"[dim]{'ORB':<12}[/] [dim]building ({self._orb_low:.0f}–{self._orb_high:.0f})[/]")

        self.update("\n".join(lines))


class SMAPanel(Static):
    current_tf: reactive[TimeFrame] = reactive(TimeFrame.M5)

    def on_mount(self) -> None:
        self.border_title = "SMA  (t = cycle)"
        self._refresh()
        self.set_interval(1.0, self._refresh)

    def cycle_tf(self) -> None:
        idx = ALL_TFS.index(self.current_tf)
        self.current_tf = ALL_TFS[(idx + 1) % len(ALL_TFS)]
        sma_service.set_current_tf(self.current_tf)

    def watch_current_tf(self, _: TimeFrame) -> None:
        self._refresh()

    def _refresh(self) -> None:
        t = feed_store.get(NIFTY)
        ltp = t.ltp if t else 0.0
        status = feed_store.get_status()
        status_color = "green" if status == "connected" else "yellow" if status == "reconnecting" else "dim white" if status == "market_closed" else "red"
        tf = self.current_tf

        def vs(sma: float | None) -> tuple[str, str]:
            if sma is None:
                return "—", "dim white"
            if not ltp:
                return f"{sma:,.1f}", "dim white"  # historical value, no live comparison
            if ltp > sma:
                return f"{sma:,.1f}  ↑", "green"
            return f"{sma:,.1f}  ↓", "red"

        smas = sma_service.get_smas(NIFTY, tf)
        v10,  c10  = vs(smas[10])
        v20,  c20  = vs(smas[20])
        v50,  c50  = vs(smas[50])
        v100, c100 = vs(smas[100])
        v200, c200 = vs(smas[200])

        ltp_str = f"{ltp:,.2f}" if ltp else "—"
        self.border_title = f"SMA ({tf.value})  t=cycle"
        self.border_subtitle = f"[{status_color}]{status}[/]  {ltp_str}"
        self.update(_kv([
            ("SMA 10",  v10,  c10),
            ("SMA 20",  v20,  c20),
            ("SMA 50",  v50,  c50),
            ("SMA 100", v100, c100),
            ("SMA 200", v200, c200),
        ]))


class OrderFlowPanel(Static):
    _last_ce_iv: float = 0.0
    _last_pe_iv: float = 0.0
    _last_atm:   int   = 0

    def on_mount(self) -> None:
        self.border_title = "Order Flow"
        self._refresh()
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        t_idx = feed_store.get(NIFTY)
        ltp   = (t_idx.ltp if t_idx else 0.0) or option_chain_service.get_spot()

        if ltp:
            atm    = round(ltp / 50) * 50
            ce_key = option_chain_service.get_key(atm, "CE")
            pe_key = option_chain_service.get_key(atm, "PE")
            t_ce   = feed_store.get(ce_key) if ce_key else None
            t_pe   = feed_store.get(pe_key) if pe_key else None

            # flow_qty: tbq/tsq when Upstox sends them, else live depth qty proxy
            ce_buy, ce_sell = feed_store.flow_qty(t_ce)
            pe_buy, pe_sell = feed_store.flow_qty(t_pe)
            total_buy  = ce_buy + pe_buy
            total_sell = ce_sell + pe_sell

            # CE net buy = bullish; PE net buy = bearish — subtract, don't add
            raw_delta = (ce_buy - ce_sell) - (pe_buy - pe_sell)

            # Use feed_store cum_delta (tick-rule accumulation, already correct)
            cum = feed_store.cum_delta(ce_key) if ce_key else 0.0

            ratio = (total_buy / total_sell) if total_sell else 0.0

            # OI: live tick preferred, REST snapshot fallback (ATM only)
            ce_sn_atm = option_chain_service.get_option(atm, "CE") or {}
            pe_sn_atm = option_chain_service.get_option(atm, "PE") or {}
            ce_oi = (t_ce.oi if t_ce else 0.0) or ce_sn_atm.get("oi", 0.0)
            pe_oi = (t_pe.oi if t_pe else 0.0) or pe_sn_atm.get("oi", 0.0)

            # OI Change: sum ±3 strikes (same scope as OI Signal) — not just ATM
            ce_oi_chg = pe_oi_chg = 0.0
            ce_vtt = pe_vtt = 0
            for i in range(-3, 4):
                s = atm + i * 50
                ce_d = option_chain_service.get_option(s, "CE") or {}
                pe_d = option_chain_service.get_option(s, "PE") or {}
                ce_oi_chg += ce_d.get("change_oi", 0.0)
                pe_oi_chg += pe_d.get("change_oi", 0.0)
                # volume from ATM tick preferred; ±3 from snapshot
                ce_vtt += ce_d.get("volume", 0)
                pe_vtt += pe_d.get("volume", 0)
            # use live ATM tick volume when available (more current)
            if t_ce and t_ce.vtt: ce_vtt = int(t_ce.vtt)
            if t_pe and t_pe.vtt: pe_vtt = int(t_pe.vtt)
            total_vtt  = ce_vtt + pe_vtt
            net_oi_chg = ce_oi_chg + pe_oi_chg

            # OI Bias: full-chain PCR (more reliable than single ATM strike)
            _full_pcr = option_chain_service.get_pcr() or 0.0
            if _full_pcr <= 0:
                oi_bias, oi_bias_color = "—", "dim white"
            elif _full_pcr < 0.8:
                oi_bias, oi_bias_color = f"CE Heavy ({_full_pcr:.2f})", "red"
            elif _full_pcr < 1.1:
                oi_bias, oi_bias_color = f"Balanced ({_full_pcr:.2f})", "yellow"
            else:
                oi_bias, oi_bias_color = f"PE Heavy ({_full_pcr:.2f})", "green"

            # Live tick IV preferred; REST snapshot fallback. Reset on ATM change.
            _ce_sn_iv = (t_ce.iv if t_ce and t_ce.iv else 0.0) or ce_sn_atm.get("iv", 0.0)
            _pe_sn_iv = (t_pe.iv if t_pe and t_pe.iv else 0.0) or pe_sn_atm.get("iv", 0.0)
            if atm != self._last_atm:
                self._last_ce_iv = 0.0
                self._last_pe_iv = 0.0
                self._last_atm   = atm
            _ce_iv_rising = bool(_ce_sn_iv and self._last_ce_iv and _ce_sn_iv > self._last_ce_iv)
            _pe_iv_rising = bool(_pe_sn_iv and self._last_pe_iv and _pe_sn_iv > self._last_pe_iv)
            if _ce_sn_iv: self._last_ce_iv = _ce_sn_iv
            if _pe_sn_iv: self._last_pe_iv = _pe_sn_iv

            # OI direction: IV-adjusted (OI↑+IV↑=buying, OI↑+IV↓=writing)
            oi_direction = pe_oi_chg - ce_oi_chg
            if ce_oi_chg > pe_oi_chg:
                _lbl = "Buying" if _ce_iv_rising else "Writing"
                oi_dir_str   = f"CE↑{ce_oi_chg/1e5:.1f}L ({_lbl}) → {'Bear' if _ce_iv_rising else 'Ceiling'}"
                oi_dir_color = "red" if _ce_iv_rising else "yellow"
            elif pe_oi_chg > ce_oi_chg:
                _lbl = "Buying" if _pe_iv_rising else "Writing"
                oi_dir_str   = f"PE↑{pe_oi_chg/1e5:.1f}L ({_lbl}) → {'Bear' if _pe_iv_rising else 'Floor'}"
                oi_dir_color = "red" if _pe_iv_rising else "green"
            else:
                oi_dir_str, oi_dir_color = "Balanced", "yellow"

            # Call wall / put wall from full chain
            _cw = option_chain_service.get_call_wall()
            _pw = option_chain_service.get_put_wall()
            cw_str = f"{_cw[0]} ({_cw[0]-ltp:+.0f}pts)" if _cw and ltp else "—"
            pw_str = f"{_pw[0]} ({_pw[0]-ltp:+.0f}pts)" if _pw and ltp else "—"

            # Flow signal: delta + cum_delta (intraday) + OI direction (positional bias)
            sig, sig_color = _signal(raw_delta, cum, oi_direction)
            delta_color = "green" if raw_delta >= 0 else "red"
            cum_color   = "green" if cum >= 0 else "red"
            oi_chg_color = "green" if net_oi_chg >= 0 else "red"
            _ce_tbq = t_ce.tbq if t_ce else 0.0
            _ce_tsq = t_ce.tsq if t_ce else 0.0
            _pe_tbq = t_pe.tbq if t_pe else 0.0
            _pe_tsq = t_pe.tsq if t_pe else 0.0
        else:
            raw_delta = cum = ratio = total_buy = total_sell = total_vtt = 0.0
            ce_oi = pe_oi = net_oi_chg = oi_direction = 0.0
            ce_oi_chg = pe_oi_chg = 0.0
            sig, sig_color = "◐ NEUTRAL", "yellow"
            delta_color = cum_color = oi_chg_color = oi_bias_color = oi_dir_color = "dim white"
            oi_bias = oi_dir_str = cw_str = pw_str = "—"
            _ce_sn_iv = _pe_sn_iv = 0.0
            _ce_tbq = _ce_tsq = _pe_tbq = _pe_tsq = 0.0
            ltp = 0.0
        lines = [
            "[dim]── Flow (ATM CE+PE, live) ──────[/]",
            f"[dim]{'Delta':<12}[/] [{delta_color}]{_fmt(raw_delta, 0)}[/]",
            f"[dim]{'Cum. Delta':<12}[/] [{cum_color}]{_fmt(cum, 0)}[/]",
            f"[dim]{'Queue Ratio':<12}[/] [white]{_fmt(ratio)}  ({'Bid Heavy' if ratio >= 1 else 'Ask Heavy'})[/]",
            f"[dim]{'Bid Qty':<12}[/] [green]{_fmt(total_buy, 0)}[/]",
            f"[dim]{'Ask Qty':<12}[/] [red]{_fmt(total_sell, 0)}[/]",
            f"[dim]{'CE tbq/tsq':<12}[/] [green]{_fmt(_ce_tbq, 0)}[/][dim]/[/][red]{_fmt(_ce_tsq, 0)}[/]",
            f"[dim]{'PE tbq/tsq':<12}[/] [green]{_fmt(_pe_tbq, 0)}[/][dim]/[/][red]{_fmt(_pe_tsq, 0)}[/]",
            "[dim]── OI Positioning (±3 strikes) ─[/]",
            f"[bold]{'OI Bias':<12}[/] [bold {oi_bias_color}]{oi_bias}[/]",
            f"[dim]{'CE OI':<12}[/] [white]{_fmt(ce_oi, 0)}[/]",
            f"[dim]{'PE OI':<12}[/] [white]{_fmt(pe_oi, 0)}[/]",
            f"[dim]{'OI Direction':<12}[/] [{oi_dir_color}]{oi_dir_str}[/]",
            f"[dim]{'OI Change':<12}[/] [{oi_chg_color}]{_fmt(net_oi_chg, 0)}[/]",
            f"[dim]{'OI Signal':<12}[/] [cyan]{option_chain_service.get_oi_signal(ltp)}[/]",
            f"[dim]{'Call Wall':<12}[/] [red]{cw_str}[/]",
            f"[dim]{'Put Wall':<12}[/] [green]{pw_str}[/]",
            f"[dim]{'Volume':<12}[/] [white]{_fmt(total_vtt, 0)}[/]",
            "[dim]── IV (ATM, live) ──────────────[/]",
            f"[dim]{'CE IV':<12}[/] [white]{_ce_sn_iv:.2f}[/]",
            f"[dim]{'PE IV':<12}[/] [white]{_pe_sn_iv:.2f}[/]",
            f"[dim]{'IV Skew':<12}[/] [{'red' if _pe_sn_iv > _ce_sn_iv else 'green' if _ce_sn_iv > _pe_sn_iv else 'dim white'}]{'PE>' if _pe_sn_iv > _ce_sn_iv else 'CE>' if _ce_sn_iv > _pe_sn_iv else '='}{abs(_pe_sn_iv - _ce_sn_iv):.2f}[/]",
            "[dim]── Signal ──────────────────────[/]",
            f"[bold]{'Action':<12}[/] [bold {sig_color}]{sig}[/]",
        ]
        self.update("\n".join(lines))


class TechnicalPanel(Static):
    def on_mount(self) -> None:
        self.border_title = "Technical"
        self._refresh()
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        tf  = sma_service.get_current_tf()
        t   = feed_store.get(NIFTY)

        live_ltp   = t.ltp   if (t and t.ltp)   else 0.0
        live_close = t.close if (t and t.close)  else 0.0

        # Fallback to candle history when offline
        ltp   = live_ltp   or sma_service.get_last_close(NIFTY, tf)
        prev  = live_close or sma_service.get_prev_close(NIFTY, tf)
        is_live = bool(live_ltp)

        chg   = ltp - prev if (ltp and prev) else 0.0
        chg_p = (chg / prev * 100) if prev else 0.0
        chg_color = "green" if chg >= 0 else "red"
        chg_str = f"{chg:+.2f} ({chg_p:+.2f}%)" if (ltp and prev) else "—"
        ltp_str = (f"{ltp:,.2f}" if is_live else f"{ltp:,.2f} [dim](last)[/]") if ltp else "—"

        patterns = pattern_service.detect_all(NIFTY, tf)

        self.border_title = f"Technical  {tf.value}"
        lines = [
            f"[dim]{'LTP':<14}[/] [white]{ltp_str}[/]",
            f"[dim]{'Change':<14}[/] [{chg_color}]{chg_str}[/]",
            f"[dim]{'Prev Close':<14}[/] [dim white]{f'{prev:,.2f}' if prev else '—'}[/]",
            "[dim]── Candle Patterns ──────────[/]",
        ]
        if patterns:
            for p in patterns[:6]:   # show up to 6
                col   = "green" if p.bias == "BULL" else "red" if p.bias == "BEAR" else "yellow"
                stars = "★" * p.strength
                icon  = "▲" if p.bias == "BULL" else "▼" if p.bias == "BEAR" else "●"
                lines.append(f"  [{col}]{icon} {p.name:<22} {stars}[/]")
        else:
            lines.append("  [dim white]No pattern[/]")
        self.update("\n".join(lines))


class BestEntryPanel(Static):
    def on_mount(self) -> None:
        self.border_title = "Best Entry  (e=set  x=exit)"
        self._refresh()
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        t = feed_store.get(NIFTY)
        ltp = (t.ltp if t else 0.0) or option_chain_service.get_spot()
        atm = round(ltp / 50) * 50 if ltp else 0

        # Determine CE or PE from order flow + SMA (same logic as OverallAnalysisPanel)
        smas = sma_service.get_smas(NIFTY, TimeFrame.M5)
        sma20, sma50 = smas.get(20), smas.get(50)
        sma_score = 0
        if ltp and sma20: sma_score += 1 if ltp > sma20 else -1
        if ltp and sma50: sma_score += 1 if ltp > sma50 else -1
        # Read flow from ATM options — index feed has no tbq/tsq in v3
        if atm:
            _ck  = option_chain_service.get_key(atm, "CE")
            _pk  = option_chain_service.get_key(atm, "PE")
            _tce = feed_store.get(_ck) if _ck else None
            _tpe = feed_store.get(_pk) if _pk else None
            _ce_buy, _ce_sell = feed_store.flow_qty(_tce)
            _pe_buy, _pe_sell = feed_store.flow_qty(_tpe)
            raw_delta = (_ce_buy - _ce_sell) - (_pe_buy - _pe_sell)
            cum = feed_store.cum_delta(_ck) if _ck else 0.0
        else:
            raw_delta = cum = 0.0
        flow_score = (1 if raw_delta > 0 else -1 if raw_delta < 0 else 0) + \
                     (1 if cum > 0 else -1 if cum < 0 else 0)
        opt_type = "CE" if (sma_score + flow_score) > 0 else "PE" if (sma_score + flow_score) < 0 else None

        # Live option LTP — WSS only
        buy_price = 0.0
        delta = None
        if atm and opt_type:
            key  = option_chain_service.get_key(atm, opt_type)
            live = feed_store.get(key) if key else None
            buy_price = live.ltp   if live else 0.0
            delta     = live.delta if live else None

        lvl  = capital_service.levels(buy_price, delta)
        sz   = capital_service.sizing_with_risk(buy_price, lvl["sl"])
        expiry = option_chain_service.get_expiry() or "—"
        opt_color = "green" if opt_type == "CE" else "red" if opt_type == "PE" else "dim white"

        sl_str  = f"{lvl['sl']:.1f}"     if lvl['sl']     else "—"
        tgt_str = f"{lvl['target']:.1f}" if lvl['target']  else "—"
        pts_str = (f"Nifty ±{lvl['sl_pts']}/{lvl['tgt_pts']}pt"
                   if lvl.get('sl_pts') else "% based")

        # ── trailing SL status ────────────────────────────────────────────
        ts = trailing_sl_service.get_status()
        if ts["active"]:
            pnl_color  = "green" if ts["in_profit"] else "red"
            stop_color = "red bold" if ts["stopped_out"] else "red"
            trail_rows: list[tuple[str, str, str]] = [
                ("── Trail SL ──", "", "dim white"),
                ("Entry",    f"{ts['entry']:.1f}  [{ts['side']}]",        "white"),
                ("Peak",     f"{ts['peak']:.1f}",                          pnl_color),
                ("Trail SL", f"{ts['trail_sl']:.1f}  ({int(ts['trail_pct']*100)}% below peak)",
                             stop_color),
                ("P&L (pts)", f"{ts['pnl_pts']:+.1f}  (peak {ts['peak_pnl_pts']:+.1f})",  pnl_color),
                ("Status",   "STOPPED OUT — press x" if ts["stopped_out"] else
                             ("In Profit ✓" if ts["in_profit"] else "Tracking..."),
                             "red bold" if ts["stopped_out"] else
                             ("green" if ts["in_profit"] else "yellow")),
            ]
        else:
            trail_rows = [("── Trail SL ──", "press e to set entry", "dim white")]

        self.update(_kv([
            ("Spot",       f"{ltp:,.2f}" if ltp else "—",                    "white"),
            ("ATM Strike", f"{atm} {opt_type}" if atm and opt_type else "—",  "yellow"),
            ("Option",     opt_type or "—",                                   opt_color),
            ("Buy Price",  f"{buy_price:.1f}" if buy_price else "—",          opt_color),
            ("Lots",       str(sz["lots"]) if buy_price else "—",             "white"),
            ("Invested",   f"₹{sz['invested']:,.0f}" if buy_price else "—",   "white"),
            ("Target",     tgt_str,                                            "green"),
            ("SL",         sl_str,                                             "red"),
            ("Basis",      pts_str,                                            "dim white"),
            ("Expiry",     expiry,                                             "dim white"),
        ] + trail_rows))


class GreeksTable(Static):
    def on_mount(self) -> None:
        self.border_title = "Option Chain"
        self._refresh()
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        t = feed_store.get(NIFTY)
        ltp = (t.ltp if t else 0.0) or option_chain_service.get_spot()
        expiry = option_chain_service.get_expiry()

        if not expiry:
            self.update("[bold cyan]▸ Option Chain[/]\n[dim white]Waiting for data...[/]")
            return
        if not ltp:
            self.update("[bold cyan]▸ Option Chain[/]\n[dim white]Waiting for spot price...[/]")
            return

        atm = round(ltp / 50) * 50
        strikes = option_chain_service.get_atm_strikes(ltp, n=5)

        snap = "" if (t and t.ltp) else "  [dim](snap)[/]"
        self.border_subtitle = f"ATM [yellow]{atm}[/]  {expiry}{snap}"
        hdr = f"[dim]{'CE LTP':>8} {'CΔ':>6} {'C OI':>7}  {'Strike':^6}  {'PE LTP':>8} {'PΔ':>6} {'P OI':>7}[/]"
        rows = [hdr]

        for strike in sorted(strikes):
            ce_key = option_chain_service.get_key(strike, "CE")
            pe_key = option_chain_service.get_key(strike, "PE")

            # Live tick preferred; fall back to REST chain snapshot
            ce_t  = feed_store.get(ce_key) if ce_key else None
            pe_t  = feed_store.get(pe_key) if pe_key else None
            ce_sn = option_chain_service.get_option(strike, "CE") or {}
            pe_sn = option_chain_service.get_option(strike, "PE") or {}

            ce_ltp   = (ce_t.ltp   if ce_t else None) or (ce_sn.get("ltp") or None)
            ce_delta = (ce_t.delta if ce_t else None) or (ce_sn.get("delta") or None)
            ce_oi    = (ce_t.oi    if ce_t else None) or (ce_sn.get("oi") or None)
            pe_ltp   = (pe_t.ltp   if pe_t else None) or (pe_sn.get("ltp") or None)
            pe_delta = (pe_t.delta if pe_t else None) or (pe_sn.get("delta") or None)
            pe_oi    = (pe_t.oi    if pe_t else None) or (pe_sn.get("oi") or None)

            atm_mark = "bold yellow" if strike == atm else "white"

            def _ltp(v):  return f"{v:8.1f}"   if v is not None else f"{'—':>8}"
            def _dlt(v):  return f"{v:+6.3f}"  if v is not None else f"{'—':>6}"
            def _oi(v):   return f"{v/1e5:6.1f}L" if v is not None else f"{'—':>7}"

            row = (
                f"[green]{_ltp(ce_ltp)} {_dlt(ce_delta)} {_oi(ce_oi)}[/]"
                f"  [{atm_mark}]{strike:^6}[/]  "
                f"[red]{_ltp(pe_ltp)} {_dlt(pe_delta)} {_oi(pe_oi)}[/]"
            )
            rows.append(row)

        self.update("\n".join(rows))


class TradeModeBar(Static):
    """Prominent banner showing PAPER / LIVE mode + last live order status."""

    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        from uptox.services import trading_mode, portfolio_feed_service

        if trading_mode.is_live():
            # Show last order status alongside mode
            last = portfolio_feed_service.get_last_order()
            pf_status = portfolio_feed_service.get_status()
            feed_icon = "●" if pf_status == "connected" else "○"

            if last:
                st   = (last.get("status") or "").lower()
                sym  = last.get("trading_symbol") or last.get("instrument_token", "?")
                txn  = last.get("transaction_type", "?")
                filled = last.get("filled_quantity", 0)
                qty    = last.get("quantity", 0)
                avg    = last.get("average_price", 0.0)
                oid    = (last.get("order_id") or "")[-8:]

                if st in ("complete", "filled"):
                    st_str = f"[bold green]FILLED {txn} {sym} {filled}/{qty} @ ₹{avg:.1f}[/]"
                elif st in ("rejected", "error"):
                    err = last.get("status_message", "")[:30]
                    st_str = f"[bold red]REJECTED {txn} {sym} — {err}[/]"
                elif st == "open":
                    st_str = f"[yellow]OPEN {txn} {sym} {filled}/{qty}[/]"
                elif st == "cancelled":
                    st_str = f"[dim]CANCELLED {txn} {sym}[/]"
                else:
                    st_str = f"[dim]{st.upper()} {txn} {sym}[/]"

                order_part = f"  │  Last order ({oid}): {st_str}"
            else:
                order_part = f"  │  {feed_icon} Portfolio feed: {pf_status}"

            self.update(
                f"[bold white on red]  ▶ LIVE TRADING — REAL ORDERS ACTIVE"
                f"{order_part}"
                f"  │  press L to switch to Paper  [/]"
            )
            self.add_class("live")
            self.remove_class("paper")
        else:
            self.update(
                "[bold black on green]  ● PAPER TRADING — no real orders  "
                "  (press L to switch to Live)  [/]"
            )
            self.add_class("paper")
            self.remove_class("live")


class LivePricePanel(Static):
    def on_mount(self) -> None:
        self.border_title = "Market"
        self._refresh()
        self.set_interval(0.5, self._refresh)

    def _refresh(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        IST = ZoneInfo("Asia/Kolkata")
        now = datetime.now(IST).strftime("%H:%M:%S")

        nt  = feed_store.get(NIFTY)
        bnt = feed_store.get(BANK)

        def _price_block(label: str, key: str, tick) -> str:
            live_ltp   = tick.ltp if (tick and tick.ltp) else 0.0
            if key == NIFTY:
                chain_spot = option_chain_service.get_spot()
            elif key == BANK:
                chain_spot = option_chain_service.get_bank_spot()
            else:
                chain_spot = 0.0
            _last = (
                sma_service.get_last_close(key, TimeFrame.M5)
                or sma_service.get_last_close(key, TimeFrame.M15)
                or sma_service.get_last_close(key, TimeFrame.H1)
                or sma_service.get_last_close(key, TimeFrame.H4)
            )
            fallback   = chain_spot or _last
            ltp        = live_ltp or fallback
            if not ltp:
                return f"[dim]{label}:[/] [dim white]—[/]"
            is_live = bool(live_ltp)
            close   = (tick.close if (tick and tick.close) else 0.0)
            chg     = ltp - close if (is_live and close) else 0.0
            pct     = chg / close * 100 if close else 0.0
            col     = "bold green" if chg >= 0 else "bold red"
            arrow   = "▲" if chg >= 0 else "▼"
            if is_live:
                return (
                    f"[dim]{label}[/]  "
                    f"[{col}]{ltp:,.2f}[/]"
                    f"  [{col}]{arrow} {chg:+.2f}  ({pct:+.2f}%)[/]"
                )
            return (
                f"[dim]{label}[/]  "
                f"[dim white]{ltp:,.2f}[/] [dim](last)[/]"
            )

        status       = feed_store.get_status()
        status_color = "green" if status == "connected" else "yellow" if status == "reconnecting" else "dim white" if status == "market_closed" else "red"

        # Dynamically colour the panel border by feed status
        self.styles.border = ("round", "green" if status == "connected" else "yellow" if status == "reconnecting" else "red")
        self.update(
            f"[{status_color}]●[/] [dim]{now}[/]"
            f"     {_price_block('NIFTY 50',   NIFTY, nt)}"
            f"     {_price_block('BANK NIFTY', BANK,  bnt)}"
        )


class CapitalPanel(Widget):
    DEFAULT_CSS = "CapitalPanel { layout: vertical; height: auto; }"

    def compose(self) -> ComposeResult:
        yield Static(id="cap-display")
        yield Input(
            placeholder="Enter capital (e.g. 25000) → Enter to confirm, Esc to cancel",
            id="cap-input",
        )

    def on_mount(self) -> None:
        self.border_title = "Capital & Risk  (]/\\[ = ±5K  c=set  r=RR  Z=reset-block)"
        self._refresh()
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        t = feed_store.get(NIFTY)
        ltp = t.ltp if t else 0.0

        cap   = capital_service.get_capital()
        rr    = capital_service.get_rr()
        total = 0
        atm_c = round(ltp / 50) * 50 if ltp else 0
        if ltp and atm_c:
            smas = sma_service.get_smas(NIFTY, TimeFrame.M5)
            s20, s50 = smas.get(20), smas.get(50)
            sc   = (1 if ltp > s20 else -1 if s20 else 0) + (1 if ltp > s50 else -1 if s50 else 0)
            _ck  = option_chain_service.get_key(atm_c, "CE")
            _pk  = option_chain_service.get_key(atm_c, "PE")
            _tce = feed_store.get(_ck) if _ck else None
            _tpe = feed_store.get(_pk) if _pk else None
            _ce_buy, _ce_sell = feed_store.flow_qty(_tce)
            _pe_buy, _pe_sell = feed_store.flow_qty(_tpe)
            _rd  = (_ce_buy - _ce_sell) - (_pe_buy - _pe_sell)
            _cum = feed_store.cum_delta(_ck) if _ck else 0.0
            fc   = (1 if _rd > 0 else -1 if _rd < 0 else 0) + (1 if _cum > 0 else -1 if _cum < 0 else 0)
            total = sc + fc

        suggested_rr = capital_service.suggest_rr(total)
        rr_color = "green" if rr >= suggested_rr else "yellow"

        key = None
        for opt in ("CE", "PE"):
            k = option_chain_service.get_key(atm_c, opt)
            if k and feed_store.get(k):
                key = k; break
        live = feed_store.get(key) if key else None

        base     = capital_service.get_base_capital()
        realised = round(cap - base, 0)
        real_col = "green" if realised >= 0 else "red"
        real_str = f"  [{real_col}]({realised:+,.0f})[/]" if realised != 0 else ""

        # Risk-sized lots for display
        lvl_disp = capital_service.levels(live.ltp if live else 0.0, None)
        sz_risk  = capital_service.sizing_with_risk(
            live.ltp if live else 0.0, lvl_disp.get("sl", 0.0)
        ) if live else {"lots": 0, "invested": 0.0, "risk_rs": 0.0, "risk_pct": 0.0, "capped_by": "—"}

        # Daily stats
        ds      = trade_book.get_daily_stats()
        day_col = "green" if ds["pnl_rs_today"] >= 0 else "red"
        dl_pct  = capital_service.get_daily_loss_limit_pct()
        ptl_rs  = capital_service.get_per_trade_loss_rs()
        blocked = ds["blocked_reason"]

        inv_str  = f"₹{sz_risk['invested']:,.0f}" if live else "—"
        risk_str = f"₹{sz_risk['risk_rs']:,.0f}  ({sz_risk['risk_pct']:.1f}%)" if live else "—"
        cap_str  = f"  [dim]cap by {sz_risk['capped_by']}[/]" if live else ""
        lots_str = str(sz_risk['lots']) if live else "—"
        cl_col   = "red" if ds["consec_losses"] >= 2 else "dim white"
        cl_str   = f"{ds['consec_losses']}/{capital_service.get_max_consec_losses()}"

        # Greek breakdown for open trade
        ot = trade_book.get_open_trade()
        greek_lines = []
        if ot and ot.get("outcome") == "OPEN":
            opt_tick = feed_store.get(ot.get("option_key", ""))
            nifty_t  = feed_store.get(NIFTY)
            if opt_tick and nifty_t:
                spot_now   = nifty_t.ltp
                spot_entry = ot.get("spot_entry", spot_now)
                delta      = opt_tick.delta or 0.0
                theta      = opt_tick.theta or 0.0
                lots       = ot.get("lots", 1)
                from uptox.services.capital_service import NIFTY_LOT_SIZE
                spot_move      = spot_now - spot_entry
                delta_impact   = round(spot_move * delta * lots * NIFTY_LOT_SIZE, 0)
                theta_per_hr   = round(theta / 6.25 * lots * NIFTY_LOT_SIZE, 0)  # theta/day ÷ 6.25 hrs
                d_col = "green" if delta_impact >= 0 else "red"
                t_col = "red"
                greek_lines = [
                    "",
                    "[dim]── Open Trade Greeks ──────────[/]",
                    f"[dim]{'Δ Impact':<14}[/] [{d_col}]₹{delta_impact:+,.0f}[/]  [dim](spot {spot_move:+.0f}pt)[/]",
                    f"[dim]{'Θ Decay/hr':<14}[/] [{t_col}]₹{theta_per_hr:,.0f}[/]",
                    f"[dim]{'Per-trade SL':<14}[/] [red]₹{ptl_rs:,.0f}[/]",
                ]

        lines = [
            f"[dim]{'Capital':<14}[/] [yellow]₹{cap:,.0f}[/]{real_str}",
            f"[dim]{'Base':<14}[/] [dim white]₹{base:,.0f}[/]",
            "",
            "[dim]── Sizing ──────────────────────[/]",
            f"[dim]{'RR':<14}[/] [{rr_color}]1:{rr}[/]  [dim white]sys=1:{suggested_rr}[/]",
            f"[dim]{'Lots':<14}[/] [white]{lots_str}[/]{cap_str}",
            f"[dim]{'Invested':<14}[/] [white]{inv_str}[/]",
            f"[dim]{'Risk/trade':<14}[/] [red]{risk_str}[/]",
            f"[dim]{'Per-trade SL':<14}[/] [red]₹{ptl_rs:,.0f}[/]  [dim](L=change)[/]",
            "",
            "[dim]── Today ──────────────────────[/]",
            f"[dim]{'Trades':<14}[/] [white]{ds['trades_today']}/{capital_service.get_max_trades_per_day()}[/]",
            f"[dim]{'Day P&L':<14}[/] [{day_col}]₹{ds['pnl_rs_today']:+,.0f}  ({ds['pnl_pct_today']:+.1f}%)[/]",
            f"[dim]{'Loss Limit':<14}[/] [dim white]-{dl_pct}% = ₹{cap * dl_pct / 100:,.0f}[/]",
            f"[dim]{'Consec Loss':<14}[/] [{cl_col}]{cl_str}[/]",
        ] + greek_lines
        if blocked:
            lines += ["", f"[bold red]⚠ {blocked}[/]", "[dim](Z = reset limits)[/]"]
        elif ds.get("block_overridden"):
            lines += ["", "[yellow]⚠ Limits reset — fresh P&L + trade slots active[/]"]

        self.query_one("#cap-display", Static).update("\n".join(lines))

    def open_input(self) -> None:
        inp = self.query_one("#cap-input", Input)
        inp.add_class("active")
        inp.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "cap-input":
            try:
                val = float(event.value.replace(",", "").replace("₹", "").strip())
                if val >= 1000:
                    # Rebase: current capital = base + realised P&L (recomputed on
                    # every restart via sync_from_history). Setting current capital
                    # directly (old behavior) was silently discarded on restart —
                    # solve for the base that makes it equal val, and persist that.
                    trades = trade_book.get_all_trades()
                    realised = sum(float(t.get("pnl_rs", 0.0)) for t in trades if t.get("outcome") != "OPEN")
                    capital_service.set_capital(val - realised)
                    capital_service.sync_from_history(trades)
            except ValueError:
                pass
            self._close_input()

    def on_key(self, event) -> None:
        if event.key == "escape":
            inp = self.query_one("#cap-input", Input)
            if "active" in inp.classes:
                self._close_input()
                event.stop()

    def _close_input(self) -> None:
        inp = self.query_one("#cap-input", Input)
        inp.remove_class("active")
        inp.clear()


class SRPanel(Static):
    def on_mount(self) -> None:
        self.border_title = "Support / Resistance  (Nifty 50)"
        self._refresh()
        self.set_interval(2.0, self._refresh)

    def _refresh(self) -> None:
        t   = feed_store.get(NIFTY)
        ltp = (t.ltp if t else 0.0) or option_chain_service.get_spot()
        if not ltp:
            self.update("[dim]Waiting for price...[/]")
            return

        SR_TOL = 150  # collect levels within ±150pt
        seen: set[float] = set()
        candidates: list[tuple[float, str]] = []

        for tf, label in ((TimeFrame.M5, "5m"), (TimeFrame.H1, "1h"), (TimeFrame.H4, "4h")):
            smas = sma_service.get_smas(NIFTY, tf)
            for p in (20, 50, 100, 200):
                v = smas.get(p)
                if v and abs(v - ltp) <= SR_TOL and round(v) not in seen:
                    seen.add(round(v))
                    candidates.append((v, f"SMA{p}/{label}"))

        base = round(ltp / 100) * 100
        for off in (-200, -100, 0, 100, 200):
            lvl = float(base + off)
            if abs(lvl - ltp) <= SR_TOL and round(lvl) not in seen:
                seen.add(round(lvl))
                candidates.append((lvl, f"Round {int(lvl)}"))

        from uptox.services import prev_day_service
        _pd = prev_day_service.get_ohlc()
        if _pd:
            _pd_levels = [(_pd["high"], "PDH"), (_pd["low"], "PDL")]
            _piv = prev_day_service.get_pivots()
            if _piv:
                _pd_levels += [(_piv["pp"], "Pivot PP"), (_piv["r1"], "Pivot R1"), (_piv["s1"], "Pivot S1")]
            for lvl, lbl in _pd_levels:
                if abs(lvl - ltp) <= SR_TOL and round(lvl) not in seen:
                    seen.add(round(lvl))
                    candidates.append((lvl, lbl))

        for lbl, lvl in _swing_levels():
            if abs(lvl - ltp) <= SR_TOL and round(lvl) not in seen:
                seen.add(round(lvl))
                candidates.append((lvl, lbl))

        candidates.sort(key=lambda x: x[0])

        above = [(lvl, lbl) for lvl, lbl in candidates if lvl > ltp]
        below = [(lvl, lbl) for lvl, lbl in candidates if lvl < ltp]

        lines: list[str] = []

        lines.append("[dim]── Resistance ────────────────[/]")
        if above:
            for lvl, lbl in above[:4]:
                dist = lvl - ltp
                lines.append(
                    f"[red]{lvl:>8,.0f}[/]  [dim]{lbl:<14}[/] [dim white]+{dist:.0f}pt[/]"
                )
        else:
            lines.append("[dim]  none within 150pt[/]")

        lines.append(f"\n[bold white]  ▶ {ltp:,.2f}  (LTP)[/]\n")

        lines.append("[dim]── Support ───────────────────[/]")
        if below:
            for lvl, lbl in reversed(below[-4:]):
                dist = ltp - lvl
                lines.append(
                    f"[green]{lvl:>8,.0f}[/]  [dim]{lbl:<14}[/] [dim white]-{dist:.0f}pt[/]"
                )
        else:
            lines.append("[dim]  none within 150pt[/]")

        self.update("\n".join(lines))


class PreMarketPanel(Static):
    """
    Shows during 09:00–09:30 IST. Fetches previous session data and gives
    a morning bias (gap, PCR, SMA posture, expected move) to help with
    directional preparation before trading starts.
    """

    def on_mount(self) -> None:
        self.border_title = "Pre-Market Analysis  (09:00–09:30)"
        self._refresh()
        self.set_interval(30.0, self._refresh)

    def _refresh(self) -> None:
        from uptox.services.market_calendar import is_premarket
        if not is_premarket():
            self.display = False
            return
        self.display = True
        self.update(self._build())

    def _build(self) -> str:
        lines: list[str] = []

        # ── Previous session close from H1 candles — tuple (o,h,l,c) ─────────
        h1_candles = sma_service.get_last_candles(NIFTY, TimeFrame.H1, n=10)
        prev_close = h1_candles[-1][3] if h1_candles else 0.0   # close
        prev_high  = max((c[1] for c in h1_candles), default=0.0)
        prev_low   = min((c[2] for c in h1_candles), default=0.0)

        # ── Current pre-open price from feed ─────────────────────────────────
        t   = feed_store.get(NIFTY)
        ltp = t.ltp if t else 0.0

        # Gap analysis
        if prev_close and ltp:
            gap_pts = ltp - prev_close
            gap_pct = gap_pts / prev_close * 100
            if gap_pts > 50:
                gap_tag = f"[green]Gap UP  +{gap_pts:.0f}pt ({gap_pct:+.2f}%)[/]"
                gap_bias = 1
            elif gap_pts < -50:
                gap_tag = f"[red]Gap DOWN {gap_pts:.0f}pt ({gap_pct:+.2f}%)[/]"
                gap_bias = -1
            else:
                gap_tag = f"[yellow]Flat open  {gap_pts:+.0f}pt ({gap_pct:+.2f}%)[/]"
                gap_bias = 0
        else:
            gap_tag  = "[dim]Waiting for pre-open price...[/]"
            gap_bias = 0

        lines.append(f"Gap:  {gap_tag}")
        if prev_close:
            lines.append(
                f"Prev: close={prev_close:,.0f}  "
                f"H={prev_high:,.0f}  L={prev_low:,.0f}"
            )

        # ── ATR expected move for today ───────────────────────────────────────
        try:
            from uptox.services import atr_service
            atr = atr_service.get_atr(NIFTY, TimeFrame.H1, period=14)
            if atr:
                lines.append(f"ATR(H1×14):  {atr:.0f}pt  → expected range {atr*1.5:.0f}pt")
        except Exception:
            pass

        lines.append("")

        # ── SMA posture (is price above/below key MAs?) ───────────────────────
        smas_m5 = sma_service.get_smas(NIFTY, TimeFrame.M5)
        sma_lines: list[str] = []
        for p in (20, 50, 200):
            v = smas_m5.get(p)
            if v and ltp:
                rel = "above" if ltp > v else "below"
                col = "green" if ltp > v else "red"
                sma_lines.append(f"  SMA{p} [{col}]{v:,.0f}[/] ({rel})")
        if sma_lines:
            lines.append("[dim]SMA posture (5m):[/]")
            lines.extend(sma_lines)
            lines.append("")

        # ── PCR snapshot ──────────────────────────────────────────────────────
        pcr = option_chain_service.get_pcr()
        if pcr:
            if pcr < 0.7:
                pcr_tag = f"[red]{pcr:.2f}  (CE-heavy — bearish OI bias)[/]"
                pcr_bias = -1
            elif pcr > 1.5:
                pcr_tag = f"[green]{pcr:.2f}  (PE-heavy — bullish OI bias)[/]"
                pcr_bias = 1
            else:
                pcr_tag = f"[yellow]{pcr:.2f}  (neutral)[/]"
                pcr_bias = 0
        else:
            pcr_tag  = "[dim]loading...[/]"
            pcr_bias = 0
        lines.append(f"PCR:  {pcr_tag}")
        lines.append("")

        # ── Bias verdict ──────────────────────────────────────────────────────
        bias_score = gap_bias + pcr_bias
        if bias_score >= 2:
            verdict = "[bold green]▲ BULLISH BIAS — favour CE trades[/]"
        elif bias_score <= -2:
            verdict = "[bold red]▼ BEARISH BIAS — favour PE trades[/]"
        elif bias_score == 1:
            verdict = "[green]Slight bullish bias — CE preferred[/]"
        elif bias_score == -1:
            verdict = "[red]Slight bearish bias — PE preferred[/]"
        else:
            verdict = "[yellow]NEUTRAL — wait for 9:30 direction[/]"

        lines.append(f"Morning bias:  {verdict}")
        lines.append("")
        lines.append("[dim]Trading execution starts at 09:30. Use this to prepare, not to trade.[/]")

        return "\n".join(lines)


class DashboardScreen(ScrollableContainer):
    DEFAULT_CSS = """
    DashboardScreen {
        height: 1fr;
        layout: vertical;
        background: $background;
    }

    /* ── mode banner ── */
    TradeModeBar {
        height: 1;
        width: 100%;
        content-align: center middle;
        text-align: center;
    }

    /* ── top bar ── */
    LivePricePanel {
        border: round green;
        border-title-color: $text-muted;
        padding: 0 2;
        height: auto;
        width: 100%;
    }

    /* ── two-column body ── */
    #col-row {
        height: auto;
        width: 100%;
    }
    #left-col  { width: 2fr; height: auto; layout: vertical; }
    #right-col { width: 3fr; height: auto; layout: vertical; }

    PreMarketPanel {
        border: round #87d7af;
        border-title-color: #87d7af;
        border-title-style: bold;
        padding: 0 2;
        height: auto;
        width: 100%;
        margin-bottom: 1;
    }

    /* ── shared panel style ── */
    CapitalPanel, OverallAnalysisPanel, BestEntryPanel,
    SMAPanel, OrderFlowPanel, TechnicalPanel, GreeksTable {
        border: round $primary;
        border-title-color: $accent;
        border-title-style: bold;
        border-subtitle-color: $text-muted;
        padding: 0 1;
        height: auto;
    }

    /* ── per-panel accent colours ── */
    CapitalPanel         { border: round #d7af5f; border-title-color: #d7af5f; }
    OverallAnalysisPanel { border: round #5f87ff; border-title-color: #5f87ff; }
    BestEntryPanel       { border: round #d7875f; border-title-color: #d7875f; }
    SMAPanel             { border: round #5fafaf; border-title-color: #5fafaf; }
    OrderFlowPanel       { border: round #875faf; border-title-color: #875faf; }
    TechnicalPanel       { border: round #5f87af; border-title-color: #5f87af; }
    GreeksTable          { border: round #5faf87; border-title-color: #5faf87; }
    SRPanel              { border: round #af8700; border-title-color: #af8700; }

    /* ── capital input ── */
    CapitalPanel #cap-input {
        display: none;
        height: 3;
        background: $surface;
        color: $text;
        border: tall $primary;
    }
    CapitalPanel #cap-input.active { display: block; }
    """

    def compose(self) -> ComposeResult:
        yield TradeModeBar()
        yield LivePricePanel()
        yield PreMarketPanel()
        with Horizontal(id="col-row"):
            with Vertical(id="left-col"):
                yield CapitalPanel()
                yield OverallAnalysisPanel()
                yield SRPanel()
                yield TechnicalPanel()
            with Vertical(id="right-col"):
                yield BestEntryPanel()
                yield SMAPanel()
                yield OrderFlowPanel()
