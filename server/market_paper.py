"""market_paper.py - paper trading on real market data (the Markets page's "Paper trading" tab).

Three virtual accounts trade by fixed rules on live prices:
  vn      1,000,000,000 VND, HOSE/HNX stocks from the Vietnam table (lots of 100, T+2, 0.15% fee + 0.1% sell tax)
  crypto  20,000 USD, top coins with a Binance USDT pair (0.1% fee)
  world   20,000 USD, US-listed stocks from the World table (whole shares)

No money moves anywhere: every order is simulated at the live price plus slippage and fees.

The rules (one pure function, `decide()`, shared by live trading and the historical simulation):
  * Market filter: new buys only when the market regime is not risk-off AND the benchmark (VN-Index, S&P 500,
    Bitcoin) trades above its 50-day average; total exposure capped at 90% (risk-on), 60% (neutral), 30% (risk-off).
  * Timing: no chasing a price far above its 20-day average and no buying a falling day; those symbols wait on the
    watch list with an acceptable price zone. Vietnam and crypto buy pullbacks inside an uptrend; World keeps a 70%
    core in SPY/QQQ while they trade above their 200-day average and 30% in stock picks (chosen from 120 and
    250-session simulations: stock picking alone lagged the S&P 500).
  * Entry: trend score >= 35, RSI 45-70, price above MA20 > MA50 and above MA200, enough liquidity, not sold in the
    last 5 days. Best scores first.
  * Size: risk 1% of equity between entry and stop; one position at most 15% (crypto 20%) of equity.
  * Stop: 2 x ATR below entry (2.5 x for crypto), clamped to 5-12% (8-20% crypto). Trailing stop 3 x ATR under the
    highest close. Half the position is sold at +3R. A broken trend (score <= -20 or price 2% under MA50) closes it.
These limit losses and let gains run; they do not guarantee a profit, and the page says so.

Capital deployment (owner, 2026-11-16: too much money went in at the start, no DCA, nothing for a falling market):
  * Ramp: a new account may hold 30% of equity in positions, rising to the regime cap over 4 weeks.
  * World core ETFs are bought in 4 weekly tranches (DCA) and not topped up after that.
  * Crypto picks start at half their planned size and get the rest at +1R with the stop moved to entry (scale-in).
    Vietnam and World buy the full size: on the 250-session replays splitting lowered their return.
  * Falling market (risk-off breadth and the benchmark under its 50-day average): the trailing stop tightens to
    2 x ATR and, once a day, up to two of the weakest picks are sold until picks are back near 40% of equity.
Rules can change only through `set_rule` (the owner, or an approved weekly proposal from market_learn.py), inside
the bounds in TUNABLE, and every change is logged and can be undone.

Every trade stores the analysis behind it; every run stores what was considered and why symbols were skipped.
Tables live in market.db next to the history archive (market_store.py). Blocking sqlite: run in a thread.
"""
from __future__ import annotations

import datetime as _dt
import json
import math
import sqlite3
import threading
import time

import market_store as ms
from market_factors import group_of

PARAMS = {
    "vn": {"cash": 1_000_000_000, "ccy": "VND", "unit": 1000, "lot": 100, "max_pos": 8, "risk": 0.01, "max_w": 0.15,
           "fee_buy": 0.0015, "fee_sell": 0.0025, "slip": 0.001, "stop_atr": 2.0, "stop_min": 0.05, "stop_max": 0.12,
           "trail_atr": 3.0, "t_plus": 2, "min_value": 20e6, "entry_every_h": 20, "bench": "VNINDEX"},
    "crypto": {"cash": 20_000, "ccy": "USD", "unit": 1, "lot": 0, "max_pos": 6, "risk": 0.01, "max_w": 0.20,
               "fee_buy": 0.001, "fee_sell": 0.001, "slip": 0.0005, "stop_atr": 2.5, "stop_min": 0.08, "stop_max": 0.20,
               "trail_atr": 3.0, "t_plus": 0, "min_value": 0, "entry_every_h": 4, "bench": "bitcoin"},
    "world": {"cash": 20_000, "ccy": "USD", "unit": 1, "lot": 1, "max_pos": 8, "risk": 0.01, "max_w": 0.15,
              "fee_buy": 0.0, "fee_sell": 0.0, "slip": 0.0005, "stop_atr": 2.0, "stop_min": 0.05, "stop_max": 0.12,
              "trail_atr": 3.0, "t_plus": 0, "min_value": 0, "entry_every_h": 20, "bench": "^GSPC",
              "core": {"SPY": 0.45, "QQQ": 0.25}},
}
# Entry style per market, chosen from the 120 and 250-session simulations (see the module docstring):
# buying pullbacks inside an uptrend held up in Vietnam and crypto; in the US a core index position beat stock picking.
STYLE = {"vn": "pullback", "crypto": "pullback", "world": "momentum"}
EXPOSURE = {"risk_on": 0.9, "neutral": 0.6, "risk_off": 0.3, "unknown": 0.3}
ENTRY = {"score": 35, "rsi_lo": 45, "rsi_hi": 70, "cooldown_days": 5, "take_profit_r": 3.0, "trend_exit": -20,
         "style": None, "pb_score": 0, "pb_rsi_lo": 38, "pb_rsi_hi": 55, "ma_exit": True, "timing": True,
         # capital deployment (owner, 2026-11-16: "vào vốn với số lượng lớn trong giai đoạn đầu")
         "ramp_weeks": 4, "ramp_floor": 0.3,          # a new account may hold 30% in picks, rising to the regime cap
         "first_tranche": 1.0, "add_at_r": 1.0,       # share of the planned size bought first; the rest at +1R
         "core_tranches": 4, "core_every_days": 7,    # World core ETFs: weekly DCA to the target weight
         "defend": 1, "defend_trail_atr": 2.0, "defend_max_sells": 2,  # falling market: tighter trail, trim weakest
         "avoid_sectors": [], "factor_weight": 0.0,   # learned: groups to skip, weight of the macro tailwind in ranking,
         "factor_min": -100}                          # and the lowest macro factor score a buy accepts (-100: off)
# Per-market defaults that differ, chosen on 250-session replays of the archive (2026-11-16): splitting entries in
# two cut crypto's worst drawdown from -6.7% to -5.2% at about the same return, but lowered the return in Vietnam
# (+0.6% -> -1.3%) and World (-3.6% -> -6.9%), so only crypto starts with half the size.
KIND_ENTRY = {"crypto": {"first_tranche": 0.5}}
# What the weekly review may propose to change, with the range it may never leave. The owner approves each change.
TUNABLE = {
    "score": (20, 60), "rsi_lo": (35, 55), "rsi_hi": (60, 80), "pb_score": (-10, 40), "pb_rsi_lo": (30, 50),
    "pb_rsi_hi": (45, 70), "cooldown_days": (2, 15), "take_profit_r": (2.0, 4.0), "stop_atr": (1.5, 3.5),
    "trail_atr": (2.0, 4.0), "first_tranche": (0.34, 1.0), "ramp_weeks": (0, 8), "defend": (0, 1),
    "factor_weight": (0.0, 0.5), "factor_min": (-100, 0), "avoid_sectors": None,
}
KINDS = tuple(PARAMS)
_LOCK = threading.Lock()
# Called as notifier(kind, fills) after every committed live buy or sell (main.py sends it to the owner's Telegram).
# Runs in the worker thread that made the trade; it must not block and must not raise into the trade.
_NOTIFIER = None


def set_notifier(fn) -> None:
    global _NOTIFIER
    _NOTIFIER = fn


def _notify(kind: str, fills: list) -> None:
    if not fills or not _NOTIFIER:
        return
    try:
        _NOTIFIER(kind, fills)
    except Exception as e:  # noqa: BLE001 - a broken channel must never undo or block a trade
        import sys
        print(f"[paper] notify {kind}: {type(e).__name__}: {e}", file=sys.stderr)


