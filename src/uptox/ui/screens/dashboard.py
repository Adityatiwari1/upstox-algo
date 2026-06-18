import logging
from textual.app import ComposeResult
from textual.containers import ScrollableContainer, Horizontal, Vertical
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


def _kv(rows: list[tuple[str, str, str]]) -> str:
    return "\n".join(
        f"[dim]{label:<12}[/] [{color}]{value}[/]"
        for label, value, color in rows
    )


def _affordable_strike(ltp: float, option: str) -> tuple[int, str | None, float]:
    """
    Return (strike, instrument_key, ltp) of cheapest affordable strike.
    Tries ATM first, then OTM in 50pt steps (up to 4 OTM).
    CE OTM = higher strike (cheaper); PE OTM = lower strike (cheaper).
    Returns (atm, None, 0.0) if nothing is affordable.
    """
    atm  = round(ltp / 50) * 50
    step = 50 if option == "CE" else -50
    for n in range(5):
        strike = atm + n * step
        key    = option_chain_service.get_key(strike, option)
        if not key:
            continue
        lo    = feed_store.get(key)
        price = (lo.ltp if lo else 0.0) or float(
            (option_chain_service.get_option(strike, option) or {}).get("ltp", 0.0)
        )
        if price and capital_service.sizing(price)["lots"] > 0:
            return strike, key, price
    return atm, None, 0.0


def _near_sr(ltp: float, option: str) -> str | None:
    """
    Return reason string if ltp is within SR_TOL points of a key level that
    would act as resistance (CE) or support (PE). Returns None if entry is clear.
    """
    SR_TOL = 35  # Nifty points
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

    for label, lvl in levels:
        dist = ltp - lvl  # positive = ltp above level, negative = level above ltp
        if abs(dist) > SR_TOL:
            continue
        # CE bullish: block only when level is AT or ABOVE ltp (dist <= 0 = resistance overhead)
        # PE bearish: block only when level is AT or BELOW ltp (dist >= 0 = support underfoot)
        if option == "CE" and dist <= 0:
            return f"Resistance: {label} ({abs(dist):.0f}pt above)"
        if option == "PE" and dist >= 0:
            return f"Support: {label} ({abs(dist):.0f}pt below)"
    return None


