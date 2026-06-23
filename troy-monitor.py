#!/usr/bin/env python3
"""
Troy's Options War Room — Monitor
Reads troy-classes.json → auto-builds watchlist → fetches prices → updates dashboard

KEY FEATURES:
  • Driven by troy-classes.json — add a new class entry and this auto-updates everything
  • Auto-injects new stock cards into the HTML when a ticker appears in classes.json for the first time
  • Checks EYL community board for new Troy posts
  • Sends alerts via iPhone (ntfy), Mac notification, and email

SETUP (one time):
  1. iPhone: download "ntfy" app → subscribe to topic: troy-eyl-eli
  2. pip3 install yfinance requests beautifulsoup4 --break-system-packages
  3. Set env vars (or edit CONFIG below):
       EMAIL_TO            your@email.com
       EMAIL_FROM          your_gmail@gmail.com
       EMAIL_APP_PASSWORD  Gmail App Password (not your login password)
       EYL_EMAIL / EYL_PASSWORD  your EYL login
  4. Run manually: python3 ~/Documents/Troy\'s\ option\ class/troy-monitor.py

ALERT TRIGGERS:
  ① Option hits alert price     → option current ≤ alert (+5% window)       — BUY SIGNAL
  ② Option enters buy zone      → option 30–40% below alert                  — ENTER
  ③ Stock hits stop-loss        → option 30% below your alert entry           — EXIT
  ④ Take-profit signal          → option up 100%+ from alert                  — CONSIDER SELLING
  ⑤ Stock approaching buy zone  → stock 10–20% off 52W high                  — WATCH
"""

import json
import re
import os
import sys
import subprocess
import smtplib
from email.mime.text import MIMEText
from datetime import datetime

import yfinance as yf
import requests

# ─── PATHS ───────────────────────────────────────────────────────────────────
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
HTML_PATH    = os.path.join(BASE_DIR, 'troy-options-tracker.html')
CLASSES_PATH = os.path.join(BASE_DIR, 'troy-classes.json')
STATE_PATH   = os.path.join(BASE_DIR, '.troy-state.json')

# ─── CONFIG ──────────────────────────────────────────────────────────────────
NTFY_TOPIC        = "troy-eyl-eli"
NTFY_URL          = f"https://ntfy.sh/{NTFY_TOPIC}"

# Email — set as env vars or fill in directly
EMAIL_TO          = os.environ.get("EMAIL_TO",           "elijahthomas1@gmail.com")
EMAIL_FROM        = os.environ.get("EMAIL_FROM",         "")          # your Gmail address
EMAIL_APP_PASS    = os.environ.get("EMAIL_APP_PASSWORD", "")          # Gmail App Password

# Mac notifications — True = show macOS banner, requires osascript
MAC_NOTIFY        = True

EYL_EMAIL    = os.environ.get("EYL_EMAIL", "")
EYL_PASSWORD = os.environ.get("EYL_PASSWORD", "")
EYL_BOARD    = "https://eyluniversity.com/community/channels/the-investors-group"

# Alert thresholds
BUY_ZONE_LOW       = 0.20   # stock 20% off 52W high = equity watch zone starts
BUY_ZONE_HIGH      = 0.30   # stock 30% off 52W high = top of equity buy zone
OPT_ALERT_WINDOW   = 0.05   # option within 5% of alert price = ① BUY SIGNAL
OPT_BUY_ZONE_LOW   = 0.29   # option 29% below alert = ② entering buy zone
OPT_BUY_ZONE_HIGH  = 0.40   # option 40% below alert = ② deep in buy zone
OPT_STOP_LOSS      = 0.30   # option 30% below alert entry = ③ STOP LOSS
OPT_TAKE_PROFIT    = 1.00   # option 100% above alert = ④ TAKE PROFIT

MARKET_PULSE = ["SPY", "QQQ", "SMH", "VIX"]

# Colour palette for auto-injected cards (cycles by ticker hash)
CARD_COLORS = [
    "#4d9fff", "#00d4a0", "#f5c842", "#a78bfa", "#f97316",
    "#34d399", "#fb923c", "#e879f9", "#38bdf8", "#f472b6",
]


