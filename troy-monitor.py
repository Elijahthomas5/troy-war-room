#!/usr/bin/env python3
"""
Troy's Options War Room — Daily Monitor
Checks buy zones against 52-week highs and sends iPhone push via ntfy.sh

SETUP (one time):
  1. iPhone App Store → download "ntfy" (free)
  2. In the ntfy app, tap + and subscribe to topic: troy-eyl-eli
  3. pip3 install yfinance requests --break-system-packages
  4. Run manually: python3 ~/Documents/Troy\'s\ option\ class/troy-monitor.py
  5. For daily auto-run: see troy-monitor.plist instructions below

Troy's Buy Rules:
  - Stock drops 20–30% from recent high  →  look at options
  - Options are 30–40% cheaper than recent price  →  ENTER
  - Option drops 30% from your entry  →  STOP LOSS EXIT
"""

import yfinance as yf
import requests
import re
import os
from datetime import datetime

# ─── CONFIG ──────────────────────────────────────────────────────────────────
NTFY_TOPIC = "troy-eyl-eli"          # ← Subscribe to this in the ntfy iPhone app
NTFY_URL   = f"https://ntfy.sh/{NTFY_TOPIC}"
HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'troy-options-tracker.html')

# ─── WATCHLIST ────────────────────────────────────────────────────────────────
WATCHLIST = {
    # AI Chips / Memory
    "NVDA": {"name": "NVIDIA Corp",          "play": "LEAPS CALLS"},
    "DRAM": {"name": "Roundhill Memory ETF", "play": "LEAPS"},
    "CDNS": {"name": "Cadence Design",       "play": "CALLS"},
    "ANET": {"name": "Arista Networks",      "play": "JAN '27 $110C"},
    "TSM":  {"name": "Taiwan Semi",          "play": "CALLS"},
    "AMKR": {"name": "Amkor Technology",     "play": "CALLS"},
    "MRVL": {"name": "Marvell Technology",   "play": "LEAPS"},
    # AI Infrastructure
    "ARM":  {"name": "ARM Holdings",         "play": "DEC '26 $270C"},
    "DELL": {"name": "Dell Technologies",    "play": "AI SERVER PLAY"},
    "ORCL": {"name": "Oracle",               "play": "CALLS"},
    "VRT":  {"name": "Vertiv Holdings",      "play": "CALLS"},
    "NOW":  {"name": "ServiceNow",           "play": "CALLS"},
    # Other
    "HOOD": {"name": "Robinhood Markets",    "play": "$90/$95 CALLS"},
    "LLY":  {"name": "Eli Lilly",            "play": "MAR '27 $860C"},
    "UBER": {"name": "Uber Technologies",    "play": "WATCHING"},
    # Photonics — NVIDIA supply chain
    "COHR": {"name": "Coherent Corp",        "play": "NVDA $2B INVEST"},
    "LITE": {"name": "Lumentum Holdings",    "play": "NVDA $2B INVEST"},
    "GLW":  {"name": "Corning Inc",          "play": "NVDA $3.2B INVEST"},
    "IREN": {"name": "IREN Limited",         "play": "AI DATA CENTER"},
}
MARKET_PULSE = ["SPY", "QQQ", "SMH", "VIX"]

BUY_ZONE_LOW  = 0.20   # 20% below 52W high = enter buy zone
BUY_ZONE_HIGH = 0.30   # 30% below 52W high = bottom of buy zone

# ─── OPTION CONTRACTS ─────────────────────────────────────────────────────────
# Troy's known contracts from 5/14/26 EYL class
# alert = Troy's entry alert price; expiry = ISO date of expiration
# Troy's option buy rule: option is 30–40% BELOW alert price → enter
OPT_BUY_LOW  = 0.29   # -29% from alert = entering option buy zone
OPT_BUY_HIGH = 0.40   # -40% from alert = bottom of option buy zone

