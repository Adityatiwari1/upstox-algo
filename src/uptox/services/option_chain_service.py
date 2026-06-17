"""
Option chain service.
- Fetches nearest expiry from Upstox contract API
- Fetches full chain (REST) to build (strike, CE|PE) → instrument_key map
- Tracks ATM so tick_handler can trigger dynamic WSS re-subscription
- Dashboard reads live ticks from feed_store using the stored keys
"""
from __future__ import annotations

import logging
import httpx
from datetime import date

_log = logging.getLogger(__name__)

NIFTY_KEY = "NSE_INDEX|Nifty 50"
_OC_URL       = "https://api.upstox.com/v2/option/chain"
_CONTRACT_URL = "https://api.upstox.com/v2/option/contract"

# (strike, "CE"|"PE") → {key, ltp, oi, change_oi, volume, delta, gamma, theta, vega, iv}
_chain: dict[tuple[int, str], dict] = {}
# instrument_key → (strike, "CE"|"PE")  for reverse lookup
_key_map: dict[str, tuple[int, str]] = {}

_expiry: str = ""
_all_expiries: list[str] = []
_subscribed_atm: int = 0
_spot_price: float = 0.0  # last underlying_spot_price from REST chain response


# ── fetch helpers ─────────────────────────────────────────────────────────────