# ─── LOAD CLASSES.JSON ───────────────────────────────────────────────────────
def load_classes():
    """
    Read troy-classes.json and build WATCHLIST + OPT_CONTRACTS.
    Iterates oldest-to-newest so newer classes overwrite older data for the same ticker.
    Returns: (watchlist_dict, opt_contracts_dict, raw_json_data)
    """
    if not os.path.exists(CLASSES_PATH):
        print("⚠  troy-classes.json not found — falling back to empty config")
        return {}, {}, {}

    with open(CLASSES_PATH) as f:
        data = json.load(f)

    watchlist      = {}
    opt_contracts  = {}

    # Process class entries oldest-first (newest entry wins on conflict)
    for cls in reversed(data.get("classes", [])):
        label = cls.get("label", cls.get("date", "?"))
        for stock in cls.get("stocks", []):
            t = stock["ticker"]
            watchlist[t] = {
                "name":        stock["name"],
                "play":        stock["contract"],
                "class_date":  cls.get("date", ""),
                "class_label": label,
            }
            opt_contracts[t] = {
                "contract":    stock["contract"],
                "expiry":      stock["expiry"],
                "strike":      stock["strike"],
                "opt_type":    stock.get("opt_type", "calls"),
                "alert":       stock.get("alert"),
                "owned":       stock.get("owned", False),   # True = you actually hold this position
                "class_date":  cls.get("date", ""),
                "class_label": label,
            }

    # Watchlist-only (no dedicated class entry)
    for stock in data.get("watchlist_only", {}).get("stocks", []):
        t = stock["ticker"]
        if t not in watchlist:
            watchlist[t] = {
                "name":        stock["name"],
                "play":        stock.get("reason", stock["contract"]),
                "class_date":  None,
                "class_label": None,
            }
            opt_contracts[t] = {
                "contract":    stock["contract"],
                "expiry":      stock["expiry"],
                "strike":      stock["strike"],
                "opt_type":    stock.get("opt_type", "calls"),
                "alert":       stock.get("alert"),
                "owned":       stock.get("owned", False),
                "class_date":  None,
                "class_label": None,
            }

    print(f"  📋 Loaded {len(watchlist)} tickers from troy-classes.json "
          f"({len(data.get('classes', []))} classes + watchlist)")
    return watchlist, opt_contracts, data


# ─── HTML: AUTO-INJECT NEW STOCK CARD ────────────────────────────────────────
def inject_new_stock_card(html, ticker, info, opt_info):
    """
    If ticker has no card in the HTML yet, auto-insert one before the closing
    </div> of watchlistGrid. Returns (html, was_injected).
    """
    if f'data-ticker="{ticker}"' in html:
        return html, False

    color       = CARD_COLORS[hash(ticker) % len(CARD_COLORS)]
    name        = info.get("name", ticker)
    contract    = opt_info.get("contract", "CALLS")
    class_date  = info.get("class_label", "")
    class_badge = f'\n          <div class="class-date">📅 {class_date}</div>' if class_date else ""

    new_card = (
        f'\n        <div class="watch-card" data-ticker="{ticker}">'
        f'\n          <div class="watch-ticker" style="color:{color}">{ticker}</div>'
        f'\n          <div class="watch-name">{name}</div>'
        f'\n          <div class="watch-price" id="wp-{ticker}">--</div>'
        f'\n          <div class="watch-change" id="wc-{ticker}">--</div>'
        f'\n          <div class="watch-badge">{contract} ✓</div>'
        f'{class_badge}'
        f'\n          <div class="bz-section" id="bz-{ticker}"></div>'
        f'\n        </div>'
    )

    # Insert just before the closing tags of watchlistGrid
    marker = '      </div>\n    </div>\n\n    <!-- TROY\'S PORTFOLIO'
    if marker in html:
        html = html.replace(marker, new_card + '\n' + marker)
        return html, True

    # Fallback: append before first </div></div> after watchlistGrid
    alt = 'id="watchlistGrid"'
    if alt in html:
        idx = html.index(alt)
        end = html.index('</div>\n    </div>', idx)
        html = html[:end] + new_card + '\n      ' + html[end:]
        return html, True

    return html, False


