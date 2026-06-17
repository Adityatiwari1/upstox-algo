import logging
from textual.app import ComposeResult
from textual.containers import ScrollableContainer
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static, Input
from uptox.services import feed_store, sma_service, option_chain_service, capital_service
from uptox.services import pattern_service, trailing_sl_service, trade_book
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


def _kv(title: str, rows: list[tuple[str, str, str]]) -> str:
    lines = [f"[bold cyan]{title}[/]"]
    for label, value, color in rows:
        lines.append(f"[dim]{label:<12}[/] [{color}]{value}[/]")
    return "\n".join(lines)


class OverallAnalysisPanel(Static):
    _last_tradeable: bool | None = None
    _last_option: str | None = None

    # ── breakout-watch state ───────────────────────────────────────────────────
    _watch: str   = "IDLE"   # "IDLE" | "WATCH_CE" | "WATCH_PE"
    _bk_ltp: float = 0.0    # Nifty LTP at the moment score crossed ±2
    _bk_t: float   = 0.0    # time.time() when watch started
    _RETEST_PTS: int = 20   # Nifty must return within 20pts of breakout level
    _MAX_WAIT:   int = 45   # seconds before momentum entry kicks in

    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        import time
        t = feed_store.get(NIFTY)
        live_ltp = t.ltp if t else 0.0
        ltp = live_ltp or option_chain_service.get_spot()

        # ATM strike (nearest 50)
        atm = round(ltp / 50) * 50 if ltp else 0

        # SMA trend score from 5min (most responsive)
        smas  = sma_service.get_smas(NIFTY, TimeFrame.M5)
        sma20 = smas.get(20)
        sma50 = smas.get(50)
        sma_ready = sma20 is not None and sma50 is not None
        sma_score = 0
        if ltp and sma20: sma_score += (1 if ltp > sma20 else -1)
        if ltp and sma50: sma_score += (1 if ltp > sma50 else -1)

        # Order flow score
        cum       = feed_store.cum_delta(NIFTY)
        raw_delta = (t.tbq - t.tsq) if t else 0.0
        oi_chg    = t.change_oi if t else 0.0
        flow_score = (
            (1 if raw_delta > 0 else -1 if raw_delta < 0 else 0)
            + (1 if cum > 0 else -1 if cum < 0 else 0)
            + (1 if oi_chg > 0 else -1 if oi_chg < 0 else 0)
        )

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

        # ── trade validity ────────────────────────────────────────────────
        no_trade_reason = None
        if not live_ltp:
            no_trade_reason = "No live data"
        elif not sma_ready:
            no_trade_reason = "SMA loading..."
        elif option == "—":
            no_trade_reason = "Trend too weak"
        elif sma_score != 0 and flow_score != 0 and (sma_score > 0) != (flow_score > 0):
            no_trade_reason = "SMA vs Flow divergence"
        elif abs(flow_score) == 1 and abs(sma_score) <= 1:
            no_trade_reason = "Weak conviction"

        tradeable = no_trade_reason is None

        # ── alert on signal change ────────────────────────────────────────
        if self._last_tradeable is not None and self._last_tradeable != tradeable:
            self.app.bell()
            log.info("Signal flip: %s → %s  (score=%+d)",
                     "TRADE" if self._last_tradeable else "NO TRADE",
                     "TRADE" if tradeable else "NO TRADE", total)
        self._last_tradeable = tradeable

        # ── option key + live price ───────────────────────────────────────
        key      = option_chain_service.get_key(atm, option) if (atm and option != "—") else None
        live_opt = feed_store.get(key) if key else None
        cur_price = live_opt.ltp if live_opt else 0.0

        # ── breakout watch + retest entry logic ───────────────────────────
        watch_tag = ""   # status line shown in panel
        entry_type: str | None = None  # "retest" | "momentum" | None

        if tradeable and cur_price:
            direction_flip = (self._last_option is not None and self._last_option != option)
            if direction_flip:
                if trade_book.has_open_trade():
                    trade_book.close_trade(cur_price, "direction_change")
                self._watch = "IDLE"  # reset watch on direction flip

            if not trade_book.has_open_trade():
                watch_dir = f"WATCH_{option}"

                if self._watch == "IDLE":
                    # Breakout just confirmed — start watching for retest
                    self._watch  = watch_dir
                    self._bk_ltp = ltp
                    self._bk_t   = time.time()
                    log.info("Breakout watch started: %s @ Nifty %.1f", option, ltp)

                elif self._watch == watch_dir:
                    elapsed  = time.time() - self._bk_t
                    retest   = abs(ltp - self._bk_ltp) <= self._RETEST_PTS
                    timed_out = elapsed >= self._MAX_WAIT

                    if retest:
                        entry_type = "retest"
                    elif timed_out:
                        entry_type = "momentum"
                    else:
                        remaining = int(self._MAX_WAIT - elapsed)
                        dist = abs(ltp - self._bk_ltp)
                        watch_tag = (
                            f"  [bold yellow]⟳ WATCH {option}[/]"
                            f" [dim]retest <{self._RETEST_PTS}pt ({dist:.0f}pt away)"
                            f"  {remaining}s[/]"
                        )

                    if entry_type:
                        delta = live_opt.delta if live_opt else None
                        lvl   = capital_service.levels(cur_price, delta)
                        sz    = capital_service.sizing(cur_price)
                        trade_book.open_trade(
                            strike=atm, option=option,
                            expiry=option_chain_service.get_expiry() or "—",
                            entry_price=cur_price,
                            lots=sz["lots"], invested=sz["invested"],
                            sl=lvl["sl"], target=lvl["target"],
                            option_key=key or "", delta=delta, spot=ltp,
                        )
                        log.info("Trade opened (%s): %s @ %.1f", entry_type, option, cur_price)
                        self._watch = "IDLE"

        elif not tradeable:
            if self._watch != "IDLE":
                log.info("Breakout watch cancelled — signal dropped")
                self._watch = "IDLE"
            if trade_book.has_open_trade() and self._last_tradeable:
                ot = trade_book.get_open_trade()
                exit_key  = ot.get("option_key", "") if ot else ""
                exit_live = feed_store.get(exit_key) if exit_key else None
                exit_p    = exit_live.ltp if exit_live else (ot["entry_price"] if ot else 0.0)
                trade_book.close_trade(exit_p, "signal_flip")

        self._last_option = option if tradeable else self._last_option

        # ── live buy price ────────────────────────────────────────────────
        buy_price = 0.0
        if tradeable and atm:
            live = feed_store.get(option_chain_service.get_key(atm, option) or "") if atm else None
            buy_price = live.ltp if live else 0.0

        # ── PCR + Max Pain ────────────────────────────────────────────────
        pcr       = option_chain_service.get_pcr()
        max_pain  = option_chain_service.get_max_pain()
        pcr_str   = f"{pcr:.2f}" if pcr else "—"
        pcr_color = ("green" if pcr and pcr > 1.2 else "red" if pcr and pcr < 0.8 else "yellow")
        mp_str    = str(max_pain) if max_pain else "—"

        # ── render ────────────────────────────────────────────────────────
        opt_color  = "green" if option == "CE" else "red" if option == "PE" else "dim white"
        strike_str = f"{atm} {option}" if atm and tradeable else (f"{atm}" if atm else "—")
        ltp_str    = (f"{ltp:,.2f}" if live_ltp else f"{ltp:,.2f} [snap]") if ltp else "—"
        buy_str    = f"{buy_price:.1f}" if buy_price else "—"

        if self._watch != "IDLE" and not entry_type:
            action_line = (
                f"[dim]{'Action':<12}[/] [bold {opt_color}]BUY {option} @ {atm}[/]{watch_tag}"
            )
        elif tradeable:
            action_line = f"[dim]{'Action':<12}[/] [bold {opt_color}]BUY {option} @ {atm}[/]"
        else:
            action_line = f"[dim]{'Action':<12}[/] [bold red]NO TRADE — {no_trade_reason}[/]"

        lines = [
            f"[bold cyan]▸ Overall Analysis[/]  [dim]LTP:[/] [yellow]{ltp_str}[/]",
            action_line,
            f"[dim]{'Strike':<12}[/] [yellow]{strike_str if tradeable else '—'}[/]",
            f"[dim]{'Option':<12}[/] [bold {opt_color}]{option if tradeable else '—'}[/]",
            f"[dim]{'Buy Price':<12}[/] [{opt_color}]{buy_str}[/]",
            f"[dim]{'Trend':<12}[/] [bold {trend_color}]{trend}[/]",
            f"[dim]{'Score':<12}[/] [dim white]{total:+d}/5  SMA {sma_score:+d}  Flow {flow_score:+d}[/]",
            f"[dim]{'PCR':<12}[/] [{pcr_color}]{pcr_str}[/]",
            f"[dim]{'Max Pain':<12}[/] [dim white]{mp_str}[/]",
        ]
        self.update("\n".join(lines))


