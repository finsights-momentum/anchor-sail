"""Unit tests for the momentum engine (Alpha Leaders S1 / Wealth Vriddhi S4+)."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import momentum as M  # noqa: E402


def make_market(n_stocks=40, n_days=600, seed=7, trend_nifty=0.0004):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2023-01-02", periods=n_days)
    syms = [f"S{i:02d}" for i in range(n_stocks)]
    # stock i has drift proportional to i → ranks are predictable
    drifts = np.linspace(-0.0008, 0.0016, n_stocks)
    rets = rng.normal(0, 0.012, (n_days, n_stocks)) + drifts
    px = pd.DataFrame(100 * np.exp(np.cumsum(rets, axis=0)), index=idx, columns=syms)
    vol = pd.DataFrame(2_000_000.0, index=idx, columns=syms)        # ₹2 cr+ per day at ₹100 → liquid
    nifty = pd.Series(10000 * np.exp(np.cumsum(rng.normal(0, 0.008, n_days) + trend_nifty)), index=idx)
    sector = {s: f"SEC{i % 8}" for i, s in enumerate(syms)}
    return px, vol, nifty, sector


def test_features_need_history():
    px, vol, nifty, _ = make_market(n_days=300)
    assert M.features_at(px, vol, nifty, px.index[100]) is None
    f = M.features_at(px, vol, nifty, px.index[-1])
    assert f is not None and set(["r126", "r252", "mom12_1", "hv252", "sharpe12", "above200", "slope200", "dcap", "liq_cr", "core"]) <= set(f.columns)
    assert f["core"].between(0, 1).all()


def test_mom12_1_skips_last_month():
    px, vol, nifty, _ = make_market()
    t = px.index[-1]
    f = M.features_at(px, vol, nifty, t)
    sub = px.loc[:t]
    expected = sub.iloc[-22]["S05"] / sub.iloc[-253]["S05"] - 1
    assert f.loc["S05", "mom12_1"] == pytest.approx(expected)


def test_s1_gate_liquidity_and_trend():
    px, vol, nifty, _ = make_market()
    t = px.index[-1]
    vol2 = vol.copy(); vol2["S39"] = 1000.0            # illiquid best performer
    f = M.features_at(px, vol2, nifty, t)
    pl = M.plan("S1", f, nifty, t)
    assert np.isnan(pl["score"]["S39"])                # illiquid → ineligible
    assert pl["exposure"] == 1.0 and pl["scheme"] == "eq"
    below = f.index[~f["above200"]]
    assert pl["score"].reindex(below).isna().all()     # below 200-DMA → ineligible


def test_s4plus_derisk_and_volspike():
    px, vol, nifty, _ = make_market(trend_nifty=-0.002)   # Nifty falls → below its 200-day mean
    t = px.index[-1]
    f = M.features_at(px, vol, nifty, t)
    pl = M.plan("S4PLUS", f, nifty, t)
    assert pl["derisk"] is True and pl["exposure"] == pytest.approx(0.5)
    f2 = f.copy(); f2["volregime"] = 2.5                # synthetic vol spike
    pl2 = M.plan("S4PLUS", f2, nifty, t)
    assert pl2["volspike"] is True and pl2["exposure"] == pytest.approx(0.5 * 0.8)
    # S4+ gate: rising 200-DMA and calmer-than-median downside capture
    elig = pl["score"].dropna().index
    assert (f.loc[elig, "slope200"] > 0).all() and (f.loc[elig, "dcap"] < f["dcap"].median()).all()


def test_sector_cap_and_buffer():
    score = pd.Series(np.linspace(1, 0, 40), index=[f"S{i:02d}" for i in range(40)])
    sector = {f"S{i:02d}": "ONE" if i < 12 else f"X{i}" for i in range(40)}
    picks, rankpos = M.select_targets(score, held=[], sector=sector)
    assert len(picks) == M.N_HOLD
    assert sum(1 for p in picks if sector[p] == "ONE") == M.MAX_SECTOR_N
    # buffer: a holding ranked 25 (inside top 30) is kept, one ranked 35 is not
    held = ["S24", "S34"]
    picks2, _ = M.select_targets(score, held=held, sector={s: s for s in score.index})
    assert "S24" in picks2 and "S34" not in picks2
    assert rankpos["S00"] == 0


def test_weights():
    f = pd.DataFrame({"hv252": [0.2, 0.4]}, index=["A", "B"])
    eq = M.weights_for(["A", "B"], "eq", f)
    assert eq["A"] == pytest.approx(0.5)
    bl = M.weights_for(["A", "B"], "blend", f)
    assert bl["A"] > bl["B"] and sum(bl.values()) == pytest.approx(1.0)
    assert bl["A"] == pytest.approx(0.5 * (2 / 3) + 0.5 * 0.5)   # 50% inverse-vol + 50% equal


def test_costs():
    assert M.buy_cost(100000) == pytest.approx(100000 * M.R_BUY)
    assert M.sell_cost(100000) == pytest.approx(100000 * M.R_SELL + 20 * 1.18)
    assert M.R_BUY > M.R_SELL                           # stamp duty only on buys


def test_simulate_month_ends_whole_shares_and_idempotent_interest():
    px, vol, nifty, sector = make_market(n_days=700)
    me = M.month_end_sessions(px.index)
    assert all(isinstance(d, pd.Timestamp) for d in me)
    assert all(d.month != (d + pd.offsets.BDay(1)).month for d in me[:-1])   # each is the last session of its month (last = running month)
    start = px.index[300]
    last_full = px.index[-1]
    completed_key = (last_full - pd.offsets.MonthBegin(1) - pd.Timedelta(days=1)).strftime("%Y-%m")
    book = M.simulate("S1", px, vol, nifty, sector, start, last_full, completed_key=completed_key)
    rebs = book.s["rebalances"]
    assert rebs and all(r["date"] <= completed_key + "-31" for r in rebs)          # running month never rebalances
    for r in rebs:
        for o in r["orders"]:
            assert isinstance(o["qty"], int) and o["qty"] > 0
    for p in book.pos.values():
        assert float(p["shares"]).is_integer()
    # exposure fully used (equal weight across picks) up to whole-share rounding
    r0 = rebs[0]
    assert r0["invested"] / r0["nav"] > 0.9
    # re-running the same day must not accrue interest twice or rebalance twice
    n_reb, interest = len(book.s["rebalances"]), book.s["interest"]
    book2 = M.simulate("S1", px, vol, nifty, sector, start, last_full, completed_key=completed_key, state=book.s)
    assert len(book2.s["rebalances"]) == n_reb and book2.s["interest"] == pytest.approx(interest)


def test_skipped_buy_when_price_above_allocation():
    px, vol, nifty, sector = make_market()
    px = px.copy(); px["S39"] *= 2000                     # one share > 5% of ₹10 L
    vol = vol.copy(); vol["S39"] /= 2000                  # keep traded value similar
    t = M.month_end_sessions(px.index)[-2]
    f = M.features_at(px, vol, nifty, t)
    book = M.Book({})
    rec = book.rebalance("S1", t, f, nifty, sector)
    if "S39" in [x["symbol"] for x in rec["targets"]]:
        assert any(k["symbol"] == "S39" for k in rec["skipped"])
        assert "S39" not in book.pos
