"""market_learn.py - the paper accounts' learning loop, level 1: study the market, review the trades, propose.

Owner, 2026-11-16: level 1 first ("Javis tự đánh giá định kỳ rồi đề xuất"), and in parallel "tìm hiểu thêm thị trường
hoạt động trong thời gian để tìm ra quy luật ... dần dần thay thế các luật viết sẵn thành luật phù hợp hơn với thực tế".

Once a week per market (and on demand from the page):
  1. Market study, on every symbol and every day of the archive (up to 250 sessions), not only the few trades the
     account made: what happened over the next 10 sessions after each setup the rules look at (RSI band, distance
     from the 20-day average, falling day, sector or category, macro factor score, market regime), and which stop
     width (1.5 to 3 ATR) would have kept the most.
  2. Trade review: the account's closed trades by exit reason, entry RSI and group.
  3. Proposals: a written rule moves one bounded step (market_paper.TUNABLE) only when the change helps in BOTH
     halves of the period (a pattern seen in one half only is most likely noise), and a replay of the account's
     rules with the change (market_paper.simulate) is not worse. Nothing changes until the owner approves; every
     approved change is logged and can be undone (market_paper.set_rule / undo_rule).
  4. A short report to the owner's Telegram, and the full study on the Paper trading tab.

What it is not: no rule is invented and nothing is applied on its own. The data is in-sample (the replay uses the
same history the study saw), which is why the halves test and the owner's approval stay in the loop.
Blocking work (sqlite, indicators): run in a thread.
"""
from __future__ import annotations

import bisect
import datetime as _dt
import json
import threading
import time

import market_factors as mf
import market_paper as mp

H = 10                      # sessions ahead a setup is judged on
MIN_HALF = 30               # samples needed in each half before a bucket counts
EVERY_DAYS = 7
_LOCK = threading.Lock()
_RUNNING: set = set()
_NOTIFIER = None

RSI_EDGES = [0, 35, 40, 45, 50, 55, 60, 65, 70, 75, 101]
STOP_KS = [1.5, 2.0, 2.5, 3.0]
RULE_NAMES = {
    "score": ("Điểm xu hướng tối thiểu để mua", "Lowest trend score to buy"),
    "pb_score": ("Điểm xu hướng tối thiểu (mua nhịp chỉnh)", "Lowest trend score (pullback buy)"),
    "cooldown_days": ("Số ngày chờ trước khi mua lại mã vừa bán", "Days before buying back a symbol just sold"),
    "take_profit_r": ("Chốt lời một nửa ở mức (lần R)", "Take half the profit at (x R)"),
    "trail_atr": ("Cắt lỗ đuổi (lần ATR)", "Trailing stop (x ATR)"),
    "first_tranche": ("Tỷ lệ mua đợt đầu", "Share bought in the first tranche"),
    "ramp_weeks": ("Số tuần giải ngân dần khi mới mở tài khoản", "Weeks to deploy cash in a new account"),
    "defend": ("Phòng thủ khi thị trường giảm (1 bật, 0 tắt)", "Defend in a falling market (1 on, 0 off)"),
    "pb_rsi_lo": ("RSI thấp nhất khi mua nhịp chỉnh", "Lowest RSI for a pullback buy"),
    "pb_rsi_hi": ("RSI cao nhất khi mua nhịp chỉnh", "Highest RSI for a pullback buy"),
    "rsi_lo": ("RSI thấp nhất khi mua", "Lowest RSI to buy"), "rsi_hi": ("RSI cao nhất khi mua", "Highest RSI to buy"),
    "stop_atr": ("Khoảng cắt lỗ (lần ATR)", "Stop distance (x ATR)"),
    "avoid_sectors": ("Nhóm ngành tạm tránh", "Groups to avoid"),
    "factor_weight": ("Trọng số yếu tố vĩ mô khi xếp hạng", "Weight of the macro factor in ranking"),
    "factor_min": ("Bỏ qua lệnh mua khi vĩ mô ngược chiều (điểm thấp nhất)", "Skip buys against a macro headwind (lowest score)"),
}


def set_notifier(fn) -> None:
    global _NOTIFIER
    _NOTIFIER = fn