def _db() -> sqlite3.Connection:
    con = ms._db()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS paper_accounts (kind TEXT PRIMARY KEY, cash REAL, start_cash REAL, started REAL,
            running INTEGER, bench_start REAL, last_entry REAL, note TEXT);
        CREATE TABLE IF NOT EXISTS paper_positions (kind TEXT, symbol TEXT, qty REAL, avg REAL, opened REAL, stop REAL,
            high REAL, risk0 REAL, partial INTEGER, sellable REAL, name TEXT, PRIMARY KEY (kind, symbol));
        CREATE TABLE IF NOT EXISTS paper_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, symbol TEXT, side TEXT,
            qty REAL, price REAL, value REAL, fee REAL, at REAL, reason TEXT, pnl REAL, pnl_pct REAL, analysis TEXT);
        CREATE TABLE IF NOT EXISTS paper_equity (kind TEXT, ts REAL, equity REAL, cash REAL, bench REAL,
            PRIMARY KEY (kind, ts));
        CREATE TABLE IF NOT EXISTS paper_log (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, at REAL, data TEXT);
        CREATE TABLE IF NOT EXISTS paper_rules (kind TEXT, key TEXT, value TEXT, set_at REAL, source TEXT,
            PRIMARY KEY (kind, key));
        CREATE TABLE IF NOT EXISTS paper_rule_log (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, key TEXT, old TEXT,
            new TEXT, at REAL, source TEXT, proposal INTEGER, undone INTEGER DEFAULT 0);
    """)
    cols = {r[1] for r in con.execute("PRAGMA table_info(paper_positions)")}
    for col, typ in (("plan_qty", "REAL"), ("adds", "INTEGER DEFAULT 0"), ("last_add", "REAL")):
        if col not in cols:
            con.execute(f"ALTER TABLE paper_positions ADD COLUMN {col} {typ}")
    return con


# ---------------------------------------------------------------- the rules (pure)

def _r(v, nd=6):
    return None if v is None else round(v, nd)


def universe(kind: str, rows: list) -> list:
    out = []
    for r in rows or []:
        if not r.get("price") or not r.get("sma50") or r.get("group") == "index":
            continue
        if kind == "world" and (r.get("group") not in ("stock", "etf") or "." in str(r.get("symbol"))):
            continue
        if kind == "crypto" and not r.get("deep"):
            continue
        out.append(r)
    return out


def effective(kind: str, rules: dict | None = None) -> tuple[dict, dict]:
    """The parameters an account trades with: the written defaults, then every rule change the owner approved."""
    p, e = dict(PARAMS[kind]), {**ENTRY, **KIND_ENTRY.get(kind, {})}
    for k, v in (rules or {}).items():
        if k in p:
            p[k] = v
        elif k in e:
            e[k] = v
    return p, e


def stop_for(kind: str, price: float, atr_pct, p: dict | None = None) -> float:
    p = p or PARAMS[kind]
    dist = (atr_pct or 0) / 100 * p["stop_atr"]
    dist = min(max(dist, p["stop_min"]), p["stop_max"])
    return price * (1 - dist)


def _lot(kind: str, qty: float) -> float:
    lot = PARAMS[kind]["lot"]
    if lot:
        return math.floor(qty / lot) * lot
    return math.floor(qty * 1e6) / 1e6


def ramp_cap(e: dict, started, now: float) -> float:
    """Ceiling on the share of equity in stock picks while a new account deploys its cash in stages."""
    weeks = e.get("ramp_weeks") or 0
    if not started or weeks <= 0:
        return 1.0
    age = max(0.0, (now - started) / (7 * 86400))
    floor = e.get("ramp_floor", 0.3)
    return min(1.0, floor + (1 - floor) * age / weeks)


def decide(kind: str, state: dict, rows: list, regime: dict, now: float, allow_entries: bool,
           rules: dict | None = None) -> dict:
    """One trading pass. state = {"cash", "positions": {sym: pos}, "recent_sells": {sym: ts}, "started"?}.
    `rules` are the owner-approved changes to the defaults (`effective`). Returns {"orders", "log"}, touching no storage."""
    p, E = effective(kind, rules)
    unit = p["unit"]
    by = {(r.get("id") or r.get("symbol")): r for r in universe(kind, rows)}
    positions = state.get("positions") or {}
    orders, skipped, considered, watch = [], [], [], []
    style = E.get("style") or STYLE.get(kind)
    pullback = style == "pullback"

    def mark(sym):
        r = by.get(sym)
        return r["price"] if r else positions[sym]["avg"]

    equity = state["cash"] + sum(pos["qty"] * mark(s) * unit for s, pos in positions.items())
    label = (regime or {}).get("label") or "unknown"
    ramp = ramp_cap(E, state.get("started"), now)
    cap = min(EXPOSURE.get(label, 0.3), ramp)
    # A falling market: risk-off breadth AND the benchmark under its 50-day average. Then trailing stops tighten and,
    # once a day, the weakest picks are sold until the book is back near the risk-off exposure.
    defending = bool(E.get("defend")) and label == "risk_off" and (regime or {}).get("bench_up") is False
    trail_atr = E["defend_trail_atr"] if defending else p["trail_atr"]

    # ---- exits first
    core = p.get("core") or {}
    for sym, pos in positions.items():
        r = by.get(sym)
        if not r:
            continue
        price = r["price"]
        if sym in core:
            if r.get("sma200") and price < r["sma200"] * 0.98:
                orders.append({"side": "sell", "symbol": sym, "qty": pos["qty"], "price": price, "reason": "core_exit",
                               "analysis": _analysis(kind, r, regime, extra={"entry": pos["avg"], "core": True})})
            continue
        atr_abs = (r.get("atr_pct") or 0) / 100 * price
        high = max(pos.get("high") or pos["avg"], price)
        trail = high - trail_atr * atr_abs if atr_abs else None
        stop = pos["stop"]
        # The trailing stop only takes over once the position is at least 1R in profit; before that the entry stop
        # rules (a 3 x ATR trail would sit above a 5% minimum stop on quiet stocks and sell them on noise).
        r0 = pos.get("risk0") or 0
        if trail and trail > stop and (not r0 or high >= pos["avg"] + r0):
            stop = trail
        can_sell = now >= (pos.get("sellable") or 0)
        reason = None
        init_stop = pos["avg"] - (pos.get("risk0") or 0) if pos.get("risk0") else pos["stop"]
        if price <= stop:
            reason = "trail" if stop > init_stop + 1e-12 else "stop"
        elif (r.get("score") or 0) <= E["trend_exit"] or (E.get("ma_exit", True) and r.get("sma50") and price < r["sma50"] * 0.98):
            reason = "trend"
        qty = pos["qty"]
        if not reason and not pos.get("partial") and r0 and price >= pos["avg"] + E["take_profit_r"] * r0:
            reason, qty = "take_profit", _lot(kind, pos["qty"] / 2)
        if reason and qty > 0:
            if not can_sell:
                skipped.append({"symbol": sym, "why": "t_plus"})
                continue
            orders.append({"side": "sell", "symbol": sym, "qty": qty, "price": price, "reason": reason,
                           "analysis": _analysis(kind, r, regime, stop=stop, extra={"high": high, "entry": pos["avg"]})})
        elif high > (pos.get("high") or 0) or stop > pos["stop"]:
            orders.append({"side": "update", "symbol": sym, "high": high, "stop": max(stop, pos["stop"])})

    selling = {o["symbol"] for o in orders if o["side"] == "sell" and o["qty"] >= positions[o["symbol"]]["qty"]}
    held = set(positions) - selling

    # ---- defend: trim the weakest picks in a falling market, once a day (the decision window)
    if defending and allow_entries:
        limit = EXPOSURE["risk_off"] + 0.1
        picks = [s for s in held if s not in core and s in by and now >= (positions[s].get("sellable") or 0)]
        inv_picks = sum(positions[s]["qty"] * mark(s) * unit for s in held if s not in core)
        sold = 0
        for s in sorted(picks, key=lambda s: (by[s].get("score") or 0, by[s]["price"] / positions[s]["avg"])):
            if inv_picks <= limit * equity or sold >= E["defend_max_sells"]:
                break
            r = by[s]
            orders.append({"side": "sell", "symbol": s, "qty": positions[s]["qty"], "price": r["price"], "reason": "defend",
                           "analysis": _analysis(kind, r, regime, extra={"entry": positions[s]["avg"], "defend": True})})
            inv_picks -= positions[s]["qty"] * r["price"] * unit
            held.discard(s)
            selling.add(s)
            sold += 1

    invested = sum(positions[s]["qty"] * mark(s) * unit for s in held)
    cash = state["cash"] + sum(o["qty"] * o["price"] * unit * (1 - p["slip"] - p["fee_sell"])
                               for o in orders if o["side"] == "sell")
    # Core index positions (World): follow their own 200-day trend only, whatever the stock-picking filter says.
    # Bought in weekly tranches (DCA) up to their target weight instead of in one go.
    if allow_entries:
        for sym, w in core.items():
            r = by.get(sym)
            if not r or sym in selling or not r.get("sma200"):
                continue
            if r["price"] < r["sma200"]:
                skipped.append({"symbol": sym, "why": "core_below_sma200"})
                continue
            pos = positions.get(sym)
            target = w * equity
            have = pos["qty"] * r["price"] * unit if pos else 0.0
            # built: the target reached, or every DCA tranche bought (no weekly top-ups after that; a fall in price is
            # not a reason to buy more of a position that is already full)
            if have >= target * 0.97 or (pos and (pos.get("adds") or 0) >= E["core_tranches"] - 1):
                continue
            if pos and now - (pos.get("last_add") or pos.get("opened") or 0) < E["core_every_days"] * 86400 - 6 * 3600:
                skipped.append({"symbol": sym, "why": "core_dca_wait"})
                continue
            if (r.get("chg_1d") or 0) < -1.5:
                watch.append({"symbol": sym, "name": r.get("name") or sym, "price": r["price"], "zone": None,
                              "why": "falling_today", "score": r.get("score") or 0, "rsi": _r(r.get("rsi"), 1)})
                continue
            price = r["price"] * (1 + p["slip"])
            tranche = target / max(1, E["core_tranches"])
            qty = _lot(kind, min(tranche, target - have, cash / (1 + p["fee_buy"])) / (price * unit))
            if qty <= 0:
                continue
            cash -= qty * price * unit * (1 + p["fee_buy"])
            invested += qty * price * unit
            held = held | {sym}
            orders.append({"side": "buy", "symbol": sym, "qty": qty, "price": r["price"], "stop": r["sma200"] * 0.98,
                           "name": r.get("name") or sym, "reason": "core_add" if pos else "core",
                           "analysis": _analysis(kind, r, regime, stop=r["sma200"] * 0.98, extra={
                               "core": True, "target_weight": round(w * 100, 1),
                               "weight": round((have + qty * price * unit) / equity * 100, 2) if equity else None})})
    if not allow_entries:
        pass
    elif label == "risk_off":
        skipped.append({"symbol": "*", "why": "regime"})
    elif (regime or {}).get("bench_up") is False:
        skipped.append({"symbol": "*", "why": "bench_down"})
    else:
        # ---- scale in: a pick bought with its first tranche gets the rest once it proves itself (+1R, trend intact).
        # The stop then moves up to the first entry, so the larger position risks little more than the first one did.
        for sym in sorted(held - set(core)):
            pos, r = positions[sym], by.get(sym)
            if not r or pos.get("adds") or pos.get("partial") or not pos.get("plan_qty") or pos["plan_qty"] <= pos["qty"]:
                continue
            r0 = pos.get("risk0") or 0
            if not r0 or r["price"] < pos["avg"] + E["add_at_r"] * r0:
                continue
            if (r.get("sma20") and r["price"] < r["sma20"]) or (r.get("score") or 0) < 0:
                continue
            price = r["price"] * (1 + p["slip"])
            room = cap * equity - invested
            qty = _lot(kind, min((pos["plan_qty"] - pos["qty"]) * price * unit, room, cash / (1 + p["fee_buy"])) / (price * unit))
            if qty <= 0 or qty * price * unit < equity * 0.005:
                continue
            cash -= qty * price * unit * (1 + p["fee_buy"])
            invested += qty * price * unit
            new_stop = max(pos["stop"], pos["avg"])
            orders.append({"side": "buy", "symbol": sym, "qty": qty, "price": r["price"], "stop": new_stop,
                           "name": pos.get("name") or sym, "reason": "add",
                           "analysis": _analysis(kind, r, regime, stop=new_stop, extra={
                               "entry": pos["avg"], "r_now": round((r["price"] - pos["avg"]) / r0, 2)})})
        avoid = set(E.get("avoid_sectors") or [])
        cands = []
        for sym, r in by.items():
            if sym in held:
                continue
            why = None
            score, rsi = r.get("score") or 0, r.get("rsi") or 50
            price = r["price"]
            if sym in core or r.get("group") == "etf":
                continue
            if score < (E["pb_score"] if pullback else E["score"]):
                continue  # not a candidate at all
            if pullback and not (r.get("sma50") and r.get("sma200") and price > r["sma50"] > r["sma200"]):
                why = "ma_order"
            elif pullback and not (E["pb_rsi_lo"] <= rsi <= E["pb_rsi_hi"]):
                why = "rsi"
            elif pullback and r.get("sma20") and not (0.97 * r["sma20"] <= price <= 1.02 * r["sma20"]):
                why = "not_pullback"
            elif not pullback and not (E["rsi_lo"] <= rsi <= E["rsi_hi"]):
                why = "rsi"
            elif not pullback and not (r.get("sma20") and r.get("sma50") and price > r["sma20"] > r["sma50"]):
                why = "ma_order"
            elif r.get("sma200") and price < r["sma200"]:
                why = "below_sma200"
            elif p["min_value"] and (r.get("value") or 0) < p["min_value"]:
                why = "liquidity"
            elif now - (state.get("recent_sells") or {}).get(sym, 0) < E["cooldown_days"] * 86400:
                why = "cooldown"
            elif avoid and group_of(kind, r) in avoid:
                why = "sector_avoid"
            elif E.get("factor_min", -100) > -100 and r.get("factor") is not None and r["factor"] < E["factor_min"]:
                why = "factor_headwind"
            zone = buy_zone(r, pullback)
            if not why and E.get("timing", True) and zone:
                # Entry timing per symbol: never chase a price that ran far above its 20-day average, never buy into
                # a falling day; such symbols wait on the watch list with the price zone that would be acceptable.
                if price > zone[1]:
                    why = "extended"
                elif (r.get("chg_1d") or 0) < (0.0 if pullback else -1.0):
                    why = "falling_today"
                if why:
                    watch.append({"symbol": sym, "name": r.get("name") or sym, "price": price, "zone": [_r(zone[0]), _r(zone[1])],
                                  "why": why, "score": score, "rsi": _r(rsi, 1)})
            elif why == "not_pullback" and zone:
                # in an uptrend but away from its 20-day average: wait for the price to come back into the zone
                why = "extended" if price > zone[1] else "not_pullback"
                watch.append({"symbol": sym, "name": r.get("name") or sym, "price": price, "zone": [_r(zone[0]), _r(zone[1])],
                              "why": why, "score": score, "rsi": _r(rsi, 1)})
            considered.append({"symbol": sym, "score": score, "rsi": _r(rsi, 1), "why": why or "ok"})
            if why:
                skipped.append({"symbol": sym, "why": why})
            else:
                cands.append(r)
        fw = E.get("factor_weight") or 0
        cands.sort(key=lambda r: -((r.get("score") or 0) + 0.3 * (r.get("chg_3m") or 0) + fw * (r.get("factor") or 0)))
        slots = p["max_pos"] - len(held - set(core))
        for r in cands:
            sym = r.get("id") or r.get("symbol")
            if slots <= 0:
                skipped.append({"symbol": sym, "why": "max_positions"})
                continue
            room = cap * equity - invested
            if room <= equity * 0.02:
                skipped.append({"symbol": sym, "why": "ramp" if ramp < EXPOSURE.get(label, 0.3) else "exposure_cap"})
                continue
            price = r["price"] * (1 + p["slip"])
            stop = stop_for(kind, r["price"], r.get("atr_pct"), p)
            risk_per = (price - stop) * unit
            if risk_per <= 0:
                continue
            budget = min(equity * p["risk"] / risk_per * price * unit, equity * p["max_w"])
            plan_qty = _lot(kind, budget / (price * unit))
            # First tranche only; the rest waits for the position to prove itself (scale in above).
            first = _lot(kind, plan_qty * E["first_tranche"]) or plan_qty
            qty = _lot(kind, min(first * price * unit, room, cash / (1 + p["fee_buy"])) / (price * unit))
            if qty <= 0 or qty * price * unit < equity * 0.01:
                skipped.append({"symbol": sym, "why": "cash"})
                continue
            cash -= qty * price * unit * (1 + p["fee_buy"])
            invested += qty * price * unit
            slots -= 1
            plan_qty = max(plan_qty, qty)
            orders.append({"side": "buy", "symbol": sym, "qty": qty, "price": r["price"], "stop": stop, "plan_qty": plan_qty,
                           "name": r.get("name") or r.get("symbol"), "reason": "entry",
                           "analysis": _analysis(kind, r, regime, stop=stop, extra={
                               "risk_amount": round(qty * (price - stop) * unit, 2), "plan_qty": plan_qty,
                               "weight": round(qty * price * unit / equity * 100, 2) if equity else None,
                               "target_r3": _r(price + E["take_profit_r"] * (price - stop))})})
    log = {"at": now, "regime": label, "cap": round(cap, 3), "ramp": round(ramp, 3), "defending": defending,
           "equity": round(equity, 2), "bench_up": (regime or {}).get("bench_up"),
           "invested_pct": round(invested / equity * 100, 1) if equity else 0, "allow_entries": allow_entries,
           "considered": sorted(considered, key=lambda c: -c["score"])[:25], "skipped": skipped[:40],
           "watch": sorted(watch, key=lambda w: -w["score"])[:15],
           "actions": [{"side": o["side"], "symbol": o["symbol"], "qty": o["qty"], "price": o["price"], "reason": o["reason"]}
                       for o in orders if o["side"] != "update"]}
    return {"orders": orders, "log": log}


def with_factor(kind: str, rows: list) -> list:
    """Rows with their group's macro factor score now (market_factors, cached an hour). Missing data: no score."""
    try:
        import market_factors as mf
        sym = mf.cached(kind).get("symbols") or {}
    except Exception:  # noqa: BLE001 - a rule that cannot see the factor simply does not use it
        return rows
    return [dict(r, factor=(sym.get(r.get("id") or r.get("symbol")) or {}).get("factor")) for r in rows]


