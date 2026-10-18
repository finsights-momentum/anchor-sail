"""
News feed builder for the FINSIGHTS dashboard.

Runs after build.py on every GitHub Actions build and writes docs/news.json with four sections:
  india      — Indian market / stock news (publisher RSS feeds + Google News)
  portfolio  — news for every stock currently held in any live book (Google News, one query per company)
  world      — global market news
  regulatory — SEBI, RBI and other regulatory / policy updates

Every feed is best-effort: a feed that fails is listed in "failed" and the rest still publish.
Only the standard library + pandas are used (no feedparser dependency).
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus

import requests

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "data" / "state"
UNI_DIR = ROOT / "data" / "universe"
OUT = ROOT / "docs" / "news.json"

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8"}
TIMEOUT = 20
MAX_PER_SECTION = 80
MAX_PER_STOCK = 6
MAX_AGE_DAYS = {"india": 3, "world": 3, "regulatory": 14, "portfolio": 10}


def gnews(query: str) -> str:
    return f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-IN&gl=IN&ceid=IN:en"


FEEDS = {
    "india": [
        ("Economic Times · Markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
        ("Economic Times · Stocks", "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"),
        ("Moneycontrol · Market reports", "https://www.moneycontrol.com/rss/marketreports.xml"),
        ("Moneycontrol · Buzzing stocks", "https://www.moneycontrol.com/rss/buzzingstocks.xml"),
        ("Moneycontrol · Market outlook", "https://www.moneycontrol.com/rss/marketoutlook.xml"),
        ("Mint · Markets", "https://www.livemint.com/rss/markets"),
        ("Business Standard · Markets", "https://www.business-standard.com/rss/markets-106.rss"),
        ("CNBC-TV18 · Market", "https://www.cnbctv18.com/commonfeeds/v1/cne/rss/market.xml"),
        ("Google News · Indian markets", gnews("Nifty OR Sensex stock market when:1d")),
    ],
    "world": [
        ("CNBC · Top news", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
        ("CNBC · Finance", "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
        ("CNBC · Investing", "https://www.cnbc.com/id/15839069/device/rss/rss.html"),
        ("MarketWatch · Top stories", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
        ("MarketWatch · Market pulse", "https://feeds.content.dowjones.io/public/rss/mw_marketpulse"),
        ("BBC · Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
        ("Google News · Global markets", gnews("global markets Wall Street OR \"Asian markets\" OR \"European markets\" OR Fed OR crude OR gold when:1d")),
    ],
    "regulatory": [
        ("SEBI", "https://www.sebi.gov.in/sebirss.xml"),
        ("RBI · Press releases", "https://www.rbi.org.in/pressreleases_rss.xml"),
        ("RBI · Notifications", "https://www.rbi.org.in/notifications_rss.xml"),
        ("Google News · Regulatory", gnews("SEBI OR RBI OR \"NSE circular\" OR \"BSE circular\" OR \"Finance Ministry\" regulation OR circular OR notification when:7d")),
    ],
}


# ----------------------------------------------------------------------------- parsing
def _text(el, *tags):
    for t in tags:
        x = el.find(t)
        if x is not None and (x.text or "").strip():
            return x.text.strip()
    return ""


def _strip_html(s: str, n: int = 240) -> str:
    s = re.sub(r"<[^>]+>", " ", html.unescape(s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return (s[: n - 1] + "…") if len(s) > n else s


def _parse_date(s: str):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def parse_feed(xml_text: str, default_source: str) -> list[dict]:
    items = []
    root = ET.fromstring(xml_text)
    ns = {"atom": "http://www.w3.org/2005/Atom", "media": "http://search.yahoo.com/mrss/", "dc": "http://purl.org/dc/elements/1.1/"}
    # RSS 2.0
    for it in root.iter("item"):
        title = _text(it, "title")
        link = _text(it, "link") or (it.find("guid").text.strip() if it.find("guid") is not None and (it.find("guid").text or "").strip().startswith("http") else "")
        pub = _parse_date(_text(it, "pubDate", "published", "{http://purl.org/dc/elements/1.1/}date"))
        src_el = it.find("source")
        source = (src_el.text or "").strip() if src_el is not None and (src_el.text or "").strip() else default_source
        desc = _strip_html(_text(it, "description", "summary"))
        if title and link:
            items.append({"title": html.unescape(title), "link": link, "source": source, "published": pub.isoformat() if pub else None, "summary": desc})
    # Atom
    if not items:
        A = "{http://www.w3.org/2005/Atom}"
        for it in root.iter(A + "entry"):
            title = _text(it, A + "title")
            lk = it.find(A + "link")
            link = lk.get("href") if lk is not None else ""
            pub = _parse_date(_text(it, A + "published", A + "updated"))
            desc = _strip_html(_text(it, A + "summary", A + "content"))
            if title and link:
                items.append({"title": html.unescape(title), "link": link, "source": default_source, "published": pub.isoformat() if pub else None, "summary": desc})
    # Google News titles end with " - Publisher"; move the publisher into source
    for x in items:
        m = re.match(r"^(.*)\s+-\s+([^-]{2,60})$", x["title"])
        if m and x["source"].startswith("Google News"):
            x["title"], x["source"] = m.group(1).strip(), m.group(2).strip()
        elif m and x["source"] and x["source"] != default_source:
            x["title"] = m.group(1).strip()
    return items


def fetch(name: str, url: str, retries: int = 2) -> tuple[str, list[dict], str | None]:
    err = None
    for k in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            if r.status_code == 200 and r.text.strip():
                return name, parse_feed(r.text, name), None
            err = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            err = type(e).__name__
        time.sleep(1.5 * (k + 1))
    return name, [], err


# ----------------------------------------------------------------------------- portfolio symbols
def held_symbols() -> dict[str, dict]:
    """{symbol: {name, books:[...]}} for every stock open in any live book."""
    names = {}
    p = UNI_DIR / "company_names.json"
    if p.exists():
        try:
            names = json.loads(p.read_text())
        except Exception:
            names = {}
    held: dict[str, dict] = {}
    for f in sorted(STATE_DIR.glob("*.json")) if STATE_DIR.exists() else []:
        try:
            st = json.loads(f.read_text())
        except Exception:
            continue
        book = f.stem
        label = {"CORE": "Bharat Wealth Portfolio", "PRECISION": "Precision", "FRONTIER": "Frontier", "SPECTRUM": "Udaan"}.get(book)
        if book.startswith("MOM_"):
            parts = book.split("_")      # MOM_S1_NIFTY200
            label = {"S1": "Alpha Leaders", "S4PLUS": "Wealth Vriddhi"}.get(parts[1], parts[1]) + " · " + parts[2].replace("NIFTY", "Nifty ")
            syms = list((st.get("positions") or {}).keys())
        else:
            syms = [x["symbol"] for x in st.get("positions", []) if x.get("status") == "open"]
        for s in syms:
            d = held.setdefault(s, {"symbol": s, "name": names.get(s, s), "books": []})
            if label and label not in d["books"]:
                d["books"].append(label)
    return held


def _clean_company(name: str) -> str:
    n = re.sub(r"\b(Ltd\.?|Limited|India|Industries|Company|Corporation|Corp\.?|\(India\))\b", " ", name, flags=re.I)
    n = re.sub(r"[^\w&\s\-]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n if len(n) >= 3 else name


# ----------------------------------------------------------------------------- main
def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for x in items:
        k = re.sub(r"[^a-z0-9]+", " ", x["title"].lower()).strip()[:90]
        if k in seen:
            continue
        seen.add(k); out.append(x)
    return out


def _recent(items: list[dict], days: float) -> list[dict]:
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    out = []
    for x in items:
        if x["published"]:
            try:
                if datetime.fromisoformat(x["published"]).timestamp() < cutoff:
                    continue
            except Exception:
                pass
        out.append(x)
    out.sort(key=lambda x: x["published"] or "", reverse=True)
    return out


def main():
    t0 = time.time()
    held = held_symbols()
    jobs = []
    for sec, feeds in FEEDS.items():
        for name, url in feeds:
            jobs.append((sec, None, name, url))
    for s, d in held.items():
        q = f"\"{_clean_company(d['name'])}\" OR \"{s}\" share OR stock OR NSE when:10d"
        jobs.append(("portfolio", s, f"Google News · {s}", gnews(q)))
    sections = {"india": [], "world": [], "regulatory": []}
    per_stock: dict[str, list] = {s: [] for s in held}
    failed = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(fetch, name, url): (sec, sym, name) for sec, sym, name, url in jobs}
        for fu in as_completed(futs):
            sec, sym, name = futs[fu]
            try:
                _, items, err = fu.result()
            except Exception as e:  # noqa: BLE001
                items, err = [], type(e).__name__
            if err:
                failed.append(f"{name}: {err}")
                continue
            if sec == "portfolio":
                per_stock[sym] = _recent(_dedupe(items), MAX_AGE_DAYS["portfolio"])[:MAX_PER_STOCK]
            else:
                sections[sec].extend(items)
    out = {"generated_at_utc": datetime.now(timezone.utc).isoformat(), "sections": {}, "portfolio": [], "failed": sorted(failed)}
    for sec in ("india", "world", "regulatory"):
        out["sections"][sec] = _recent(_dedupe(sections[sec]), MAX_AGE_DAYS[sec])[:MAX_PER_SECTION]
    for s in sorted(held):
        out["portfolio"].append({**held[s], "items": per_stock.get(s, [])})
    out["runtime_sec"] = round(time.time() - t0, 1)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    print(f"news: india {len(out['sections']['india'])} · world {len(out['sections']['world'])} · regulatory {len(out['sections']['regulatory'])} · "
          f"portfolio {sum(len(p['items']) for p in out['portfolio'])} items for {len(held)} stocks · {len(failed)} feed(s) failed · {out['runtime_sec']}s")
    for f in failed:
        print("  feed failed:", f)


if __name__ == "__main__":
    sys.exit(main())