def _db():
    con = mp._db()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS paper_reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, at REAL, data TEXT);
        CREATE TABLE IF NOT EXISTS paper_proposals (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, review INTEGER,
            created REAL, status TEXT, key TEXT, old TEXT, new TEXT, data TEXT, decided REAL);
    """)
    return con


# ---------------------------------------------------------------- 1. market study

def _bucket(v, edges):
    if v is None:
        return None
    i = bisect.bisect_right(edges, v) - 1
    return max(0, min(i, len(edges) - 2))


def _stats(xs: list) -> dict:
    n = len(xs)
    if not n:
        return {"n": 0, "mean": None, "win": None}
    return {"n": n, "mean": round(sum(xs) / n, 3), "win": round(sum(1 for x in xs if x > 0) / n * 100, 1)}


def _split(samples: list, key, mid: str) -> dict:
    """{bucket: {"all", "h1", "h2"}} stats of forward returns, with the two halves of the period apart."""
    acc: dict = {}
    for s in samples:
        b = key(s)
        if b is None:
            continue
        a = acc.setdefault(b, ([], [], []))
        a[0].append(s["fwd"])
        (a[1] if s["d"] < mid else a[2]).append(s["fwd"])
    return {b: {"all": _stats(a[0]), "h1": _stats(a[1]), "h2": _stats(a[2])} for b, a in acc.items()}


def samples(kind: str, hist: list, raw: dict, raw_by_kind: dict) -> list:
    """One sample per symbol per day (except the last H days): the setup and what followed."""
    p, e = mp.effective(kind)
    idx = {s: {d: i for i, d in enumerate(b["d"])} for s, b in raw.items()}
    fscore: dict = {}
    out = []
    for h in hist[:-H] if len(hist) > H else []:
        day, label = h["d"], (h["regime"] or {}).get("label") or "unknown"
        for r in mp.universe(kind, h["rows"]):
            sym = r.get("id") or r.get("symbol")
            if r.get("group") == "etf" or sym == p["bench"]:
                continue
            b, i = raw.get(sym), idx.get(sym, {}).get(day)
            if b is None or i is None or i + H >= len(b["c"]) or not b["c"][i]:
                continue
            price, s20, s50, s200, atr = r["price"], r.get("sma20"), r.get("sma50"), r.get("sma200"), r.get("atr_pct")
            g = mf.group_of(kind, r)
            if (g, day) not in fscore:
                fscore[(g, day)] = mf.score_at(kind, g, day, raw_by_kind)
            # a simple trade from the next open, judged after H sessions, for each stop width
            entry = b["o"][i + 1] or b["c"][i]
            stops = {}
            for k in STOP_KS:
                dist = min(max((atr or 0) / 100 * k, p["stop_min"]), p["stop_max"])
                stop = entry * (1 - dist)
                ret = None
                for j in range(i + 1, i + H + 1):
                    if b["l"][j] is not None and b["l"][j] <= stop:
                        ret = (stop / entry - 1) * 100
                        break
                stops[k] = (b["c"][i + H] / entry - 1) * 100 if ret is None else ret
            out.append({
                "d": day, "sym": sym, "group": g, "fwd": (b["c"][i + H] / b["c"][i] - 1) * 100, "regime": label,
                "bench_up": (h["regime"] or {}).get("bench_up"), "rsi": r.get("rsi"), "score": r.get("score") or 0,
                "chg_1d": r.get("chg_1d") or 0, "factor": fscore[(g, day)],
                "pb": bool(s50 and s200 and price > s50 > s200),
                "mom": bool(s20 and s50 and s200 and price > s20 > s50 and price > s200),
                "zone": bool(s20 and 0.97 * s20 <= price <= 1.02 * s20),
                "stops": stops,
            })
    return out


def study(kind: str, smp: list, rules: dict) -> dict:
    """Tables of what followed each setup, over the whole market (see the module docstring)."""
    p, e = mp.effective(kind, rules)
    pullback = (e.get("style") or mp.STYLE.get(kind)) == "pullback"
    if not smp:
        return {"n": 0}
    days = sorted({s["d"] for s in smp})
    mid = days[len(days) // 2]
    aligned = [s for s in smp if (s["pb"] if pullback else s["mom"])]
    lo, hi = (e["pb_rsi_lo"], e["pb_rsi_hi"]) if pullback else (e["rsi_lo"], e["rsi_hi"])
    entry_like = [s for s in aligned if s["rsi"] is not None and lo <= s["rsi"] <= hi and (s["zone"] or not pullback)]
    stop_pop = entry_like if len(entry_like) >= 2 * MIN_HALF else aligned
    stop_rows = {}
    for k in STOP_KS:
        xs = [{"d": s["d"], "fwd": s["stops"][k]} for s in stop_pop]
        stop_rows[k] = _split(xs, lambda s: "x", mid).get("x")
    return {
        "n": len(smp), "days": len(days), "from": days[0], "to": days[-1], "mid": mid, "horizon": H,
        "pullback": pullback, "band": [lo, hi],
        "base": _split(smp, lambda s: "x", mid).get("x"),
        "aligned": _split(aligned, lambda s: "x", mid).get("x"),
        "rsi": {f"{RSI_EDGES[b]}-{RSI_EDGES[b + 1] if RSI_EDGES[b + 1] <= 100 else 100}": v
                for b, v in sorted(_split(aligned, lambda s: _bucket(s["rsi"], RSI_EDGES), mid).items())},
        "groups": _split(aligned, lambda s: s["group"], mid),
        "regime": _split(smp, lambda s: s["regime"], mid),
        "factor": _split(aligned, lambda s: None if s["factor"] is None else ("tail" if s["factor"] > 33 else
                                                                            "head" if s["factor"] < -33 else "mixed"), mid),
        "falling": _split([s for s in aligned if s["zone"]], lambda s: "down" if s["chg_1d"] < 0 else "up", mid),
        "stops": {str(k): v for k, v in stop_rows.items()}, "stop_pop": len(stop_pop),
        "_aligned": aligned,
    }


# ---------------------------------------------------------------- 2. trade review

def journal(kind: str) -> dict:
    """The account's closed trades, matched to the buy that opened them."""
    with mp._LOCK:
        con = mp._db()
        try:
            rows = con.execute("SELECT symbol, side, at, reason, pnl, pnl_pct, analysis FROM paper_trades WHERE kind=? ORDER BY id",
                               (kind,)).fetchall()
        finally:
            con.close()
    last_buy, closed = {}, []
    for sym, side, at, reason, pnl, pnl_pct, an in rows:
        a = json.loads(an or "{}")
        if side == "buy" and reason in ("entry", "core"):
            last_buy[sym] = a
        elif side == "sell" and pnl is not None:
            b = last_buy.get(sym) or {}
            closed.append({"symbol": sym, "reason": reason, "pnl": pnl, "pnl_pct": pnl_pct, "r": a.get("r_multiple"),
                           "rsi": b.get("rsi"), "score": b.get("score"), "regime": b.get("regime"),
                           "group": mf.group_of(kind, {"symbol": sym, "id": sym, "sector": _vn_sector(kind, sym)})})

    def agg(key):
        out = {}
        for t in closed:
            k = key(t)
            if k is None:
                continue
            o = out.setdefault(k, {"n": 0, "wins": 0, "pnl": 0.0, "r": []})
            o["n"] += 1
            o["wins"] += t["pnl"] > 0
            o["pnl"] += t["pnl"]
            if t["r"] is not None:
                o["r"].append(t["r"])
        for o in out.values():
            o["win"] = round(o["wins"] / o["n"] * 100, 1)
            o["avg_r"] = round(sum(o["r"]) / len(o["r"]), 2) if o["r"] else None
            del o["r"], o["wins"]
        return out
    return {"closed": len(closed), "all": agg(lambda t: "x").get("x"), "by_reason": agg(lambda t: t["reason"]),
            "by_rsi": agg(lambda t: None if t["rsi"] is None else f"{RSI_EDGES[_bucket(t['rsi'], RSI_EDGES)]}+"),
            "by_group": agg(lambda t: t["group"])}