def buy_zone(r: dict, pullback: bool) -> tuple | None:
    """Acceptable buy prices for a symbol: around its 20-day average, padded by its own volatility (ATR)."""
    s20, price = r.get("sma20"), r.get("price")
    if not s20 or not price:
        return None
    atr = (r.get("atr_pct") or 0) / 100 * price or price * 0.02
    return (s20 - atr, s20 + (1.0 if pullback else 2.0) * atr)


def with_bench(regime: dict | None, bench_row: dict | None) -> dict:
    out = dict(regime or {})
    if bench_row and bench_row.get("sma50") and bench_row.get("price"):
        out["bench_up"] = bench_row["price"] > bench_row["sma50"]
        out["bench_price"] = bench_row["price"]
        out["bench_sma50"] = bench_row["sma50"]
    return out


def _analysis(kind: str, r: dict, regime: dict, stop=None, extra=None) -> dict:
    a = {k: r.get(k) for k in ("price", "score", "signal", "rsi", "trend", "sma20", "sma50", "sma200", "atr_pct",
                                 "exp_move_5d", "move_score", "chg_1d", "chg_5d", "chg_1m", "chg_3m", "vol_ratio",
                                 "reasons", "from_high")}
    a["regime"] = (regime or {}).get("label")
    a["regime_score"] = (regime or {}).get("score")
    a["bench_up"] = (regime or {}).get("bench_up")
    if stop is not None:
        a["stop"] = _r(stop)
        a["stop_pct"] = round((stop / r["price"] - 1) * 100, 2) if r.get("price") else None
    a.update(extra or {})
    return a


