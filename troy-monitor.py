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
  2. pip3 install requests beautifulsoup4 --break-system-packages
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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# All user-facing timestamps must be Eastern time, not the host clock.
# GitHub Actions runners default to UTC — datetime.now() without a tz
# silently produced UTC times mislabeled "EDT" (4hr off during DST).
ET = ZoneInfo("America/New_York")

# ─── SCHEDULED TOUCHPOINTS (self-correcting for DST) ─────────────────────────
# monitor.yml's cron fires twice per touchpoint (once for EDT, once for EST)
# since cron is UTC-only and can't shift for daylight saving on its own.
# This list is the real source of truth: zoneinfo knows the actual US DST
# transition dates, so whichever firing lands outside the tolerance window
# below is skipped — no manual cron edits needed when clocks change.
TOUCHPOINTS_ET = [
    (9, 30),   # market open
    (10, 30),
    (11, 30),
    (12, 30),
    (13, 30),
    (14, 30),
    (15, 30),
    (16, 0),   # market close
]
TOUCHPOINT_TOLERANCE_MIN = 25   # GitHub Actions can run up to ~20 min late; "wrong season" crons are 60 min off so 25 is still safe


def is_scheduled_touchpoint(now_et=None):
    """True if now_et (default: current time) falls within TOUCHPOINT_TOLERANCE_MIN
    minutes of one of TOUCHPOINTS_ET. Used to no-op the "wrong season" cron
    firing (see monitor.yml)."""
    now_et = now_et or datetime.now(ET)
    for h, m in TOUCHPOINTS_ET:
        target = now_et.replace(hour=h, minute=m, second=0, microsecond=0)
        if abs((now_et - target).total_seconds()) <= TOUCHPOINT_TOLERANCE_MIN * 60:
            return True
    return False

# ─── SCHWAB CLIENT ───────────────────────────────────────────────────────────
# schwab-py handles OAuth token refresh automatically once authenticated.
# Run schwab_auth.py once to set up the token, then this loads it every run.
_schwab_client = None

def get_schwab_client():
    """
    Returns an authenticated Schwab client, or None if not set up.
    Token is auto-refreshed by schwab-py — no manual intervention needed.
    """
    global _schwab_client
    if _schwab_client is not None:
        return _schwab_client

    creds_path = os.path.join(BASE_DIR, ".schwab_creds.json")
    if not os.path.exists(creds_path):
        return None

    try:
        with open(creds_path) as f:
            creds = json.load(f)
        token_path = creds.get("token_path", os.path.join(BASE_DIR, ".schwab_token.json"))
        if not os.path.exists(token_path):
            return None

        import schwab
        _schwab_client = schwab.auth.client_from_token_file(
            token_path=token_path,
            api_key=creds["client_id"],
            app_secret=creds["client_secret"],
        )
        return _schwab_client
    except Exception as e:
        print(f"  ⚠  Schwab client init failed: {e}")
        return None

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
_NTFY_PRIORITY = {"urgent": 5, "high": 4, "default": 3, "low": 2, "min": 1}