def _vn_sector(kind, sym):
    if kind != "vn":
        return None
    import market_data as md
    return (md.VN_STOCKS.get(sym) or ("other", ""))[0]


# ---------------------------------------------------------------- 3. proposals

def _mean_where(pop, cond, mid):
    h1 = [s["fwd"] for s in pop if s["d"] < mid and cond(s)]
    h2 = [s["fwd"] for s in pop if s["d"] >= mid and cond(s)]
    if len(h1) < MIN_HALF or len(h2) < MIN_HALF:
        return None
    return sum(h1) / len(h1), sum(h2) / len(h2), len(h1) + len(h2)


def candidates(kind: str, st: dict, rules: dict) -> list:
    """Bounded one-step changes the study supports in both halves. Not yet checked by a replay."""
    if not st.get("n"):
        return []
    p, e = mp.effective(kind, rules)
    out = []
    mid, pop = st["mid"], st["_aligned"]
    # RSI band of the entry rule
    lo_k, hi_k = ("pb_rsi_lo", "pb_rsi_hi") if st["pullback"] else ("rsi_lo", "rsi_hi")
    lo, hi = e[lo_k], e[hi_k]

    def band(a, b):
        return _mean_where(pop, lambda s: s["rsi"] is not None and a <= s["rsi"] <= b, mid)
    cur = band(lo, hi)
    if cur:
        best = None
        for key, val in ((lo_k, lo + 5), (lo_k, lo - 5), (hi_k, hi + 5), (hi_k, hi - 5)):
            val = mp.clamp_rule(kind, key, val)
            a, b = (val, hi) if key == lo_k else (lo, val)
            if a >= b - 5 or val == e[key]:
                continue
            m = band(a, b)
            if not m:
                continue
            gain = min(m[0] - cur[0], m[1] - cur[1])
            if gain >= 0.2 and (best is None or gain > best[0]):
                best = (gain, key, val, m)
        if best:
            gain, key, val, m = best
            out.append({"key": key, "old": e[key], "new": val, "kind_of": "rsi",
                        "evidence": {"band_old": [lo, hi], "band_new": [val, hi] if key == lo_k else [lo, val],
                                     "old_h": [round(cur[0], 2), round(cur[1], 2)], "new_h": [round(m[0], 2), round(m[1], 2)],
                                     "n": m[2]}})
    # stop width
    sr = st["stops"]
    k0 = p["stop_atr"]
    kc = min(STOP_KS, key=lambda k: abs(k - k0))
    best = None
    for k in STOP_KS:
        if abs(k - kc) != 0.5 or not sr.get(str(k)) or not sr.get(str(kc)):
            continue
        a, c = sr[str(k)], sr[str(kc)]
        if min(a["h1"]["n"] or 0, a["h2"]["n"] or 0) < MIN_HALF:
            continue
        gain = min(a["h1"]["mean"] - c["h1"]["mean"], a["h2"]["mean"] - c["h2"]["mean"])
        if gain >= 0.15 and (best is None or gain > best[0]):
            best = (gain, k, a, c)
    if best:
        gain, k, a, c = best
        out.append({"key": "stop_atr", "old": k0, "new": k, "kind_of": "stop",
                    "evidence": {"old_h": [c["h1"]["mean"], c["h2"]["mean"]], "new_h": [a["h1"]["mean"], a["h2"]["mean"]],
                                 "n": a["all"]["n"]}})
    # groups to avoid (or to stop avoiding)
    base = st["aligned"]
    avoid = list(e.get("avoid_sectors") or [])
    add, drop = [], []
    for g, v in st["groups"].items():
        if min(v["h1"]["n"], v["h2"]["n"]) < MIN_HALF // 2 + 5:
            continue
        d1, d2 = v["h1"]["mean"] - base["h1"]["mean"], v["h2"]["mean"] - base["h2"]["mean"]
        if g not in avoid and d1 <= -1.0 and d2 <= -1.0:
            add.append((min(d1, d2), g, v))
        elif g in avoid and d1 >= 0 and d2 >= 0:
            drop.append(g)
    add.sort()
    if add or drop:
        new = sorted((set(avoid) | {g for _, g, _ in add[:2]}) - set(drop))
        out.append({"key": "avoid_sectors", "old": avoid, "new": new, "kind_of": "groups",
                    "evidence": {"add": [{"group": g, "h": [v["h1"]["mean"], v["h2"]["mean"]], "n": v["all"]["n"]} for _, g, v in add[:2]],
                                 "drop": drop, "base_h": [base["h1"]["mean"], base["h2"]["mean"]]}})
    # the macro factor in ranking
    f = st["factor"]
    if f.get("tail") and f.get("head") and min(f["tail"]["h1"]["n"], f["tail"]["h2"]["n"], f["head"]["h1"]["n"], f["head"]["h2"]["n"]) >= MIN_HALF // 2 + 5:
        d1 = f["tail"]["h1"]["mean"] - f["head"]["h1"]["mean"]
        d2 = f["tail"]["h2"]["mean"] - f["head"]["h2"]["mean"]
        w = e.get("factor_weight") or 0.0
        new = None
        if d1 >= 1.0 and d2 >= 1.0 and w < 0.5:
            new = round(min(w + 0.2, 0.5), 2)
        elif d1 <= 0 and d2 <= 0 and w > 0:
            new = round(max(w - 0.2, 0.0), 2)
        if new is not None:
            out.append({"key": "factor_weight", "old": w, "new": new, "kind_of": "factor",
                        "evidence": {"spread_h": [round(d1, 2), round(d2, 2)]}})
        # and as a filter: skip buys against a macro headwind when that setup loses in both halves
        fm = e.get("factor_min", -100)
        h1 = f["head"]["h1"]["mean"] - base["h1"]["mean"]
        h2 = f["head"]["h2"]["mean"] - base["h2"]["mean"]
        if fm <= -100 and h1 <= -1.0 and h2 <= -1.0:
            out.append({"key": "factor_min", "old": fm, "new": -33, "kind_of": "factor_filter",
                        "evidence": {"head_vs_base_h": [round(h1, 2), round(h2, 2)]}})
        elif fm > -100 and h1 >= 0 and h2 >= 0:
            out.append({"key": "factor_min", "old": fm, "new": -100, "kind_of": "factor_filter",
                        "evidence": {"head_vs_base_h": [round(h1, 2), round(h2, 2)]}})
    return out