class SMAPanel(Static):
    current_tf: reactive[TimeFrame] = reactive(TimeFrame.M5)

    def on_mount(self) -> None:
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
        status_color = "green" if status == "connected" else "yellow" if status == "reconnecting" else "red"
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
        self.update(_kv(
            f"▸ SMA [{tf.value}]  [{status_color}]{status}[/]  LTP:[yellow]{ltp_str}[/]",
            [
                ("SMA 10",  v10,  c10),
                ("SMA 20",  v20,  c20),
                ("SMA 50",  v50,  c50),
                ("SMA 100", v100, c100),
                ("SMA 200", v200, c200),
            ]
        ))


class OrderFlowPanel(Static):
    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        t = feed_store.get(NIFTY)

        if t:
            raw_delta = t.tbq - t.tsq
            cum       = feed_store.cum_delta(NIFTY)
            ratio     = (t.tbq / t.tsq) if t.tsq else 0.0
            sig, sig_color = _signal(raw_delta, cum, t.change_oi)
            delta_color = "green" if raw_delta >= 0 else "red"
            cum_color   = "green" if cum >= 0 else "red"
            oi_color    = "green" if t.change_oi >= 0 else "red"
            mbp_bias    = "Buyers" if t.mbp_buy > t.mbp_sell else "Sellers"
            mbp_color   = "green" if t.mbp_buy > t.mbp_sell else "red"
        else:
            raw_delta = cum = ratio = 0.0
            sig, sig_color = "◐ NEUTRAL", "yellow"
            delta_color = cum_color = oi_color = mbp_color = "dim white"
            mbp_bias = "—"

        def row(label: str, value: str, color: str) -> tuple[str, str, str]:
            return (label, value, color)

        lines = [
            "[bold cyan]▸ Order Flow Analysis[/]",
            "[dim]── Delta ──────────────────────[/]",
            f"[dim]{'Delta':<12}[/] [{delta_color}]{_fmt(raw_delta, 0)}[/]",
            f"[dim]{'Cum. Delta':<12}[/] [{cum_color}]{_fmt(cum, 0)}[/]",
            f"[dim]{'Buy/Sell':<12}[/] [white]{_fmt(ratio)}  ({'↑ Buyers' if ratio >= 1 else '↓ Sellers'})[/]",
            "[dim]── Imbalance ───────────────────[/]",
            f"[dim]{'MBP Bias':<12}[/] [{mbp_color}]{mbp_bias}[/]",
            f"[dim]{'Buy Qty':<12}[/] [green]{_fmt(t.tbq, 0) if t else '—'}[/]",
            f"[dim]{'Sell Qty':<12}[/] [red]{_fmt(t.tsq, 0) if t else '—'}[/]",
            "[dim]── OI / Liquidity ─────────────[/]",
            f"[dim]{'OI':<12}[/] [white]{_fmt(t.oi, 0) if t else '—'}[/]",
            f"[dim]{'OI Change':<12}[/] [{oi_color}]{_fmt(t.change_oi, 0) if t else '—'}[/]",
            f"[dim]{'Volume':<12}[/] [white]{_fmt(t.vtt, 0) if t else '—'}[/]",
            "[dim]── OI Interpretation ─────────[/]",
            f"[dim]{'OI Signal':<12}[/] [cyan]{option_chain_service.get_oi_signal(t.ltp if t else 0)}[/]",
            "[dim]── Signal ──────────────────────[/]",
            f"[bold]{'Action':<12}[/] [bold {sig_color}]{sig}[/]",
        ]
        self.update("\n".join(lines))