# ─── NOTIFICATIONS ───────────────────────────────────────────────────────────
def send_push(title, body, priority="high", tags=("bell",)):
    """iPhone push via ntfy.sh — install the ntfy app and subscribe to NTFY_TOPIC."""
    try:
        # HTTP headers must be latin-1 safe; strip/replace non-ASCII chars in title
        safe_title = title.encode("ascii", errors="replace").decode("ascii")
        r = requests.post(
            NTFY_URL,
            data=body.encode("utf-8"),
            headers={
                "Title": safe_title,
                "Priority": priority,
                "Tags": ",".join(tags),
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=10,
        )
        ok = r.status_code == 200
        print(f"    📱 iPhone push: {'✓' if ok else '✗ ' + str(r.status_code)}")
        return ok
    except Exception as e:
        print(f"    📱 iPhone push failed: {e}")
        return False


def send_mac_notification(title, body):
    """macOS banner notification via osascript."""
    if not MAC_NOTIFY:
        return
    try:
        script = f'display notification "{body[:200]}" with title "{title}"'
        subprocess.run(["osascript", "-e", script], check=True,
                       capture_output=True, timeout=5)
        print(f"    🖥  Mac notification: ✓")
    except Exception as e:
        print(f"    🖥  Mac notification failed: {e}")


def send_email(title, body):
    """Email alert via Gmail SMTP. Set EMAIL_FROM and EMAIL_APP_PASS to enable."""
    if not EMAIL_FROM or not EMAIL_APP_PASS:
        print("    📧 Email skipped (EMAIL_FROM / EMAIL_APP_PASSWORD not set)")
        return
    try:
        msg = MIMEText(body, "plain")
        msg["Subject"] = f"🚨 Troy War Room: {title}"
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
            smtp.login(EMAIL_FROM, EMAIL_APP_PASS)
            smtp.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        print(f"    📧 Email sent to {EMAIL_TO}: ✓")
    except Exception as e:
        print(f"    📧 Email failed: {e}")


def notify(title, body, priority="high", tags=("bell",)):
    """Send to ALL channels: iPhone push + Mac notification + email."""
    print(f"\n  🚨 ALERT: {title}")
    send_push(title, body, priority=priority, tags=tags)
    send_mac_notification(title, body)
    send_email(title, body)


# ─── EYL COMMUNITY BOARD CHECK ───────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

# Common stock tickers to detect in post text
KNOWN_TICKERS = set([
    "NVDA","DRAM","CDNS","ANET","LLY","ARM","AMKR","MU","TSM","MRVL",
    "DELL","ORCL","VRT","NOW","HOOD","UBER","COHR","LITE","GLW","IREN",
    "AVGO","AMD","FN","MCHP","SNDK","WDC","STX","MSFT","AAPL","META",
    "GOOGL","AMZN","CRM","PLTR","SNOW","AI","SMCI","INTC","QCOM",
])

def check_eyl_board(watchlist, opt_contracts, classes_data):
    """
    Attempt to check EYL community board for new Troy M. posts.
    Requires EYL_EMAIL + EYL_PASSWORD env vars. Sends push if new class detected.
    Returns list of any newly detected tickers not yet in our watchlist.
    """
    if not EYL_EMAIL or not EYL_PASSWORD:
        print("  ℹ  EYL credentials not set — skipping community board check")
        print("     Add EYL_EMAIL and EYL_PASSWORD as GitHub secrets to enable")
        return []

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("  ⚠  beautifulsoup4 not installed — pip install beautifulsoup4")
        return []

    print("  🔍 Checking EYL community board for new Troy posts...")
    state  = load_state()
    last_seen_post = state.get("last_troy_post", "")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 Chrome/120.0",
    })

    # ── Step 1: Log in ──────────────────────────────────────────
    try:
        login_page = session.get("https://eyluniversity.com/login", timeout=15)
        soup = BeautifulSoup(login_page.text, "html.parser")

        # Look for CSRF token or form fields
        csrf_input = soup.find("input", {"name": re.compile(r"csrf|token|_token", re.I)})
        form_data  = {
            "email":    EYL_EMAIL,
            "password": EYL_PASSWORD,
        }
        if csrf_input:
            form_data[csrf_input["name"]] = csrf_input.get("value", "")

        login_r = session.post(
            "https://eyluniversity.com/login",
            data=form_data,
            timeout=15,
            allow_redirects=True,
        )
        if "logout" not in login_r.text.lower() and "dashboard" not in login_r.url:
            print("  ⚠  EYL login may have failed — check credentials")
            return []
        print("  ✓ EYL login successful")

    except Exception as e:
        print(f"  ⚠  EYL login error: {e}")
        return []

    # ── Step 2: Fetch community board ──────────────────────────
    try:
        board_r = session.get(EYL_BOARD, timeout=15)
        soup2   = BeautifulSoup(board_r.text, "html.parser")

        # Look for posts — EYL uses a modern framework, posts may be in divs/articles
        # Try common patterns
        posts = (
            soup2.find_all("article") or
            soup2.find_all("div", class_=re.compile(r"post|message|content", re.I))
        )

        if not posts:
            print("  ℹ  EYL board returned no parseable posts "
                  "(site may require JavaScript — consider Chrome extension approach)")
            return []

        new_tickers  = []
        newest_id    = last_seen_post
        new_troy_post = False

        for post in posts[:20]:  # Check most recent 20 posts
            text    = post.get_text(" ", strip=True)
            post_id = post.get("id", "") or post.get("data-id", "")

            # Skip if we've already seen this post
            if post_id and post_id == last_seen_post:
                break

            # Only care about posts by Troy M.
            author_el = post.find(class_=re.compile(r"author|name|user", re.I))
            author    = author_el.get_text(strip=True) if author_el else ""
            if "troy" not in author.lower() and "troy m" not in text.lower()[:200]:
                continue

            new_troy_post = True
            if not newest_id and post_id:
                newest_id = post_id

            # Check for class-related keywords
            class_keywords = ["class", "masterclass", "options", "leaps", "contract",
                               "strike", "expir", "call", "covered", "pick"]
            is_class_post  = any(kw in text.lower() for kw in class_keywords)

            # Extract tickers mentioned
            found = set(re.findall(r'\b([A-Z]{2,5})\b', text)) & KNOWN_TICKERS
            unknown = found - set(opt_contracts.keys())
            new_tickers.extend(unknown)

            if is_class_post or unknown:
                snippet = text[:300].replace("\n", " ")
                print(f"\n  🆕 New Troy post detected!")
                print(f"     Tickers: {found}")
                if unknown:
                    print(f"     NEW tickers not yet tracked: {unknown}")
                print(f"     Preview: {snippet}...")

                push_body = (
                    f"New Troy post detected on EYL community board.\n\n"
                    f"Tickers mentioned: {', '.join(sorted(found)) or 'none parsed'}\n"
                )
                if unknown:
                    push_body += f"⭐ NEW tickers: {', '.join(sorted(unknown))}\n"
                push_body += f"\nPreview: {snippet[:200]}...\n\nCheck the board and update troy-classes.json"
                notify("🆕 New Troy Class Alert", push_body, priority="high",
                       tags=("school", "chart_with_upwards_trend"))

        # Save the newest post ID
        if newest_id and newest_id != last_seen_post:
            state["last_troy_post"] = newest_id
            save_state(state)

        if not new_troy_post:
            print("  ✓ No new Troy posts since last check")

        return list(set(new_tickers))

    except Exception as e:
        print(f"  ⚠  EYL board check error: {e}")
        return []