def _with_factor(kind: str, hist: list, raw_by_kind: dict) -> list:
    """History rows with each symbol's factor score of that day, for replays that rank by it."""
    cache, out = {}, []
    for h in hist:
        rows = []
        for r in h["rows"]:
            g = mf.group_of(kind, r)
            if (g, h["d"]) not in cache:
                cache[(g, h["d"])] = mf.score_at(kind, g, h["d"], raw_by_kind)
            rows.append(dict(r, factor=cache[(g, h["d"])] or 0))
        out.append(dict(h, rows=rows))
    return out


def _sim_brief(r: dict) -> dict:
    return {k: (round(r[k], 2) if isinstance(r.get(k), float) else r.get(k))
            for k in ("return_pct", "max_drawdown", "trades", "win_rate", "bench_return", "avg_invested")} if r.get("ok") else {}


def validate(kind: str, cands: list, rules: dict, hist: list) -> tuple[dict, list]:
    """Replay the account with each candidate change; keep those that are not worse. Returns (current, kept)."""
    days = len(hist) - 1
    base = _sim_brief(mp.simulate(kind, days, rules=rules, hist=hist))
    kept = []
    for c in cands:
        new = _sim_brief(mp.simulate(kind, days, rules=dict(rules, **{c["key"]: c["new"]}), hist=hist))
        c["sim"] = {"old": base, "new": new}
        if not base or not new:
            continue
        better_ret = new["return_pct"] - base["return_pct"]
        better_dd = new["max_drawdown"] - base["max_drawdown"]     # drawdowns are negative: higher is better
        if better_ret >= -0.5 and (better_ret >= 0.3 or better_dd >= 0.5):
            kept.append(c)
    return base, kept


