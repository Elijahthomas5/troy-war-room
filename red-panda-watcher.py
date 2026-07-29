#!/usr/bin/env python3
"""
Red Panda Zone Watcher — Kajabi Scraper
Logs into Red Panda Academy on Kajabi, reads Ian Dunlap's Stock Club
monthly price post, parses LTB / Swing / Entry zones for each ticker,
and updates red-panda-zones.json.

Runs via GitHub Actions (.github/workflows/rp-watcher.yml).
Requires: RP_EMAIL + RP_PASSWORD as GitHub secrets.
          pip install playwright && playwright install chromium
"""

import json
import os
import re
import sys
from datetime import date

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
ZONES_PATH  = os.path.join(BASE_DIR, 'red-panda-zones.json')

RP_EMAIL    = os.environ.get("RP_EMAIL", "")
RP_PASSWORD = os.environ.get("RP_PASSWORD", "")

KAJABI_LOGIN = "https://redpandaacademy.mykajabi.com/login"
PRICES_URL   = "https://redpandaacademy.mykajabi.com/products/red-panda-academy-stock-club-monthly/categories/2149257404/posts/2153795270"

# Label aliases the parser recognizes
LTB_ALIASES   = {"ltb", "load the boat", "load", "l.t.b", "boat", "ltb zone"}
SWING_ALIASES = {"swing", "swing trade", "swing zone", "s.t", "swing entry"}
ENTRY_ALIASES = {"entry", "quick entry", "qe", "quick", "q.e", "entry zone"}

# ─── LOAD / SAVE ZONES ───────────────────────────────────────────────────────
def load_zones():
    if not os.path.exists(ZONES_PATH):
        return {"_updated": "", "_label": "", "_note": "", "tickers": {}}
    with open(ZONES_PATH) as f:
        return json.load(f)

