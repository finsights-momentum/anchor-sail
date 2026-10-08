# FINSIGHTS dashboard — setup and upgrades (no coding)

> **Already running an earlier version?** Jump to **Upgrading an existing dashboard** at the end.

After this setup the dashboard rebuilds itself on GitHub's servers every trading hour and
every evening. You never run anything again — you just open the link.

> **Why GitHub?** A browser page cannot fetch prices on its own (Dhan and Yahoo both block
> browser calls). GitHub Actions is a free scheduler that runs the Python engine and publishes
> the result; GitHub Pages hosts the page at a permanent URL.

---

## Step 1 — Create a GitHub account (skip if you have one)

1. Go to <https://github.com/signup> and create an account (free plan).
2. Verify the email address GitHub sends you.

## Step 2 — Create the repository

1. Click the **+** at the top-right → **New repository**.
2. Repository name: `anchor-sail`
3. Choose **Public** (GitHub Pages is free only on public repositories; the URL is not listed
   anywhere, but anyone who has the link can open it. If you want it private, GitHub Pro at
   about $4/month allows Pages on private repositories.)
4. Tick **Add a README file**.
5. Click **Create repository**.

## Step 3 — Upload the dashboard files

1. Unzip `anchor-sail-v4.zip` on your computer.
2. In the repository page click **Add file** → **Upload files**.
3. Drag these folders and files from the unzipped folder into the upload area:
   `engine`, `data`, `docs`, `tests`, `requirements.txt`, `README.md`, `SETUP.md`
   (drag the folders themselves — GitHub keeps the folder structure; `data` contains the
   `universe` snapshots and the `benchmarks` folder with the Nifty 200 Momentum 30 history).
4. Scroll down, click **Commit changes**.

## Step 4 — Add the scheduler file (the one hidden folder)

Finder hides folders that start with a dot, so this one is created by paste:

1. Click **Add file** → **Create new file**.
2. In the file-name box type exactly: `.github/workflows/daily_dashboard.yml`
   (typing the `/` automatically creates the folders).
3. Open `daily_dashboard.yml` from the unzipped folder (inside `.github/workflows/`) in
   TextEdit, select all, copy, and paste into the big editor box on GitHub.
4. Click **Commit changes**.

## Step 5 — Allow the scheduler to save data

1. **Settings** (tab at the top of the repository) → left menu **Actions** → **General**.
2. Scroll to **Workflow permissions** → choose **Read and write permissions** → **Save**.

## Step 6 — Turn on the web page

1. **Settings** → left menu **Pages**.
2. Under **Build and deployment** → **Source**: choose **GitHub Actions** (the workflow publishes
   the dashboard directly after every build; nothing else to configure).
3. Your URL is `https://<account>.github.io/anchor-sail/` — bookmark it (also works on your phone).
   To have the company name in the address, create a free GitHub **Organization** (e.g.
   `finsights-momentum`) and transfer the repository into it: repository **Settings → General →
   Danger Zone → Transfer ownership**. Then repeat Steps 5–6 inside the new location.

## Step 7 — First run

1. Click the **Actions** tab → **Anchor & Sail daily dashboard** (left) → **Run workflow** →
   green **Run workflow** button.
2. Wait 4–8 minutes (first run downloads ~10 years of prices for ~500 stocks).
   A green tick means success; click the run to see the log summary.
3. Open your Pages URL. If it still says "No data yet", wait one more minute (Pages
   republishes after each data update) and reload.

From now on it runs automatically: every 15 minutes 09:30–15:45 IST on trading days and a
final pass at 18:00 IST every day (GitHub's scheduler can be 5–20 minutes late). Opening the URL always shows the latest build;
the page also refreshes itself every 5 minutes while open.

---

## Optional: official Midcap 150 / Smallcap 250 benchmark files

Yahoo carries `NIFTYMIDCAP150.NS` and `NIFTYSMLCAP250.NS`; if either disappears the engine
falls back to a labelled ETF proxy and shows a warning. To force the official series, download
the historical CSV from niftyindices.com (Reports → Historical Data → index → date range) and
upload it as `data/benchmarks/PRECISION.csv` or `data/benchmarks/FRONTIER.csv`
(columns `Date` and `Close`). The engine uses the CSV whenever it is present.

## If a run fails

Actions → click the red run → read the summary. The most common cause is Yahoo being slow;
the next scheduled run simply retries and the dashboard keeps showing the last good data.
Nothing in the ledger is lost — the open books live in `data/state/*.json` and are committed
after every end-of-day run or material change.

## Upgrading an existing dashboard (v3 → v4: FINSIGHTS, momentum strategies)

Nothing already running is lost — the four Anchor & Sail ledgers in `data/state/` stay as they
are. Uploading a file with the same name replaces the old copy.

1. Unzip `anchor-sail-v4.zip`.
2. Repository page → **Add file** → **Upload files**. Drag in the folders `engine`, `docs`,
   `tests`, the `data` folder (it only contains `universe/` and `benchmarks/` — the new Nifty 200
   list, the NSE sector file and the Nifty 200 Momentum 30 history; your ledgers are not touched)
   and the files `README.md`, `SETUP.md`. Click **Commit changes**.
3. The scheduler file does not need to change for v4.
4. **Actions** → **Anchor & Sail daily dashboard** → **Run workflow** → **Run workflow**.
   The first v4 run takes 8–12 minutes: it downloads prices for the Nifty 200 list and runs six
   momentum backtests from 2016 plus the live books from the 30 Sep 2026 close. Later runs are
   as fast as before because the price cache is reused.
5. Open your dashboard URL and hard-refresh once (Ctrl+F5 / Cmd+Shift+R) so the browser drops the
   old page. The Overview is the new landing page; each strategy has its own page in the top bar.

What to expect on the first v4 build:

* Warnings naming the ETF used to extend the Nifty 200 Momentum 30 series past 18 Sep 2026 — this
  is expected; the index itself is not on Yahoo. Download a fresh history from niftyindices.com
  (Reports → Historical Data → Nifty 200 Momentum 30) and upload it as
  `data/benchmarks/NIFTY200MOM30.csv` (columns `Date`, `index_level`) whenever you want the exact figures.
* The Alpha Leaders / Wealth Vriddhi backtest CAGRs differ from the FINSIGHTS package figures,
  which are shown next to them as "published": this build uses NSE's official constituents and
  sector classification instead of the package's hand-typed lists.
* If NSE's constituent files were not reachable, the usual "using committed snapshot" warnings appear.

## Changing the strategy parameters

Do not — the brief says the strategy must not change. Everything is in `engine/strategy.py`
(parameters at the top), `engine/momentum.py` (momentum parameters at the top) and `engine/build.py`
(`INCEPTION_MONTH`, `MOM_INCEPTION`, portfolio table). If a
change is ever agreed, edit the file on GitHub (pencil icon) and the next run picks it up.