# ---------------------------------------------------------------- 4. the review, stored and reported

def run_review(kind: str, notify: bool = True, days: int = 250) -> dict:
    """The whole weekly pass for one market. Pending proposals of an earlier review are superseded."""
    with _LOCK:
        if kind in _RUNNING:
            return {"ok": False, "error": "running"}
        _RUNNING.add(kind)
    try:
        raw_by_kind = mf.load_all()
        raw = raw_by_kind.get(kind) or {}
        hist = mp.history_rows(kind, days, raw)
        if len(hist) < 60:
            return {"ok": False, "error": "no_history", "days": len(hist)}
        rules = mp.rules_for(kind)
        hist_f = _with_factor(kind, hist, raw_by_kind)
        smp = samples(kind, hist, raw, raw_by_kind)
        st = study(kind, smp, rules)
        jr = journal(kind)
        cands = candidates(kind, st, rules)
        base, kept = validate(kind, cands, rules, hist_f)
        st.pop("_aligned", None)
        data = {"study": st, "journal": jr, "sim": base, "candidates": len(cands), "rejected": [
            {"key": c["key"], "new": c["new"], "sim": c.get("sim")} for c in cands if c not in kept]}
        with mp._LOCK:
            con = _db()
            try:
                now = time.time()
                rid = con.execute("INSERT INTO paper_reviews (kind, at, data) VALUES (?,?,?)", (kind, now, json.dumps(data))).lastrowid
                con.execute("DELETE FROM paper_reviews WHERE kind=? AND id NOT IN (SELECT id FROM paper_reviews WHERE kind=? ORDER BY id DESC LIMIT 12)",
                            (kind, kind))
                con.execute("UPDATE paper_proposals SET status='expired', decided=? WHERE kind=? AND status='pending'", (now, kind))
                for c in kept:
                    c["id"] = con.execute(
                        "INSERT INTO paper_proposals (kind, review, created, status, key, old, new, data) VALUES (?,?,?,?,?,?,?,?)",
                        (kind, rid, now, "pending", c["key"], json.dumps(c["old"]), json.dumps(c["new"]),
                         json.dumps({"evidence": c["evidence"], "sim": c["sim"], "kind_of": c["kind_of"]}))).lastrowid
                con.commit()
            finally:
                con.close()
        import market_store as ms
        ms.meta_set("learn_" + kind, str(time.time()))
        res = {"ok": True, "kind": kind, "review": rid, "proposals": kept, **data}
        if notify and _NOTIFIER and _started(kind):
            try:
                _NOTIFIER(kind, report_text(kind, res))
            except Exception as ex:  # noqa: BLE001 - the review is stored either way
                import sys
                print(f"[learn] notify {kind}: {type(ex).__name__}: {ex}", file=sys.stderr)
        return res
    finally:
        with _LOCK:
            _RUNNING.discard(kind)


def _started(kind: str) -> bool:
    with mp._LOCK:
        con = mp._db()
        try:
            return mp._account(con, kind) is not None
        finally:
            con.close()


def due(kind: str, now: float | None = None) -> bool:
    import market_store as ms
    now = now or time.time()
    try:
        last = float(ms.meta_get("learn_" + kind) or 0)
    except ValueError:
        last = 0.0
    try:
        tried = float(ms.meta_get("learn_try_" + kind) or 0)
    except ValueError:
        tried = 0.0
    # weekly; after a failed try (too little history yet) wait a day before the next attempt
    return now - last >= EVERY_DAYS * 86400 and now - tried >= 86400


def mark_try(kind: str) -> None:
    import market_store as ms
    ms.meta_set("learn_try_" + kind, str(time.time()))


