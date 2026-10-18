"""
Market snapshot for the Live-markets page: Indian indices, world indices, commodities, currencies.

Two sources, merged:
  1. Yahoo Finance (always) — NSE/BSE indices (≈15 min delayed), world indices, US/LME-style futures, FX.
  2. Dhan (optional) — if the GitHub secrets DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are set, NSE/BSE
     index LTPs and MCX front-month futures (gold, silver, crude, natural gas, base metals) are taken
     from Dhan's market-quote API, which is the only clean source for MCX. Dhan rows overwrite the
     Yahoo rows for the same index. Any failure falls back silently to Yahoo with a warning.

Output (payload["quotes"]):
  {"as_of_utc": ..., "groups": {"india": [...], "world": [...], "mcx": [...], "commodities": [...], "fx": [...]},
   "sources": {"yahoo": "ok|failed", "dhan": "ok|not configured|failed: ..."}, "warnings": [...]}
  each row: {"symbol", "name", "last", "prev_close", "chg", "chg_pct", "open", "high", "low", "time", "source"}
"""
from __future__ import annotations

import io
import json
import math
import os
import time
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests

IST = timezone(timedelta(hours=5, minutes=30))

YAHOO = {
    "india": [("^NSEI", "Nifty 50"), ("^BSESN", "Sensex"), ("^NSEBANK", "Bank Nifty"), ("NIFTY_FIN_SERVICE.NS", "Nifty Financial Services"),
              ("^CNXIT", "Nifty IT"), ("NIFTY_MIDCAP_100.NS", "Nifty Midcap 100"), ("^CNXSC", "Nifty Smallcap 100"), ("^CNX100", "Nifty 100"),
              ("^CNX200", "Nifty 200"), ("^CRSLDX", "Nifty 500"), ("^INDIAVIX", "India VIX"), ("^CNXAUTO", "Nifty Auto"), ("^CNXPHARMA", "Nifty Pharma"),
              ("^CNXFMCG", "Nifty FMCG"), ("^CNXMETAL", "Nifty Metal"), ("^CNXENERGY", "Nifty Energy"), ("^CNXREALTY", "Nifty Realty"), ("^CNXPSUBANK", "Nifty PSU Bank"),
              ("^CNXINFRA", "Nifty Infra"), ("^CNXMEDIA", "Nifty Media"), ("^CNXCONSUM", "Nifty Consumption")],
    "world": [("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq Composite"), ("^NDX", "Nasdaq 100"), ("^DJI", "Dow Jones"), ("^RUT", "Russell 2000"), ("^VIX", "VIX"),
              ("^FTSE", "FTSE 100"), ("^GDAXI", "DAX"), ("^FCHI", "CAC 40"), ("^STOXX50E", "Euro Stoxx 50"), ("^N225", "Nikkei 225"), ("^HSI", "Hang Seng"),
              ("000001.SS", "Shanghai Composite"), ("^KS11", "KOSPI"), ("^STI", "Straits Times"), ("^AXJO", "ASX 200"), ("^TWII", "Taiwan Weighted")],
    "commodities": [("GC=F", "Gold (COMEX, $/oz)"), ("SI=F", "Silver (COMEX, $/oz)"), ("PL=F", "Platinum ($/oz)"), ("PA=F", "Palladium ($/oz)"), ("CL=F", "WTI Crude ($/bbl)"),
                    ("BZ=F", "Brent Crude ($/bbl)"), ("NG=F", "Natural Gas ($/MMBtu)"), ("HG=F", "Copper (COMEX, $/lb)"), ("ALI=F", "Aluminium ($/t)"),
                    ("ZW=F", "Wheat (¢/bu)"), ("ZC=F", "Corn (¢/bu)"), ("ZS=F", "Soybeans (¢/bu)"), ("KC=F", "Coffee (¢/lb)"), ("SB=F", "Sugar (¢/lb)"), ("CT=F", "Cotton (¢/lb)")],
    "fx": [("INR=X", "USD / INR"), ("EURINR=X", "EUR / INR"), ("GBPINR=X", "GBP / INR"), ("JPYINR=X", "JPY / INR"), ("DX-Y.NYB", "Dollar Index"), ("EURUSD=X", "EUR / USD"),
           ("JPY=X", "USD / JPY"), ("^TNX", "US 10Y yield (%)"), ("BTC-USD", "Bitcoin ($)")],
}

