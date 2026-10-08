"""
Runs the FINSIGHTS momentum strategies (Alpha Leaders / Wealth Vriddhi) for the live dashboard:
a fresh 2016 → today backtest per universe and an incremental live book per universe, and shapes
both into the dashboard payload. Called from build.py.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

import momentum as M

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "data" / "state"

MOM_UNIVERSES = {
    "NIFTY100": {"label": "Nifty 100", "bench_name": "Nifty 100", "bench_symbols": ["^CNX100", "NIFTY_100.NS"]},
    "NIFTY200": {"label": "Nifty 200", "bench_name": "Nifty 200", "bench_symbols": ["^CNX200", "NIFTY_200.NS"]},
    "NIFTY500": {"label": "Nifty 500", "bench_name": "Nifty 500", "bench_symbols": ["^CRSLDX", "NIFTY500.NS"]},
}
DEFAULT_UNIVERSE = "NIFTY200"
BACKTEST_START = "2016-01-01"
PUBLISHED = {   # FINSIGHTS package figures (hand-typed universes, Yahoo sectors), Jan 2016 – Aug 2026, for reference
    ("S1", "NIFTY100"): 25.91, ("S1", "NIFTY200"): 31.89, ("S1", "NIFTY500"): 34.19,
    ("S4PLUS", "NIFTY100"): 23.70, ("S4PLUS", "NIFTY200"): 30.36, ("S4PLUS", "NIFTY500"): 32.13,
}


def fnum(x, nd=2):
    try:
        if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
            return None
        return round(float(x), nd)
    except Exception:
        return None


def _at(series, d):
    if series is None:
        return None
    s = series[series.index <= d]
    return float(s.iloc[-1]) if len(s) else None


def _cagr(v0, v1, d0, d1):
    yrs = (pd.Timestamp(d1) - pd.Timestamp(d0)).days / 365.25
    return (v1 / v0) ** (1 / yrs) - 1 if yrs > 0 and v0 and v1 else None


def _max_dd(vals):
    peak, mdd = -1e18, 0.0
    for v in vals:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1)
    return mdd


def _metrics(book: M.Book, bench, bench2, last_date) -> dict:
    h = book.s["history"]
    days = sorted(h)
    navs = [h[k]["nav"] for k in days]
    d0, d1 = days[0], days[-1]
    nav0, nav1 = navs[0], navs[-1]
    cagr = _cagr(nav0, nav1, d0, d1)
    out = {"start": d0, "as_of": d1, "years": fnum((pd.Timestamp(d1) - pd.Timestamp(d0)).days / 365.25),
           "final_value": fnum(nav1), "net_profit": fnum(nav1 - M.INITIAL_CAPITAL), "total_return_pct": fnum((nav1 / M.INITIAL_CAPITAL - 1) * 100),
           "cagr_pct": fnum(cagr * 100) if cagr is not None else None, "max_dd_pct": fnum(_max_dd(navs) * 100)}
    for key, b in (("bench", bench), ("bench2", bench2)):
        b0, b1 = _at(b, pd.Timestamp(d0)), _at(b, pd.Timestamp(d1))
        if b0 and b1:
            bc = _cagr(b0, b1, d0, d1)
            out[f"{key}_return_pct"] = fnum((b1 / b0 - 1) * 100)
            out[f"{key}_cagr_pct"] = fnum(bc * 100) if bc is not None else None
            out[f"excess_{key}_pct"] = fnum((cagr - bc) * 100) if (cagr is not None and bc is not None) else None
        else:
            out[f"{key}_return_pct"] = out[f"{key}_cagr_pct"] = out[f"excess_{key}_pct"] = None
    # monthly return stats on month-end NAVs
    ser = pd.Series(navs, index=pd.to_datetime(days))
    mnav = ser.groupby([ser.index.year, ser.index.month]).last()
    mrets = mnav.pct_change().dropna()
    out["sharpe"] = fnum(mrets.mean() / mrets.std(ddof=1) * math.sqrt(12)) if len(mrets) > 2 and mrets.std(ddof=1) > 0 else None
    out["calmar"] = fnum(cagr / abs(_max_dd(navs))) if (cagr is not None and _max_dd(navs) < 0) else None
    out["best_month_pct"] = fnum(mrets.max() * 100) if len(mrets) else None
    out["worst_month_pct"] = fnum(mrets.min() * 100) if len(mrets) else None
    out["positive_months_pct"] = fnum((mrets > 0).mean() * 100) if len(mrets) else None
    tr = book.s["trades"]
    wins = [t for t in tr if (t["net_pnl"] or 0) > 0]
    losses = [t for t in tr if (t["net_pnl"] or 0) <= 0]
    gp = sum(t["net_pnl"] for t in wins); gl = -sum(t["net_pnl"] for t in losses)
    out.update({
        "closed_trades": len(tr), "open_positions": len(book.pos), "wins": len(wins),
        "win_rate_pct": fnum(len(wins) / len(tr) * 100) if tr else None,
        "profit_factor": fnum(gp / gl) if gl > 0 else None,
        "avg_win_pct": fnum(np.mean([t["return_pct"] for t in wins if t["return_pct"] is not None])) if wins else None,
        "avg_loss_pct": fnum(np.mean([t["return_pct"] for t in losses if t["return_pct"] is not None])) if losses else None,
        "avg_holding_months": fnum(np.mean([t["months"] for t in tr]), 1) if tr else None,
        "charges_paid": fnum(book.s["charges"]), "interest_earned": fnum(book.s["interest"]),
        "realised_pnl": fnum(sum(t["net_pnl"] for t in tr)),
        "avg_exposure_pct": fnum(np.mean([r["exposure_pct"] for r in book.s["rebalances"]])) if book.s["rebalances"] else None,
        "months_derisked": sum(1 for r in book.s["rebalances"] if r["derisk"]),
        "rebalances": len(book.s["rebalances"]),
        "cash_now": fnum(h[d1]["cash"]), "invested_now": fnum(h[d1]["inv"]),
    })
    return out


def _yearly(book: M.Book, bench, bench2) -> list[dict]:
    h = book.s["history"]; days = sorted(h)
    rows = []; prev_nav = M.INITIAL_CAPITAL
    prev_b = _at(bench, pd.Timestamp(days[0])); prev_b2 = _at(bench2, pd.Timestamp(days[0]))
    by_year = {}
    for k in days:
        by_year.setdefault(k[:4], k)
    for y, last in by_year.items():
        nav = h[last]["nav"]; b = _at(bench, pd.Timestamp(last)); b2 = _at(bench2, pd.Timestamp(last))
        pr = nav / prev_nav - 1
        br = (b / prev_b - 1) if (b and prev_b) else None
        br2 = (b2 / prev_b2 - 1) if (b2 and prev_b2) else None
        rows.append({"year": y, "port_pct": fnum(pr * 100), "bench_pct": fnum(br * 100) if br is not None else None,
                     "bench2_pct": fnum(br2 * 100) if br2 is not None else None,
                     "excess_pct": fnum((pr - br) * 100) if br is not None else None, "nav_end": fnum(nav),
                     "trades": sum(1 for t in book.s["trades"] if (t["exit_date"] or "")[:4] == y),
                     "wins": sum(1 for t in book.s["trades"] if (t["exit_date"] or "")[:4] == y and (t["net_pnl"] or 0) > 0),
                     "avg_exposure_pct": fnum(np.mean([r["exposure_pct"] for r in book.s["rebalances"] if r["date"][:4] == y])) if any(r["date"][:4] == y for r in book.s["rebalances"]) else None})
        prev_nav = nav; prev_b = b or prev_b; prev_b2 = b2 or prev_b2
    return rows


def _history_points(book: M.Book, bench, bench2, monthly_only: bool) -> list[dict]:
    h = book.s["history"]; days = sorted(h)
    if monthly_only:
        keep = {}
        for k in days:
            keep[k[:7]] = k
        days = sorted(keep.values())
    exp_by_month = {r["date"][:7]: r["exposure_pct"] for r in book.s["rebalances"]}
    return [{"date": k, "nav": h[k]["nav"], "bench": _at(bench, pd.Timestamp(k)), "bench2": _at(bench2, pd.Timestamp(k)),
             "n": h[k]["n"], "cash": h[k]["cash"], "exposure_pct": exp_by_month.get(k[:7])} for k in days]


def _holdings(book: M.Book, prices: pd.Series, f_now: pd.DataFrame | None, plan_now: dict | None, names, industry, nav) -> list[dict]:
    rows = []
    rankpos = {}
    if plan_now is not None:
        ranked = plan_now["score"].dropna().sort_values(ascending=False)
        rankpos = {t: i + 1 for i, t in enumerate(ranked.index)}
    for sym, p in book.pos.items():
        ltp = prices.get(sym, np.nan)
        ltp = float(ltp) if ltp is not None and not (isinstance(ltp, float) and math.isnan(ltp)) else p.get("last_px")
        avg = p["bought_val"] / p["bought_sh"] if p["bought_sh"] else None
        val = p["shares"] * ltp if ltp else None
        rows.append({"symbol": sym, "name": names.get(sym, ""), "industry": industry.get(sym, "—"), "shares": int(round(p["shares"])),
                     "avg_entry": fnum(avg), "entry_date": p["entry_date"], "ltp": fnum(ltp), "value": fnum(val),
                     "weight_pct": fnum(val / nav * 100) if (val and nav) else None,
                     "pnl_pct": fnum((ltp / avg - 1) * 100) if (ltp and avg) else None,
                     "unrealised": fnum((ltp - avg) * p["shares"]) if (ltp and avg) else None,
                     "rank_now": rankpos.get(sym), "score_now": fnum(float(f_now["core"].get(sym, np.nan)) * 100, 1) if f_now is not None else None,
                     "eligible_now": bool(plan_now["gate"].get(sym, False)) if plan_now is not None else None,
                     "in_buffer": (rankpos.get(sym) is not None and rankpos[sym] <= M.BUFFER_RANK)})
    rows.sort(key=lambda r: -(r["weight_pct"] or 0))
    return rows


def _universe_rows(f: pd.DataFrame, pl: dict, names, industry, held: set) -> list[dict]:
    ranked = pl["score"].dropna().sort_values(ascending=False)
    rankpos = {t: i + 1 for i, t in enumerate(ranked.index)}
    rows = []
    for sym in f.index:
        r = f.loc[sym]
        rows.append({"symbol": sym, "name": names.get(sym, ""), "industry": industry.get(sym, "—"),
                     "rank": rankpos.get(sym), "score": fnum(float(r["core"]) * 100, 1) if pd.notna(r["core"]) else None,
                     "eligible": bool(pl["gate"].get(sym, False)), "held": sym in held,
                     "close": fnum(r["close"]), "mom12_1_pct": fnum(r["mom12_1"] * 100), "r126_pct": fnum(r["r126"] * 100),
                     "r252_pct": fnum(r["r252"] * 100), "sharpe12": fnum(r["sharpe12"]), "above200": bool(r["above200"]) if pd.notna(r["above200"]) else None,
                     "dist200_pct": fnum((r["close"] / r["sma200"] - 1) * 100) if pd.notna(r["sma200"]) and r["sma200"] else None,
                     "slope200_pct": fnum(r["slope200"] * 100), "dcap": fnum(r["dcap"]), "liq_cr": fnum(r["liq_cr"], 1),
                     "hv252_pct": fnum(r["hv252"] * 100, 1), "volregime": fnum(r["volregime"])})
    rows.sort(key=lambda x: (x["rank"] is None, x["rank"] or 0))
    return rows


def run_momentum(prices: dict, nsei: pd.DataFrame, unis: dict, names: dict, industry: dict, data_date, today, after_close,
                 completed_key: str, inception: str, bench2, bench2_label: str, fetch_bench, log, mock: bool = False) -> tuple[dict, bool]:
    """Returns (payload dict for the momentum strategies, material_change flag)."""
    nifty = nsei["Close"]
    all_syms = [s for s in unis["NIFTY500"] if s in prices]
    px_all = pd.DataFrame({s: prices[s]["Close"] for s in all_syms}).sort_index()
    vol_all = pd.DataFrame({s: prices[s]["Volume"] if "Volume" in prices[s] else pd.Series(np.nan, index=prices[s].index) for s in all_syms}).sort_index()
    last_date = px_all.index.max()
    final_cutoff = today if after_close else today - pd.Timedelta(days=1)
    material = False
    out = {"default_universe": DEFAULT_UNIVERSE, "universe_labels": {k: v["label"] for k, v in MOM_UNIVERSES.items()},
           "capital": M.INITIAL_CAPITAL, "inception": inception, "bench2_label": bench2_label,
           "rules": {"hold": M.N_HOLD, "buffer": M.BUFFER_RANK, "sector_cap": M.MAX_SECTOR_N, "liq_min_cr": M.LIQ_MIN_CR,
                     "cash_yield_pct": M.CASH_YIELD * 100, "derisk_exposure_pct": M.S4_DERISK_EXPOSURE * 100,
                     "volspike_cut_pct": (1 - M.S4_VOLSPIKE_CUT) * 100, "volregime_max": M.S4_VOLREGIME_MAX},
           "strategies": {}}
    for code, meta in M.STRATEGIES.items():
        out["strategies"][code] = {"code": code, "name": meta["name"], "style": meta["style"], "universes": {}}

    for ukey, ucfg in MOM_UNIVERSES.items():
        syms = [s for s in unis[ukey] if s in px_all.columns]
        px, vol = px_all[syms], vol_all[syms]
        bench, blabel = (nifty * 1.0, "MOCK benchmark") if mock else fetch_bench(ucfg["bench_symbols"], ucfg["bench_name"])
        cache: dict = {}
        # features as of the latest session (provisional, for the "if rebalanced today" view and the universe table)
        f_now = M.features_at(px, vol, nifty, last_date)
        log(f"  momentum {ukey}: {len(syms)} symbols, features ok={f_now is not None}")
        for code in M.STRATEGIES:
            # ---------------- backtest (fresh every run)
            bt = M.simulate(code, px, vol, nifty, industry, BACKTEST_START, last_date, completed_key=completed_key, feature_cache=cache)
            # ---------------- live book (incremental, persisted)
            sid = f"MOM_{code}_{ukey}"
            sp = STATE_DIR / f"{sid}.json"
            state = json.loads(sp.read_text()) if sp.exists() else {"id": sid, "strategy": code, "universe": ukey, "inception": inception,
                                                                    "capital": M.INITIAL_CAPITAL, "version": 1}
            n_reb_before = len(state.get("rebalances", []))
            if pd.Timestamp(inception) <= last_date and completed_key >= inception[:7]:
                live = M.simulate(code, px, vol, nifty, industry, inception, last_date, completed_key=completed_key, state=state, feature_cache=cache)
            else:
                live = M.Book(state)
            if len(state.get("rebalances", [])) != n_reb_before:
                material = True
            # the intraday session is re-processed next run (interest is idempotent; rebalances never happen intraday)
            if state.get("last_processed_date"):
                state["last_processed_date"] = str(min(pd.Timestamp(state["last_processed_date"]), final_cutoff).date())
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            sp.write_text(json.dumps(state, default=str))

            prices_now = px.loc[last_date]
            plan_now = M.plan(code, f_now, nifty, last_date) if f_now is not None else None
            live_hist = live.s["history"]
            nav_now = live_hist[sorted(live_hist)[-1]]["nav"] if live_hist else M.INITIAL_CAPITAL
            # provisional targets if the month closed today
            prov = None
            if plan_now is not None:
                tg, rankpos = M.select_targets(plan_now["score"], list(live.pos.keys()), industry)
                w = M.weights_for(tg, plan_now["scheme"], f_now)
                held = set(live.pos.keys())
                prov = {"exposure_pct": fnum(plan_now["exposure"] * 100, 1), "derisk": bool(plan_now["derisk"]), "volspike": bool(plan_now["volspike"]),
                        "targets": [{"symbol": t, "name": names.get(t, ""), "industry": industry.get(t, "—"), "rank": rankpos.get(t, -1) + 1,
                                     "weight_pct": fnum(w[t] * plan_now["exposure"] * 100), "score": fnum(float(plan_now["score"].get(t, np.nan)) * 100, 1),
                                     "action": "HOLD" if t in held else "BUY"} for t in tg],
                        "would_sell": [{"symbol": s, "name": names.get(s, ""), "rank": rankpos.get(s), "score": fnum(float(plan_now["score"].get(s, np.nan)) * 100, 1) if s in plan_now["score"].index else None,
                                        "reason": "outside top 30" if (rankpos.get(s) is None or rankpos[s] >= M.BUFFER_RANK) else "sector cap / slot"} for s in held if s not in tg],
                        "watch": [{"symbol": t, "name": names.get(t, ""), "industry": industry.get(t, "—"), "rank": rankpos.get(t, -1) + 1,
                                   "score": fnum(float(plan_now["score"].get(t, np.nan)) * 100, 1)}
                                  for t in list(plan_now["score"].dropna().sort_values(ascending=False).index)[M.N_HOLD:M.BUFFER_RANK]]}
            b0 = _at(bench, pd.Timestamp(inception)); b1 = _at(bench, last_date)
            b20 = _at(bench2, pd.Timestamp(inception)); b21 = _at(bench2, last_date)
            n200 = nifty.loc[:last_date]
            regime = {"nifty": fnum(float(n200.iloc[-1])), "nifty_200dma": fnum(float(n200.iloc[-200:].mean())) if len(n200) >= 200 else None,
                      "nifty_vs_200dma_pct": fnum((float(n200.iloc[-1]) / float(n200.iloc[-200:].mean()) - 1) * 100) if len(n200) >= 200 else None,
                      "median_volregime": fnum(float(f_now["volregime"].median())) if f_now is not None else None,
                      "eligible_now": int(plan_now["score"].notna().sum()) if plan_now is not None else None}
            last_reb = live.s["rebalances"][-1] if live.s["rebalances"] else None
            # next rebalance = last trading session of the running month (estimated as last business day)
            nxt = (pd.Timestamp(today) + pd.offsets.BMonthEnd(0))
            out["strategies"][code]["universes"][ukey] = {
                "universe": ukey, "universe_label": ucfg["label"], "universe_size": len(syms),
                "benchmark": ucfg["bench_name"], "benchmark_source": blabel, "bench2": bench2_label,
                "live": {
                    "inception": inception, "started": bool(live.s["rebalances"]),
                    "performance": {
                        "nav": fnum(nav_now), "return_pct": fnum((nav_now / M.INITIAL_CAPITAL - 1) * 100),
                        "bench_return_pct": fnum((b1 / b0 - 1) * 100) if (b0 and b1) else None,
                        "bench2_return_pct": fnum((b21 / b20 - 1) * 100) if (b20 and b21) else None,
                        "cash": fnum(live.s["cash"]), "invested": fnum(nav_now - live.s["cash"]),
                        "exposure_pct": last_reb["exposure_pct"] if last_reb else None, "holdings": len(live.pos),
                        "interest": fnum(live.s["interest"]), "charges": fnum(live.s["charges"]),
                        "realised": fnum(sum(t["net_pnl"] for t in live.s["trades"])),
                        "max_dd_pct": fnum(_max_dd([live_hist[k]["nav"] for k in sorted(live_hist)]) * 100) if live_hist else 0,
                        "days": (last_date - pd.Timestamp(inception)).days},
                    "history": _history_points(live, bench, bench2, monthly_only=False),
                    "holdings": _holdings(live, prices_now, f_now, plan_now, names, industry, nav_now),
                    "last_rebalance": last_reb, "rebalances": live.s["rebalances"][-6:], "trades": live.s["trades"][-60:],
                    "next_rebalance": str(nxt.date()), "provisional": prov, "regime": regime,
                },
                "universe_rows": _universe_rows(f_now, plan_now, names, industry, set(live.pos.keys())) if plan_now is not None else [],
                "backtest": {
                    "metrics": _metrics(bt, bench, bench2, last_date), "yearly": _yearly(bt, bench, bench2),
                    "history": _history_points(bt, bench, bench2, monthly_only=True),
                    "trades": sorted(bt.s["trades"], key=lambda t: t["exit_date"], reverse=True),
                    "holdings": _holdings(bt, prices_now, f_now, plan_now, names, industry, bt.s["history"][sorted(bt.s["history"])[-1]]["nav"]),
                    "published_cagr_pct": PUBLISHED.get((code, ukey)),
                },
            }
            m = out["strategies"][code]["universes"][ukey]["backtest"]["metrics"]
            log(f"    {code} {ukey}: backtest CAGR {m['cagr_pct']}%  MDD {m['max_dd_pct']}%  trades {m['closed_trades']} | live nav {nav_now:,.0f} holdings {len(live.pos)}")
    return out, material