def running(kind: str) -> bool:
    return kind in _RUNNING


def proposals(kind: str, status: str | None = None) -> list:
    with mp._LOCK:
        con = _db()
        try:
            q = "SELECT id, review, created, status, key, old, new, data, decided FROM paper_proposals WHERE kind=?"
            args = [kind]
            if status:
                q += " AND status=?"
                args.append(status)
            rows = con.execute(q + " ORDER BY id DESC LIMIT 40", args).fetchall()
        finally:
            con.close()
    out = []
    for i, rv, cr, stt, key, old, new, data, dec in rows:
        d = json.loads(data or "{}")
        out.append({"id": i, "review": rv, "created": cr, "status": stt, "key": key, "old": json.loads(old),
                    "new": json.loads(new), "decided": dec, **d})
    return out


def decide_proposal(pid: int, action: str) -> dict:
    """apply / reject a pending proposal. Applying writes the rule through market_paper.set_rule (undoable)."""
    with mp._LOCK:
        con = _db()
        try:
            row = con.execute("SELECT kind, status, key, new FROM paper_proposals WHERE id=?", (pid,)).fetchone()
        finally:
            con.close()
    if not row:
        return {"ok": False, "error": "not_found"}
    kind, status, key, new = row
    if status != "pending":
        return {"ok": False, "error": "not_pending", "status": status}
    log = None
    if action == "apply":
        log = mp.set_rule(kind, key, json.loads(new), source="review", proposal=pid)
    elif action != "reject":
        return {"ok": False, "error": "action"}
    with mp._LOCK:
        con = _db()
        try:
            con.execute("UPDATE paper_proposals SET status=?, decided=? WHERE id=?",
                        ("applied" if action == "apply" else "rejected", time.time(), pid))
            con.commit()
        finally:
            con.close()
    return {"ok": True, "kind": kind, "log": log}


