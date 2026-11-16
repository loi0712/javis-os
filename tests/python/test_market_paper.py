# -*- coding: utf-8 -*-
"""Paper trading (server/market_paper.py): rules, order book, report and historical simulation.

    python tests/run.py market_paper

No network. Rows are hand-made indicator dicts; the simulation runs on synthetic bars written to the archive.
"""
from _paths import ROOT, SERVER  # noqa: E402,F401
import math
import os
import sys
import tempfile
import time

os.environ.setdefault("JAVIS_STATE_DIR", tempfile.mkdtemp(prefix="javis-paper-"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import market_paper as mp  # noqa: E402
import market_store as ms  # noqa: E402

_fails = []


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


def row(sym, price=100.0, **kw):
    r = {"symbol": sym, "price": price, "sma20": price * 0.99, "sma50": price * 0.95, "sma200": price * 0.85, "score": 50,
         "rsi": 48, "atr_pct": 2.0, "chg_1d": 0.5, "chg_3m": 10, "value": 50e6, "sector": "bank"}
    r.update(kw)
    return r


ON = {"label": "risk_on", "score": 40, "bench_up": True}
NOW = 1_800_000_000.0

# ---------------------------------------------------------------- entries
st = {"cash": 1_000_000_000, "positions": {}, "recent_sells": {}}
res = mp.decide("vn", st, [row("AAA"), row("BBB", score=-10)], ON, NOW, True)
buys = [o for o in res["orders"] if o["side"] == "buy"]
check("buys a symbol that meets the rules", [o["symbol"] for o in buys] == ["AAA"], res["log"])
b = buys[0]
check("VN quantity is a multiple of 100", b["qty"] % 100 == 0 and b["qty"] > 0)
risk = b["qty"] * (b["price"] * 1.001 - b["stop"]) * 1000
check("risk per order is at most 1% of capital", 0 < risk <= 0.0105e9, risk)
check("position is at most 15% of capital", b["qty"] * b["price"] * 1000 <= 0.15e9 * 1.001)
check("stop sits 5-12% under the entry", 0.88 <= b["stop"] / 100 <= 0.95, b["stop"])
check("the order carries its analysis", b["analysis"]["score"] == 50 and b["analysis"]["regime"] == "risk_on" and "risk_amount" in b["analysis"])

res = mp.decide("vn", st, [row("AAA", price=110, sma20=100)], ON, NOW, True)
check("no chasing: an extended price waits on the watch list",
      not [o for o in res["orders"] if o["side"] == "buy"] and res["log"]["watch"][0]["why"] == "extended" and res["log"]["watch"][0]["zone"])
res = mp.decide("vn", st, [row("AAA", chg_1d=-0.4)], ON, NOW, True)
check("pullback style waits for a green day", res["log"]["watch"] and res["log"]["watch"][0]["why"] == "falling_today")
res = mp.decide("vn", st, [row("AAA")], {"label": "risk_off"}, NOW, True)
check("no new buys when the market is risk-off", not res["orders"] and res["log"]["skipped"][0]["why"] == "regime")
res = mp.decide("vn", st, [row("AAA")], {"label": "neutral", "bench_up": False}, NOW, True)
check("no new buys when the benchmark is under its MA50", not res["orders"] and res["log"]["skipped"][0]["why"] == "bench_down")
res = mp.decide("vn", st, [row("AAA", value=1e6)], ON, NOW, True)
check("thin VN stocks are skipped", not res["orders"])
res = mp.decide("vn", st, [row("AAA")], ON, NOW, False)
check("outside the decision window nothing is bought", not res["orders"])
many = [row(f"S{i:02d}", score=60 - i) for i in range(12)]
res = mp.decide("vn", st, many, ON, NOW, True)
check("at most 8 positions", len([o for o in res["orders"] if o["side"] == "buy"]) <= 8)
res = mp.decide("vn", {"cash": 1e9, "positions": {}, "recent_sells": {"AAA": NOW - 86400}}, [row("AAA")], ON, NOW, True)
check("a symbol sold in the last 5 days is not bought back", not res["orders"])

# ---------------------------------------------------------------- exits
pos = {"qty": 1000, "avg": 100.0, "opened": NOW - 10 * 86400, "stop": 93.0, "high": 100.0, "risk0": 7.0, "partial": False, "sellable": 0}
res = mp.decide("vn", {"cash": 0, "positions": {"AAA": dict(pos)}}, [row("AAA", price=92)], ON, NOW, False)
check("stop hit sells everything", res["orders"][0]["side"] == "sell" and res["orders"][0]["reason"] == "stop" and res["orders"][0]["qty"] == 1000)
res = mp.decide("vn", {"cash": 0, "positions": {"AAA": dict(pos, high=130.0)}}, [row("AAA", price=120, atr_pct=2.0)], ON, NOW, False)
check("trailing stop under the high locks a gain", res["orders"][0]["reason"] == "trail", res["orders"])
res = mp.decide("vn", {"cash": 0, "positions": {"AAA": dict(pos)}}, [row("AAA", price=122, sma50=100)], ON, NOW, False)
check("half the position goes at +3R", res["orders"][0]["reason"] == "take_profit" and res["orders"][0]["qty"] == 500)
res = mp.decide("vn", {"cash": 0, "positions": {"AAA": dict(pos)}}, [row("AAA", price=101, score=-30)], ON, NOW, False)
check("a broken trend closes the position", res["orders"][0]["reason"] == "trend")
res = mp.decide("vn", {"cash": 0, "positions": {"AAA": dict(pos, sellable=NOW + 3600)}}, [row("AAA", price=90)], ON, NOW, False)
check("T+2: unsettled shares are not sold", not [o for o in res["orders"] if o["side"] == "sell"] and res["log"]["skipped"][0]["why"] == "t_plus")
res = mp.decide("vn", {"cash": 0, "positions": {"AAA": dict(pos)}}, [row("AAA", price=105)], ON, NOW, False)
check("a rising position moves its high up", res["orders"] and res["orders"][0]["side"] == "update" and res["orders"][0]["high"] == 105)
check("T+2 settles two weekdays later", mp._add_weekdays(1794193200, 2) - 1794193200 >= 2 * 86400 - 12 * 3600)  # a Monday

# ---------------------------------------------------------------- world core
spy = row("SPY", price=500, group="etf", sma200=450)
res = mp.decide("world", {"cash": 20000, "positions": {}, "recent_sells": {}}, [spy, row("AAA", group="stock")], {"label": "risk_off"}, NOW, True)
core = [o for o in res["orders"] if o["side"] == "buy" and o["reason"] == "core"]
check("world core buys a weekly DCA tranche of the index ETF even when stock picking is off",
      [o["symbol"] for o in core] == ["SPY"] and core[0]["qty"] == math.floor(20000 * 0.45 / 4 / 500.25), core)
held_spy = {"qty": 4, "avg": 500.0, "opened": NOW - 3 * 86400, "last_add": NOW - 3 * 86400, "stop": 441, "high": 500, "risk0": 59, "partial": False, "sellable": 0}
res = mp.decide("world", {"cash": 18000, "positions": {"SPY": held_spy}, "recent_sells": {}}, [spy], {"label": "risk_off"}, NOW, True)
check("core DCA waits a week between tranches", not [o for o in res["orders"] if o["side"] == "buy"]
      and any(s["why"] == "core_dca_wait" for s in res["log"]["skipped"]))
res = mp.decide("world", {"cash": 18000, "positions": {"SPY": dict(held_spy, last_add=NOW - 8 * 86400)}, "recent_sells": {}}, [spy], {"label": "risk_off"}, NOW, True)
top = [o for o in res["orders"] if o["side"] == "buy"]
check("core DCA tops up a week later", top and top[0]["reason"] == "core_add")
res = mp.decide("world", {"cash": 0, "positions": {"SPY": dict(pos, avg=500, stop=441)}}, [dict(spy, price=430)], ON, NOW, False)
check("core leaves when the ETF breaks its MA200", res["orders"][0]["reason"] == "core_exit")
check("crypto quantities can be fractional", mp._lot("crypto", 0.1234567) == 0.123456)

# ---------------------------------------------------------------- capital deployment
res = mp.decide("vn", {"cash": 1e9, "positions": {}, "recent_sells": {}}, [row("AAA")], ON, NOW, True, {"first_tranche": 0.5})
b = [o for o in res["orders"] if o["side"] == "buy"][0]
check("first_tranche 0.5 buys half the planned size first", b["plan_qty"] >= 2 * b["qty"] - 100 and b["qty"] > 0, b)
res = mp.decide("vn", {"cash": 1e9, "positions": {}, "recent_sells": {}}, [row("AAA")], ON, NOW, True)
check("Vietnam buys the whole size at once by default", [o for o in res["orders"] if o["side"] == "buy"][0]["qty"] == b["plan_qty"])
check("crypto starts with half the size by default", mp.effective("crypto")[1]["first_tranche"] == 0.5 and mp.effective("vn")[1]["first_tranche"] == 1.0)
check("ramp: a brand-new account may hold 30% in picks", abs(mp.ramp_cap(mp.ENTRY, NOW, NOW) - 0.3) < 1e-9)
check("ramp: four weeks later the regime cap applies", mp.ramp_cap(mp.ENTRY, NOW - 28 * 86400, NOW) == 1.0)
many = [row(f"S{i:02d}", score=60 - i) for i in range(12)]
res = mp.decide("vn", {"cash": 1e9, "positions": {}, "recent_sells": {}, "started": NOW}, many, ON, NOW, True, {"first_tranche": 1.0})
inv = sum(o["qty"] * o["price"] * 1000 for o in res["orders"] if o["side"] == "buy")
check("ramp: day one invests at most 30% of the account", 0 < inv <= 0.3e9 * 1.01, inv)
check("ramp: the rest waits with a reason", any(s["why"] == "ramp" for s in res["log"]["skipped"]))

first = {"qty": 500, "avg": 100.0, "opened": NOW - 5 * 86400, "stop": 93.0, "high": 108.0, "risk0": 7.0, "partial": False,
         "sellable": 0, "plan_qty": 1000, "adds": 0}
res = mp.decide("vn", {"cash": 5e8, "positions": {"AAA": first}, "recent_sells": {}}, [row("AAA", price=108, sma20=104)], ON, NOW, True)
add = [o for o in res["orders"] if o["side"] == "buy"]
check("scale in: +1R in an intact trend buys the rest", add and add[0]["reason"] == "add" and add[0]["qty"] == 500, res["orders"])
check("scale in: the stop moves up to the first entry", add and add[0]["stop"] == 100.0)
res = mp.decide("vn", {"cash": 5e8, "positions": {"AAA": dict(first, adds=1)}, "recent_sells": {}}, [row("AAA", price=108, sma20=104)], ON, NOW, True)
check("scale in happens once", not [o for o in res["orders"] if o["side"] == "buy"])
res = mp.decide("vn", {"cash": 5e8, "positions": {"AAA": first}, "recent_sells": {}}, [row("AAA", price=104)], ON, NOW, True)
check("no add before the position proves itself", not [o for o in res["orders"] if o["side"] == "buy"])
m = mp.merge_buy(first, 500, 108.0, 100.0, NOW, 0)
check("merge: weighted average price, stop raised, one add counted", m["qty"] == 1000 and abs(m["avg"] - 104.0) < 1e-9 and m["stop"] == 100.0 and m["adds"] == 1)

weak = {f"W{i}": {"qty": 3000, "avg": 100.0, "opened": NOW - 20 * 86400, "stop": 80.0, "high": 100.0, "risk0": 20.0, "partial": False, "sellable": 0}
        for i in range(4)}
rows_w = [row(f"W{i}", price=98, score=10 + i, sma50=95) for i in range(4)]
down = {"label": "risk_off", "bench_up": False}
res = mp.decide("vn", {"cash": 0, "positions": weak, "recent_sells": {}}, rows_w, down, NOW, True)
trim = [o for o in res["orders"] if o.get("reason") == "defend"]
check("defend: a falling market trims the weakest picks first", [o["symbol"] for o in trim] == ["W0", "W1"], res["orders"])
check("defend: the log says the account is defending", res["log"]["defending"] is True)
res = mp.decide("vn", {"cash": 0, "positions": weak, "recent_sells": {}}, rows_w, down, NOW, False)
check("defend trims only in the decision window", not [o for o in res["orders"] if o.get("reason") == "defend"])
res = mp.decide("vn", {"cash": 0, "positions": weak, "recent_sells": {}}, rows_w, down, NOW, True, {"defend": 0})
check("defend can be switched off", not [o for o in res["orders"] if o.get("reason") == "defend"])
res = mp.decide("vn", {"cash": 0, "positions": weak, "recent_sells": {}}, rows_w, {"label": "risk_off", "bench_up": True}, NOW, True)
check("defend needs the benchmark under its MA50 too", not [o for o in res["orders"] if o.get("reason") == "defend"])

# ---------------------------------------------------------------- learned rules
st0 = {"cash": 1e9, "positions": {}, "recent_sells": {}}
res = mp.decide("vn", st0, [row("AAA")], ON, NOW, True, {"avoid_sectors": ["bank"]})
check("an avoided sector is skipped", not res["orders"] and res["log"]["skipped"][0]["why"] == "sector_avoid")
res = mp.decide("vn", st0, [row("AAA", factor=-50)], ON, NOW, True, {"factor_min": -33})
check("factor_min skips buys against a macro headwind", not res["orders"] and res["log"]["skipped"][0]["why"] == "factor_headwind")
res = mp.decide("vn", st0, [row("AAA", factor=-50)], ON, NOW, True)
check("the factor filter is off by default", [o for o in res["orders"] if o["side"] == "buy"])
log = mp.set_rule("crypto", "pb_rsi_hi", 99)
check("set_rule clamps to the allowed range", mp.rules_for("crypto")["pb_rsi_hi"] == 70 and log["old"] == 55)
check("rule_view marks the change", [r for r in mp.rule_view("crypto")["rules"] if r["key"] == "pb_rsi_hi"][0]["changed"])
mp.undo_rule(log["id"])
check("undo puts the default back", "pb_rsi_hi" not in mp.rules_for("crypto"))
check("undo works once", mp.undo_rule(log["id"]) is None)
try:
    mp.set_rule("crypto", "fee_buy", 0)
    check("only TUNABLE rules can change", False)
except ValueError:
    check("only TUNABLE rules can change", True)
mp.set_rule("crypto", "avoid_sectors", ["meme", "meme", "ai"])
check("list rules are stored sorted and unique", mp.rules_for("crypto")["avoid_sectors"] == ["ai", "meme"])
mp.set_rule("crypto", "avoid_sectors", [])
check("setting a rule back to its default removes it", "avoid_sectors" not in mp.rules_for("crypto"))

# ---------------------------------------------------------------- order book, report
data = {"rows": [row("AAA"), row("BBB", score=-10)], "indices": [{"symbol": "VNINDEX", "price": 1700, "sma50": 1650}], "regime": {"label": "risk_on"}}
sent = []
mp.set_notifier(lambda kind, fills: sent.append((kind, list(fills))))
mp.start("vn", 1700)
mp.run("vn", data, now=NOW, force=True)
rep = mp.report("vn", data)
check("a committed buy reaches the notifier once", len(sent) == 1 and sent[0][0] == "vn" and [f["side"] for f in sent[0][1]] == ["buy"])
fb = sent[0][1][0]
check("the buy notice carries the fill price, fees and cash left", fb["fill"] > 100 and fb["fee"] > 0 and abs(fb["cash"] - rep["stats"]["cash"]) < 1)
msg = mp.fill_message("vn", fb)
check("the buy message names the symbol, quantity, stop and VND price", "AAA" in msg and ("MUA" in msg or "BUY" in msg)
      and "đ" in msg and ("Cắt lỗ" in msg or "Stop" in msg), msg)
check("a forced run buys and records the trade", rep["started"] and len(rep["trades"]) == 1 and rep["positions"][0]["symbol"] == "AAA")
check("cash plus positions equals the starting capital minus fees", abs(rep["stats"]["equity"] - (1e9 - rep["stats"]["fees"] - rep["positions"][0]["qty"] * 100 * 1000 * 0.001)) < 1)
check("the decision log keeps what was considered", rep["log"] and rep["log"][0]["considered"])
data2 = {"rows": [row("AAA", price=90), row("BBB", score=-10)], "indices": [{"symbol": "VNINDEX", "price": 1600, "sma50": 1650}], "regime": {"label": "neutral"}}
try:
    mp.close_position("vn", "AAA", 90, now=NOW + 60)
    check("T+2 blocks a manual sell too", False)
except ValueError:
    check("T+2 blocks a manual sell too", True)
check("a blocked manual sell sends nothing", len(sent) == 1)
mp.run("vn", data2, now=NOW + 5 * 86400, force=True)
rep = mp.report("vn", data2)
sell = [t for t in rep["trades"] if t["side"] == "sell"]
fs = sent[-1][1][0] if len(sent) == 2 else {}
check("the stop-out reaches the notifier with its P&L", fs.get("side") == "sell" and fs.get("reason") == "stop"
      and abs(fs.get("pnl", 0) - sell[0]["pnl"]) < 1e-6)
msg = mp.fill_message("vn", fs)
check("the sell message shows the loss and the reason", ("BÁN" in msg or "SELL" in msg) and ("Lãi/lỗ: -" in msg or "P&L: -" in msg)
      and ("Chạm cắt lỗ" in msg or "Stop hit" in msg), msg)
cm = mp.fill_message("crypto", {"side": "buy", "symbol": "bitcoin", "name": "Bitcoin", "qty": 0.0125, "fill": 64000.5, "value": 800,
                                "fee": 0.8, "reason": "entry", "stop": 58000, "cash": 19199.2})
check("crypto messages keep fractional quantities and dollars", ("0.0125" in cm or "0,0125" in cm) and "$" in cm, cm)
mp.set_notifier(lambda kind, fills: 1 / 0)
check("the stop sells after settlement", sell and sell[0]["reason"] == "stop" and sell[0]["pnl"] < 0)
check("the loss stays near the planned risk (a gap through the stop can add to it)", -0.02e9 < sell[0]["pnl"] < 0, sell[0]["pnl"])
check("report: win rate, drawdown and benchmark", rep["stats"]["win_rate"] == 0 and rep["stats"]["max_drawdown"] <= 0
      and round(rep["stats"]["bench_return"], 2) == round((1600 / 1700 - 1) * 100, 2))
check("the sell keeps its R multiple and holding time", sell[0]["analysis"]["r_multiple"] < 0 and sell[0]["analysis"]["held_days"] >= 5)
mp.set_notifier(None)
mp.set_running("vn", False)
check("a paused account does not trade", mp.run("vn", data, now=NOW + 6 * 86400, force=True) == {})
mp.reset("vn")
check("reset clears the account", not mp.report("vn", data)["started"])

# ---------------------------------------------------------------- simulation on synthetic history
DAY = 86400
T0 = 1_760_000_000 // DAY * DAY + 7200


def series(n, drift, wave):
    c = [100 * math.exp(drift * i) * (1 + wave * math.sin(i / 6)) for i in range(n)]
    return {"t": [T0 + i * DAY for i in range(n)], "o": [x * 0.999 for x in c], "h": [x * 1.01 for x in c],
            "l": [x * 0.99 for x in c], "c": c, "v": [1e6] * n}


ms.upsert_bars("vn", {"UPA": series(320, 0.002, 0.04), "UPB": series(320, 0.0015, 0.05), "DNC": series(320, -0.002, 0.03),
                      "VNINDEX": series(320, 0.001, 0.01)})
sim = mp.simulate("vn", 120)
check("simulation runs on the archive", sim["ok"] and sim["days"] == 121, sim.get("error"))
check("simulation trades and reports against the benchmark", sim["trades"] > 0 and sim["bench_return"] is not None)
check("simulation never trades the index itself", all(t["symbol"] != "VNINDEX" for t in sim["last_trades"]))
check("simulation curve starts at the capital", abs(sim["curve"][0]["equity"] - 1e9) < 1)
check("simulation reports exposure", 0 <= sim["avg_invested"] <= sim["max_invested"] <= 100)
hist = mp.history_rows("vn", 120)
again = mp.simulate("vn", 120, hist=hist)
check("a replay on shared history rows gives the same result", abs(again["return_pct"] - sim["return_pct"]) < 1e-9)
alt = mp.simulate("vn", 120, rules={"first_tranche": 1.0, "ramp_weeks": 0}, hist=hist)
check("a replay can test another rule set", alt["ok"] and alt["trades"] >= 1)

# ---------------------------------------------------------------- drivers and the weekly review
import market_factors as mf  # noqa: E402
import market_learn as ml  # noqa: E402
check("VN rows group by sector", mf.group_of("vn", {"sector": "bank"}) == "bank")
check("world stocks and coins have groups", mf.group_of("world", {"symbol": "NVDA"}) == "semis" and mf.group_of("crypto", {"id": "dogecoin"}) == "meme")
check("every driver of every group is a known series", all(d in mf.DRIVERS for k in mf.GROUPS for g in mf.GROUPS[k].values() for d, *_ in g[0]))
ds = [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(80)]
up = {"d": ds, "c": [100 + i for i in range(80)]}
raw_k = {"world": {"^TNX": up}, "vn": {"VNINDEX": up}}
check("factor score: a rising yield is a headwind for real estate, the rising market a tailwind",
      mf.score_at("vn", "realestate", ds[-1], {"world": {"^TNX": up}}) == -100 and mf.score_at("vn", "other", ds[-1], raw_k) == 100)
check("factor score uses only data up to the day", mf.score_at("vn", "other", ds[10], raw_k) is None or mf.score_at("vn", "other", ds[10], raw_k) == 0)
x = [0.01 * ((i * 7) % 5 - 2) for i in range(40)]
c, beta = mf._corr_beta([2 * v for v in x], x)
check("correlation and beta", abs(c - 1) < 1e-9 and abs(beta - 2) < 1e-9)
fac = mf.analyze("vn", {"vn": mp.load_bars("vn"), "world": {}, "crypto": {}})
check("sector analysis runs on the archive", fac.get("ok") and fac["groups"], fac)
rv = ml.run_review("vn", notify=False, days=120)
check("the weekly review runs and stores a study", rv.get("ok") and rv["study"]["n"] > 0, rv.get("error"))
lt = ml.latest("vn")
check("the page sees the last review", lt["review"] and lt["review"]["samples"] == rv["study"]["n"] and isinstance(lt["insights"], list))
check("the report names the account", "VN" in ml.report_text("vn", rv) or "Vietnam" in ml.report_text("vn", rv))
fake = {"n": 400, "mid": "2026-02-01", "pullback": True, "_aligned":
        [{"d": "2026-01-15" if i % 2 else "2026-02-15", "rsi": 40 + (i % 20), "fwd": (1.0 if 40 + (i % 20) <= 50 else -1.0)} for i in range(400)],
        "stops": {}, "aligned": {"h1": {"mean": 0}, "h2": {"mean": 0}}, "groups": {}, "factor": {}}
cs = ml.candidates("vn", fake, {})
rsi_c = [c for c in cs if c["kind_of"] == "rsi"]
check("a band that loses in both halves is proposed one step narrower", rsi_c and rsi_c[0]["key"] == "pb_rsi_hi" and rsi_c[0]["new"] == 50, cs)
noisy = dict(fake, _aligned=[dict(s, fwd=(s["fwd"] if s["d"] < "2026-02-01" else -s["fwd"])) for s in fake["_aligned"]])
check("a pattern seen in one half only is not proposed", not [c for c in ml.candidates("vn", noisy, {}) if c["kind_of"] == "rsi"])
con = ml._db()
pid = con.execute("INSERT INTO paper_proposals (kind, review, created, status, key, old, new, data) "
                  "VALUES ('vn', 0, 0, 'pending', 'pb_rsi_hi', '55', '50', '{}')").lastrowid
con.commit()
con.close()
r = ml.decide_proposal(pid, "apply")
check("applying a proposal changes the rule and logs it", r["ok"] and mp.rules_for("vn").get("pb_rsi_hi") == 50 and r["log"]["new"] == 50)
check("a decided proposal cannot be applied twice", not ml.decide_proposal(pid, "apply")["ok"])
mp.undo_rule(r["log"]["id"])
check("an empty archive cannot be simulated", not mp.simulate("crypto", 120)["ok"])

if _fails:
    print(f"\n{len(_fails)} FAIL")
    sys.exit(1)
print("\nall ok")
