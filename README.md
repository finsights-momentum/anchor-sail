# FINSIGHTS by Himanshu Arora — live strategy dashboard

Six model portfolios from two engines, rebuilt automatically by GitHub Actions and published on
GitHub Pages. Setup and upgrade steps: see `SETUP.md`.

| Page | Engine | Universe | Live since | Capital | Benchmark |
|---|---|---|---|---|---|
| Bharat Wealth Portfolio (CORE) | Anchor & Sail, ranking OFF | NIFTY 50 + NIFTY NEXT 50 | 31 Aug 2026 close | ₹26 L | Nifty 100 |
| Precision | Anchor & Sail, ranking ON | NIFTY MIDCAP 150 | 31 Aug 2026 close | ₹26 L | Nifty Midcap 150 |
| Frontier | Anchor & Sail, ranking OFF | NIFTY SMALLCAP 250 | 31 Aug 2026 close | ₹26 L | Nifty Smallcap 250 |
| Udaan (SPECTRUM) | Anchor & Sail, ranking ON | all three pooled (~500) | 31 Aug 2026 close | ₹26 L | Nifty 500 |
| Alpha Leaders (S1) | Momentum, fully invested | Nifty 100 / **Nifty 200** / Nifty 500 | 30 Sep 2026 close | ₹10 L each | Nifty 100 / 200 / 500 |
| Wealth Vriddhi (S4+) | Defensive momentum | Nifty 100 / **Nifty 200** / Nifty 500 | 30 Sep 2026 close | ₹10 L each | Nifty 100 / 200 / 500 |

Every book is additionally compared with the **Nifty 200 Momentum 30** index as a common
yardstick. The FINSIGHTS package's "Bharat Wealth Portfolio" and "Udaan" are the Anchor & Sail
CORE and SPECTRUM books, so they are shown under those client names rather than duplicated.

## Pages

* **Overview** — today's alerts (exits, stops within 3%, rebalances, de-risk regime), one card
  per strategy, a live-performance table for all ten books, a backtest table (CAGR, benchmark
  CAGR, Mom 30 CAGR, drawdown, Sharpe, win rate, trades, final value), CAGR bars and a
  growth-of-₹100 chart of every strategy since January 2016. Rows and cards open the strategy page.
* **One page per strategy**, each with a LIVE BOOK / BACKTEST switch. Momentum pages also have a
  Nifty 100 / 200 / 500 switch (Nifty 200 is the default and the page remembers the choice in the URL).
* The theme switch (Dark / Light) changes the palette and the data colours — each theme has its
  own set of series colours tuned to its background. Nothing else changes.

## Anchor & Sail (four books)

Monthly Williams %R(14): armed when < −40, trigger when ≥ −20 on a later completed monthly close
with EMA 5 > 15 > 50. Entry at that close, stop −15%, target +30% with a 50% scale-out, stop to
breakeven after the scale-out and then trailed to the previous month's low. 25 slots, size =
corpus / 25 (compounding), idle cash earns the Nifty 50 (^NSEI) return, daily stop monitoring.
Per page: exit signals this month, every entry signal from the last completed close (taken / no
slot), the open book with live stop levels, performance vs both benchmarks, watchlists and the
full universe table with OHLC, %R, EMAs and state. The backtest view runs the identical `Book`
from the 31 Dec 2015 close.

## Alpha Leaders (S1) and Wealth Vriddhi (S4+)

Ported from the FINSIGHTS package's `combined_backtest.py` and run live with the same parameters:

* **Score** = mean percentile rank of 12-1 month return, 6-month return and 12-month Sharpe.
* **S1 gate**: liquidity ≥ ₹5 cr/day (60-day median traded value) and price above the 200-DMA.
  Equal weight, always 100% invested.
* **S4+ gate** adds a rising 200-DMA (vs its own level 20 sessions earlier) and downside capture
  below the universe median. Exposure 50% when the Nifty 50 is below its 200-day mean, ×0.8 when
  the median 20d/252d volatility ratio exceeds 1.7. Weights = 50% inverse-volatility + 50% equal.
* Hold 20, keep an existing holding while it stays inside the top 30, at most 5 names per NSE
  sector (NSE's own "Industry" classification from the constituent file). Idle cash earns a
  liquid-fund yield of 7% p.a., accrued daily.
* **Rebalance** on the last trading session of every month at that session's close — the live
  book rebalances when the month's last session has closed (the 18:00 IST build), never during
  the running month. Between rebalances the page shows a *next-rebalance preview*: the target
  list, would-be sells and the rank 21–30 buffer as if the month closed today.
* Costs: the package's delivery charges (transaction, STT, stamp duty on buys, SEBI, GST, DP ₹20
  + GST per sell). Quantities are whole shares; a target whose single share costs more than its
  allocation is listed as "not bought" and the cash stays in the liquid fund.
* Backtests start at the first month-end of 2016 with ₹10 L; the package's published CAGR for
  each strategy/universe is shown alongside for reference. Differences come from NSE's official
  constituent lists and sectors (the package used hand-typed lists and Yahoo sectors), the
  survivorship-biased universe and whole-share quantities.

## Benchmarks

Primary: Yahoo `^CNX100`, `^CNX200`, `^CRSLDX`, `NIFTYMIDCAP150.NS`, `NIFTYSMLCAP250.NS`
(labelled ETF proxies only as a visible fallback; an official CSV in `data/benchmarks/
<PORTFOLIO>.csv` with `Date, Close` overrides). Common yardstick: the official **Nifty 200
Momentum 30** history in `data/benchmarks/NIFTY200MOM30.csv` (`Date, index_level`), extended
beyond its last date by chaining the daily returns of a tracking ETF (a warning names the ETF;
refresh the CSV from niftyindices.com now and then to remove the tracking error).

## Data notes

* Prices: Yahoo Finance via yfinance, adjusted, ~15 minutes delayed. Downloads are chunked,
  retried per symbol and reconciled against the previous run's cache; anything served from cache
  is listed in the warnings banner.
* Universes: NSE's official constituent CSVs every run (Nifty 50, Next 50, Midcap 150, Smallcap
  250, Nifty 200); if NSE is unreachable the committed snapshot in `data/universe/` is used **and
  a warning is shown**. Overlaps between the size indices are reported.
* The 15-minute intraday builds monitor stops; the 18:00 IST run is the day's final word and the
  only one that can rebalance the momentum books. Ledgers (`data/state/*.json`) are committed
  when something material happened or on the end-of-day pass; `data.json` is published to Pages
  directly and never committed.

## Files

```
engine/strategy.py        Anchor & Sail indicators, state machine, ledger
engine/momentum.py        S1 / S4+ features, gates, selection, weights, ledger, simulator
engine/momentum_build.py  runs the six momentum books (live + backtest), builds their payload
engine/build.py           orchestrator: downloads, both engines, summary, docs/data.json
engine/data.py            yfinance download / reconcile / benchmarks / Nifty 200 Mom 30
engine/universes.py       NSE constituent lists + NSE sectors + snapshot fallback
docs/index.html           the dashboard (single file)
data/state/               CORE/PRECISION/FRONTIER/SPECTRUM.json + MOM_<S1|S4PLUS>_<universe>.json
data/universe/            constituent snapshots, company_names.json, industry.json
data/benchmarks/          NIFTY200MOM30.csv (official history)
tests/                    test_engine.py (Anchor & Sail), test_momentum.py (momentum)
.github/workflows/daily_dashboard.yml   the schedule
```