# ---------------------------------------------------------------- storage

def _account(con, kind):
    r = con.execute("SELECT cash, start_cash, started, running, bench_start, last_entry FROM paper_accounts WHERE kind=?",
                    (kind,)).fetchone()
    if not r:
        return None
    return {"cash": r[0], "start_cash": r[1], "started": r[2], "running": bool(r[3]), "bench_start": r[4],
            "last_entry": r[5] or 0}


def _positions(con, kind):
    out = {}
    for sym, qty, avg, opened, stop, high, risk0, partial, sellable, name, plan_qty, adds, last_add in con.execute(
            "SELECT symbol, qty, avg, opened, stop, high, risk0, partial, sellable, name, plan_qty, adds, last_add "
            "FROM paper_positions WHERE kind=?", (kind,)):
        out[sym] = {"qty": qty, "avg": avg, "opened": opened, "stop": stop, "high": high, "risk0": risk0,
                    "partial": bool(partial), "sellable": sellable, "name": name, "plan_qty": plan_qty,
                    "adds": adds or 0, "last_add": last_add}
    return out


def merge_buy(pos: dict | None, qty: float, price: float, stop: float, now: float, sellable: float,
              plan_qty: float | None = None, name: str = "") -> dict:
    """A position after a buy: a new one, or a held one topped up (scale-in, core DCA) at a weighted average price.
    A top-up keeps the first settlement day: the T+2 lock of the added shares is not tracked separately."""
    if not pos:
        return {"qty": qty, "avg": price, "opened": now, "stop": stop, "high": price, "risk0": price - stop,
                "partial": False, "sellable": sellable, "name": name, "plan_qty": plan_qty or qty, "adds": 0, "last_add": now}
    total = pos["qty"] + qty
    return dict(pos, qty=total, avg=(pos["qty"] * pos["avg"] + qty * price) / total, stop=max(stop, pos["stop"]),
                high=max(pos.get("high") or price, price), adds=(pos.get("adds") or 0) + 1, last_add=now,
                plan_qty=max(pos.get("plan_qty") or 0, total))


# ---------------------------------------------------------------- learned rules (approved by the owner)

def rules_for(kind: str, con=None) -> dict:
    """The rule changes the owner approved for an account, {key: value}."""
    own = con is None
    con = con or _db()
    try:
        return {k: json.loads(v) for k, v in con.execute("SELECT key, value FROM paper_rules WHERE kind=?", (kind,))}
    finally:
        if own:
            con.close()


def _default(kind: str, key: str):
    p, e = effective(kind)
    return p[key] if key in p else e.get(key)


def clamp_rule(kind: str, key: str, value):
    """A value forced inside the key's TUNABLE range, with the type of its default."""
    if key not in TUNABLE:
        raise ValueError(key)
    rng = TUNABLE[key]
    if rng is None:
        return sorted({str(x) for x in (value or [])})
    v = min(max(float(value), rng[0]), rng[1])
    return int(round(v)) if isinstance(_default(kind, key), int) else round(v, 3)