def latest(kind: str) -> dict:
    """The last review of a market, its proposals and the rules in force, for the page."""
    with mp._LOCK:
        con = _db()
        try:
            row = con.execute("SELECT id, at, data FROM paper_reviews WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
        finally:
            con.close()
    rev = {"id": row[0], "at": row[1], **json.loads(row[2])} if row else None
    import localefmt
    rules = mp.rule_view(kind)
    for r in rules["rules"] + rules["log"]:
        r["name"] = localefmt.chu(*RULE_NAMES.get(r["key"], (r["key"], r["key"])))
    for r in rules["rules"]:
        if r["key"] == "avoid_sectors":
            r["value_text"] = ", ".join(_gname(kind, g) for g in r["value"])
    pending = proposals(kind, "pending")
    for c in pending:
        c["text"] = proposal_text(kind, c)
        c["name"] = localefmt.chu(*RULE_NAMES.get(c["key"], (c["key"], c["key"])))
    history = [p for p in proposals(kind) if p["status"] != "pending"][:10]
    for c in history:
        c["text"] = proposal_text(kind, c)
    return {"ok": True, "kind": kind, "review": _brief(kind, rev), "running": running(kind), "due": due(kind),
            "pending": pending, "history": history, "rules": rules, "insights": insights(kind, rev) if rev else []}


def _brief(kind: str, rev: dict | None) -> dict | None:
    """What the page shows of a review: when, how much data, the replay, how many candidates were turned down."""
    if not rev:
        return None
    st = rev.get("study") or {}
    return {"id": rev["id"], "at": rev["at"], "days": st.get("days"), "from": st.get("from"), "to": st.get("to"),
            "samples": st.get("n"), "horizon": st.get("horizon"), "sim": rev.get("sim"), "candidates": rev.get("candidates"),
            "rejected": rev.get("rejected"), "closed": (rev.get("journal") or {}).get("closed"),
            "groups": [{"key": g, "name": _gname(kind, g), **v["all"]}
                       for g, v in sorted((st.get("groups") or {}).items(), key=lambda gv: -(gv[1]["all"]["mean"] or 0))]}


# ---------------------------------------------------------------- words

def _f(v, nd=2, sign=True):
    import localefmt
    if v is None:
        return "-"
    t = f"{v:+.{nd}f}" if sign else f"{v:.{nd}f}"
    return t.replace(".", ",") if localefmt.chu("vi", "en") == "vi" else t


def _gname(kind, g):
    import localefmt
    return localefmt.chu(*GROUP_NAMES.get(g, (g, g)))


GROUP_NAMES = {
    "bank": ("Ngân hàng", "Banks"), "broker": ("Chứng khoán", "Brokers"), "realestate": ("Bất động sản", "Real estate"),
    "indpark": ("Khu công nghiệp", "Industrial parks"), "materials": ("Thép, vật liệu", "Steel and materials"),
    "energy": ("Năng lượng", "Energy"), "consumer": ("Tiêu dùng, bán lẻ", "Consumer and retail"),
    "tech": ("Công nghệ", "Technology"), "transport": ("Vận tải, cảng", "Transport and ports"),
    "construction": ("Xây dựng", "Construction"), "agri": ("Nông, thủy sản", "Agriculture and seafood"),
    "insurance": ("Bảo hiểm", "Insurance"), "other": ("Khác", "Other"),
    "semis": ("Bán dẫn", "Semiconductors"), "bigtech": ("Công nghệ lớn", "Big tech"), "financials": ("Tài chính", "Financials"),
    "health": ("Y tế", "Health care"), "crypto_stock": ("Cổ phiếu tiền số", "Crypto stocks"),
    "china": ("Trung Quốc", "China"), "japan": ("Nhật Bản", "Japan"), "index_etf": ("Quỹ chỉ số", "Index funds"),
    "store_of_value": ("Lưu trữ giá trị (Bitcoin)", "Store of value (Bitcoin)"), "smart_contract": ("Nền tảng hợp đồng thông minh (L1)", "Smart-contract platforms (L1)"),
    "layer2": ("Lớp 2 (L2)", "Layer 2"), "defi": ("Tài chính phi tập trung (DeFi)", "DeFi"),
    "infra": ("Hạ tầng, oracle", "Infrastructure and oracles"), "ai": ("Trí tuệ nhân tạo (AI)", "AI"),
    "exchange": ("Token sàn", "Exchange tokens"), "meme": ("Meme", "Meme"), "payments": ("Thanh toán", "Payments"),
    "privacy": ("Riêng tư", "Privacy"), "rwa": ("Tài sản thật mã hóa (RWA)", "Real-world assets"),
    "gold_token": ("Token vàng", "Gold tokens"),
}
REGIME_NAMES = {"risk_on": ("thuận lợi", "risk-on"), "neutral": ("trung tính", "neutral"),
                "risk_off": ("rủi ro", "risk-off"), "unknown": ("chưa rõ", "unknown")}


def insights(kind: str, rev: dict) -> list:
    """The patterns worth telling, as plain sentences (both halves agreeing, enough samples)."""
    import localefmt
    chu = localefmt.chu
    st = (rev or {}).get("study") or {}
    if not st.get("n"):
        return []
    out = []

    def steady(v, min_n=MIN_HALF):
        return v and v["h1"]["n"] >= min_n and v["h2"]["n"] >= min_n

    a = st.get("aligned")
    if a:
        out.append(chu("Mã đang trong xu hướng tăng: sau {h} phiên trung bình {m}%, thắng {w}% ({n} mẫu).",
                       "Symbols in an uptrend: after {h} sessions {m}% on average, up {w}% of the time ({n} samples).",
                       h=st["horizon"], m=_f(a["all"]["mean"]), w=_f(a["all"]["win"], 0, False), n=a["all"]["n"]))
    rsi = {k: v for k, v in (st.get("rsi") or {}).items() if steady(v)}
    if len(rsi) >= 2:
        best = max(rsi.items(), key=lambda kv: min(kv[1]["h1"]["mean"], kv[1]["h2"]["mean"]))
        worst = min(rsi.items(), key=lambda kv: max(kv[1]["h1"]["mean"], kv[1]["h2"]["mean"]))
        out.append(chu("RSI lúc mua tốt nhất: {b} (trung bình {bm}%); kém nhất: {w} ({wm}%).",
                       "Best RSI to buy at: {b} ({bm}% on average); worst: {w} ({wm}%).",
                       b=best[0], bm=_f(best[1]["all"]["mean"]), w=worst[0], wm=_f(worst[1]["all"]["mean"])))
    fl = st.get("falling") or {}
    if steady(fl.get("down"), MIN_HALF // 2) and steady(fl.get("up"), MIN_HALF // 2):
        d, u = fl["down"]["all"]["mean"], fl["up"]["all"]["mean"]
        out.append(chu("Mua nhịp chỉnh vào ngày giảm: {d}%, vào ngày tăng: {u}%. {c}",
                       "Pullback buys on a down day: {d}%, on an up day: {u}%. {c}",
                       d=_f(d), u=_f(u), c=chu("Luật chờ ngày tăng đang đúng.", "Waiting for an up day is right.") if u >= d else
                       chu("Luật chờ ngày tăng đang bỏ lỡ cơ hội.", "Waiting for an up day is missing gains.")))
    reg = st.get("regime") or {}
    if reg.get("risk_off") and reg.get("risk_on"):
        out.append(chu("Thị trường rủi ro: sau {h} phiên {o}%; thị trường thuận lợi: {n}%.",
                       "Risk-off market: {o}% over the next {h} sessions; risk-on: {n}%.",
                       h=st["horizon"], o=_f(reg["risk_off"]["all"]["mean"]), n=_f(reg["risk_on"]["all"]["mean"])))
    groups = [(g, v) for g, v in (st.get("groups") or {}).items() if steady(v, MIN_HALF // 2)]
    if len(groups) >= 3:
        groups.sort(key=lambda gv: -gv[1]["all"]["mean"])
        out.append(chu("Nhóm cho kết quả tốt nhất khi đang tăng: {a}; kém nhất: {b}.",
                       "Best groups while trending up: {a}; worst: {b}.",
                       a=", ".join(f"{_gname(kind, g)} ({_f(v['all']['mean'])}%)" for g, v in groups[:2]),
                       b=", ".join(f"{_gname(kind, g)} ({_f(v['all']['mean'])}%)" for g, v in groups[-2:])))
    f = st.get("factor") or {}
    if steady(f.get("tail"), MIN_HALF // 2) and steady(f.get("head"), MIN_HALF // 2):
        out.append(chu("Khi các yếu tố vĩ mô thuận chiều: {t}%; khi ngược chiều: {h}%.",
                       "With macro drivers as a tailwind: {t}%; as a headwind: {h}%.",
                       t=_f(f["tail"]["all"]["mean"]), h=_f(f["head"]["all"]["mean"])))
    stops = {k: v for k, v in (st.get("stops") or {}).items() if v}
    if stops:
        best = max(stops.items(), key=lambda kv: kv[1]["all"]["mean"])
        out.append(chu("Cắt lỗ giữ được nhiều nhất ở {k} lần ATR (trung bình {m}% mỗi lệnh thử).",
                       "The stop that kept the most: {k} x ATR ({m}% per test trade).", k=_f(float(best[0]), 1, False),
                       m=_f(best[1]["all"]["mean"])))
    jr = (rev or {}).get("journal") or {}
    if jr.get("closed"):
        a = jr["all"]
        out.append(chu("Tài khoản đã đóng {n} lệnh: thắng {w}%, R trung bình {r}.",
                       "The account closed {n} trades: {w}% won, average R {r}.",
                       n=jr["closed"], w=_f(a["win"], 0, False), r=_f(a.get("avg_r"))))
    return out


def proposal_text(kind: str, c: dict) -> str:
    import localefmt
    chu = localefmt.chu
    name = chu(*RULE_NAMES.get(c["key"], (c["key"], c["key"])))
    old, new = c["old"], c["new"]
    if c["key"] == "avoid_sectors":
        old = ", ".join(_gname(kind, g) for g in old) or chu("không", "none")
        new = ", ".join(_gname(kind, g) for g in new) or chu("không", "none")
    s = c.get("sim") or {}
    o, n = s.get("old") or {}, s.get("new") or {}
    line = chu("{name}: {old} → {new}", "{name}: {old} → {new}", name=name, old=old, new=new)
    if o and n:
        line += chu(" (mô phỏng: lãi {a}% → {b}%, sụt tối đa {c}% → {d}%)", " (replay: return {a}% → {b}%, max drawdown {c}% → {d}%)",
                    a=_f(o["return_pct"]), b=_f(n["return_pct"]), c=_f(o["max_drawdown"]), d=_f(n["max_drawdown"]))
    return line


ACCOUNTS = {"vn": ("Chứng khoán VN", "Vietnam stocks"), "crypto": ("Tiền số", "Crypto"), "world": ("Chứng khoán thế giới", "World stocks")}


def report_text(kind: str, res: dict) -> str:
    """The Telegram report of one review."""
    import localefmt
    chu = localefmt.chu
    st = res.get("study") or {}
    lines = ["🧠 " + chu("Tự đánh giá tuần: {a}", "Weekly self-review: {a}", a=chu(*ACCOUNTS[kind])),
             chu("Dữ liệu: {d} phiên ({f} đến {t}), {n} mẫu.", "Data: {d} sessions ({f} to {t}), {n} samples.",
                 d=st.get("days"), f=st.get("from"), t=st.get("to"), n=st.get("n"))]
    ins = insights(kind, res)
    if ins:
        lines.append("")
        lines.append(chu("Quy luật thấy được:", "Patterns found:"))
        lines += ["- " + x for x in ins[:6]]
    lines.append("")
    if res.get("proposals"):
        lines.append(chu("Đề xuất (chờ bạn duyệt ở trang Thị trường, tab Giao dịch ảo):",
                         "Proposals (waiting for you on the Markets page, Paper trading tab):"))
        lines += [f"{i}. " + proposal_text(kind, c) for i, c in enumerate(res["proposals"], 1)]
    else:
        lines.append(chu("Không có đề xuất: chưa thấy thay đổi nào vừa đúng ở cả hai nửa dữ liệu vừa qua được mô phỏng.",
                         "No proposals: no change held in both halves of the data and passed the replay."))
    return "\n".join(lines)