# Dhan: index security IDs on the IDX_I segment (from Dhan's scrip master; resolved by name at runtime when possible)
DHAN_INDEX_NAMES = {"NIFTY": "Nifty 50", "BANKNIFTY": "Bank Nifty", "FINNIFTY": "Nifty Financial Services", "MIDCPNIFTY": "Nifty Midcap Select", "SENSEX": "Sensex",
                    "BANKEX": "Bankex", "INDIA VIX": "India VIX", "NIFTY IT": "Nifty IT", "NIFTY 100": "Nifty 100", "NIFTY 200": "Nifty 200", "NIFTY 500": "Nifty 500",
                    "NIFTY MIDCAP 100": "Nifty Midcap 100", "NIFTY SMLCAP 100": "Nifty Smallcap 100", "NIFTY AUTO": "Nifty Auto", "NIFTY PHARMA": "Nifty Pharma",
                    "NIFTY FMCG": "Nifty FMCG", "NIFTY METAL": "Nifty Metal", "NIFTY ENERGY": "Nifty Energy", "NIFTY REALTY": "Nifty Realty", "NIFTY PSU BANK": "Nifty PSU Bank"}
DHAN_MCX = [("GOLD", "Gold (MCX, ₹/10g)"), ("GOLDM", "Gold Mini (MCX, ₹/10g)"), ("SILVER", "Silver (MCX, ₹/kg)"), ("SILVERM", "Silver Mini (MCX, ₹/kg)"),
            ("CRUDEOIL", "Crude Oil (MCX, ₹/bbl)"), ("NATURALGAS", "Natural Gas (MCX, ₹/MMBtu)"), ("COPPER", "Copper (MCX, ₹/kg)"), ("ZINC", "Zinc (MCX, ₹/kg)"),
            ("ALUMINIUM", "Aluminium (MCX, ₹/kg)"), ("LEAD", "Lead (MCX, ₹/kg)"), ("NICKEL", "Nickel (MCX, ₹/kg)")]
DHAN_SCRIP = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
DHAN_OHLC = "https://api.dhan.co/v2/marketfeed/ohlc"


def _f(x):
    try:
        v = float(x)
        return None if math.isnan(v) or math.isinf(v) else round(v, 4)
    except Exception:
        return None


# ----------------------------------------------------------------------------- Yahoo
def yahoo_rows(log=print) -> tuple[dict[str, list[dict]], str]:
    import yfinance as yf
    groups: dict[str, list[dict]] = {k: [] for k in YAHOO}
    tickers = [t for g in YAHOO.values() for t, _ in g]
    try:
        raw = yf.download(tickers, period="7d", interval="1d", group_by="ticker", threads=True, progress=False, auto_adjust=False)
    except Exception as e:  # noqa: BLE001
        log(f"quotes: yahoo download failed: {e}")
        return groups, f"failed: {type(e).__name__}"
    ok = 0
    for g, lst in YAHOO.items():
        for t, name in lst:
            try:
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df.dropna(subset=["Close"])
                if df.empty:
                    raise ValueError("no rows")
                last = df.iloc[-1]; prev = df.iloc[-2] if len(df) > 1 else None
                pc = _f(prev["Close"]) if prev is not None else None
                lp = _f(last["Close"])
                groups[g].append({"symbol": t, "name": name, "last": lp, "prev_close": pc, "chg": round(lp - pc, 4) if (lp is not None and pc is not None) else None,
                                  "chg_pct": round((lp / pc - 1) * 100, 2) if (lp and pc) else None, "open": _f(last["Open"]), "high": _f(last["High"]), "low": _f(last["Low"]),
                                  "time": str(df.index[-1].date()), "source": "Yahoo"})
                ok += 1
            except Exception:
                groups[g].append({"symbol": t, "name": name, "last": None, "prev_close": None, "chg": None, "chg_pct": None, "open": None, "high": None, "low": None, "time": None, "source": "Yahoo"})
    log(f"quotes: yahoo {ok}/{len(tickers)} symbols")
    return groups, "ok" if ok else "failed: no symbols"