def set_rule(kind: str, key: str, value, source: str = "owner", proposal: int | None = None) -> dict:
    """Change one rule (inside its TUNABLE range). Returns the log entry; `undo_rule` takes it back."""
    value = clamp_rule(kind, key, value)
    with _LOCK:
        con = _db()
        try:
            cur = rules_for(kind, con)
            old = cur.get(key, _default(kind, key))
            if value == _default(kind, key):
                con.execute("DELETE FROM paper_rules WHERE kind=? AND key=?", (kind, key))
            else:
                con.execute("INSERT OR REPLACE INTO paper_rules VALUES (?,?,?,?,?)", (kind, key, json.dumps(value), time.time(), source))
            c = con.execute("INSERT INTO paper_rule_log (kind, key, old, new, at, source, proposal) VALUES (?,?,?,?,?,?,?)",
                            (kind, key, json.dumps(old), json.dumps(value), time.time(), source, proposal))
            con.commit()
            return {"id": c.lastrowid, "kind": kind, "key": key, "old": old, "new": value}
        finally:
            con.close()


def undo_rule(log_id: int) -> dict | None:
    """Put a rule back to what it was before one change (the default when that change was the first)."""
    with _LOCK:
        con = _db()
        try:
            row = con.execute("SELECT kind, key, old, undone FROM paper_rule_log WHERE id=?", (log_id,)).fetchone()
            if not row or row[3]:
                return None
            kind, key, old = row[0], row[1], json.loads(row[2])
            if old == _default(kind, key):
                con.execute("DELETE FROM paper_rules WHERE kind=? AND key=?", (kind, key))
            else:
                con.execute("INSERT OR REPLACE INTO paper_rules VALUES (?,?,?,?,?)", (kind, key, json.dumps(old), time.time(), "undo"))
            con.execute("UPDATE paper_rule_log SET undone=1 WHERE id=?", (log_id,))
            con.commit()
            return {"kind": kind, "key": key, "value": old}
        finally:
            con.close()


def rule_view(kind: str) -> dict:
    """Defaults, current values and the change log, for the page."""
    with _LOCK:
        con = _db()
        try:
            cur = rules_for(kind, con)
            log = [dict(zip(("id", "key", "old", "new", "at", "source", "proposal", "undone"), r)) for r in con.execute(
                "SELECT id, key, old, new, at, source, proposal, undone FROM paper_rule_log WHERE kind=? ORDER BY id DESC LIMIT 30",
                (kind,))]
        finally:
            con.close()
    p1, e1 = effective(kind, cur)
    out = []
    for k, rng in TUNABLE.items():
        d = _default(kind, k)
        v = p1[k] if k in p1 else e1.get(k)
        out.append({"key": k, "default": d, "value": v, "changed": d != v, "range": list(rng) if rng else None})
    for x in log:
        x["old"], x["new"], x["undone"] = json.loads(x["old"]), json.loads(x["new"]), bool(x["undone"])
    return {"kind": kind, "rules": out, "log": log}


def _recent_sells(con, kind, now):
    return {s: t for s, t in con.execute(
        "SELECT symbol, MAX(at) FROM paper_trades WHERE kind=? AND side='sell' AND at>? GROUP BY symbol",
        (kind, now - 30 * 86400))}


def _add_weekdays(ts: float, n: int, tz: int = 7 * 3600) -> float:
    """Settlement day for T+n: n weekdays later, sellable from 09:00 local."""
    d = _dt.datetime.fromtimestamp(ts + tz, _dt.timezone.utc).date()
    while n > 0:
        d += _dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return _dt.datetime(d.year, d.month, d.day, 9, 0, tzinfo=_dt.timezone.utc).timestamp() - tz


def start(kind: str, bench_price: float | None = None) -> dict:
    with _LOCK:
        con = _db()
        try:
            acc = _account(con, kind)
            if acc:
                con.execute("UPDATE paper_accounts SET running=1 WHERE kind=?", (kind,))
            else:
                cash = PARAMS[kind]["cash"]
                con.execute("INSERT INTO paper_accounts VALUES (?,?,?,?,?,?,?,?)",
                            (kind, cash, cash, time.time(), 1, bench_price, 0, ""))
            con.commit()
            return _account(con, kind)
        finally:
            con.close()


def set_running(kind: str, running: bool) -> dict | None:
    with _LOCK:
        con = _db()
        try:
            con.execute("UPDATE paper_accounts SET running=? WHERE kind=?", (1 if running else 0, kind))
            con.commit()
            return _account(con, kind)
        finally:
            con.close()


def reset(kind: str) -> None:
    with _LOCK:
        con = _db()
        try:
            for t in ("paper_accounts", "paper_positions", "paper_trades", "paper_equity", "paper_log"):
                con.execute(f"DELETE FROM {t} WHERE kind=?", (kind,))
            con.commit()
        finally:
            con.close()


def _apply(con, kind: str, orders: list, now: float) -> list:
    p = PARAMS[kind]
    unit = p["unit"]
    acc = _account(con, kind)
    cash = acc["cash"]
    positions = _positions(con, kind)
    done = []
    for o in orders:
        sym = o["symbol"]
        if o["side"] == "update":
            con.execute("UPDATE paper_positions SET high=?, stop=? WHERE kind=? AND symbol=?", (o["high"], o["stop"], kind, sym))
            continue
        if o["side"] == "buy":
            price = o["price"] * (1 + p["slip"])
            value = o["qty"] * price * unit
            fee = value * p["fee_buy"]
            if value + fee > cash + 1e-6:
                continue
            cash -= value + fee
            sellable = _add_weekdays(now, p["t_plus"]) if p["t_plus"] else now
            x = merge_buy(positions.get(sym), o["qty"], price, o["stop"], now, sellable, o.get("plan_qty"), o.get("name") or sym)
            positions[sym] = x
            con.execute("INSERT OR REPLACE INTO paper_positions (kind, symbol, qty, avg, opened, stop, high, risk0, partial, "
                        "sellable, name, plan_qty, adds, last_add) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (kind, sym, x["qty"], x["avg"], x["opened"], x["stop"], x["high"], x["risk0"], int(x["partial"]),
                         x["sellable"], x.get("name") or sym, x["plan_qty"], x["adds"], x["last_add"]))
            con.execute("INSERT INTO paper_trades (kind, symbol, side, qty, price, value, fee, at, reason, pnl, pnl_pct, analysis) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (kind, sym, "buy", o["qty"], price, value, fee, now, o.get("reason") or "entry", None, None,
                         json.dumps(o.get("analysis") or {})))
            done.append(dict(o, fill=price, value=value, fee=fee, cash=cash, at=now, held=x["qty"]))
        elif o["side"] == "sell":
            pos = positions.get(sym)
            if not pos:
                continue
            qty = min(o["qty"], pos["qty"])
            price = o["price"] * (1 - p["slip"])
            value = qty * price * unit
            fee = value * p["fee_sell"]
            cost = qty * pos["avg"] * unit * (1 + p["fee_buy"])
            pnl = value - fee - cost
            cash += value - fee
            left = pos["qty"] - qty
            if left <= 1e-9:
                con.execute("DELETE FROM paper_positions WHERE kind=? AND symbol=?", (kind, sym))
            else:
                con.execute("UPDATE paper_positions SET qty=?, partial=1 WHERE kind=? AND symbol=?", (left, kind, sym))
            an = dict(o.get("analysis") or {})
            an["held_days"] = round((now - (pos.get("opened") or now)) / 86400, 1)
            an["r_multiple"] = round((price - pos["avg"]) / pos["risk0"], 2) if pos.get("risk0") else None
            con.execute("INSERT INTO paper_trades (kind, symbol, side, qty, price, value, fee, at, reason, pnl, pnl_pct, analysis) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (kind, sym, "sell", qty, price, value, fee, now, o["reason"], pnl, pnl / cost * 100 if cost else None,
                         json.dumps(an)))
            done.append(dict(o, qty=qty, fill=price, value=value, fee=fee, pnl=pnl, pnl_pct=pnl / cost * 100 if cost else None,
                             left=max(left, 0.0), cash=cash, at=now, name=pos.get("name") or sym))
    con.execute("UPDATE paper_accounts SET cash=? WHERE kind=?", (cash, kind))
    return done