class TechnicalPanel(Static):
    def on_mount(self) -> None:
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

        pattern = pattern_service.detect(NIFTY, tf)
        pat_color = (
            "green" if "Bull" in pattern or "Hammer" in pattern else
            "red"   if "Bear" in pattern or "Shoot"  in pattern else
            "yellow"
        )

        self.update(_kv(f"▸ Technical  [dim]({tf.value})[/]", [
            ("LTP",        ltp_str,                               "white"),
            ("Change",     chg_str,                               chg_color),
            ("Prev Close", f"{prev:,.2f}" if prev else "—",       "dim white"),
            ("Pattern",    pattern,                                pat_color),
        ]))


class BestEntryPanel(Static):
    def on_mount(self) -> None:
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
        cum = feed_store.cum_delta(NIFTY)
        raw_delta = (t.tbq - t.tsq) if t else 0.0
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
        sz   = capital_service.sizing(buy_price)
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
                ("P&L (pts)", f"{ts['pnl_pts']:+.1f}",                    pnl_color),
                ("Status",   "STOPPED OUT — press x" if ts["stopped_out"] else
                             ("In Profit ✓" if ts["in_profit"] else "Tracking..."),
                             "red bold" if ts["stopped_out"] else
                             ("green" if ts["in_profit"] else "yellow")),
            ]
        else:
            trail_rows = [("── Trail SL ──", "press e to set entry", "dim white")]

        self.update(_kv("▸ Best Entry  [dim]( e = set entry  x = exit )[/]", [
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

        spot_label = "" if (t and t.ltp) else "  [dim](snapshot)[/]"
        hdr = (
            f"[bold cyan]▸ Option Chain[/]  "
            f"[dim]ATM:[/][yellow]{atm}[/]  "
            f"[dim]Expiry:[/][white]{expiry}[/]{spot_label}\n"
            f"[dim]{'CE LTP':>8} {'CΔ':>6} {'C OI':>7}  {'Strike':^6}  {'PE LTP':>8} {'PΔ':>6} {'P OI':>7}[/]"
        )
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


class LivePricePanel(Static):
    def on_mount(self) -> None:
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
            live_ltp  = tick.ltp if (tick and tick.ltp) else 0.0
            chain_spot = option_chain_service.get_spot() if key == NIFTY else 0.0
            fallback   = chain_spot or sma_service.get_last_close(key)
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
        status_color = "green" if status == "connected" else "yellow" if status == "reconnecting" else "red"

        self.update(
            f"[{status_color}]●[/] [dim]{now}[/]"
            f"     {_price_block('NIFTY 50',   NIFTY, nt)}"
            f"     {_price_block('BANK NIFTY', BANK,  bnt)}"
        )


class CapitalPanel(Widget):
    DEFAULT_CSS = """
    CapitalPanel { layout: vertical; height: auto; }
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
        yield Static(id="cap-display")
        yield Input(
            placeholder="Enter capital (e.g. 25000) → Enter to confirm, Esc to cancel",
            id="cap-input",
        )

    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        t = feed_store.get(NIFTY)
        ltp = t.ltp if t else 0.0

        cap   = capital_service.get_capital()
        rr    = capital_service.get_rr()
        total = 0
        if t:
            smas = sma_service.get_smas(NIFTY, TimeFrame.M5)
            s20, s50 = smas.get(20), smas.get(50)
            sc = (1 if ltp > s20 else -1 if s20 else 0) + (1 if ltp > s50 else -1 if s50 else 0)
            cum = feed_store.cum_delta(NIFTY)
            fc  = (1 if t.tbq > t.tsq else -1) + (1 if cum > 0 else -1)
            total = sc + fc

        suggested_rr = capital_service.suggest_rr(total)
        rr_color = "green" if rr >= suggested_rr else "yellow"

        atm = round(ltp / 50) * 50 if ltp else 0
        key = None
        for opt in ("CE", "PE"):
            k = option_chain_service.get_key(atm, opt)
            if k and feed_store.get(k):
                key = k; break
        live = feed_store.get(key) if key else None
        sz   = capital_service.sizing(live.ltp if live else 0.0)

        lines = [
            f"[bold cyan]▸ Capital & Risk[/]  "
            "[dim]( [/][dim white][[/][yellow]][[/][dim white]/[/][yellow]]][/]"
            "[dim] = ±₹5K  [/][dim white]c[/][dim] = set capital  [/]"
            "[dim white]r[/][dim] = cycle RR )[/]",
            f"[dim]{'Capital':<12}[/] [yellow]₹{cap:,.0f}[/]",
            f"[dim]{'RR (user)':<12}[/] [{rr_color}]1:{rr}[/]",
            f"[dim]{'RR (sys)':<12}[/] [dim white]1:{suggested_rr}[/]",
            f"[dim]{'Lot Size':<12}[/] [dim white]{capital_service.NIFTY_LOT_SIZE} units[/]",
            f"[dim]{'Lots':<12}[/] [white]{sz['lots'] if live else '—'}[/]",
            "[dim]" + f"{'Invested':<12}" + "[/] [white]" + (f"₹{sz['invested']:,.0f}" if live else "—") + "[/]",
            "[dim]" + f"{'Max Loss':<12}" + "[/] [red]"   + (f"₹{sz['invested'] * 0.3:,.0f}" if live else "—") + "[/]",
        ]
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
                    capital_service.set_capital(val)
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


class DashboardScreen(ScrollableContainer):
    DEFAULT_CSS = """
    DashboardScreen {
        height: 1fr;
        layout: vertical;
    }
    DashboardScreen > * {
        border: solid $primary-darken-2;
        padding: 0 1;
        width: 100%;
        height: auto;
    }
    GreeksTable {
        height: auto;
    }
    GreeksTable DataTable {
        height: auto;
    }
    """

    def compose(self) -> ComposeResult:
        yield LivePricePanel()
        yield CapitalPanel()
        yield OverallAnalysisPanel()
        yield SMAPanel()
        yield OrderFlowPanel()
        yield BestEntryPanel()
        yield TechnicalPanel()
        yield GreeksTable()