# ─── DATA FETCH ──────────────────────────────────────────────────────────────
def fetch_option_price(symbol, expiry, strike, opt_type="calls"):
    """Returns dict {"mid": price, "iv": implied_vol_pct} — iv is 0-100 scale."""
    empty = {"mid": None, "iv": None}
    try:
        tk        = yf.Ticker(symbol)
        available = tk.options
        if not available:
            return empty
        target  = datetime.strptime(expiry, "%Y-%m-%d").date()
        closest = min(available, key=lambda d: abs(
            (datetime.strptime(d, "%Y-%m-%d").date() - target).days))
        chain     = tk.option_chain(closest)
        contracts = getattr(chain, opt_type, None)
        if contracts is None or contracts.empty:
            return empty
        row = contracts[contracts["strike"] == float(strike)]
        if row.empty:
            row = contracts.iloc[(contracts["strike"] - float(strike)).abs().argsort()[:1]]
        bid = float(row["bid"].iloc[0])
        ask = float(row["ask"].iloc[0])
        mid = round((bid + ask) / 2, 2)
        # Implied volatility — yfinance returns as decimal (0.28 = 28%)
        iv_pct = None
        if "impliedVolatility" in row.columns:
            try:
                iv_raw = float(row["impliedVolatility"].iloc[0])
                if iv_raw > 0:
                    iv_pct = round(iv_raw * 100, 1)
            except (ValueError, TypeError):
                pass
        return {"mid": mid, "iv": iv_pct}
    except Exception as e:
        print(f"    ⚠  {symbol} option: {e}")
        return empty

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
        return {"price": current, "change": change, "high52": high52, "pct_from_high": pct_off}
    except Exception as e:
        print(f"  ⚠  {symbol}: {e}")
        return None