def _equity_point(con, kind, now, prices: dict, bench: float | None):
    acc = _account(con, kind)
    unit = PARAMS[kind]["unit"]
    pos = _positions(con, kind)
    eq = acc["cash"] + sum(p["qty"] * prices.get(s, p["avg"]) * unit for s, p in pos.items())
    last = con.execute("SELECT ts FROM paper_equity WHERE kind=? ORDER BY ts DESC LIMIT 1", (kind,)).fetchone()
    # one point per 30 minutes is plenty for the curve
    if last and now - last[0] < 1800:
        con.execute("UPDATE paper_equity SET equity=?, cash=?, bench=? WHERE kind=? AND ts=?", (eq, acc["cash"], bench, kind, last[0]))
    else:
        con.execute("INSERT OR REPLACE INTO paper_equity VALUES (?,?,?,?,?)", (kind, now, eq, acc["cash"], bench))
    return eq


def entries_allowed(kind: str, acc: dict, now: float, force: bool = False) -> bool:
    if force:
        return True
    if now - (acc.get("last_entry") or 0) < PARAMS[kind]["entry_every_h"] * 3600:
        return False
    utc = _dt.datetime.fromtimestamp(now, _dt.timezone.utc)
    if kind == "vn":
        import market_data as md
        vn = md.vn_now(now)
        m = vn.hour * 60 + vn.minute
        return md.vn_in_session(now) and 14 * 60 <= m <= 14 * 60 + 28  # end-of-day confirmation, before ATC
    if kind == "world":
        m = utc.hour * 60 + utc.minute
        return utc.weekday() < 5 and 19 * 60 <= m <= 20 * 60 + 50  # last two hours of the US session
    return True


def market_open(kind: str, now: float) -> bool:
    if kind == "vn":
        import market_data as md
        return md.vn_in_session(now)
    if kind == "world":
        utc = _dt.datetime.fromtimestamp(now, _dt.timezone.utc)
        m = utc.hour * 60 + utc.minute
        return utc.weekday() < 5 and 13 * 60 + 30 <= m <= 21 * 60
    return True


def run(kind: str, data: dict, now: float | None = None, force: bool = False, live_prices: dict | None = None) -> dict:
    """One live pass on a freshly computed section. Exits are checked whenever the market trades; entries only in
    the account's decision window (or when forced from the page). Returns the decision log."""
    now = now or time.time()
    rows = list(data.get("rows") or [])
    if live_prices:
        rows = [dict(r, price=live_prices.get(r.get("id") or r.get("symbol"), [r.get("price")])[0] or r.get("price"))
                for r in rows]
    bench_row = next((r for r in (data.get("indices") or []) + rows if (r.get("id") or r.get("symbol")) == PARAMS[kind]["bench"]), None)
    with _LOCK:
        con = _db()
        try:
            acc = _account(con, kind)
            if not acc or not acc["running"]:
                return {}
            prices = {(r.get("id") or r.get("symbol")): r["price"] for r in rows if r.get("price")}
            bench = bench_row["price"] if bench_row else None
            if acc["bench_start"] is None and bench:
                con.execute("UPDATE paper_accounts SET bench_start=? WHERE kind=?", (bench, kind))
            if not market_open(kind, now) and not force:
                _equity_point(con, kind, now, prices, bench)
                con.commit()
                return {}
            allow = entries_allowed(kind, acc, now, force)
            state = {"cash": acc["cash"], "positions": _positions(con, kind), "recent_sells": _recent_sells(con, kind, now),
                     "started": acc["started"]}
            rules = rules_for(kind, con)
            if rules.get("factor_weight") or rules.get("factor_min", -100) > -100:
                rows = with_factor(kind, rows)
            res = decide(kind, state, rows, with_bench(data.get("regime"), bench_row), now, allow, rules)
            done = _apply(con, kind, res["orders"], now)
            if allow:
                con.execute("UPDATE paper_accounts SET last_entry=? WHERE kind=?", (now, kind))
            if allow or done:
                res["log"]["forced"] = force
                res["log"]["market_open"] = market_open(kind, now)
                con.execute("INSERT INTO paper_log (kind, at, data) VALUES (?,?,?)", (kind, now, json.dumps(res["log"])))
                con.execute("DELETE FROM paper_log WHERE kind=? AND id NOT IN (SELECT id FROM paper_log WHERE kind=? ORDER BY id DESC LIMIT 300)", (kind, kind))
            _equity_point(con, kind, now, prices, bench)
            con.commit()
        finally:
            con.close()
    _notify(kind, done)
    return res["log"]


def close_position(kind: str, symbol: str, price: float, now: float | None = None) -> bool:
    now = now or time.time()
    with _LOCK:
        con = _db()
        try:
            pos = _positions(con, kind).get(symbol)
            if not pos:
                return False
            if now < (pos.get("sellable") or 0):
                raise ValueError("t_plus")
            done = _apply(con, kind, [{"side": "sell", "symbol": symbol, "qty": pos["qty"], "price": price, "reason": "manual",
                                       "analysis": {"price": price}}], now)
            con.commit()
        finally:
            con.close()
    _notify(kind, done)
    return True


# ---------------------------------------------------------------- trade notice (Telegram)

_REASONS = {
    "entry": ("Tín hiệu vào lệnh", "Signal entry"), "core": ("Mua phần lõi chỉ số", "Core index buy"),
    "stop": ("Chạm cắt lỗ", "Stop hit"), "trail": ("Chạm cắt lỗ đuổi (giữ lãi)", "Trailing stop (gain kept)"),
    "trend": ("Gãy xu hướng", "Trend broke"), "take_profit": ("Chốt lời một nửa", "Half take-profit"),
    "core_exit": ("Thoát phần lõi (dưới MA200)", "Core exit (below MA200)"), "manual": ("Bán tay trên trang", "Sold by hand"),
    "add": ("Mua thêm đợt 2: vị thế đã lãi, cắt lỗ dời lên giá vốn", "Second tranche: position in profit, stop moved to entry"),
    "core_add": ("Mua định kỳ phần lõi (DCA hằng tuần)", "Scheduled core buy (weekly DCA)"),
    "defend": ("Thị trường xấu: giảm tỷ trọng mã yếu nhất", "Falling market: trimming the weakest holding"),
}
_ACCOUNTS = {"vn": ("Chứng khoán VN", "Vietnam stocks"), "crypto": ("Tiền số", "Crypto"),
             "world": ("Chứng khoán thế giới", "World stocks")}