class OverallAnalysisPanel(Static):
    _last_tradeable: bool | None = None
    _last_option: str | None = None

    # ── breakout-watch state ───────────────────────────────────────────────────
    _watch: str      = "IDLE"   # "IDLE" | "WATCH_CE" | "WATCH_PE"
    _bk_ltp: float   = 0.0     # Nifty LTP at the moment score crossed ±2
    _bk_t: float     = 0.0     # time.time() when watch started
    _bk_max_dist: float = 0.0  # max pts price traveled away from breakout level
    _RETEST_PTS:  int = 20     # must return within 20pts after departure
    _MIN_DEPART:  int = 25     # must move ≥25pt away before retest can qualify
    _MAX_WAIT:    int = 45     # seconds before momentum entry kicks in
    _why_tag: str = ""         # human-readable reason why no trade entered yet

    def on_mount(self) -> None:
        self.border_title = "Overall Analysis"
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

        # Order flow score — IndexFullFeed v3 has no tbq/tsq/oi; read from ATM options
        if atm:
            _ck  = option_chain_service.get_key(atm, "CE")
            _pk  = option_chain_service.get_key(atm, "PE")
            _tce = feed_store.get(_ck) if _ck else None
            _tpe = feed_store.get(_pk) if _pk else None
            _csn = option_chain_service.get_option(atm, "CE") or {}
            _psn = option_chain_service.get_option(atm, "PE") or {}
            raw_delta = (
                (_tce.tbq if _tce else 0.0) + (_tpe.tbq if _tpe else 0.0)
                - (_tce.tsq if _tce else 0.0) - (_tpe.tsq if _tpe else 0.0)
            )
            cum    = feed_store.cum_delta(_ck) if _ck else 0.0
            oi_dir = _psn.get("change_oi", 0.0) - _csn.get("change_oi", 0.0)
        else:
            raw_delta = cum = oi_dir = 0.0
        flow_score = (
            (1 if raw_delta > 0 else -1 if raw_delta < 0 else 0)
            + (1 if cum > 0 else -1 if cum < 0 else 0)
            + (1 if oi_dir > 0 else -1 if oi_dir < 0 else 0)
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
        # ATM key (for general display/reference)
        key      = option_chain_service.get_key(atm, option) if (atm and option != "—") else None
        live_opt = feed_store.get(key) if key else None
        cur_price = live_opt.ltp if live_opt else 0.0

        # Affordable entry strike (ATM or OTM if ATM too expensive)
        if atm and option != "—":
            entry_strike, entry_key, entry_price = _affordable_strike(ltp, option)
        else:
            entry_strike, entry_key, entry_price = atm, None, 0.0
        entry_live = feed_store.get(entry_key) if entry_key else None

        # ── breakout watch + retest entry logic ───────────────────────────
        watch_tag = ""   # status line shown in panel
        entry_type: str | None = None  # "retest" | "momentum" | None
        self._why_tag = ""  # reset each tick; set below to explain no-trade reason

        if tradeable and cur_price:
            direction_flip = (self._last_option is not None and self._last_option != option)
            if direction_flip:
                if trade_book.has_open_trade():
                    trade_book.close_trade(cur_price, "direction_change")
                self._watch = "IDLE"
                self._bk_max_dist = 0.0

            if not trade_book.has_open_trade():
                watch_dir = f"WATCH_{option}"

                if self._watch == "IDLE":
                    # Breakout just confirmed — start watching for retest
                    self._watch      = watch_dir
                    self._bk_ltp     = ltp
                    self._bk_t       = time.time()
                    self._bk_max_dist = 0.0
                    log.info("Breakout watch started: %s @ Nifty %.1f", option, ltp)

                elif self._watch == watch_dir:
                    elapsed   = time.time() - self._bk_t
                    dist_now  = abs(ltp - self._bk_ltp)
                    self._bk_max_dist = max(self._bk_max_dist, dist_now)

                    # Price must travel ≥MIN_DEPART pts first; prevents instant
                    # entry on restart when price == breakout price
                    departed  = self._bk_max_dist >= self._MIN_DEPART
                    retest    = departed and dist_now <= self._RETEST_PTS
                    timed_out = departed and elapsed >= self._MAX_WAIT

                    if retest:
                        entry_type = "retest"
                    elif timed_out:
                        entry_type = "momentum"
                    else:
                        remaining = int(self._MAX_WAIT - elapsed)
                        if not departed:
                            depart_str = f"depart {self._bk_max_dist:.0f}/{self._MIN_DEPART}pt"
                            self._why_tag = (
                                f"Needs {self._MIN_DEPART - self._bk_max_dist:.0f}pt more departure "
                                f"({self._bk_max_dist:.0f}/{self._MIN_DEPART}pt so far)"
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

                    if entry_type:
                        # S/R check — block entry if near a key level
                        sr_block = _near_sr(ltp, option)
                        if sr_block:
                            watch_tag = f"  [bold red]⚠ BLOCKED — {sr_block}[/]"
                            self._why_tag = f"S/R block: {sr_block} — waiting to clear"
                            log.info("Entry blocked by S/R: %s @ %.1f  (%s)", option, ltp, sr_block)
                            entry_type = None  # stay in WATCH, retry next tick
                        elif not entry_key or not entry_price:
                            # No affordable strike — wait for price to drop
                            atm_p = cur_price or 0.0
                            self._why_tag = (
                                f"ATM ₹{atm_p:.1f} too expensive "
                                f"(cap ₹{capital_service.get_capital():.0f}) — waiting for cheaper price"
                            )
                            watch_tag = f"  [bold red]⚠ NO CAPITAL — ATM ₹{atm_p:.1f}[/]"
                            log.info("Entry skipped: ATM %.1f > capital %.0f", atm_p, capital_service.get_capital())
                            entry_type = None
                        else:
                            delta  = entry_live.delta if entry_live else None
                            lvl    = capital_service.levels(entry_price, delta)
                            sz     = capital_service.sizing(entry_price)
                            otm_note = f" OTM{entry_strike}" if entry_strike != atm else ""
                            reason = (
                                f"{entry_type} | SMA{sma_score:+d} Flow{flow_score:+d}"
                                f" | Nifty@{ltp:.0f} | {option}@{entry_strike}{otm_note}"
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
                            )
                            log.info(
                                "Trade opened (%s): %s %s @ %.1f  lots=%d  [S/R clear]%s",
                                entry_type, option, entry_strike, entry_price, sz["lots"],
                                f"  [OTM from ATM {atm}]" if entry_strike != atm else "",
                            )
                            self._watch = "IDLE"
                            self._why_tag = ""

        elif not tradeable:
            if self._watch != "IDLE":
                log.info("Breakout watch cancelled — signal dropped")
                self._watch = "IDLE"
                self._bk_max_dist = 0.0
            if trade_book.has_open_trade() and self._last_tradeable:
                ot_exit = trade_book.get_open_trade()
                exit_key  = ot_exit.get("option_key", "") if ot_exit else ""
                exit_live = feed_store.get(exit_key) if exit_key else None
                exit_p    = exit_live.ltp if exit_live else (ot_exit["entry_price"] if ot_exit else 0.0)
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
                f"[dim]{'Score':<12}[/] [dim white]{total:+d}/5  SMA {sma_score:+d}  Flow {flow_score:+d}[/]",
                f"[dim]{'PCR':<12}[/] [{pcr_color}]{pcr_str}[/]",
                f"[dim]{'Max Pain':<12}[/] [dim white]{mp_str}[/]",
            ]
        else:
            otm_label  = f" [dim](OTM from {atm})[/]" if (entry_strike and entry_strike != atm) else ""
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
                f"[dim]{'Score':<12}[/] [dim white]{total:+d}/5  SMA {sma_score:+d}  Flow {flow_score:+d}[/]",
                f"[dim]{'PCR':<12}[/] [{pcr_color}]{pcr_str}[/]",
                f"[dim]{'Max Pain':<12}[/] [dim white]{mp_str}[/]",
            ]

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

            # tbq/tsq only on MarketFullFeed (options); index feed has none
            ce_tbq = t_ce.tbq if t_ce else 0.0
            ce_tsq = t_ce.tsq if t_ce else 0.0
            pe_tbq = t_pe.tbq if t_pe else 0.0
            pe_tsq = t_pe.tsq if t_pe else 0.0
            total_buy  = ce_tbq + pe_tbq
            total_sell = ce_tsq + pe_tsq
            raw_delta  = total_buy - total_sell
            cum        = feed_store.cum_delta(ce_key) if ce_key else 0.0
            ratio      = (total_buy / total_sell) if total_sell else 0.0

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

            # CE vs PE OI bias (absolute position)
            oi_bias       = "CE Heavy (Bear)" if ce_oi > pe_oi else "PE Heavy (Bull)"
            oi_bias_color = "red" if ce_oi > pe_oi else "green"

            # OI direction from change: PE writers growing more → bullish; CE growing more → bearish
            # pe_oi_chg - ce_oi_chg > 0 = more puts written (floor being added) = bullish
            oi_direction = pe_oi_chg - ce_oi_chg
            if ce_oi_chg > pe_oi_chg:
                oi_dir_str   = f"CE↑{ce_oi_chg/1e5:.1f}L > PE↑{pe_oi_chg/1e5:.1f}L → Bear"
                oi_dir_color = "red"
            elif pe_oi_chg > ce_oi_chg:
                oi_dir_str   = f"PE↑{pe_oi_chg/1e5:.1f}L > CE↑{ce_oi_chg/1e5:.1f}L → Bull"
                oi_dir_color = "green"
            else:
                oi_dir_str, oi_dir_color = "Balanced", "yellow"

            # Flow signal: delta + cum_delta (intraday) + OI direction (positional bias)
            sig, sig_color = _signal(raw_delta, cum, oi_direction)
            delta_color = "green" if raw_delta >= 0 else "red"
            cum_color   = "green" if cum >= 0 else "red"
            oi_chg_color = "green" if net_oi_chg >= 0 else "red"
        else:
            raw_delta = cum = ratio = total_buy = total_sell = total_vtt = 0.0
            ce_oi = pe_oi = net_oi_chg = oi_direction = 0.0
            ce_oi_chg = pe_oi_chg = 0.0
            sig, sig_color = "◐ NEUTRAL", "yellow"
            delta_color = cum_color = oi_chg_color = oi_bias_color = oi_dir_color = "dim white"
            oi_bias = oi_dir_str = "—"
            ltp = 0.0

        lines = [
            "[dim]── Flow (ATM CE+PE, live) ──────[/]",
            f"[dim]{'Delta':<12}[/] [{delta_color}]{_fmt(raw_delta, 0)}[/]",
            f"[dim]{'Cum. Delta':<12}[/] [{cum_color}]{_fmt(cum, 0)}[/]",
            f"[dim]{'Buy/Sell':<12}[/] [white]{_fmt(ratio)}  ({'↑ Buyers' if ratio >= 1 else '↓ Sellers'})[/]",
            f"[dim]{'Buy Qty':<12}[/] [green]{_fmt(total_buy, 0)}[/]",
            f"[dim]{'Sell Qty':<12}[/] [red]{_fmt(total_sell, 0)}[/]",
            "[dim]── OI Positioning (±3 strikes) ─[/]",
            f"[bold]{'OI Bias':<12}[/] [bold {oi_bias_color}]{oi_bias}[/]",
            f"[dim]{'CE OI':<12}[/] [white]{_fmt(ce_oi, 0)}[/]",
            f"[dim]{'PE OI':<12}[/] [white]{_fmt(pe_oi, 0)}[/]",
            f"[dim]{'OI Direction':<12}[/] [{oi_dir_color}]{oi_dir_str}[/]",
            f"[dim]{'OI Change':<12}[/] [{oi_chg_color}]{_fmt(net_oi_chg, 0)}[/]",
            f"[dim]{'OI Signal':<12}[/] [cyan]{option_chain_service.get_oi_signal(ltp)}[/]",
            f"[dim]{'Volume':<12}[/] [white]{_fmt(total_vtt, 0)}[/]",
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

        pattern = pattern_service.detect(NIFTY, tf)
        pat_color = (
            "green" if "Bull" in pattern or "Hammer" in pattern else
            "red"   if "Bear" in pattern or "Shoot"  in pattern else
            "yellow"
        )

        self.border_title = f"Technical  {tf.value}"
        self.update(_kv([
            ("LTP",        ltp_str,                               "white"),
            ("Change",     chg_str,                               chg_color),
            ("Prev Close", f"{prev:,.2f}" if prev else "—",       "dim white"),
            ("Pattern",    pattern,                                pat_color),
        ]))


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
            raw_delta = (
                (_tce.tbq if _tce else 0.0) + (_tpe.tbq if _tpe else 0.0)
                - (_tce.tsq if _tce else 0.0) - (_tpe.tsq if _tpe else 0.0)
            )
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
        self.border_title = "Capital & Risk  (]/] = ±5K  c=set  r=RR)"
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
            _rd  = ((_tce.tbq if _tce else 0.0) + (_tpe.tbq if _tpe else 0.0)
                    - (_tce.tsq if _tce else 0.0) - (_tpe.tsq if _tpe else 0.0))
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
        sz   = capital_service.sizing(live.ltp if live else 0.0)

        lines = [
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


class DashboardScreen(ScrollableContainer):
    DEFAULT_CSS = """
    DashboardScreen {
        height: 1fr;
        layout: vertical;
        background: $background;
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
        yield LivePricePanel()
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