def save_zones(data):
    with open(ZONES_PATH, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  ✅ Saved {ZONES_PATH}")

# ─── PRICE PARSER ────────────────────────────────────────────────────────────
# Pull all stock tickers from current zones file so we know what to look for.
def get_known_tickers():
    z = load_zones()
    return set(z.get("tickers", {}).keys())

PRICE_RE   = re.compile(r'\$?([\d,]+\.?\d*)')   # matches $573.63 or 573.63
TICKER_RE  = re.compile(r'\b([A-Z]{1,5})\b')     # word-boundary uppercase


def parse_prices(raw_text: str, known_tickers: set) -> dict:
    """
    Parse free-form text into {TICKER: [ltb, swing, entry]} mapping.

    Handles multiple common formats Ian might use, e.g.:

      VOO  573.63 / 651.21 / 686.42
      AAPL - LTB $229.67  Swing $270.21  Entry $302.87
      MSFT
        Load the Boat: $319.38
        Swing: $385.04
        Entry: $396.62

    Strategy:
      1. Walk line by line.
      2. If a line starts with (or is) a known ticker → open a new ticker context.
      3. Collect dollar amounts / labelled prices into ltb/swing/entry for that context.
      4. Assign unlabelled amounts positionally (first=ltb, second=swing, third=entry).
    """
    updates = {}
    lines   = [l.strip() for l in raw_text.replace("\r", "\n").split("\n")]

    current_ticker = None
    buf = {"ltb": None, "swing": None, "entry": None, "unlabelled": []}

    def flush():
        nonlocal buf
        if not current_ticker:
            return
        # Fill from unlabelled in order: ltb → swing → entry
        for slot in ["ltb", "swing", "entry"]:
            if buf[slot] is None and buf["unlabelled"]:
                buf[slot] = buf["unlabelled"].pop(0)
        # Only save if we got at least one price
        if any(buf[k] is not None for k in ["ltb", "swing", "entry"]):
            updates[current_ticker] = [buf["ltb"], buf["swing"], buf["entry"]]
            print(f"    {current_ticker}: {updates[current_ticker]}")
        buf = {"ltb": None, "swing": None, "entry": None, "unlabelled": []}

    for line in lines:
        if not line:
            continue

        line_lower = line.lower()

        # ── Detect ticker at the start of a line ─────────────────────────────
        # e.g.  "VOO  573.63 / 651.21 / 686.42"
        #       "AAPL - LTB $229.67 ..."
        first_word = line.split()[0].strip(":-/$.,")
        if first_word in known_tickers:
            flush()
            current_ticker = first_word
            remainder = line[len(first_word):].strip(":-/ \t")
            line = remainder  # process the rest of this line normally
            line_lower = line.lower()

        if current_ticker is None:
            continue

        # ── Detect labelled prices ────────────────────────────────────────────
        def try_labelled(aliases, slot):
            for alias in aliases:
                # Match "alias : $X" or "alias $X" with optional colon/dash
                pat = rf'(?i)\b{re.escape(alias)}\b[\s:–-]*\$?([\d,]+\.?\d*)'
                m = re.search(pat, line)
                if m:
                    try:
                        val = float(m.group(1).replace(",", ""))
                        buf[slot] = val
                    except ValueError:
                        pass

        try_labelled(LTB_ALIASES,   "ltb")
        try_labelled(SWING_ALIASES, "swing")
        try_labelled(ENTRY_ALIASES, "entry")

        # ── Collect unlabelled dollar amounts on this line ───────────────────
        # Only if the line doesn't contain a label we already matched
        has_label = any(alias in line_lower
                        for aliases in [LTB_ALIASES, SWING_ALIASES, ENTRY_ALIASES]
                        for alias in aliases)

        # Extract all bare prices from the line
        prices_on_line = []
        for m in PRICE_RE.finditer(line):
            try:
                val = float(m.group(1).replace(",", ""))
                if val > 0:
                    prices_on_line.append(val)
            except ValueError:
                pass

        if not has_label:
            buf["unlabelled"].extend(prices_on_line)

    flush()
    return updates


# ─── MERGE UPDATES INTO ZONES ────────────────────────────────────────────────
def merge_updates(zones: dict, updates: dict, page_label: str) -> dict:
    changed = 0
    for ticker, prices in updates.items():
        existing = zones["tickers"].get(ticker)
        if existing is None:
            # New ticker Ian added — insert with placeholder name
            zones["tickers"][ticker] = {"name": ticker, "prices": prices}
            print(f"  ➕ New ticker added: {ticker}")
            changed += 1
        else:
            old = existing.get("prices", [None, None, None])
            if old != prices:
                existing["prices"] = prices
                print(f"  🔄 Updated {ticker}: {old} → {prices}")
                changed += 1

    today = date.today()
    zones["_updated"] = today.strftime("%Y-%m-%d")
    zones["_label"]   = page_label or f"Ian Dunlap · Stock Club · {today.strftime('%b %d, %Y')}"
    print(f"\n  {changed} ticker(s) changed.")
    return zones, changed


# ─── MAIN SCRAPER ────────────────────────────────────────────────────────────
def run():
    if not RP_EMAIL or not RP_PASSWORD:
        print("⚠  RP_EMAIL / RP_PASSWORD not set.")
        print("   GitHub → Settings → Secrets → add RP_EMAIL and RP_PASSWORD.")
        sys.exit(0)

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print("⚠  Playwright not installed. Run: pip install playwright && playwright install chromium")
        sys.exit(1)

    known_tickers = get_known_tickers()
    print(f"\n🐼 Red Panda Watcher — {date.today()}")
    print(f"   Tracking {len(known_tickers)} tickers\n")

    page_text  = ""
    page_label = ""

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()

        # ── Log into Kajabi ───────────────────────────────────────────────────
        print(f"  Logging in as {RP_EMAIL}…")
        try:
            page.goto(KAJABI_LOGIN, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            # Kajabi login form: member[email] / member[password]
            email_sel = (
                'input[name="member[email]"], '
                'input[id="member_email"], '
                'input[type="email"]'
            )
            pw_sel = (
                'input[name="member[password]"], '
                'input[id="member_password"], '
                'input[type="password"]'
            )
            submit_sel = (
                'input[type="submit"], '
                'button[type="submit"]'
            )

            page.wait_for_selector(email_sel, timeout=10000)
            page.fill(email_sel, RP_EMAIL)
            page.fill(pw_sel, RP_PASSWORD)
            page.click(submit_sel)
            page.wait_for_timeout(4000)

            if "login" in page.url or "sign_in" in page.url:
                print("  ⚠  Login may have failed — check RP_EMAIL / RP_PASSWORD secrets")
                browser.close()
                sys.exit(1)

            print(f"  ✓ Logged in → {page.url}")

        except PWTimeout:
            print("  ⚠  Login page timed out")
            browser.close()
            sys.exit(1)

        # ── Navigate to the prices post ───────────────────────────────────────
        print(f"  Navigating to prices post…")
        try:
            page.goto(PRICES_URL, wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(3000)

            # Try to get the page title / heading as the label
            try:
                heading = page.query_selector("h1, h2, .post-title, .lesson-title")
                if heading:
                    page_label = heading.inner_text().strip()
            except:
                pass

            # Extract post content — try multiple Kajabi content selectors
            content_selectors = [
                ".fr-view",                # Froala rich text editor output
                ".post-body",
                ".post-content",
                ".lesson-content",
                "article .content",
                "[data-post-content]",
                ".prose",
                "article",
                "main",
            ]
            for sel in content_selectors:
                el = page.query_selector(sel)
                if el:
                    page_text = el.inner_text()
                    print(f"  ✓ Got content via selector: {sel} ({len(page_text)} chars)")
                    break

            if not page_text:
                # Last resort: grab everything visible in main area
                page_text = page.inner_text("body")
                print(f"  ⚠  Using full body text ({len(page_text)} chars)")

        except PWTimeout:
            print("  ⚠  Price post page timed out")
            browser.close()
            sys.exit(1)

        browser.close()

    if not page_text.strip():
        print("  ⚠  No text extracted from page — aborting.")
        sys.exit(1)

    # ── Parse prices ─────────────────────────────────────────────────────────
    print(f"\n  Parsing prices from post text…")
    updates = parse_prices(page_text, known_tickers)

    if not updates:
        print("  ⚠  No ticker prices found in post. The format may have changed.")
        print("     First 500 chars of page text:")
        print("     " + page_text[:500].replace("\n", "\n     "))
        sys.exit(0)

    print(f"\n  Parsed {len(updates)} ticker(s) from post.")

    # ── Merge and save ────────────────────────────────────────────────────────
    zones = load_zones()
    zones, changed = merge_updates(zones, updates, page_label)

    if changed > 0:
        save_zones(zones)
        print(f"\n✅ red-panda-zones.json updated ({changed} change(s)).")
    else:
        print("\n✓ No price changes — zones already up to date.")

    sys.exit(0)


if __name__ == "__main__":
    run()