def fill_message(kind: str, f: dict) -> str:
    """One plain-text Telegram notice for a committed buy or sell of a virtual account."""
    import localefmt
    chu = localefmt.chu
    vi = chu("vi", "en") == "vi"
    p = PARAMS[kind]
    unit = p["unit"]

    def n(v, nd=0):
        t = f"{v:,.{nd}f}"
        return t.translate(str.maketrans(",.", ".,")) if vi else t

    def money(v):
        return (n(v) + " đ") if p["ccy"] == "VND" else ("$" + n(v, 2))

    def price(v):
        if p["ccy"] == "VND":
            return n(v * unit) + " đ"
        return "$" + n(v, 6 if v < 1 else 2)

    def qty(v):
        if kind != "crypto":
            return n(v)
        t, dec = n(v, 6), ("," if vi else ".")
        return t.rstrip("0").rstrip(dec) if dec in t else t

    sym = f.get("symbol") or ""
    name = f.get("name") or sym
    label = sym if name == sym else f"{sym} ({name})"
    acc = chu(*_ACCOUNTS.get(kind, (kind, kind)))
    buy = f.get("side") == "buy"
    head = ("🟢 " + chu("MUA", "BUY") if buy else "🔴 " + chu("BÁN", "SELL")) + f" {label}"
    lines = [head, chu("Tài khoản ảo: {a}", "Virtual account: {a}", a=acc),
             chu("Khối lượng: {q} @ {p}", "Quantity: {q} @ {p}", q=qty(f.get("qty") or 0), p=price(f.get("fill") or 0)),
             chu("Giá trị: {v} (phí {fee})", "Value: {v} (fee {fee})", v=money(f.get("value") or 0), fee=money(f.get("fee") or 0))]
    why = _REASONS.get(str(f.get("reason") or ""))
    if why:
        lines.append(chu("Lý do: {r}", "Reason: {r}", r=chu(*why)))
    if buy and f.get("stop"):
        lines.append(chu("Cắt lỗ đặt ở: {s}", "Stop set at: {s}", s=price(f["stop"])))
    if buy and f.get("held") and f.get("reason") in ("add", "core_add"):
        lines.append(chu("Đang giữ tổng cộng: {q}", "Now holding: {q}", q=qty(f["held"])))
    if buy and f.get("held") and f.get("reason") in ("add", "core_add"):
        lines.append(chu("Đang giữ tổng cộng: {q}", "Now holding: {q}", q=qty(f["held"])))
    if not buy and f.get("pnl") is not None:
        pct = f.get("pnl_pct")
        sign = "+" if f["pnl"] > 0 else ""
        lines.append(chu("Lãi/lỗ: {v}{p}", "P&L: {v}{p}", v=sign + money(f["pnl"]),
                         p=f" ({sign}{n(pct, 2)}%)" if pct is not None else ""))
        if f.get("left"):
            lines.append(chu("Còn giữ: {q}", "Still held: {q}", q=qty(f["left"])))
    if f.get("cash") is not None:
        lines.append(chu("Tiền mặt còn: {c}", "Cash left: {c}", c=money(f["cash"])))
    return "\n".join(lines)


# ---------------------------------------------------------------- report

def report(kind: str, data: dict | None = None, live_prices: dict | None = None) -> dict:
    p = PARAMS[kind]
    unit = p["unit"]
    rows = (data or {}).get("rows") or []
    by = {(r.get("id") or r.get("symbol")): r for r in rows + ((data or {}).get("indices") or [])}
    with _LOCK:
        con = _db()
        try:
            acc = _account(con, kind)
            # the plan shown is the one in force: written defaults plus the changes the owner approved
            pe, ee = effective(kind, rules_for(kind, con))
            plan = {"kind": kind, **pe, "exposure": EXPOSURE, "entry": ee}
            if not acc:
                return {"kind": kind, "started": False, "plan": plan}
            pos = _positions(con, kind)
            trades = [dict(zip(("id", "symbol", "side", "qty", "price", "value", "fee", "at", "reason", "pnl", "pnl_pct", "analysis"), t))
                      for t in con.execute("SELECT id, symbol, side, qty, price, value, fee, at, reason, pnl, pnl_pct, analysis "
                                           "FROM paper_trades WHERE kind=? ORDER BY id DESC LIMIT 300", (kind,))]
            curve = [{"ts": t, "equity": e, "cash": c, "bench": b} for t, e, c, b in con.execute(
                "SELECT ts, equity, cash, bench FROM paper_equity WHERE kind=? ORDER BY ts", (kind,))]
            logs = [json.loads(d) for (d,) in con.execute("SELECT data FROM paper_log WHERE kind=? ORDER BY id DESC LIMIT 30", (kind,))]
            fees = con.execute("SELECT COALESCE(SUM(fee),0) FROM paper_trades WHERE kind=?", (kind,)).fetchone()[0]
        finally:
            con.close()
    for t in trades:
        try:
            t["analysis"] = json.loads(t["analysis"] or "{}")
        except Exception:
            t["analysis"] = {}
    plist = []
    invested = 0.0
    for sym, x in pos.items():
        r = by.get(sym) or {}
        price = (live_prices or {}).get(sym, [None])[0] or r.get("price") or x["avg"]
        val = x["qty"] * price * unit
        invested += val
        cost = x["qty"] * x["avg"] * unit
        plist.append({"symbol": sym, "name": x.get("name") or sym, "qty": x["qty"], "avg": x["avg"], "price": price,
                      "value": val, "pnl": val - cost, "pnl_pct": (price / x["avg"] - 1) * 100 if x["avg"] else None,
                      "stop": x["stop"], "high": x["high"], "opened": x["opened"], "sellable": x["sellable"],
                      "partial": x["partial"], "score": r.get("score"), "rsi": r.get("rsi"), "chg_1d": r.get("chg_1d"),
                      "image": r.get("image"), "symbol_label": r.get("symbol") or sym})
    equity = acc["cash"] + invested
    for x in plist:
        x["weight"] = x["value"] / equity * 100 if equity else 0
    plist.sort(key=lambda x: -x["value"])
    sells = [t for t in trades if t["side"] == "sell" and t["pnl"] is not None]
    wins = [t["pnl"] for t in sells if t["pnl"] > 0]
    losses = [t["pnl"] for t in sells if t["pnl"] <= 0]
    peak, mdd = -math.inf, 0.0
    for c in curve:
        peak = max(peak, c["equity"])
        if peak > 0:
            mdd = min(mdd, c["equity"] / peak - 1)
    bench_now = (by.get(p["bench"]) or {}).get("price")
    bench_ret = (bench_now / acc["bench_start"] - 1) * 100 if bench_now and acc.get("bench_start") else None
    stats = {
        "equity": equity, "cash": acc["cash"], "invested": invested, "start_cash": acc["start_cash"],
        "pnl": equity - acc["start_cash"], "pnl_pct": (equity / acc["start_cash"] - 1) * 100,
        "realized": sum(t["pnl"] for t in sells), "unrealized": sum(x["pnl"] for x in plist), "fees": fees,
        "exposure": invested / equity * 100 if equity else 0, "trades": len(trades), "closed": len(sells),
        "win_rate": len(wins) / len(sells) * 100 if sells else None,
        "avg_win": sum(wins) / len(wins) if wins else None, "avg_loss": sum(losses) / len(losses) if losses else None,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        "max_drawdown": mdd * 100, "bench_return": bench_ret,
        "alpha": ((equity / acc["start_cash"] - 1) * 100 - bench_ret) if bench_ret is not None else None,
        "days": (time.time() - acc["started"]) / 86400,
    }
    return {"kind": kind, "started": True, "running": acc["running"], "started_at": acc["started"], "plan": plan,
            "stats": stats, "positions": plist, "trades": trades, "curve": curve, "log": logs,
            "regime": (data or {}).get("regime")}


# ---------------------------------------------------------------- historical simulation

def load_bars(kind: str) -> dict:
    """Every archived daily bar of a market, {symbol: {"d", "o", "h", "l", "c", "v"}}, oldest first."""
    with ms._LOCK:
        con = ms._db()
        try:
            raw = {}
            for sym, d, o, h, low, c, v in con.execute(
                    "SELECT symbol, date, o, h, l, c, v FROM bars WHERE kind=? ORDER BY symbol, date", (kind,)):
                b = raw.setdefault(sym, {"d": [], "o": [], "h": [], "l": [], "c": [], "v": []})
                for k, val in (("d", d), ("o", o), ("h", h), ("l", low), ("c", c), ("v", v)):
                    b[k].append(val)
        finally:
            con.close()
    return raw


