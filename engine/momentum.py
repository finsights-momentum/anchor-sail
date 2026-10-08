"""
FINSIGHTS momentum engine — Alpha Leaders (S1, pure momentum) and Wealth Vriddhi (S4+, defensive
momentum with a liquid-fund brake). Logic ported from the FINSIGHTS Strategy Package's
combined_backtest.py (plan_for / select_targets / weights_for / run_strategy), unchanged in substance:

  features at a month-end t (daily closes up to t, >= 270 sessions):
    r126, r252, mom12_1 = C[t-21]/C[t-252]-1, hv252, hv20, volregime = hv20/hv252,
    sharpe12 = r252/hv252, above200 = C > SMA200, slope200 = SMA200 / SMA200(20 sessions ago) - 1,
    dcap = mean(stock ret | Nifty down day) / mean(Nifty ret | Nifty down day) over 252 sessions,
    liq_cr = 60-session median traded value in Rs crore
  core score = mean of percentile ranks of (mom12_1, r126, sharpe12)

  S1  : eligible = liquid (>= 5 cr) & above200;  score = core;  exposure 100%;  equal weights
  S4+ : eligible = liquid & above200 & slope200 > 0 & dcap < median(dcap);  score = core;
        exposure = 50% if Nifty < its 200-session mean, x0.8 more if median volregime > 1.7;
        weights = 50% inverse-vol + 50% equal
  both: hold 20, keep a held name while it is still inside the top 30, max 5 per sector,
        rebalance on the last trading day of each month at that close, idle cash earns 7% p.a.
        (compounded per session); itemised delivery costs, no tax.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------- parameters
INITIAL_CAPITAL = 1_000_000.0
N_HOLD = 20
BUFFER_RANK = 30
MAX_SECTOR_N = 5
LIQ_MIN_CR = 5.0
CASH_YIELD = 0.07                 # liquid fund, all idle cash, all strategies (package GLOBAL_CASH_YIELD)
S4_DERISK_EXPOSURE = 0.50
S4_VOLSPIKE_CUT = 0.80
S4_VOLREGIME_MAX = 1.7
S4_BLEND_INVVOL = 0.50
MIN_HISTORY = 270                 # sessions needed before a stock can be scored

# costs (equity delivery; package values)
TXN = 0.00325 / 100; STT = 0.001; STAMP = 0.00015; SEBI = 1e-6; GST = 0.18; DP = 20.0
R_BUY = TXN + STT + STAMP + SEBI + GST * (TXN + SEBI)
R_SELL = TXN + STT + SEBI + GST * (TXN + SEBI)


def buy_cost(v: float) -> float:
    return v * R_BUY


def sell_cost(v: float) -> float:
    return v * R_SELL + DP * (1 + GST)


STRATEGIES = {
    "S1": {"name": "Alpha Leaders", "style": "Pure momentum — fully invested"},
    "S4PLUS": {"name": "Wealth Vriddhi", "style": "Defensive momentum — liquid-fund brake"},
}


# ----------------------------------------------------------------------------- features
def features_at(px: pd.DataFrame, vol: pd.DataFrame, nifty: pd.Series, t) -> pd.DataFrame | None:
    """Cross-sectional features for every column of px at session t (inclusive)."""
    sub = px.loc[:t]
    if len(sub) < MIN_HISTORY:
        return None
    last = sub.iloc[-1]
    dr = sub.pct_change()
    f = pd.DataFrame(index=px.columns)
    f["r126"] = last / sub.iloc[-127] - 1
    f["r252"] = last / sub.iloc[-253] - 1
    f["mom12_1"] = sub.iloc[-22] / sub.iloc[-253] - 1
    hv252 = dr.iloc[-252:].std() * math.sqrt(252)
    hv20 = dr.iloc[-20:].std() * math.sqrt(252)
    f["hv252"] = hv252
    f["volregime"] = hv20 / hv252.replace(0, np.nan)
    f["sharpe12"] = f["r252"] / hv252.replace(0, np.nan)
    sma200 = sub.iloc[-200:].mean()
    f["sma200"] = sma200
    f["above200"] = last > sma200
    f["slope200"] = sma200 / sub.iloc[-220:-20].mean() - 1
    bn = nifty.reindex(sub.index).ffill()
    bdr = bn.pct_change().iloc[-252:]
    down = bdr < 0
    sdr = dr.iloc[-252:]
    if down.sum() > 3 and bdr[down].mean() != 0:
        f["dcap"] = sdr[down].mean() / bdr[down].mean()
    else:
        f["dcap"] = 1.0
    v = vol.reindex(index=sub.index, columns=px.columns)
    f["liq_cr"] = (sub.iloc[-60:] * v.iloc[-60:]).median() / 1e7
    f["close"] = last
    # core momentum score: mean percentile rank
    f["core"] = (f["mom12_1"].rank(pct=True) + f["r126"].rank(pct=True) + f["sharpe12"].rank(pct=True)) / 3
    return f


def market_derisk(nifty: pd.Series, t) -> bool:
    b = nifty.loc[:t].dropna()
    return len(b) >= 200 and float(b.iloc[-1]) < float(b.iloc[-200:].mean())


def plan(strategy: str, f: pd.DataFrame, nifty: pd.Series, t) -> dict:
    """score (NaN = ineligible), exposure, weight scheme, regime flags."""
    liq = f["liq_cr"] >= LIQ_MIN_CR
    if strategy == "S1":
        gate = liq & f["above200"]
        return {"score": f["core"].where(gate), "exposure": 1.0, "scheme": "eq",
                "derisk": False, "volspike": False, "gate": gate}
    if strategy == "S4PLUS":
        gate = liq & f["above200"] & (f["slope200"] > 0) & (f["dcap"] < f["dcap"].median())
        exposure = 1.0
        derisk = market_derisk(nifty, t)
        if derisk:
            exposure = S4_DERISK_EXPOSURE
        volspike = bool(f["volregime"].median() > S4_VOLREGIME_MAX)
        if volspike:
            exposure *= S4_VOLSPIKE_CUT
        return {"score": f["core"].where(gate), "exposure": min(exposure, 1.0), "scheme": "blend",
                "derisk": derisk, "volspike": volspike, "gate": gate}
    raise ValueError(strategy)


def select_targets(score: pd.Series, held: list[str], sector: dict, n: int = N_HOLD) -> tuple[list[str], dict]:
    ranked = score.dropna().sort_values(ascending=False)
    order = list(ranked.index)
    rankpos = {t: i for i, t in enumerate(order)}
    picks, seccount = [], {}

    def try_add(t):
        s = sector.get(t, "NA")
        if seccount.get(s, 0) >= MAX_SECTOR_N:
            return False
        picks.append(t); seccount[s] = seccount.get(s, 0) + 1
        return True

    for t in held:                               # buffer: keep holdings still inside the top 30
        if t in rankpos and rankpos[t] < BUFFER_RANK:
            try_add(t)
        if len(picks) >= n:
            break
    for t in order:
        if len(picks) >= n:
            break
        if t in picks:
            continue
        try_add(t)
    return picks[:n], rankpos


def weights_for(targets: list[str], scheme: str, f: pd.DataFrame) -> dict:
    if not targets:
        return {}
    if scheme == "eq":
        return {t: 1.0 / len(targets) for t in targets}
    iv = 1.0 / f["hv252"].reindex(targets).replace(0, np.nan)
    iv = iv.fillna(iv.median())
    wv = iv / iv.sum()
    we = pd.Series(1.0 / len(targets), index=targets)
    w = S4_BLEND_INVVOL * wv + (1 - S4_BLEND_INVVOL) * we
    return (w / w.sum()).to_dict()


# ----------------------------------------------------------------------------- ledger
class Book:
    """Monthly-rebalanced momentum book. State is a plain dict (JSON-serialisable)."""

    def __init__(self, state: dict):
        self.s = state
        self.s.setdefault("cash", INITIAL_CAPITAL)
        self.s.setdefault("positions", {})           # sym -> dict(shares, basis, entry_date, bought_sh, bought_val, sold_sh, sold_val, costs)
        self.s.setdefault("trades", [])               # closed positions
        self.s.setdefault("rebalances", [])           # one record per month-end
        self.s.setdefault("history", {})              # date -> nav, cash, inv, n
        self.s.setdefault("interest", 0.0)
        self.s.setdefault("charges", 0.0)
        self.s.setdefault("last_processed_date", None)

    # ---- helpers
    @property
    def pos(self):
        return self.s["positions"]

    def value(self, prices: pd.Series) -> tuple[float, float]:
        inv = 0.0
        for sym, p in self.pos.items():
            px = prices.get(sym, np.nan)
            if px is None or (isinstance(px, float) and math.isnan(px)):
                px = p.get("last_px", p["basis"] / p["shares"] if p["shares"] else 0)
            inv += p["shares"] * float(px)
        return self.s["cash"] + inv, inv

    def accrue(self, date):
        """One session of liquid-fund interest on idle cash; idempotent per session (intraday re-runs are safe)."""
        dk = str(date)[:10]
        if self.s.get("accrued_date") and dk <= self.s["accrued_date"]:
            return
        intr = self.s["cash"] * ((1 + CASH_YIELD) ** (1 / 252) - 1)
        self.s["cash"] += intr
        self.s["interest"] += intr
        self.s["accrued_date"] = dk

    def _buy(self, sym, sh, px, date):
        v = sh * px; c = buy_cost(v)
        p = self.pos.setdefault(sym, {"shares": 0.0, "basis": 0.0, "entry_date": str(date)[:10], "bought_sh": 0.0,
                                      "bought_val": 0.0, "sold_sh": 0.0, "sold_val": 0.0, "costs": 0.0, "last_px": px})
        p["shares"] += sh; p["basis"] += v + c; p["bought_sh"] += sh; p["bought_val"] += v; p["costs"] += c; p["last_px"] = px
        self.s["cash"] -= v + c; self.s["charges"] += c
        return v, c

    def _sell(self, sym, sh, px, date, reason):
        p = self.pos[sym]
        v = sh * px; c = sell_cost(v); avg = p["basis"] / p["shares"]
        realised = (v - c) - avg * sh
        p["basis"] -= avg * sh; p["shares"] -= sh; p["sold_sh"] += sh; p["sold_val"] += v; p["costs"] += c; p["last_px"] = px
        p["realised"] = p.get("realised", 0.0) + realised
        self.s["cash"] += v - c; self.s["charges"] += c
        if p["shares"] <= 1e-6:
            self.s["trades"].append({
                "symbol": sym, "entry_date": p["entry_date"], "exit_date": str(date)[:10],
                "avg_entry": round(p["bought_val"] / p["bought_sh"], 2) if p["bought_sh"] else None,
                "avg_exit": round(p["sold_val"] / p["sold_sh"], 2) if p["sold_sh"] else None,
                "shares": round(p["bought_sh"], 2), "invested": round(p["bought_val"], 2), "costs": round(p["costs"], 2),
                "net_pnl": round(p["realised"], 2),
                "return_pct": round(100 * p["realised"] / p["bought_val"], 2) if p["bought_val"] else None,
                "months": (pd.Timestamp(date).year - pd.Timestamp(p["entry_date"]).year) * 12 + pd.Timestamp(date).month - pd.Timestamp(p["entry_date"]).month,
                "reason": reason})
            del self.pos[sym]
        return v, c

    # ---- the monthly rebalance
    def rebalance(self, strategy: str, date, f: pd.DataFrame, nifty: pd.Series, sector: dict) -> dict:
        pl = plan(strategy, f, nifty, date)
        prices = f["close"]
        total, _ = self.value(prices)
        held = list(self.pos.keys())
        targets, rankpos = select_targets(pl["score"], held, sector) if pl["exposure"] > 0 else ([], {})
        w = weights_for(targets, pl["scheme"], f)
        tgt_val = {t: pl["exposure"] * w[t] * total for t in targets}
        orders = []
        for sym in list(self.pos):                                   # sells / trims first
            px = prices.get(sym, np.nan)
            if px is None or math.isnan(px):
                continue
            cur = self.pos[sym]["shares"] * px
            tgt = tgt_val.get(sym, 0.0)
            if tgt < cur - 1:
                full = sym not in tgt_val
                sh = self.pos[sym]["shares"] if full else float(math.floor((cur - tgt) / px))   # whole shares only
                if sh > 0:
                    v, c = self._sell(sym, sh, float(px), date, "Dropped out of top 30" if full else "Trim to target weight")
                    orders.append({"symbol": sym, "side": "SELL" if full else "TRIM", "qty": int(round(sh)), "price": round(float(px), 2),
                                   "value": round(v, 2), "rank": (rankpos.get(sym, None) + 1) if sym in rankpos else None})
        skipped = []
        for sym in targets:                                          # buys / top-ups
            px = prices.get(sym, np.nan)
            if px is None or math.isnan(px) or px <= 0:
                skipped.append({"symbol": sym, "rank": rankpos.get(sym, -1) + 1, "reason": "No price on rebalance day"})
                continue
            cur = self.pos[sym]["shares"] * px if sym in self.pos else 0.0
            need = tgt_val[sym] - cur
            if need > 1:
                sh = math.floor(need / (px * (1 + R_BUY)))
                if sh > 0 and self.s["cash"] > sh * px * (1 + R_BUY):
                    new = sym not in self.pos
                    v, c = self._buy(sym, sh, float(px), date)
                    orders.append({"symbol": sym, "side": "BUY" if new else "TOP-UP", "qty": int(sh), "price": round(float(px), 2),
                                   "value": round(v, 2), "rank": rankpos.get(sym, -1) + 1, "weight_pct": round(w[sym] * pl["exposure"] * 100, 2)})
                elif sh <= 0 and sym not in self.pos:                # one share costs more than the whole target allocation
                    skipped.append({"symbol": sym, "rank": rankpos.get(sym, -1) + 1, "price": round(float(px), 2), "target_value": round(tgt_val[sym], 2),
                                    "reason": "Share price above the target allocation — cannot buy a whole share"})
                elif sh > 0 and sym not in self.pos:
                    skipped.append({"symbol": sym, "rank": rankpos.get(sym, -1) + 1, "price": round(float(px), 2), "target_value": round(tgt_val[sym], 2),
                                    "reason": "Insufficient cash"})
        for sym, p in self.pos.items():
            px = prices.get(sym, np.nan)
            if px is not None and not math.isnan(px):
                p["last_px"] = float(px)
        nav, inv = self.value(prices)
        rec = {"date": str(date)[:10], "exposure_pct": round(pl["exposure"] * 100, 1), "derisk": bool(pl["derisk"]), "volspike": bool(pl["volspike"]),
               "targets": [{"symbol": t, "rank": rankpos.get(t, -1) + 1, "weight_pct": round(w[t] * pl["exposure"] * 100, 2),
                            "score": round(float(pl["score"].get(t, np.nan)) * 100, 1)} for t in targets],
               "orders": orders, "skipped": skipped, "nav": round(nav, 2), "cash": round(self.s["cash"], 2), "invested": round(inv, 2),
               "eligible": int(pl["score"].notna().sum()), "universe": int(len(f))}
        self.s["rebalances"].append(rec)
        return rec


# ----------------------------------------------------------------------------- simulation driver (shared by backtest and live)
def month_end_sessions(index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """Last trading session of each calendar month present in the index."""
    s = pd.Series(index, index=index)
    return [pd.Timestamp(x) for x in s.groupby([index.year, index.month]).last().values]


def simulate(strategy: str, px: pd.DataFrame, vol: pd.DataFrame, nifty: pd.Series, sector: dict, start, end,
             completed_key: str | None = None, state: dict | None = None, feature_cache: dict | None = None, log=None) -> Book:
    """Run the book over sessions in (last_processed, end]. Rebalances on the last session of each COMPLETED
    month >= start (months after `completed_key`, e.g. the running month, never rebalance). Sessions between
    rebalances only accrue interest and mark to market. Deterministic given data."""
    book = Book(state if state is not None else {})
    idx = px.index
    me = set(pd.Timestamp(d) for d in month_end_sessions(idx)
             if completed_key is None or f"{d.year:04d}-{d.month:02d}" <= completed_key)
    lp = pd.Timestamp(book.s["last_processed_date"]) if book.s.get("last_processed_date") else None
    started = bool(book.s["rebalances"])
    for d in idx:
        if d > pd.Timestamp(end):
            break
        if lp is not None and d <= lp:
            continue
        if d < pd.Timestamp(start):
            continue
        if started:
            book.accrue(d)
        if d in me and not any(r["date"] == str(d.date()) for r in book.s["rebalances"]):
            key = (strategy, d)
            f = None
            if feature_cache is not None and ("F", d) in feature_cache:
                f = feature_cache[("F", d)]
            if f is None:
                f = features_at(px, vol, nifty, d)
                if feature_cache is not None:
                    feature_cache[("F", d)] = f
            if f is not None:
                book.rebalance(strategy, d, f, nifty, sector)
                started = True
                if log:
                    r = book.s["rebalances"][-1]
                    log(f"    {strategy} {str(d.date())}  exp {r['exposure_pct']:>5}%  targets {len(r['targets']):>2}  orders {len(r['orders']):>2}  nav {r['nav']:,.0f}")
        if started:
            prices = px.loc[d]
            nav, inv = book.value(prices)
            book.s["history"][str(d.date())] = {"nav": round(nav, 2), "cash": round(book.s["cash"], 2), "inv": round(inv, 2), "n": len(book.pos)}
        book.s["last_processed_date"] = str(d.date())
    return book