# ----------------------------------------------------------------------------- Dhan (optional)
def dhan_rows(log=print) -> tuple[dict[str, list[dict]], str]:
    cid, tok = os.environ.get("DHAN_CLIENT_ID", "").strip(), os.environ.get("DHAN_ACCESS_TOKEN", "").strip()
    out = {"india": [], "mcx": []}
    if not cid or not tok:
        return out, "not configured"
    try:
        r = requests.get(DHAN_SCRIP, timeout=60)
        r.raise_for_status()
        sm = pd.read_csv(io.StringIO(r.text), low_memory=False)
        cols = {c.upper(): c for c in sm.columns}
        C = lambda *names: next((cols[n] for n in names if n in cols), None)  # noqa: E731
        c_exch, c_seg, c_sid, c_inst, c_und, c_sym, c_disp, c_exp = (C("EXCH_ID", "SEM_EXM_EXCH_ID"), C("SEGMENT", "SEM_SEGMENT"), C("SECURITY_ID", "SEM_SMST_SECURITY_ID"),
                                                                    C("INSTRUMENT", "SEM_INSTRUMENT_NAME"), C("UNDERLYING_SYMBOL", "SEM_TRADING_SYMBOL"), C("SYMBOL_NAME", "SM_SYMBOL_NAME"),
                                                                    C("DISPLAY_NAME", "SEM_CUSTOM_SYMBOL"), C("SM_EXPIRY_DATE", "SEM_EXPIRY_DATE"))
        if not all([c_exch, c_sid]):
            return out, "failed: unexpected scrip-master columns"
        today = datetime.now(IST).date()
        # indices
        idx_ids = {}
        idx = sm[(sm[c_exch].astype(str).str.upper().isin(["NSE", "BSE"])) & (sm[c_seg].astype(str).str.upper().isin(["I", "IDX", "INDEX"]) if c_seg else True)]
        norm = lambda x: "".join(ch for ch in str(x).upper() if ch.isalnum())  # noqa: E731
        aliases = {}
        for k, disp in DHAN_INDEX_NAMES.items():
            for a in (k, disp):
                aliases[norm(a)] = k
        aliases.update({"NIFTY50": "NIFTY", "NIFTYBANK": "BANKNIFTY", "NIFTYFINSERVICE": "FINNIFTY", "NIFTYFINANCIALSERVICES": "FINNIFTY", "NIFTYMIDSELECT": "MIDCPNIFTY",
                        "NIFTYMIDCAPSELECT": "MIDCPNIFTY", "NIFTYSMALLCAP100": "NIFTY SMLCAP 100", "NIFTYMIDCAP100": "NIFTY MIDCAP 100", "INDIAVIX": "INDIA VIX"})
        for _, row in idx.iterrows():
            for cand in ((row.get(c_sym) if c_sym else None), (row.get(c_disp) if c_disp else None), (row.get(c_und) if c_und else None)):
                k = aliases.get(norm(cand)) if cand is not None else None
                if k and k not in idx_ids:
                    idx_ids[k] = int(row[c_sid]); break
        # MCX front-month futures
        mcx_ids = {}
        if c_inst and c_und and c_exp:
            fut = sm[(sm[c_exch].astype(str).str.upper() == "MCX") & (sm[c_inst].astype(str).str.upper().str.startswith("FUTCOM"))].copy()
            fut["_exp"] = pd.to_datetime(fut[c_exp], errors="coerce").dt.date
            fut = fut[fut["_exp"] >= today].sort_values("_exp")
            for u, _ in DHAN_MCX:
                m = fut[fut[c_und].astype(str).str.upper() == u]
                if len(m):
                    mcx_ids[u] = (int(m.iloc[0][c_sid]), str(m.iloc[0]["_exp"]))
        body = {}
        if idx_ids:
            body["IDX_I"] = list(idx_ids.values())
        if mcx_ids:
            body["MCX_COMM"] = [v[0] for v in mcx_ids.values()]
        if not body:
            return out, "failed: no instruments resolved from scrip master"
        hdr = {"access-token": tok, "client-id": cid, "Content-Type": "application/json", "Accept": "application/json"}
        q = requests.post(DHAN_OHLC, headers=hdr, data=json.dumps(body), timeout=30)
        if q.status_code != 200:
            return out, f"failed: HTTP {q.status_code} {q.text[:120]}"
        data = (q.json() or {}).get("data", {})
        now = datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")

        def mk(sym, name, d, extra=None):
            lp = _f(d.get("last_price")); o = d.get("ohlc", {}) or {}
            pc = _f(o.get("close"))
            return {"symbol": sym, "name": name, "last": lp, "prev_close": pc, "chg": round(lp - pc, 4) if (lp is not None and pc) else None,
                    "chg_pct": round((lp / pc - 1) * 100, 2) if (lp and pc) else None, "open": _f(o.get("open")), "high": _f(o.get("high")), "low": _f(o.get("low")),
                    "time": now, "source": "Dhan", **(extra or {})}
        seg = data.get("IDX_I", {})
        for nm, sid in idx_ids.items():
            d = seg.get(str(sid)) or seg.get(sid)
            if d:
                out["india"].append(mk(nm, DHAN_INDEX_NAMES[nm], d))
        seg = data.get("MCX_COMM", {})
        for u, name in DHAN_MCX:
            if u in mcx_ids:
                sid, exp = mcx_ids[u]
                d = seg.get(str(sid)) or seg.get(sid)
                if d:
                    out["mcx"].append(mk(u, name, d, {"expiry": exp}))
        log(f"quotes: dhan indices {len(out['india'])} · mcx {len(out['mcx'])}")
        return out, "ok"
    except Exception as e:  # noqa: BLE001
        log(f"quotes: dhan failed: {type(e).__name__}: {e}")
        return out, f"failed: {type(e).__name__}"