def history_rows(kind: str, days: int = 120, raw: dict | None = None) -> list:
    """What the page would have shown on each of the last `days` closes: [{"d", "ts", "rows", "regime", "opens"}].
    The slow part of a simulation (indicators per symbol per day), computed once and shared by every replay."""
    import market_indicators as mi
    p = PARAMS[kind]
    raw = raw if raw is not None else load_bars(kind)
    if not raw:
        return []
    meta = _sim_meta(kind)
    if kind == "vn":
        import market_data as md
        for s, _ in md.VN_INDICES:
            meta.setdefault(s, {"symbol": s})["group"] = "index"
    bench = p["bench"]
    dates = sorted({d for b in raw.values() for d in b["d"][-(days + 1):]})[-(days + 1):]
    idx = {s: {d: i for i, d in enumerate(b["d"])} for s, b in raw.items()}
    out = []
    for day in dates:
        ts = _dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc).timestamp() + 12 * 3600
        rows, opens = [], {}
        for sym, b in raw.items():
            i = idx[sym].get(day)
            if i is None:
                continue
            opens[sym] = b["o"][i]
            if i < 60:
                continue
            cut = {"t": list(range(i + 1)), "o": b["o"][:i + 1], "h": b["h"][:i + 1], "l": b["l"][:i + 1],
                   "c": b["c"][:i + 1], "v": b["v"][:i + 1]}
            a = mi.analyze(cut, "crypto" if kind == "crypto" else "stock")
            if not a:
                continue
            row = dict(meta.get(sym) or {"symbol": sym})
            row.update(a)
            row.setdefault("symbol", sym)
            if kind == "crypto":
                row["id"], row["deep"] = sym, True
            for k in ("spark", "reasons"):
                row.pop(k, None)
            rows.append(row)
        bench_row = next((r for r in rows if (r.get("id") or r.get("symbol")) == bench), None)
        reg = with_bench(mi.regime([r for r in universe(kind, rows)], bench_row), bench_row)
        out.append({"d": day, "ts": ts, "rows": rows, "regime": reg, "opens": opens,
                    "bench": bench_row["price"] if bench_row else None})
    return out


def simulate(kind: str, days: int = 120, rules: dict | None = None, raw: dict | None = None,
             hist: list | None = None) -> dict:
    """Replay the same rules over the archived daily bars: decide on each close, fill at the next session's open.
    Uses the bars archive (market_store), so it needs the archive to hold enough history. `rules` defaults to the
    account's approved rules; pass {} for the written defaults, or a candidate set to test a proposal. `hist`
    (from `history_rows`) lets several replays share one indicator pass."""
    p = PARAMS[kind]
    unit = p["unit"]
    if rules is None:
        rules = rules_for(kind)
    if hist is None:
        hist = history_rows(kind, days, raw)
    else:
        hist = hist[-(days + 1):]
    if len(hist) < 20:
        return {"ok": False, "error": "no_history"}
    state = {"cash": float(p["cash"]), "positions": {}, "recent_sells": {}, "started": None}
    trades, curve, pending = [], [], []
    last_entry = 0.0
    for di, h in enumerate(hist):
        day, ts, rows, reg = h["d"], h["ts"], h["rows"], h["regime"]
        state["started"] = state["started"] or ts
        # 1) fill yesterday's orders at today's open
        if pending:
            fills = [dict(o, price=h["opens"][o["symbol"]]) for o in pending if h["opens"].get(o["symbol"])]
            _sim_apply(kind, state, fills, ts, trades)
            pending = []
        # 2) decide on today's close
        prices = {(r.get("id") or r.get("symbol")): r["price"] for r in rows}
        eq = state["cash"] + sum(x["qty"] * prices.get(s, x["avg"]) * unit for s, x in state["positions"].items())
        curve.append({"d": day, "equity": eq, "bench": h["bench"], "invested": (eq - state["cash"]) / eq * 100 if eq else 0})
        if di == len(hist) - 1:
            break
        allow = ts - last_entry >= p["entry_every_h"] * 3600 - 1
        res = decide(kind, state, rows, reg, ts, allow, rules)
        if allow:
            last_entry = ts
        for o in res["orders"]:
            if o["side"] == "update":
                x = state["positions"].get(o["symbol"])
                if x:
                    x["high"], x["stop"] = o["high"], o["stop"]
            else:
                pending.append(o)
    # close the open positions at the last close for the report
    start_eq, end_eq = curve[0]["equity"], curve[-1]["equity"]
    b0 = next((c["bench"] for c in curve if c["bench"]), None)
    b1 = next((c["bench"] for c in reversed(curve) if c["bench"]), None)
    sells = [t for t in trades if t["side"] == "sell"]
    wins = [t["pnl"] for t in sells if t["pnl"] > 0]
    losses = [t["pnl"] for t in sells if t["pnl"] <= 0]
    peak, mdd = -math.inf, 0.0
    for c in curve:
        peak = max(peak, c["equity"])
        mdd = min(mdd, c["equity"] / peak - 1)
    return {"ok": True, "kind": kind, "days": len(curve), "from": curve[0]["d"], "to": curve[-1]["d"],
            "return_pct": (end_eq / start_eq - 1) * 100, "end_equity": end_eq, "start_equity": start_eq,
            "bench_return": (b1 / b0 - 1) * 100 if b0 and b1 else None, "max_drawdown": mdd * 100,
            "trades": len(trades), "closed": len(sells), "win_rate": len(wins) / len(sells) * 100 if sells else None,
            "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
            "open_positions": len(state["positions"]),
            "avg_invested": sum(c["invested"] for c in curve) / len(curve), "max_invested": max(c["invested"] for c in curve),
            "curve": [{"d": c["d"], "equity": round(c["equity"], 2), "bench": c["bench"]} for c in curve],
            "last_trades": [{k: t[k] for k in ("d", "symbol", "side", "qty", "price", "pnl", "reason")} for t in trades[-20:]]}


def _sim_meta(kind: str) -> dict:
    try:
        import market_data as md
        hit = md._SECTION.get(kind)
        rows = (hit[1].get("rows") or []) + (hit[1].get("indices") or []) if hit else []
    except Exception:
        rows = []
    out = {}
    for r in rows:
        key = r.get("id") or r.get("symbol")
        out[key] = {k: r.get(k) for k in ("symbol", "name", "sector", "group", "region", "pair", "image") if r.get(k) is not None}
    return out


def _sim_apply(kind, state, orders, ts, trades):
    p = PARAMS[kind]
    unit = p["unit"]
    for o in orders:
        sym = o["symbol"]
        if o["side"] == "buy":
            price = o["price"] * (1 + p["slip"])
            stop = min(o["stop"], price * (1 - p["stop_min"]))
            qty = o["qty"]
            cost = qty * price * unit * (1 + p["fee_buy"])
            if cost > state["cash"]:
                qty = _lot(kind, state["cash"] / (price * unit * (1 + p["fee_buy"])))
                cost = qty * price * unit * (1 + p["fee_buy"])
            if qty <= 0:
                continue
            state["cash"] -= cost
            held = state["positions"].get(sym)
            state["positions"][sym] = merge_buy(held, qty, price, o["stop"] if held else stop, ts,
                                                _add_weekdays(ts, p["t_plus"]) if p["t_plus"] else ts, o.get("plan_qty"))
            trades.append({"d": _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%d"), "symbol": sym,
                           "side": "buy", "qty": qty, "price": price, "pnl": None, "reason": o.get("reason") or "entry"})
        elif o["side"] == "sell":
            x = state["positions"].get(sym)
            if not x:
                continue
            qty = min(o["qty"], x["qty"])
            price = o["price"] * (1 - p["slip"])
            value = qty * price * unit * (1 - p["fee_sell"])
            pnl = value - qty * x["avg"] * unit * (1 + p["fee_buy"])
            state["cash"] += value
            x["qty"] -= qty
            if x["qty"] <= 1e-9:
                del state["positions"][sym]
            else:
                x["partial"] = True
            state["recent_sells"][sym] = ts
            trades.append({"d": _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%d"), "symbol": sym,
                           "side": "sell", "qty": qty, "price": price, "pnl": pnl, "reason": o["reason"]})
