"""
Market data recorder — writes one JSONL row per 5-min bar to
logs/market_data/YYYY-MM-DD.jsonl

Stored fields per bar:
  ts          bar-close timestamp (ISO8601 IST)
  nifty       Nifty 50 LTP
  bank        Bank Nifty LTP
  atm         ATM strike (nearest 50)
  ce_ltp      ATM CE LTP
  pe_ltp      ATM PE LTP
  ce_oi       ATM CE open interest
  pe_oi       ATM PE open interest
  ce_oi_chg   ATM CE OI change
  pe_oi_chg   ATM PE OI change
  ce_iv       ATM CE implied volatility (%)
  pe_iv       ATM PE implied volatility (%)
  pcr         pe_oi / ce_oi
  cum_delta   cumulative order flow delta (Nifty)
  sma20       SMA20 5-min
  sma50       SMA50 5-min
  sma_score   -2 to +2

After a month of data, replay_runner.py can use real ce_ltp / pe_ltp
as option prices instead of the spot × 0.004 approximation.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

_IST      = ZoneInfo("Asia/Kolkata")
_DATA_DIR = Path("logs/market_data")
_NIFTY    = "NSE_INDEX|Nifty 50"
_BANK     = "NSE_INDEX|Nifty Bank"


def record_bar() -> dict | None:
    """
    Snapshot current market state and append one row to today's JSONL file.
    Called once per 5-min bar close from tick_handler. Returns the row it
    wrote so live callers (the quant runner) can consume the identical
    snapshot instead of re-deriving it — or None if the bar was skipped
    (no price yet) or an error occurred.
    """
    try:
        from uptox.services import feed_store, option_chain_service, sma_service
        from uptox.services.sma_service import TimeFrame

        now   = datetime.now(_IST)
        nifty = feed_store.get(_NIFTY)
        bank  = feed_store.get(_BANK)

        nifty_ltp = (nifty.ltp if nifty else 0.0) or 0.0
        bank_ltp  = (bank.ltp  if bank  else 0.0) or 0.0

        if not nifty_ltp:
            return None  # no price — skip this bar

        atm = round(nifty_ltp / 50) * 50

        ce_sn = option_chain_service.get_option(atm, "CE") or {}
        pe_sn = option_chain_service.get_option(atm, "PE") or {}

        ce_key = option_chain_service.get_key(atm, "CE")
        pe_key = option_chain_service.get_key(atm, "PE")
        ce_t   = feed_store.get(ce_key) if ce_key else None
        pe_t   = feed_store.get(pe_key) if pe_key else None

        def _lv(tick, snap, field):
            live = getattr(tick, field, None) if tick else None
            return live if live else snap.get(field)

        ce_ltp    = _lv(ce_t, ce_sn, "ltp")    or 0.0
        pe_ltp    = _lv(pe_t, pe_sn, "ltp")    or 0.0
        ce_oi     = _lv(ce_t, ce_sn, "oi")     or 0.0
        pe_oi     = _lv(pe_t, pe_sn, "oi")     or 0.0
        ce_oi_chg = _lv(ce_t, ce_sn, "change_oi") or 0.0
        pe_oi_chg = _lv(pe_t, pe_sn, "change_oi") or 0.0
        ce_iv     = _lv(ce_t, ce_sn, "iv")     or 0.0
        pe_iv     = _lv(pe_t, pe_sn, "iv")     or 0.0

        pcr = round(pe_oi / ce_oi, 3) if ce_oi else 0.0

        cum_delta = feed_store.cum_delta(ce_key) if ce_key else 0.0

        smas  = sma_service.get_smas(_NIFTY, TimeFrame.M5)
        s20   = smas.get(20) or 0.0
        s50   = smas.get(50) or 0.0
        sc20  = (1 if nifty_ltp > s20 else -1) if s20 else 0
        sc50  = (1 if nifty_ltp > s50 else -1) if s50 else 0
        sma_score = sc20 + sc50

        row = {
            "ts":        now.isoformat(),
            "nifty":     round(nifty_ltp, 2),
            "bank":      round(bank_ltp, 2),
            "atm":       atm,
            "ce_ltp":    round(ce_ltp, 2),
            "pe_ltp":    round(pe_ltp, 2),
            "ce_oi":     int(ce_oi),
            "pe_oi":     int(pe_oi),
            "ce_oi_chg": int(ce_oi_chg),
            "pe_oi_chg": int(pe_oi_chg),
            "ce_iv":     round(ce_iv, 2),
            "pe_iv":     round(pe_iv, 2),
            "pcr":       pcr,
            "cum_delta": round(cum_delta, 2),
            "sma20":     round(s20, 2),
            "sma50":     round(s50, 2),
            "sma_score": sma_score,
        }

        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        fname = _DATA_DIR / f"{now.strftime('%Y-%m-%d')}.jsonl"
        with fname.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

        return row

    except Exception as e:
        log.error("data_recorder.record_bar error: %s", e)
        return None