OPT_CONTRACTS = {
    # ── Troy's confirmed picks (alert = his entry price from 5/14/26 EYL class) ──
    "NVDA": {"contract": "Mar '27 $180C", "expiry": "2027-03-19", "strike": 180.0, "opt_type": "calls", "alert": 36.67},
    "DRAM": {"contract": "Jun '27 $33C",  "expiry": "2027-06-18", "strike": 33.0,  "opt_type": "calls", "alert": 12.61},
    "CDNS": {"contract": "Jun '26 $310C", "expiry": "2026-06-20", "strike": 310.0, "opt_type": "calls", "alert": 23.01},
    "ANET": {"contract": "Jan '27 $110C", "expiry": "2027-01-15", "strike": 110.0, "opt_type": "calls", "alert": 27.67},
    "LLY":  {"contract": "Mar '27 $860C", "expiry": "2027-03-19", "strike": 860.0, "opt_type": "calls", "alert": 130.98},
    "ARM":  {"contract": "Dec '26 $270C", "expiry": "2026-12-18", "strike": 270.0, "opt_type": "calls", "alert": None},
    "HOOD": {"contract": "Sep '26 $90C",  "expiry": "2026-09-18", "strike": 90.0,  "opt_type": "calls", "alert": None},
    # ── ATM LEAPS trackers (no alert yet — update when Troy announces entry) ──
    "TSM":  {"contract": "Jan '27 $460C", "expiry": "2027-01-15", "strike": 460.0, "opt_type": "calls", "alert": None},
    "AMKR": {"contract": "Jan '27 $90C",  "expiry": "2027-01-15", "strike": 90.0,  "opt_type": "calls", "alert": None},
    "MRVL": {"contract": "Jan '27 $325C", "expiry": "2027-01-15", "strike": 325.0, "opt_type": "calls", "alert": None},
    "DELL": {"contract": "Jan '27 $430C", "expiry": "2027-01-15", "strike": 430.0, "opt_type": "calls", "alert": None},
    "ORCL": {"contract": "Jan '27 $190C", "expiry": "2027-01-15", "strike": 190.0, "opt_type": "calls", "alert": None},
    "VRT":  {"contract": "Jan '27 $335C", "expiry": "2027-01-15", "strike": 335.0, "opt_type": "calls", "alert": None},
    "NOW":  {"contract": "Jan '27 $100C", "expiry": "2027-01-15", "strike": 100.0, "opt_type": "calls", "alert": None},
    "UBER": {"contract": "Jan '27 $75C",  "expiry": "2027-01-15", "strike": 75.0,  "opt_type": "calls", "alert": None},
    "COHR": {"contract": "Jan '27 $395C", "expiry": "2027-01-15", "strike": 395.0, "opt_type": "calls", "alert": None},
    "LITE": {"contract": "Jan '27 $845C", "expiry": "2027-01-15", "strike": 845.0, "opt_type": "calls", "alert": None},
    "GLW":  {"contract": "Jan '27 $195C", "expiry": "2027-01-15", "strike": 195.0, "opt_type": "calls", "alert": None},
    "IREN": {"contract": "Jan '27 $60C",  "expiry": "2027-01-15", "strike": 60.0,  "opt_type": "calls", "alert": None},
}

# ─── NOTIFICATIONS ───────────────────────────────────────────────────────────
def send_push(title, body, priority="high", tags=("bell",)):
    try:
        r = requests.post(
            NTFY_URL,
            data=body.encode("utf-8"),
            headers={
                "Title":    title,
                "Priority": priority,
                "Tags":     ",".join(tags),
            },
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"  ⚠  Push failed: {e}")
        return False