def send_push(title, body, priority="high", tags=("bell",)):
    """iPhone push via ntfy.sh — install the ntfy app and subscribe to NTFY_TOPIC.
    Uses JSON body so UTF-8 titles (emoji, ①②③④⑤ etc.) come through correctly."""
    try:
        r = requests.post(
            NTFY_URL,
            json={
                "title":    title,
                "message":  body,
                "priority": _NTFY_PRIORITY.get(priority, 3),
                "tags":     list(tags),
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

# ── Schwab quote helpers ──────────────────────────────────────────────────────

def _schwab_quote(symbols):
    """
    Fetch live quotes from Schwab for a list of symbols.
    Returns dict: {symbol: {"price", "change", "high52", "pct_from_high"}} or {}
    """
    client = get_schwab_client()
    if client is None:
        return {}
    try:
        r = client.get_quotes(symbols)
        if not r.ok:
            print(f"  ⚠  Schwab quotes HTTP {r.status_code}")
            return {}
        data = r.json()
        out = {}
        for sym, payload in data.items():
            q = payload.get("quote", {})
            rf = payload.get("reference", {})
            current = q.get("lastPrice") or q.get("mark") or q.get("closePrice")
            prev    = q.get("closePrice") or current
            high52  = q.get("52WkHigh") or rf.get("52WeekHigh") or current
            if current is None:
                continue
            current = round(float(current), 2)
            prev    = round(float(prev),    2)
            high52  = round(float(high52),  2)
            change  = round((current - prev) / prev * 100, 2) if prev else 0.0
            pct_off = round((high52 - current) / high52 * 100, 1) if high52 else 0.0
            out[sym] = {
                "price":        current,
                "change":       change,
                "high52":       high52,
                "pct_from_high": pct_off,
            }
        return out
    except Exception as e:
        print(f"  ⚠  Schwab quote fetch: {e}")
        return {}


def _schwab_option_chain(symbol, expiry=None, strike=None, opt_type="CALL"):
    """
    Fetch option chain from Schwab.
    If expiry+strike given: return the single matching contract dict (for price/IV check).
    If neither given: return all LEAP calls (>12 months) as a list sorted by expiry, strike.
    """
    client = get_schwab_client()
    if client is None:
        return None

    try:
        import schwab as _schwab

        kwargs = dict(
            symbol=symbol,
            contract_type=_schwab.client.Client.Options.ContractType.CALL,
            include_underlying_quote=True,
        )

        # If we want a single contract, narrow the query
        if expiry:
            from_dt = datetime.strptime(expiry, "%Y-%m-%d")
            kwargs["from_date"] = from_dt
            kwargs["to_date"]   = from_dt
        else:
            # LEAP chains: >12 months out
            kwargs["from_date"] = datetime.now() + timedelta(days=365)

        if strike is not None:
            s = float(strike)
            kwargs["strike"]    = s

        r = client.get_option_chain(**kwargs)
        if not r.ok:
            return None

        data = r.json()
        call_map = data.get("callExpDateMap", {})

        results = []
        for exp_key, strikes_dict in call_map.items():
            # exp_key format: "2027-09-17:700"
            exp_date = exp_key.split(":")[0]
            for strike_str, contracts in strikes_dict.items():
                for c in contracts:
                    bid   = float(c.get("bid") or 0)
                    ask   = float(c.get("ask") or 0)
                    mid   = round((bid + ask) / 2, 2)
                    if bid == 0 and ask == 0:
                        continue
                    if mid < 0.50:
                        continue

                    iv_raw = c.get("volatility")
                    iv_pct = round(float(iv_raw), 1) if iv_raw and float(iv_raw) > 0 else None

                    delta = c.get("delta")
                    delta = round(float(delta), 3) if delta is not None else None

                    theta = c.get("theta")
                    theta = round(float(theta), 3) if theta is not None else None

                    str_val = float(strike_str)
                    results.append({
                        "strike":    str_val,
                        "expiry":    exp_date,
                        "bid":       round(bid, 2),
                        "ask":       round(ask, 2),
                        "price":     mid,
                        "iv":        iv_pct,
                        "delta":     delta,
                        "theta":     theta,
                        "volume":    c.get("totalVolume"),
                        "oi":        c.get("openInterest"),
                        "moneyness": c.get("inTheMoney") and "ITM" or "OTM",
                    })

        results.sort(key=lambda x: (x["expiry"], x["strike"]))
        return results

    except Exception as e:
        print(f"  ⚠  Schwab option chain {symbol}: {e}")
        return None


def fetch_schwab_positions():
    """
    Pull live positions from Schwab account.
    Returns dict: {ticker: {"qty", "avg_cost", "current_value", "unrealized_pnl", "pnl_pct"}}
    For options: ticker is the underlying, with contract details embedded.
    """
    client = get_schwab_client()
    if client is None:
        return {}
    try:
        import schwab as _schwab
        r = client.get_accounts(fields=[_schwab.client.Client.Account.Fields.POSITIONS])
        if not r.ok:
            print(f"  ⚠  Schwab positions HTTP {r.status_code}")
            return {}
        accounts = r.json()
        positions = {}
        for acct in accounts:
            for pos in acct.get("securitiesAccount", {}).get("positions", []):
                instr = pos.get("instrument", {})
                sym   = instr.get("symbol", "")
                qty   = pos.get("longQuantity", 0) - pos.get("shortQuantity", 0)
                avg   = pos.get("averagePrice", 0)
                mktv  = pos.get("marketValue", 0)
                pnl   = pos.get("unrealizedProfitLoss", None)
                pnl_pct = None
                if avg and qty:
                    cost = avg * qty
                    if cost:
                        pnl_pct = round(((mktv - cost) / cost) * 100, 1)
                positions[sym] = {
                    "qty":            qty,
                    "avg_cost":       round(float(avg), 2),
                    "current_value":  round(float(mktv), 2),
                    "unrealized_pnl": round(float(pnl), 2) if pnl is not None else None,
                    "pnl_pct":        pnl_pct,
                    "asset_type":     instr.get("assetType", "EQUITY"),
                    "description":    instr.get("description", ""),
                }
        return positions
    except Exception as e:
        print(f"  ⚠  Schwab positions fetch: {e}")
        return {}


# (yfinance removed — Tradier is the data source for all market data)


# ── Tradier (free developer API — real-time bid/ask/mark, no brokerage needed) ──
# Sign up at developer.tradier.com → copy "API Access Token" → add as GitHub
# secret TRADIER_TOKEN. Uses sandbox endpoint which provides real market data.

TRADIER_BASE = "https://sandbox.tradier.com/v1"


def _tradier_token():
    return os.environ.get("TRADIER_TOKEN", "")


def _tradier_headers():
    return {
        "Authorization": f"Bearer {_tradier_token()}",
        "Accept": "application/json",
    }


def _to_occ(symbol, expiry, strike, opt_type="calls"):
    """Convert to OCC option symbol e.g. MSFT270917C00385000"""
    dt = datetime.strptime(expiry, "%Y-%m-%d")
    yymmdd = dt.strftime("%y%m%d")
    c_or_p = "C" if str(opt_type).lower().startswith("c") else "P"
    strike_int = int(round(float(strike) * 1000))
    return f"{symbol.upper()}{yymmdd}{c_or_p}{strike_int:08d}"


def _tradier_quote(symbols):
    """Bulk stock quote — all symbols in ONE API call. Returns {sym: {...}} dict."""
    token = _tradier_token()
    if not token:
        return {}
    try:
        resp = requests.get(
            f"{TRADIER_BASE}/markets/quotes",
            headers=_tradier_headers(),
            params={"symbols": ",".join(symbols), "greeks": "false"},
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json().get("quotes", {}).get("quote", [])
        if isinstance(raw, dict):
            raw = [raw]
        result = {}
        for q in raw:
            sym     = q.get("symbol", "")
            price   = q.get("last") or q.get("close") or q.get("prevclose")
            prev    = q.get("prevclose") or price
            high52  = q.get("week_52_high") or q.get("high")
            if price is None:
                continue
            price  = round(float(price), 2)
            prev   = round(float(prev),  2) if prev  else price
            high52 = round(float(high52),2) if high52 else price
            change  = round((price - prev)  / prev   * 100, 2) if prev   else 0
            pct_off = round((high52 - price) / high52 * 100, 1) if high52 else 0
            result[sym] = {
                "price": price, "change": change,
                "high52": high52, "pct_from_high": pct_off,
            }
        return result
    except Exception as e:
        print(f"  ⚠  Tradier quote error: {e}")
        return {}


def _tradier_option_price(symbol, expiry, strike, opt_type="calls"):
    """Single option mark price from Tradier. Returns {"mid":…,"iv":…,"delta":…,"theta":…}"""
    empty = {"mid": None, "iv": None, "delta": None, "theta": None}
    token = _tradier_token()
    if not token:
        return empty
    try:
        occ = _to_occ(symbol, expiry, strike, opt_type)
        resp = requests.get(
            f"{TRADIER_BASE}/markets/options/quotes",
            headers=_tradier_headers(),
            params={"symbols": occ, "greeks": "true"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        q = data.get("options", {}).get("option")
        if q is None:
            return empty
        if isinstance(q, list):
            q = q[0]
        bid = float(q.get("bid") or 0)
        ask = float(q.get("ask") or 0)
        mid = round((bid + ask) / 2, 2)
        greeks  = q.get("greeks") or {}
        iv_raw  = greeks.get("mid_iv") or q.get("implied_volatility")
        iv_pct  = round(float(iv_raw) * 100, 1) if iv_raw else None
        delta   = round(float(greeks["delta"]), 3) if greeks.get("delta") is not None else None
        theta   = round(float(greeks["theta"]), 3) if greeks.get("theta") is not None else None
        return {"mid": mid, "iv": iv_pct, "delta": delta, "theta": theta}
    except Exception as e:
        print(f"  ⚠  Tradier option price ({symbol}): {e}")
        return empty


def _tradier_option_chain(symbol, current_price):
    """Full LEAP call chain from Tradier for all expirations > 12 months."""
    token = _tradier_token()
    if not token:
        return []
    results = []
    try:
        # Step 1: get available expirations
        exp_resp = requests.get(
            f"{TRADIER_BASE}/markets/options/expirations",
            headers=_tradier_headers(),
            params={"symbol": symbol, "includeAllRoots": "true", "strikes": "false"},
            timeout=10,
        )
        exp_resp.raise_for_status()
        expirations = exp_resp.json().get("expirations", {}).get("date", [])
        if isinstance(expirations, str):
            expirations = [expirations]
        min_expiry = datetime.now() + timedelta(days=365)
        leap_exps  = [e for e in (expirations or [])
                      if datetime.strptime(e, "%Y-%m-%d") >= min_expiry]

        # Step 2: fetch each LEAP expiration's chain
        for expiry_str in leap_exps:
            try:
                chain_resp = requests.get(
                    f"{TRADIER_BASE}/markets/options/chains",
                    headers=_tradier_headers(),
                    params={"symbol": symbol, "expiration": expiry_str, "greeks": "true"},
                    timeout=10,
                )
                chain_resp.raise_for_status()
                options = chain_resp.json().get("options", {}).get("option", []) or []
                if isinstance(options, dict):
                    options = [options]
                for opt in options:
                    if opt.get("option_type") != "call":
                        continue
                    strike = float(opt.get("strike", 0))
                    if not (current_price * 0.50 <= strike <= current_price * 1.60):
                        continue
                    bid = float(opt.get("bid") or 0)
                    ask = float(opt.get("ask") or 0)
                    if bid == 0 and ask == 0:
                        continue
                    mid = round((bid + ask) / 2, 2)
                    if mid < 0.50:
                        continue
                    greeks  = opt.get("greeks") or {}
                    iv_raw  = greeks.get("mid_iv") or opt.get("implied_volatility")
                    iv_pct  = round(float(iv_raw) * 100, 1) if iv_raw else None
                    delta   = round(float(greeks["delta"]), 3) if greeks.get("delta") is not None else None
                    theta   = round(float(greeks["theta"]), 3) if greeks.get("theta") is not None else None
                    try:   vol = int(float(opt["volume"]))      if opt.get("volume")        not in (None,"") else None
                    except: vol = None
                    try:   oi  = int(float(opt["open_interest"])) if opt.get("open_interest") not in (None,"") else None
                    except: oi  = None
                    pct_diff  = (strike - current_price) / current_price
                    moneyness = "ITM" if pct_diff < -0.03 else ("ATM" if pct_diff <= 0.03 else "OTM")
                    results.append({
                        "strike": strike, "expiry": expiry_str,
                        "bid": round(bid, 2), "ask": round(ask, 2),
                        "price": mid, "iv": iv_pct, "delta": delta, "theta": theta,
                        "volume": vol, "oi": oi, "moneyness": moneyness,
                    })
            except Exception as e:
                print(f"    ⚠  Tradier chain {symbol} {expiry_str}: {e}")
        results.sort(key=lambda x: (x["expiry"], x["strike"]))
    except Exception as e:
        print(f"  ⚠  Tradier option chain ({symbol}): {e}")
    return results


# ── Public fetch API — Schwab → Tradier ──────────────────────────────────────

def fetch_ticker(symbol):
    """Fetch stock price. Schwab → Tradier."""
    client = get_schwab_client()
    if client is not None:
        result = _schwab_quote([symbol])
        if symbol in result:
            return result[symbol]
    result = _tradier_quote([symbol])
    return result.get(symbol)


def fetch_tickers_bulk(symbols):
    """Fetch multiple tickers in one API call. Schwab bulk → Tradier bulk."""
    client = get_schwab_client()
    if client is not None:
        result = _schwab_quote(symbols)
        if result:
            return result
    return _tradier_quote(symbols)


def fetch_option_price(symbol, expiry, strike, opt_type="calls"):
    """Returns dict {"mid": price, "iv": iv_pct, "delta": delta, "theta": theta}.
    Schwab → Tradier."""
    client = get_schwab_client()
    if client is not None:
        chain = _schwab_option_chain(symbol, expiry=expiry, strike=strike)
        if chain is not None and len(chain) > 0:
            target = float(strike)
            best   = min(chain, key=lambda c: abs(c["strike"] - target))
            return {
                "mid":   best["price"],
                "iv":    best["iv"],
                "delta": best["delta"],
                "theta": best["theta"],
            }
    return _tradier_option_price(symbol, expiry, strike, opt_type)


def fetch_option_chain(symbol, current_price):
    """
    Fetch all LEAP calls with expiry > 12 months. Schwab → Tradier.
    Returns list of dicts sorted by expiry then strike, tagged ITM/ATM/OTM.
    Strike range: 50% below to 60% above current price.
    """
    client = get_schwab_client()
    if client is not None:
        chain = _schwab_option_chain(symbol)
        if chain is not None:
            filtered = []
            for c in chain:
                s = c["strike"]
                if s < current_price * 0.50 or s > current_price * 1.60:
                    continue
                pct_diff = (s - current_price) / current_price
                c["moneyness"] = "ITM" if pct_diff < -0.03 else ("ATM" if pct_diff <= 0.03 else "OTM")
                filtered.append(c)
            return filtered
    return _tradier_option_chain(symbol, current_price)


# ─── HTML UPDATE ─────────────────────────────────────────────────────────────
def update_html(all_data, opt_data, watchlist, opt_contracts, chain_data=None, schwab_positions=None):
    try:
        with open(HTML_PATH) as f:
            html = f.read()

        now_str = datetime.now(ET).strftime("%b %d, %Y ~%-I:%M %p %Z")

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

        # ── 6. Build GREEKS_DATA block ───────────────────────────
        glines = [
            "  // ── GREEKS (updated by troy-monitor.py) ────────────────────────────────────────",
            "  // delta = option moves $X per $1 stock move  |  theta = $ lost per day (per contract = theta*100)",
            "  const GREEKS_DATA = {",
        ]
        for t, info in opt_contracts.items():
            result = opt_data.get(t, {})
            delta = result.get("delta") if isinstance(result, dict) else None
            theta = result.get("theta") if isinstance(result, dict) else None
            d_str = f"{delta:.3f}" if delta is not None else "null"
            t_str = f"{theta:.3f}" if theta is not None else "null"
            glines.append(f"    {t.ljust(4)}: {{ delta: {d_str}, theta: {t_str} }},")
        glines.append("  };")
        new_greeks = "\n".join(glines)
        html = re.sub(
            r"  // ── GREEKS.*?const GREEKS_DATA = \{.*?\};",
            new_greeks, html, flags=re.DOTALL,
        )

        # ── 7. Build CONTRACT_DATA block ─────────────────────────
        if chain_data:
            clines = [
                "  // ── OPTION CHAIN DATA (updated by troy-monitor.py) ───────────────────────────",
                "  // Full LEAP call chains per stock — expiry >12 months, all strikes ITM→OTM",
                "  const CONTRACT_DATA = {",
            ]
            for t, contracts in chain_data.items():
                clines.append(f"    {json.dumps(t)}: {json.dumps(contracts)},")
            clines.append("  };")
            new_chain = "\n".join(clines)
            html = re.sub(
                r"  // ── OPTION CHAIN DATA.*?const CONTRACT_DATA = \{.*?\};",
                new_chain, html, flags=re.DOTALL,
            )

        # ── 8. Build SCHWAB_POSITIONS block ─────────────────────
        if schwab_positions:
            plines = [
                "  // ── SCHWAB LIVE POSITIONS (updated by troy-monitor.py) ──────────────────────",
                "  // Pulled from your Schwab account — auto-synced each run",
                f"  // As of: {now_str}",
                "  const SCHWAB_POSITIONS = {",
            ]
            for sym, pos in schwab_positions.items():
                plines.append(
                    f"    {json.dumps(sym)}: {json.dumps(pos)},"
                )
            plines.append("  };")
            new_positions = "\n".join(plines)
            if "const SCHWAB_POSITIONS" in html:
                html = re.sub(
                    r"  // ── SCHWAB LIVE POSITIONS.*?const SCHWAB_POSITIONS = \{.*?\};",
                    new_positions, html, flags=re.DOTALL,
                )
            else:
                # Insert before closing </script> of the data block
                html = html.replace(
                    "  const CONTRACT_DATA",
                    new_positions + "\n\n  const CONTRACT_DATA",
                    1,
                )

        # ── 9. Build MY_OWNED_POSITIONS block ────────────────
        owned_list = [(t, i) for t, i in opt_contracts.items() if i.get("owned")]
        oplines = [
            "  // ── MY OWNED POSITIONS (auto-synced from troy-classes.json) ──────────",
            "  // Entries with owned:true — current prices come from OPT_PRICES above",
            f"  // Updated: {now_str}",
            "  const MY_OWNED_POSITIONS = [",
        ]
        for sym, info in owned_list:
            entry_str = f"{info['alert']:.2f}" if info.get("alert") is not None else "null"
            opt_type = "CALL" if info.get("opt_type", "calls").lower().startswith("c") else "PUT"
            contract = info.get("contract", "").replace('"', '\\"')
            expiry   = info.get("expiry", "")
            strike   = info.get("strike", 0)
            oplines.append(
                f'    {{ticker:"{sym}",type:"{opt_type}",strike:{strike},exp:"{expiry}",entry:{entry_str},contract:"{contract}"}},'
            )
        oplines.append("  ];")
        new_owned_block = "\n".join(oplines)
        html = re.sub(
            r"  // ── MY OWNED POSITIONS.*?const MY_OWNED_POSITIONS = \[.*?\];",
            new_owned_block, html, flags=re.DOTALL,
        )

        with open(HTML_PATH, "w") as f:
            f.write(html)
        print(f"  ✓ HTML updated ({now_str})")

    except Exception as e:
        print(f"  ⚠  HTML update failed: {e}")
        import traceback; traceback.print_exc()


# ─── HOURLY STATUS PUSH ──────────────────────────────────────────────────────
def send_hourly_snapshot(all_data, opt_data, opt_contracts, schwab_positions,
                         buy_zone_hits, alert_hits, stop_loss_hits):
    """
    Always-on push sent every monitor run — quick snapshot of owned positions
    and watchlist highlights. Not an alert; just a regular status update.
    """
    now = datetime.now(ET)
    time_str = now.strftime("%-I:%M %p")
    lines = [f"📊 {time_str} War Room Update"]

    # ── Owned positions ──────────────────────────────────────────
    owned = [(t, i) for t, i in opt_contracts.items() if i.get("owned")]
    if owned:
        lines.append("")
        lines.append("💼 Your Positions")
        for t, info in owned:
            stk    = all_data.get(t, {})
            result = opt_data.get(t, {})
            opt_mid = result.get("mid") if isinstance(result, dict) else None
            stk_price = stk.get("price")
            stk_chg   = stk.get("change")

            # P&L vs alert entry
            alert = info.get("alert")
            pnl_str = ""
            if opt_mid and alert:
                pnl_pct = (opt_mid - alert) / alert * 100
                pnl_str = f"  {pnl_pct:+.1f}% vs entry"

            stk_str = f"${stk_price:.2f} ({stk_chg:+.1f}%)" if stk_price and stk_chg else "--"
            opt_str = f"${opt_mid:.2f}" if opt_mid else "--"

            # Status tag
            if stop_loss_hits and any(x[0] == t for x in stop_loss_hits):
                status = "⛔ STOP-LOSS"
            elif alert_hits and any(x[0] == t for x in alert_hits):
                status = "🚨 AT ALERT"
            else:
                status = "✓ Holding"

            lines.append(f"  {t} {info['contract']}")
            lines.append(f"  Stock: {stk_str} | Option: {opt_str}{pnl_str}")
            lines.append(f"  {status}")

    # ── Market pulse ──────────────────────────────────────────────
    pulse_items = []
    for t in MARKET_PULSE:
        d = all_data.get(t)
        if d:
            sign = "+" if d["change"] >= 0 else ""
            pulse_items.append(f"{t} {sign}{d['change']:.1f}%")
    if pulse_items:
        lines.append("")
        lines.append("🌍 " + "  |  ".join(pulse_items))

    # ── Buy zone hits ─────────────────────────────────────────────
    if buy_zone_hits:
        lines.append("")
        lines.append(f"🟠 Buy Zone: {', '.join(t for t, _, _ in buy_zone_hits)}")

    # ── Alert summary ─────────────────────────────────────────────
    n_alerts = len(alert_hits) + len(stop_loss_hits)
    if n_alerts == 0:
        lines.append("")
        lines.append("✓ No alerts triggered")

    body = "\n".join(lines)
    title = f"War Room {time_str}"

    # Lower priority so hourly pings don't feel urgent
    send_push(title, body, priority="low", tags=("chart_with_upwards_trend",))
    print(f"  📱 Hourly snapshot sent ({time_str})")


# ─── MAIN ────────────────────────────────────────────────────────────────────
def main():
    now_et = datetime.now(ET)
    print(f"\n🔍 Troy's War Room Monitor — {now_et.strftime('%Y-%m-%d %H:%M %Z')}\n")

    # ── Skip the "wrong season" cron firing (see TOUCHPOINTS_ET above) ───
    # Manual runs (workflow_dispatch) always run in full; only scheduled
    # firings get gated to the real touchpoint list.
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule" and not is_scheduled_touchpoint(now_et):
        print(f"  ⏭  {now_et.strftime('%-I:%M %p %Z')} isn't a scheduled touchpoint "
              f"— this is the other DST offset's cron firing. Skipping.\n")
        return

    # ── Load class config ────────────────────────────────────────
    watchlist, opt_contracts, classes_data = load_classes()
    if not watchlist:
        print("⚠  No watchlist — check troy-classes.json"); sys.exit(1)

    all_data = {}
    buy_zone_hits   = []
    watch_zone_hits = []

    # ── Detect data source ───────────────────────────────────────
    using_schwab  = get_schwab_client() is not None
    using_tradier = bool(_tradier_token())
    src_tag = (
        "🔴 Schwab live" if using_schwab else
        "📊 Tradier real-time" if using_tradier else
        "⚠  no data source (add TRADIER_TOKEN secret)"
    )
    print(f"\n  Data source: {src_tag}")

    # ── Fetch equity prices (bulk call — all symbols in one request) ──────
    print("\n  Fetching stock prices...")
    all_symbols = list(watchlist.keys()) + MARKET_PULSE
    if using_schwab:
        bulk = _schwab_quote(all_symbols)
        for sym in all_symbols:
            if sym in bulk:
                all_data[sym] = bulk[sym]
            else:
                d = _tradier_quote([sym]).get(sym)
                if d:
                    all_data[sym] = d
    elif using_tradier:
        bulk = _tradier_quote(all_symbols)
        for sym in all_symbols:
            if sym in bulk:
                all_data[sym] = bulk[sym]

    for ticker, info in watchlist.items():
        d = all_data.get(ticker)
        if not d:
            print(f"  {ticker:<5} — skip"); continue
        pct = d["pct_from_high"]
        if   pct < 10:  zone = "📈 near high"
        elif pct < 20:  zone = "👀 watch zone"
        elif pct <= 30: zone = "🟠 BUY ZONE ←"
        else:           zone = "❌ below zone"
        print(f"  {ticker:<5} ${d['price']:>9.2f}  |  {pct:>5.1f}% off high  |  {zone}")

        pf = pct / 100
        if BUY_ZONE_LOW <= pf <= BUY_ZONE_HIGH:
            buy_zone_hits.append((ticker, d, info))
        elif 0.10 <= pf < BUY_ZONE_LOW:
            watch_zone_hits.append((ticker, d, info))

    # ── Fetch & print Schwab positions ───────────────────────────
    schwab_positions = {}
    if using_schwab:
        print("\n  Fetching Schwab account positions...")
        schwab_positions = fetch_schwab_positions()
        if schwab_positions:
            option_pos = {k: v for k, v in schwab_positions.items() if v.get("asset_type") == "OPTION"}
            equity_pos = {k: v for k, v in schwab_positions.items() if v.get("asset_type") == "EQUITY"}
            if option_pos:
                print(f"  📋 {len(option_pos)} option position(s):")
                for sym, pos in option_pos.items():
                    pnl_str = f"  P&L: ${pos['unrealized_pnl']:+.0f} ({pos['pnl_pct']:+.1f}%)" if pos.get("unrealized_pnl") is not None else ""
                    print(f"    {sym}  qty:{pos['qty']}  avg:${pos['avg_cost']:.2f}  mktv:${pos['current_value']:.2f}{pnl_str}")
            if equity_pos:
                print(f"  📋 {len(equity_pos)} equity position(s)")
        else:
            print("  ℹ  No positions found (or empty account)")

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

    # ── Fetch full option chains for drawer ──────────────────────
    print("\n  Fetching full option chains (LEAP calls)...")
    chain_data = {}
    for ticker in watchlist:
        eq = all_data.get(ticker)
        if not eq:
            continue
        print(f"    {ticker:<5}", end=" ", flush=True)
        contracts = fetch_option_chain(ticker, eq["price"])
        chain_data[ticker] = contracts
        print(f"{len(contracts)} contracts")

    # ── Update HTML ──────────────────────────────────────────────
    update_html(all_data, opt_data, watchlist, opt_contracts, chain_data, schwab_positions)

    # ── Hourly status push (always fires) ────────────────────────
    send_hourly_snapshot(all_data, opt_data, opt_contracts, schwab_positions,
                         buy_zone_hits, alert_hits, stop_loss_hits)

    print("\n✅ Done.\n")


if __name__ == "__main__":
    main()