async def _fetch_expiries(access_token: str) -> list[str]:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(
            _CONTRACT_URL,
            params={"instrument_key": NIFTY_KEY},
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
    r.raise_for_status()
    raw = r.json().get("data", [])
    today = date.today().isoformat()
    # API returns list of contract objects with an "expiry" field (not bare strings)
    if raw and isinstance(raw[0], dict):
        expiries = sorted(set(
            item["expiry"] for item in raw
            if item.get("expiry", "") >= today
        ))
    else:
        expiries = sorted(e for e in raw if isinstance(e, str) and e >= today)
    _log.info("Fetched %d future expiries: %s", len(expiries), expiries[:5])
    return expiries


async def _load_chain(expiry: str, access_token: str) -> None:
    """Fetch and replace _chain/_key_map for the given expiry."""
    global _spot_price
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(
            _OC_URL,
            params={"instrument_key": NIFTY_KEY, "expiry_date": expiry},
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
    r.raise_for_status()

    _chain.clear()
    _key_map.clear()

    rows = r.json().get("data", [])
    if rows:
        _spot_price = float(rows[0].get("underlying_spot_price", 0) or 0)

    for row in rows:
        strike = int(row["strike_price"])
        for opt_type, field in (("CE", "call_options"), ("PE", "put_options")):
            opt = row.get(field) or {}
            if not opt:
                continue
            key = opt.get("instrument_key", "")
            md  = opt.get("market_data") or {}
            og  = opt.get("option_greeks") or {}
            _chain[(strike, opt_type)] = {
                "key":       key,
                "ltp":       float(md.get("ltp", 0)),
                "oi":        float(md.get("oi", 0)),
                "change_oi": float(md.get("change_oi", 0)),
                "volume":    int(md.get("vtt", 0)),
                "delta":     float(og.get("delta", 0)),
                "gamma":     float(og.get("gamma", 0)),
                "theta":     float(og.get("theta", 0)),
                "vega":      float(og.get("vega", 0)),
                "iv":        float(og.get("iv", 0)),
            }
            if key:
                _key_map[key] = (strike, opt_type)

    _log.info("oc loaded %d strikes, expiry=%s", len(_chain) // 2, expiry)


async def fetch_and_prime(access_token: str) -> None:
    global _expiry, _all_expiries
    try:
        _all_expiries = await _fetch_expiries(access_token)
        if not _all_expiries:
            _log.warning("oc no expiries found")
            return
        _expiry = _all_expiries[0]
        await _load_chain(_expiry, access_token)
    except Exception as e:
        _log.exception("oc fetch_and_prime error: %s", e)


async def switch_expiry(expiry: str, access_token: str) -> None:
    """Switch active expiry, reload chain, re-subscribe ATM strikes."""
    global _expiry, _subscribed_atm
    try:
        _expiry = expiry
        _subscribed_atm = 0  # force ATM re-subscription
        await _load_chain(expiry, access_token)
        # Re-subscribe ATM ±5 for new expiry
        from uptox.services import feed_store
        from uptox.api.feed_client import subscribe
        t = feed_store.get(NIFTY_KEY)
        if t and t.ltp:
            keys = get_keys_for_atm(t.ltp, n=5)
            if keys:
                await subscribe(keys)
    except Exception as e:
        _log.exception("oc switch_expiry error: %s", e)


# ── public helpers ─────────────────────────────────────────────────────────────

def get_expiry() -> str:
    return _expiry


def get_spot() -> float:
    """Last underlying spot price from the chain REST snapshot (fallback when feed is offline)."""
    return _spot_price


def get_all_expiries() -> list[str]:
    return list(_all_expiries)


def get_atm_strikes(ltp: float, n: int = 5) -> list[int]:
    atm = round(ltp / 50) * 50
    return sorted(atm + i * 50 for i in range(-n, n + 1))


def get_keys_for_atm(ltp: float, n: int = 5) -> list[str]:
    """Instrument keys for CE+PE of ±n strikes around ATM."""
    keys = []
    for strike in get_atm_strikes(ltp, n):
        for opt_type in ("CE", "PE"):
            d = _chain.get((strike, opt_type)) or {}
            k = d.get("key", "")
            if k:
                keys.append(k)
    return keys


def get_all_subscribed_keys() -> list[str]:
    """All option keys currently in chain (for re-subscribe on WSS reconnect)."""
    return [d["key"] for d in _chain.values() if d.get("key")]


def atm_changed(ltp: float) -> bool:
    """True if ATM crossed a 50-point boundary since last call."""
    global _subscribed_atm
    new_atm = round(ltp / 50) * 50
    if new_atm != _subscribed_atm:
        _subscribed_atm = new_atm
        return True
    return False


def get_option(strike: int, opt_type: str) -> dict | None:
    return _chain.get((strike, opt_type))


def get_key(strike: int, opt_type: str) -> str | None:
    d = _chain.get((strike, opt_type))
    return d.get("key") if d else None


def get_pcr() -> float | None:
    """Total PE OI / Total CE OI across all strikes in current chain."""
    ce_oi = sum(d["oi"] for (_, t), d in _chain.items() if t == "CE" and d["oi"])
    pe_oi = sum(d["oi"] for (_, t), d in _chain.items() if t == "PE" and d["oi"])
    return round(pe_oi / ce_oi, 2) if ce_oi else None


def get_max_pain() -> int | None:
    """Strike that minimises total ITM value (max pain to option buyers)."""
    strikes = sorted({s for s, _ in _chain.keys()})
    if not strikes:
        return None
    min_val, mp = float("inf"), strikes[0]
    for S in strikes:
        pain = sum(
            max(0, S - K) * (_chain.get((K, "CE")) or {}).get("oi", 0)
            + max(0, K - S) * (_chain.get((K, "PE")) or {}).get("oi", 0)
            for K in strikes
        )
        if pain < min_val:
            min_val, mp = pain, S
    return mp


def get_oi_signal(ltp: float, n: int = 3) -> str:
    """Interpret cumulative OI change across ATM ±n strikes."""
    if not ltp or not _chain:
        return "—"
    from uptox.services import feed_store
    atm = round(ltp / 50) * 50
    ce_chg = pe_chg = 0.0
    for i in range(-n, n + 1):
        s = atm + i * 50
        for opt_type in ("CE", "PE"):
            key = (_chain.get((s, opt_type)) or {}).get("key", "")
            tick = feed_store.get(key) if key else None
            chg  = tick.change_oi if tick else (_chain.get((s, opt_type)) or {}).get("change_oi", 0.0)
            if opt_type == "CE":
                ce_chg += chg
            else:
                pe_chg += chg
    if ce_chg > 0 and pe_chg < 0:  return "BULLISH  (CE↑ PE↓)"
    if ce_chg < 0 and pe_chg > 0:  return "BEARISH  (CE↓ PE↑)"
    if ce_chg > 0 and pe_chg > 0:  return "SIDEWAYS (CE↑ PE↑)"
    if ce_chg < 0 and pe_chg < 0:  return "UNWIND   (CE↓ PE↓)"
    return "NEUTRAL"