# ─── DATA FETCH ──────────────────────────────────────────────────────────────
def fetch_option_price(symbol, expiry, strike, opt_type="calls"):
    """Fetch the bid/ask midpoint for a specific option contract via yfinance.
    Returns the midpoint price, or None on failure."""
    try:
        tk = yf.Ticker(symbol)
        # Get available expiry dates and find the closest match
        available = tk.options
        if not available:
            print(f"    ⚠  {symbol}: no option dates available")
            return None
        # Find nearest matching expiry
        target = datetime.strptime(expiry, "%Y-%m-%d").date()
        closest = min(available, key=lambda d: abs((datetime.strptime(d, "%Y-%m-%d").date() - target).days))
        chain = tk.option_chain(closest)
        contracts = getattr(chain, opt_type, None)
        if contracts is None or contracts.empty:
            return None
        row = contracts[contracts["strike"] == float(strike)]
        if row.empty:
            # Try nearest strike
            row = contracts.iloc[(contracts["strike"] - float(strike)).abs().argsort()[:1]]
        bid = float(row["bid"].iloc[0])
        ask = float(row["ask"].iloc[0])
        mid = round((bid + ask) / 2, 2)
        return mid
    except Exception as e:
        print(f"    ⚠  {symbol} option fetch: {e}")
        return None

def fetch_ticker(symbol):
    try:
        hist = yf.Ticker(symbol).history(period="1y")
        if hist.empty:
            return None
        current = round(float(hist["Close"].iloc[-1]), 2)
        prev    = round(float(hist["Close"].iloc[-2]), 2)
        high52  = round(float(hist["High"].max()), 2)
        change  = round((current - prev) / prev * 100, 2)
        pct_off = round((high52 - current) / high52 * 100, 1)
        return {
            "price":         current,
            "change":        change,
            "high52":        high52,
            "pct_from_high": pct_off,
        }
    except Exception as e:
        print(f"  ⚠  {symbol}: {e}")
        return None

# ─── HTML UPDATE ─────────────────────────────────────────────────────────────
def update_html(all_data, opt_data=None):
    """Patch PRICES, PRICES_AS_OF, HIGHS_52W, and OPT_PRICES blocks in the tracker HTML."""
    try:
        with open(HTML_PATH, "r") as f:
            html = f.read()

        now_str = datetime.now().strftime("%b %d, %Y ~%-I:%M %p EDT")

        # --- build PRICES block ---
        sections = [
            ("Market Pulse",   MARKET_PULSE),
            ("Core Watchlist", ["AMKR", "ARM", "DRAM", "HOOD", "NOW"]),
            ("AI / Semis",     ["NVDA", "TSM", "CDNS", "ANET", "MRVL", "DELL", "ORCL", "VRT"]),
            ("Other Picks",    ["LLY", "UBER"]),
            ("Photonics",      ["COHR", "LITE", "GLW", "IREN"]),
        ]
        lines = [
            "  // ── PRE-LOADED PRICES (updated by troy-monitor.py) ───",
            f"  // Last refreshed: {now_str}",
            "  // To update manually: ask Claude to refresh, or run troy-monitor.py",
            "  const PRICES = {",
        ]
        for label, tickers in sections:
            lines.append(f"    // {label}")
            for t in tickers:
                if t in all_data:
                    d = all_data[t]
                    sign = "+" if d["change"] >= 0 else ""
                    lines.append(
                        f"    {t.ljust(4)}: {{ price: {d['price']:.2f},  change: {sign}{d['change']:.2f}  }},"
                    )
        lines.append("  };")
        lines.append(f"  const PRICES_AS_OF = '{now_str}';")
        new_prices_block = "\n".join(lines)

        # replace old PRICES block
        html = re.sub(
            r"  // ── PRE-LOADED PRICES.*?const PRICES_AS_OF = '[^']*';",
            new_prices_block,
            html,
            flags=re.DOTALL,
        )

        # --- build HIGHS_52W block ---
        hlines = ["  const HIGHS_52W = {"]
        for t in list(WATCHLIST.keys()):
            if t in all_data and "high52" in all_data[t]:
                hlines.append(f"    {t.ljust(4)}: {all_data[t]['high52']:.2f},")
        hlines.append("  };")
        new_highs = "\n".join(hlines)

        if "const HIGHS_52W" in html:
            html = re.sub(
                r"  const HIGHS_52W = \{[^}]*\};",
                new_highs,
                html,
                flags=re.DOTALL,
            )
        else:
            html = html.replace(
                "  function refreshPrices()",
                new_highs + "\n\n  function refreshPrices()",
            )

        # --- build OPT_PRICES block ---
        if opt_data:
            olines = [
                "  // ── OPTION CONTRACT REFERENCE PRICES ────────────────────────────────────────",
                f"  // alert  = Troy's entry/alert price from 5/14/26 EYL class",
                f"  // current = live bid/ask mid — updated daily by troy-monitor.py",
                f"  // Troy's option buy rule: option drops 30–40% BELOW alert → entry signal",
                "  const OPT_PRICES = {",
            ]
            for t, info in OPT_CONTRACTS.items():
                current = opt_data.get(t)
                alert   = info["alert"]
                contract = info["contract"]
                if current is not None:
                    cur_str = f"{current:.2f}"
                else:
                    # Preserve existing value — don't overwrite with null
                    cur_str = "null"
                alert_str = f"{alert:.2f}" if alert is not None else "null"
                olines.append(
                    f"    {t.ljust(4)}: {{ contract: \"{contract}\", alert: {alert_str}, current: {cur_str}  }},"
                )
            olines.append("  };")
            olines.append(f"  const OPT_PRICES_AS_OF = '{now_str}';")
            new_opt_block = "\n".join(olines)

            html = re.sub(
                r"  // ── OPTION CONTRACT REFERENCE PRICES.*?const OPT_PRICES_AS_OF = '[^']*';",
                new_opt_block,
                html,
                flags=re.DOTALL,
            )

        with open(HTML_PATH, "w") as f:
            f.write(html)
        print(f"  ✓ HTML updated ({now_str})")
    except Exception as e:
        print(f"  ⚠  HTML update failed: {e}")

# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    print(f"\n🔍 Troy's War Room Monitor — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    all_data        = {}
    buy_zone_hits   = []
    watch_zone_hits = []

    # ── Fetch equity prices ──────────────────────────────────────
    for ticker, info in WATCHLIST.items():
        print(f"  {ticker:<5}", end=" ")
        d = fetch_ticker(ticker)
        if not d:
            print("— skip")
            continue
        all_data[ticker] = d
        pct = d["pct_from_high"]
        if   pct < 10:  zone = "📈 near high"
        elif pct < 20:  zone = "👀 watch zone"
        elif pct <= 30: zone = "🟠 BUY ZONE ←"
        else:           zone = "❌ below zone"
        print(f"${d['price']:>9.2f}  |  {pct:>5.1f}% off 52W high  |  {zone}")

        pct_frac = pct / 100
        if BUY_ZONE_LOW <= pct_frac <= BUY_ZONE_HIGH:
            buy_zone_hits.append((ticker, d, info))
        elif 0.10 <= pct_frac < BUY_ZONE_LOW:
            watch_zone_hits.append((ticker, d, info))

    # Fetch market pulse tickers
    for t in MARKET_PULSE:
        d = fetch_ticker(t)
        if d:
            all_data[t] = d

    # ── Fetch option prices ──────────────────────────────────────
    print("\n  Fetching option prices...")
    opt_data = {}
    both_zone_hits = []  # tickers where BOTH stock AND option are in buy zones
    for ticker, info in OPT_CONTRACTS.items():
        print(f"    {ticker:<5}", end=" ")
        mid = fetch_option_price(ticker, info["expiry"], info["strike"], info["opt_type"])
        opt_data[ticker] = mid
        if mid is None:
            print("— skip")
            continue
        alert = info["alert"]
        if alert:
            pct_vs_alert = (mid - alert) / alert * 100
            flag = ""
            if -40 <= pct_vs_alert <= -29:
                flag = "🟠 OPTION BUY ZONE ←"
            elif pct_vs_alert < -40:
                flag = "❌ below opt zone"
            print(f"${mid:>7.2f}  (alert ${alert})  |  {pct_vs_alert:+.1f}% vs alert  {flag}")
            # Check BOTH conditions
            eq = all_data.get(ticker)
            if eq:
                pct_off_high = eq["pct_from_high"] / 100
                stock_in_zone = BUY_ZONE_LOW <= pct_off_high <= BUY_ZONE_HIGH
                opt_in_zone   = -40 <= pct_vs_alert <= -29
                if stock_in_zone and opt_in_zone:
                    both_zone_hits.append((ticker, eq, info, mid, pct_vs_alert))
        else:
            print(f"${mid:>7.2f}  (no alert price set)")

    print(f"\n  → {len(buy_zone_hits)} stocks in equity buy zone")
    print(f"  → {len(both_zone_hits)} with BOTH stock + option in zone\n")

    # ── Push: BOTH conditions triggered (highest priority) ──────
    if both_zone_hits:
        lines = []
        for t, d, info, opt_mid, opt_pct in both_zone_hits:
            alert = OPT_CONTRACTS[t]["alert"]
            buy_hi = round(d["high52"] * 0.80, 2)
            buy_lo = round(d["high52"] * 0.70, 2)
            lines.append(
                f"• {t} ({info['name']})\n"
                f"  Stock: ${d['price']} | {d['pct_from_high']}% off high\n"
                f"  Option ({OPT_CONTRACTS[t]['contract']}): ${opt_mid} | {opt_pct:+.1f}% vs ${alert} alert\n"
                f"  Buy zone: ${buy_hi}–${buy_lo}"
            )
        body = (
            "BOTH criteria met — this is Troy's ENTER signal:\n\n"
            + "\n\n".join(lines)
            + "\n\nStock 20-30% off high ✓ + Option 30-40% cheaper than alert ✓"
        )
        ok = send_push("FULL BUY SIGNAL - Troy War Room", body, priority="urgent",
                       tags=("rotating_light", "chart_with_upwards_trend"))
        print(f"  Push (FULL SIGNAL): {'✓ sent' if ok else '✗ failed'}")

    # ── Push: Stock only in buy zone (equity signal, check options manually) ──
    elif buy_zone_hits:
        lines = []
        for t, d, info in buy_zone_hits:
            buy_hi = round(d["high52"] * 0.80, 2)
            buy_lo = round(d["high52"] * 0.70, 2)
            opt_line = ""
            opt_mid = opt_data.get(t)
            if opt_mid and OPT_CONTRACTS.get(t, {}).get("alert"):
                alert = OPT_CONTRACTS[t]["alert"]
                pct = (opt_mid - alert) / alert * 100
                opt_line = f"\n  Option: ${opt_mid} | {pct:+.1f}% vs ${alert} alert"
            lines.append(
                f"• {t} ({info['name']})\n"
                f"  Stock: ${d['price']} | {d['pct_from_high']}% off 52W high"
                + opt_line
            )
        body = (
            "Stock in Troy's 20-30% window. Check if option is 30-40% cheaper:\n\n"
            + "\n\n".join(lines)
        )
        ok = send_push("Troy Buy Zone Alert - Stock", body, priority="high",
                       tags=("bell", "chart_with_upwards_trend"))
        print(f"  Push (stock zone): {'✓ sent' if ok else '✗ failed'}")

    # ── Push: Approaching (watch zone) ──────────────────────────
    if watch_zone_hits:
        lines = [
            f"• {t} — ${d['price']} | {d['pct_from_high']}% from high | {info['play']}"
            for t, d, info in watch_zone_hits
        ]
        body = "Getting close to Troy's 20% entry window:\n\n" + "\n".join(lines)
        ok = send_push("Approaching Buy Zone", body, priority="default", tags=("eyes",))
        print(f"  Push (watch):      {'✓ sent' if ok else '✗ failed'}")

    # ── No alerts ────────────────────────────────────────────────
    if not buy_zone_hits and not watch_zone_hits:
        print("  No alerts. All stocks near highs — stay patient.")

    # ── Update HTML ──────────────────────────────────────────────
    update_html(all_data, opt_data)
    print("\n✅ Done.\n")


if __name__ == "__main__":
    main()