# ----------------------------------------------------------------------------- merge
def build_quotes(log=print, mock: bool = False) -> dict:
    warnings = []
    if mock:
        import random
        groups = {g: [{"symbol": t, "name": n, "last": round(random.uniform(50, 80000), 2), "prev_close": None, "chg": round(random.uniform(-300, 300), 2),
                       "chg_pct": round(random.uniform(-2.5, 2.5), 2), "open": None, "high": None, "low": None, "time": "mock", "source": "MOCK"} for t, n in lst]
                  for g, lst in YAHOO.items()}
        groups["mcx"] = [{"symbol": u, "name": n, "last": round(random.uniform(200, 130000), 1), "prev_close": None, "chg": 0, "chg_pct": round(random.uniform(-2, 2), 2),
                          "open": None, "high": None, "low": None, "time": "mock", "source": "MOCK", "expiry": "2026-12-05"} for u, n in DHAN_MCX]
        return {"as_of_utc": datetime.now(timezone.utc).isoformat(), "groups": groups, "sources": {"yahoo": "mock", "dhan": "mock"}, "warnings": []}
    groups, ystat = yahoo_rows(log)
    groups["mcx"] = []
    drows, dstat = dhan_rows(log)
    if dstat == "ok":
        by_name = {r["name"]: r for r in drows["india"]}
        merged = []
        for r in groups["india"]:
            merged.append(by_name.pop(r["name"], r))
        merged.extend(by_name.values())
        groups["india"] = merged
        groups["mcx"] = drows["mcx"]
    elif dstat != "not configured":
        warnings.append(f"Dhan market quotes unavailable ({dstat}) — Indian indices shown from Yahoo (≈15 min delayed); MCX commodities not available this build.")
    if ystat != "ok":
        warnings.append(f"Yahoo market snapshot failed ({ystat}) — Live-markets tables may be empty this build.")
    return {"as_of_utc": datetime.now(timezone.utc).isoformat(), "groups": groups, "sources": {"yahoo": ystat, "dhan": dstat}, "warnings": warnings}


if __name__ == "__main__":
    t0 = time.time()
    q = build_quotes()
    print(json.dumps({k: len(v) for k, v in q["groups"].items()}), q["sources"], f"{time.time()-t0:.1f}s")