# ─── HTML UPDATE ─────────────────────────────────────────────────────────────
def update_html(all_data, opt_data, watchlist, opt_contracts):
    try:
        with open(HTML_PATH) as f:
            html = f.read()

        now_str = datetime.now().strftime("%b %d, %Y ~%-I:%M %p EDT")

        # ── 1. Auto-inject any new stock cards ──────────────────
        injected = []
        for ticker, info in watchlist.items():
            opt_info = opt_contracts.get(ticker, {})
            html, was_new = inject_new_stock_card(html, ticker, info, opt_info)
            if was_new:
                injected.append(ticker)
                print(f"  ✨ Auto-injected new card: {ticker} ({info['name']})")

        if injected:
            push_body = (
                f"Auto-added {len(injected)} new stock(s) to the dashboard:\n"
                + ", ".join(injected)
                + "\n\nUpdate troy-classes.json with Troy's alert price when he announces entry."
            )
            notify("Dashboard Updated — New Stocks Added", push_body,
               priority="default", tags=("new",))

        # ── 2. Build PRICES block ────────────────────────────────
        all_tickers = list(watchlist.keys())
        lines = [
            "  // ── PRE-LOADED PRICES (updated by troy-monitor.py) ───",
            f"  // Last refreshed: {now_str}",
            "  // To update: run troy-monitor.py or trigger GitHub Action",
            "  const PRICES = {",
            "    // Market Pulse",
        ]
        for t in MARKET_PULSE:
            if t in all_data:
                d = all_data[t]
                sign = "+" if d["change"] >= 0 else ""
                lines.append(f"    {t.ljust(4)}: {{ price: {d['price']:.2f},  change: {sign}{d['change']:.2f}  }},")
        lines.append("    // Watchlist")
        for t in all_tickers:
            if t in all_data:
                d = all_data[t]
                sign = "+" if d["change"] >= 0 else ""
                lines.append(f"    {t.ljust(4)}: {{ price: {d['price']:.2f},  change: {sign}{d['change']:.2f}  }},")
        lines.append("  };")
        lines.append(f"  const PRICES_AS_OF = '{now_str}';")
        new_prices = "\n".join(lines)

        html = re.sub(
            r"  // ── PRE-LOADED PRICES.*?const PRICES_AS_OF = '[^']*';",
            new_prices, html, flags=re.DOTALL,
        )

        # ── 3. Build HIGHS_52W block ─────────────────────────────
        hlines = ["  const HIGHS_52W = {"]
        for t in all_tickers:
            if t in all_data:
                hlines.append(f"    {t.ljust(4)}: {all_data[t]['high52']:.2f},")
        hlines.append("  };")
        new_highs = "\n".join(hlines)
        html = re.sub(r"  const HIGHS_52W = \{[^}]*\};", new_highs, html, flags=re.DOTALL)

        # ── 4. Build OPT_PRICES block ────────────────────────────
        olines = [
            "  // ── OPTION CONTRACT REFERENCE PRICES ────────────────────────────────────────",
            "  // alert  = Troy's entry price  |  current = live bid/ask mid",
            "  // Troy's option buy rule: option drops 30–40% BELOW alert → entry signal",
            "  const OPT_PRICES = {",
        ]

        # Preserve existing current prices if new fetch returned null
        existing_opt = {}
        m = re.search(r"const OPT_PRICES = \{(.*?)\};", html, re.DOTALL)
        if m:
            for line in m.group(1).split("\n"):
                tm = re.search(r'(\w+)\s*:\s*\{.*?current:\s*([\d.]+)', line)
                if tm:
                    existing_opt[tm.group(1)] = float(tm.group(2))

        for t, info in opt_contracts.items():
            fetched_result = opt_data.get(t, {})
            fetched = fetched_result.get("mid") if isinstance(fetched_result, dict) else fetched_result
            current = fetched if fetched is not None else existing_opt.get(t)
            cur_str   = f"{current:.2f}" if current is not None else "null"
            alert     = info.get("alert")
            alert_str = f"{alert:.2f}" if alert is not None else "null"
            olines.append(
                f'    {t.ljust(4)}: {{ contract: "{info["contract"]}", alert: {alert_str}, current: {cur_str}  }},'
            )
        olines.append("  };")
        olines.append(f"  const OPT_PRICES_AS_OF = '{now_str}';")
        new_opt = "\n".join(olines)

        html = re.sub(
            r"  // ── OPTION CONTRACT REFERENCE PRICES.*?const OPT_PRICES_AS_OF = '[^']*';",
            new_opt, html, flags=re.DOTALL,
        )

        # ── 5. Build IV_DATA block ───────────────────────────────
        ivlines = ["  // ── IMPLIED VOLATILITY (updated by troy-monitor.py) ──────────────────────────",
                   "  // Troy's rule: IV < 35% = low risk / good entry. 35-50% = caution. >50% = avoid.",
                   "  const IV_DATA = {"]
        for t, info in opt_contracts.items():
            result = opt_data.get(t, {})
            iv = result.get("iv") if isinstance(result, dict) else None
            iv_str = f"{iv:.1f}" if iv is not None else "null"
            ivlines.append(f"    {t.ljust(4)}: {iv_str},")
        ivlines.append("  };")
        new_iv = "\n".join(ivlines)
        html = re.sub(
            r"  // ── IMPLIED VOLATILITY.*?const IV_DATA = \{[^}]*\};",
            new_iv, html, flags=re.DOTALL,
        )

        with open(HTML_PATH, "w") as f:
            f.write(html)
        print(f"  ✓ HTML updated ({now_str})")

    except Exception as e:
        print(f"  ⚠  HTML update failed: {e}")
        import traceback; traceback.print_exc()


# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    print(f"\n🔍 Troy's War Room Monitor — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    # ── Load class config ────────────────────────────────────────
    watchlist, opt_contracts, classes_data = load_classes()
    if not watchlist:
        print("⚠  No watchlist — check troy-classes.json"); sys.exit(1)

    all_data = {}
    buy_zone_hits   = []
    watch_zone_hits = []

    # ── Fetch equity prices ──────────────────────────────────────
    print("\n  Fetching stock prices...")
    for ticker, info in watchlist.items():
        print(f"  {ticker:<5}", end=" ")
        d = fetch_ticker(ticker)
        if not d:
            print("— skip"); continue
        all_data[ticker] = d
        pct = d["pct_from_high"]
        if   pct < 10:  zone = "📈 near high"
        elif pct < 20:  zone = "👀 watch zone"
        elif pct <= 30: zone = "🟠 BUY ZONE ←"
        else:           zone = "❌ below zone"
        print(f"${d['price']:>9.2f}  |  {pct:>5.1f}% off high  |  {zone}")

        pf = pct / 100
        if BUY_ZONE_LOW <= pf <= BUY_ZONE_HIGH:
            buy_zone_hits.append((ticker, d, info))
        elif 0.10 <= pf < BUY_ZONE_LOW:
            watch_zone_hits.append((ticker, d, info))

    for t in MARKET_PULSE:
        d = fetch_ticker(t)
        if d: all_data[t] = d

    # ── Fetch option prices + IV ─────────────────────────────────
    print("\n  Fetching option prices + IV...")
    opt_data       = {}   # {ticker: {"mid": price, "iv": iv_pct}}
    both_zone_hits = []
    full_entry_signals = []  # ALL 3 criteria: stock ATH% + option price in zone + IV < 35%
    for ticker, info in opt_contracts.items():
        print(f"    {ticker:<5}", end=" ")
        result = fetch_option_price(ticker, info["expiry"], info["strike"], info["opt_type"])
        opt_data[ticker] = result
        mid    = result["mid"]
        iv_pct = result["iv"]
        if mid is None:
            print("— skip"); continue
        iv_str = f"  IV:{iv_pct:.0f}%" if iv_pct is not None else ""
        iv_flag = ""
        if iv_pct is not None:
            iv_flag = " ✓low-IV" if iv_pct < 35 else (" ⚠IV" if iv_pct < 50 else " ⛔HIGH-IV")
        alert = info.get("alert")
        opt_pct_vs = None
        if alert:
            opt_pct_vs = (mid - alert) / alert * 100
            flag   = ""
            if -40 <= opt_pct_vs <= -29: flag = "🟠 OPTION BUY ZONE ←"
            elif opt_pct_vs < -40:       flag = "❌ below opt zone"
            print(f"${mid:>7.2f}  (alert ${alert})  |  {opt_pct_vs:+.1f}%  {flag}{iv_str}{iv_flag}")
            eq = all_data.get(ticker)
            if eq:
                pf = eq["pct_from_high"] / 100
                if BUY_ZONE_LOW <= pf <= BUY_ZONE_HIGH and -40 <= opt_pct_vs <= -29:
                    both_zone_hits.append((ticker, eq, info, mid, opt_pct_vs))
        else:
            print(f"${mid:>7.2f}  (no alert set){iv_str}{iv_flag}")

        # Full entry signal: ALL 3 of Troy's criteria must be met
        #   ① Stock 20-30% off 52W high
        #   ② Option 30-40% below alert price (the "undervalued contract" rule)
        #   ③ IV < 35% (low risk entry)
        eq = all_data.get(ticker)
        stock_in_zone = eq and BUY_ZONE_LOW <= eq["pct_from_high"] / 100 <= BUY_ZONE_HIGH
        opt_in_zone   = alert and opt_pct_vs is not None and -40 <= opt_pct_vs <= -29
        iv_ok         = iv_pct is not None and iv_pct < 35
        if stock_in_zone and opt_in_zone and iv_ok:
            full_entry_signals.append((ticker, eq, iv_pct, info, mid, opt_pct_vs))

    print(f"\n  → {len(buy_zone_hits)} stocks in equity buy zone")
    print(f"  → {len(both_zone_hits)} with BOTH conditions met\n")

    # ── ALERT EVALUATION ─────────────────────────────────────────
    # Run after both equity + option prices are fetched
    print("  Checking alert triggers...\n")

    alert_hits       = []   # ① option AT alert price
    opt_buy_zone     = []   # ② option 30-40% BELOW alert
    stop_loss_hits   = []   # ③ option 30%+ BELOW alert entry
    take_profit_hits = []   # ④ option 100%+ ABOVE alert

    for ticker, info in opt_contracts.items():
        alert = info.get("alert")
        if not alert:
            continue
        mid = opt_data.get(ticker, {}).get("mid")
        if mid is None:
            continue

        pct = (mid - alert) / alert   # + = above alert, - = below alert

        if -OPT_ALERT_WINDOW <= pct <= OPT_ALERT_WINDOW:
            alert_hits.append((ticker, info, mid, pct))
        elif OPT_BUY_ZONE_LOW <= -pct <= OPT_BUY_ZONE_HIGH:
            opt_buy_zone.append((ticker, info, mid, pct))
        elif -pct > OPT_STOP_LOSS and info.get("owned"):
            # ③ Stop-loss: only fire if you actually own this position
            stop_loss_hits.append((ticker, info, mid, pct))

        if pct >= OPT_TAKE_PROFIT and info.get("owned"):
            # ④ Take-profit: only fire if you actually own this position
            take_profit_hits.append((ticker, info, mid, pct))

    # ── ① Option AT alert price ───────────────────────────────────
    for ticker, info, mid, pct in alert_hits:
        stk = all_data.get(ticker, {})
        body = (
            f"{ticker} — {info['contract']}\n\n"
            f"Option price: ${mid} (alert: ${info['alert']}  |  {pct*100:+.1f}%)\n"
            f"Stock price:  ${stk.get('price','?')}  |  {stk.get('pct_from_high','?')}% off 52W high\n\n"
            f"Troy's alert price has been reached — this is the BUY target.\n"
            f"Check community board for confirmation before entering."
        )
        notify(f"① BUY SIGNAL — {ticker} at alert price!", body,
               priority="urgent", tags=("rotating_light", "chart_with_upwards_trend"))

    # ── ② Option in buy zone (30-40% below alert) ────────────────
    for ticker, info, mid, pct in opt_buy_zone:
        stk = all_data.get(ticker, {})
        body = (
            f"{ticker} — {info['contract']}\n\n"
            f"Option price: ${mid}  |  {pct*100:.1f}% below ${info['alert']} alert\n"
            f"Stock price:  ${stk.get('price','?')}  |  {stk.get('pct_from_high','?')}% off 52W high\n\n"
            f"Troy's rule: option 30–40% below alert = ENTER zone.\n"
            f"Buy zone range: ${round(info['alert']*0.71,2)}–${round(info['alert']*0.60,2)}"
        )
        notify(f"② ENTER ZONE — {ticker} option {abs(pct*100):.0f}% below alert",
               body, priority="high", tags=("bell", "chart_with_upwards_trend"))

    # ── ③ Stop-loss trigger ───────────────────────────────────────
    for ticker, info, mid, pct in stop_loss_hits:
        body = (
            f"{ticker} — {info['contract']}\n\n"
            f"Option price: ${mid}  |  {pct*100:.1f}% below ${info['alert']} entry\n\n"
            f"⛔ STOP LOSS: option is {abs(pct*100):.0f}% below alert entry — exceeds Troy's 30% stop rule.\n"
            f"Consider exiting to protect capital."
        )
        notify(f"③ STOP LOSS — {ticker} down {abs(pct*100):.0f}% from entry",
               body, priority="urgent", tags=("rotating_light", "no_entry"))

    # ── ④ Take-profit signal ──────────────────────────────────────
    for ticker, info, mid, pct in take_profit_hits:
        body = (
            f"{ticker} — {info['contract']}\n\n"
            f"Option price: ${mid}  |  +{pct*100:.0f}% above ${info['alert']} alert\n\n"
            f"🎯 Up {pct*100:.0f}% — consider taking partial or full profits.\n"
            f"Troy's pattern: sell half, let the rest ride."
        )
        notify(f"④ TAKE PROFIT — {ticker} up {pct*100:.0f}%!",
               body, priority="high", tags=("moneybag", "tada"))

    # ── ⑤ Stock approaching buy zone (watch) ─────────────────────
    if watch_zone_hits:
        lines = [f"• {t} — ${d['price']} | {d['pct_from_high']}% from 52W high"
                 for t, d, _ in watch_zone_hits]
        body = "Getting close to Troy's buy zone (10–20% off 52W high):\n\n" + "\n".join(lines)
        notify("⑤ Approaching Buy Zone", body,
               priority="default", tags=("eyes",))

    # ── Both zone hits (stock + option price both in range) ──────
    if both_zone_hits:
        lines = []
        for t, d, info, opt_mid, opt_pct in both_zone_hits:
            alert = opt_contracts[t]["alert"]
            lines.append(
                f"• {t} — Stock: ${d['price']} ({d['pct_from_high']}% off high)\n"
                f"  Option ({opt_contracts[t]['contract']}): ${opt_mid} | {opt_pct:+.1f}% vs ${alert} alert"
            )
        body = ("BOTH criteria met — stock AND option in Troy's entry zones:\n\n" +
                "\n\n".join(lines))
        notify("FULL BUY SIGNAL — both zones hit!", body,
               priority="urgent", tags=("rotating_light", "chart_with_upwards_trend"))

    # ── Full entry signal: ALL 3 criteria met ────────────────────
    if full_entry_signals:
        lines = []
        for t, d, iv, info, mid, opt_pct in full_entry_signals:
            buy_price = round(info["alert"] * 0.65, 2)  # midpoint of 30-40% off
            lines.append(
                f"• {t} — {info['contract']}\n"
                f"  Stock:  ${d['price']} ({d['pct_from_high']:.1f}% off ATH)  ✓\n"
                f"  Option: ${mid:.2f}  ({opt_pct:+.1f}% vs ${info['alert']} alert)  ✓\n"
                f"  IV:     {iv:.0f}%  (low risk)  ✓\n"
                f"  Suggested entry near: ${buy_price}"
            )
        body = (
            "ALL 3 of Troy's entry criteria are GREEN:\n"
            "  ① Stock 20-30% off 52W high\n"
            "  ② Option 30-40% below alert price\n"
            "  ③ IV < 35% (low risk)\n\n"
            + "\n\n".join(lines)
            + "\n\nVerify catalyst before entering. Check EYL community board."
        )
        notify("ENTRY SIGNAL — All 3 criteria met", body,
               priority="urgent", tags=("green_circle", "chart_with_upwards_trend"))

    # ── EYL community board check (once per day, ~market close) ──
    hour = datetime.now().hour
    if 20 <= hour <= 21:   # ~4-5 PM ET in UTC
        check_eyl_board(watchlist, opt_contracts, classes_data)

    # ── Update HTML ──────────────────────────────────────────────
    update_html(all_data, opt_data, watchlist, opt_contracts)
    print("\n✅ Done.\n")


if __name__ == "__main__":
    main()
