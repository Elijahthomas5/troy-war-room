#!/usr/bin/env python3
"""
Troy's EYL Class Watcher — Fully Automatic
Logs into EYL University community board, finds Troy M.'s latest posts,
extracts any new stock picks/contracts, and auto-updates troy-classes.json.
Dashboard cards are injected on the next price monitor run.

Runs once daily via GitHub Actions (see .github/workflows/eyl-watcher.yml).
Requires: EYL_EMAIL + EYL_PASSWORD as GitHub secrets.
          pip install playwright && playwright install chromium
"""

import json
import os
import re
import sys
from datetime import datetime, date

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
CLASSES_PATH  = os.path.join(BASE_DIR, 'troy-classes.json')
STATE_PATH    = os.path.join(BASE_DIR, '.troy-state.json')

EYL_EMAIL     = os.environ.get("EYL_EMAIL", "")
EYL_PASSWORD  = os.environ.get("EYL_PASSWORD", "")
NTFY_TOPIC    = "troy-eyl-eli"
NTFY_URL      = f"https://ntfy.sh/{NTFY_TOPIC}"

EYL_LOGIN     = "https://eyluniversity.com/login"
EYL_CATALOG   = "https://eyluniversity.com/catalog"
EYL_BOARD     = "https://eyluniversity.com/community/channels/the-investors-group"
EYL_GENERAL   = "https://eyluniversity.com/community/channels/general"

# Stock tickers to detect in post text (expand as Troy covers more names)
KNOWN_TICKERS = {
    "NVDA","DRAM","CDNS","ANET","LLY","ARM","AMKR","MU","TSM","MRVL",
    "DELL","ORCL","VRT","NOW","HOOD","UBER","COHR","LITE","GLW","IREN",
    "AVGO","AMD","FN","MCHP","SNDK","WDC","STX","MSFT","AAPL","META",
    "GOOGL","AMZN","CRM","PLTR","SNOW","AI","SMCI","INTC","QCOM","SPCX",
    "ASML","MU","TSM","AMAT","KLAC","LRCX","ON","WOLF","SWKS","QRVO",
}

# Words that indicate Troy is announcing class picks (not just chatting)
CLASS_KEYWORDS = [
    "masterclass", "class", "options class", "leaps", "leap", "calls",
    "strike", "expir", "entry", "alert", "buy zone", "contract", "position",
    "covered tonight", "covered today", "went over", "picked up", "watching",
    "our next pick", "new pick", "adding to the book",
]

# Contract patterns: "Jan '27 $180C" or "$180 call" or "180 strike"
CONTRACT_RE = re.compile(
    r'(?:'
    r'(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)'
    r"[\s']+(?P<yr>\d{2,4})\s+\$?(?P<strike>[\d,]+)[CP]?"
    r'|'
    r'\$(?P<strike2>[\d,]+)\s*(?:call|put|strike|C\b|P\b)'
    r')',
    re.IGNORECASE,
)

# Alert/entry price patterns: "at $36.67" "entry: $12" "alert price $23"
ALERT_RE = re.compile(
    r'(?:at|@|entry|alert|bought|entered|price)[:\s]+\$?([\d.]+)',
    re.IGNORECASE,
)

# Month abbreviation → number
MONTH_MAP = {m: i+1 for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
)}

# Full month name → abbreviation (for speech transcript parsing)
MONTH_NAME_MAP = {
    'january': 'Jan', 'february': 'Feb', 'march': 'Mar', 'april': 'Apr',
    'may': 'May', 'june': 'Jun', 'july': 'Jul', 'august': 'Aug',
    'september': 'Sep', 'october': 'Oct', 'november': 'Nov', 'december': 'Dec',
}

# Speech-pattern contract regex: "315 call for June of 2027", "out to January 2029 at 150 strike"
SPEECH_CONTRACT_RE = re.compile(
    r'(?:'
    # "315 call for/out to June of 2027"
    r'(?P<strike3>[\d,]+)\s+(?:dollar\s+)?(?:calls?|puts?)'
    r'\s+(?:for|out\s+to|expir\w*|through)'
    r'\s*(?P<month2>january|february|march|april|may|june|july|august|september|october|november|december)'
    r'\s*(?:of\s*)?(?P<yr2>\d{4})'
    r'|'
    # "out to January 2029 ... 150 strike/call"
    r'out\s+to\s+(?P<month3>january|february|march|april|may|june|july|august|september|october|november|december)'
    r'\s*(?:of\s*)?(?P<yr3>\d{4})'
    r'(?:[^.]{0,150}?(?P<strike4>[\d,]+)\s*(?:strike|calls?|puts?))?'
    r')',
    re.IGNORECASE,
)


# ─── STATE ───────────────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


# ─── PUSH NOTIFICATION ───────────────────────────────────────────────────────
def send_push(title, body, priority="high", tags=("bell",)):
    try:
        import requests
        r = requests.post(NTFY_URL, data=body.encode(),
                          headers={"Title": title, "Priority": priority,
                                   "Tags": ",".join(tags)}, timeout=10)
        return r.status_code == 200
    except:
        return False


# ─── PARSE CONTRACT FROM TEXT ─────────────────────────────────────────────────
def parse_contracts_from_text(text):
    """
    Try to extract option contract details from free-form post text.
    Returns list of dicts: [{ticker, contract, strike, expiry_str, alert}]
    """
    found = []
    # Find all tickers mentioned
    words = text.upper().split()
    tickers_mentioned = [w.strip(".,!?:()") for w in words if w.strip(".,!?:()") in KNOWN_TICKERS]

    for m in CONTRACT_RE.finditer(text):
        strike_raw = m.group("strike") or m.group("strike2") or ""
        strike     = float(strike_raw.replace(",", "")) if strike_raw else None
        month_str  = m.group("month")
        yr_str     = m.group("yr")

        if not strike:
            continue

        # Build expiry string
        expiry = None
        contract_label = f"${int(strike)}C"
        if month_str and yr_str:
            month_num = MONTH_MAP.get(month_str[:3].capitalize(), 1)
            yr        = int(yr_str) if len(yr_str) == 4 else 2000 + int(yr_str)
            # Use 3rd Friday of that month as expiry
            import calendar
            cal = calendar.monthcalendar(yr, month_num)
            fridays = [w[4] for w in cal if w[4] != 0]
            expiry_day = fridays[2] if len(fridays) >= 3 else fridays[-1]
            expiry = f"{yr}-{month_num:02d}-{expiry_day:02d}"
            contract_label = f"{month_str[:3]} '{str(yr)[2:]} ${int(strike)}C"

        # Find nearest ticker (within ~100 chars before the match)
        pre_text = text[max(0, m.start()-120):m.start()]
        ticker_match = None
        for t in reversed(KNOWN_TICKERS):
            if re.search(rf'\b{re.escape(t)}\b', pre_text, re.IGNORECASE):
                ticker_match = t
                break

        if not ticker_match and tickers_mentioned:
            ticker_match = tickers_mentioned[0]

        # Try to find alert/entry price near this contract mention
        context = text[max(0, m.start()-50):m.end()+80]
        alert = None
        am = ALERT_RE.search(context)
        if am:
            try:
                alert = float(am.group(1))
            except:
                pass

        if ticker_match and strike:
            found.append({
                "ticker":   ticker_match,
                "contract": contract_label,
                "strike":   strike,
                "expiry":   expiry,
                "alert":    alert,
            })

    return found


# ─── UPDATE CLASSES.JSON ─────────────────────────────────────────────────────
def update_classes_json(new_picks, class_date_str, class_label, notes="Auto-detected from EYL community board."):
    """
    Add new picks to troy-classes.json. Creates a new class entry or
    appends to existing one if same date. Returns list of newly added tickers.
    """
    if not os.path.exists(CLASSES_PATH):
        print("⚠  troy-classes.json not found"); return []

    with open(CLASSES_PATH) as f:
        data = json.load(f)

    classes = data.get("classes", [])
    watchlist_stocks = data.get("watchlist_only", {}).get("stocks", [])

    # All tickers already tracked (in classes OR watchlist)
    existing_class_tickers = set()
    for cls in classes:
        for s in cls.get("stocks", []):
            existing_class_tickers.add(s["ticker"])
    existing_watchlist_tickers = {s["ticker"] for s in watchlist_stocks}
    all_tracked = existing_class_tickers | existing_watchlist_tickers

    # Filter to genuinely new picks
    truly_new = [p for p in new_picks if p["ticker"] not in existing_class_tickers]
    watchlist_promotions = [p for p in new_picks
                            if p["ticker"] in existing_watchlist_tickers
                            and p["ticker"] not in existing_class_tickers
                            and p.get("alert")]  # only promote if we have an alert

    added = []

    # Find or create a class entry for this date
    entry = next((c for c in classes if c["id"] == class_date_str), None)
    if entry is None and (truly_new or watchlist_promotions):
        entry = {
            "id":     class_date_str,
            "date":   class_label,
            "label":  f"{datetime.strptime(class_date_str, '%Y-%m-%d').strftime('%b %Y')} Class (auto)",
            "notes":  notes,
            "stocks": [],
        }
        classes.insert(0, entry)  # newest first

    for pick in truly_new:
        stock_entry = {
            "ticker":   pick["ticker"],
            "name":     pick["ticker"],  # name resolved later by monitor
            "contract": pick.get("contract", f"Jan '27 ${int(pick.get('strike',100))}C"),
            "expiry":   pick.get("expiry", f"{datetime.now().year + 1}-01-15"),
            "strike":   pick.get("strike", 100.0),
            "opt_type": "calls",
            "alert":    pick.get("alert"),
        }
        if entry:
            entry["stocks"].append(stock_entry)
        added.append(pick["ticker"])
        print(f"  ✨ Added new class pick: {pick['ticker']}")

    # If alert price now known for a watchlist-only stock, promote it to a class entry
    for pick in watchlist_promotions:
        if entry:
            existing_in_class = next((s for s in entry["stocks"] if s["ticker"] == pick["ticker"]), None)
            if not existing_in_class:
                # Find the watchlist entry to copy its contract details
                wl_stock = next((s for s in watchlist_stocks if s["ticker"] == pick["ticker"]), {})
                stock_entry = {
                    "ticker":   pick["ticker"],
                    "name":     wl_stock.get("name", pick["ticker"]),
                    "contract": pick.get("contract", wl_stock.get("contract", "")),
                    "expiry":   pick.get("expiry", wl_stock.get("expiry", "")),
                    "strike":   pick.get("strike", wl_stock.get("strike", 100.0)),
                    "opt_type": "calls",
                    "alert":    pick.get("alert"),
                }
                entry["stocks"].append(stock_entry)
                added.append(pick["ticker"])
                print(f"  📌 Promoted watchlist → class pick: {pick['ticker']} (alert: ${pick['alert']})")

    data["classes"] = classes
    with open(CLASSES_PATH, "w") as f:
        json.dump(data, f, indent=2)

    return added


# ─── VIDEO PIPELINE ──────────────────────────────────────────────────────────

def vtt_to_text(vtt_content):
    """Convert WebVTT caption file to plain text (strip timestamps/cue numbers)."""
    lines = vtt_content.split('\n')
    speech = []
    for line in lines:
        line = line.strip()
        if not line or line == 'WEBVTT':
            continue
        if re.match(r'^\d+$', line):          # cue number
            continue
        if re.match(r'\d+:\d+:\d+', line):    # timestamp
            continue
        if line.startswith('NOTE'):
            continue
        speech.append(line)
    return ' '.join(speech)


def parse_vtt_for_picks(vtt_text):
    """
    Parse auto-generated speech captions for option contract picks.
    Handles natural-language patterns Troy uses in class: '315 call for June 2027',
    'out to January 2029 at the 150 strike', etc.
    Returns list of pick dicts (same schema as parse_contracts_from_text).
    """
    import calendar as cal_mod
    plain = vtt_to_text(vtt_text)
    picks = []

    # Run existing written-format parser first
    picks.extend(parse_contracts_from_text(plain))

    # Speech-pattern parser
    for m in SPEECH_CONTRACT_RE.finditer(plain):
        strike_raw  = m.group("strike3") or m.group("strike4")
        month_name  = (m.group("month2") or m.group("month3") or "").lower()
        yr_str      = m.group("yr2") or m.group("yr3")

        if not (month_name and yr_str):
            continue

        month_abbr = MONTH_NAME_MAP.get(month_name, 'Jan')
        yr         = int(yr_str)
        month_num  = MONTH_MAP[month_abbr]

        cal = cal_mod.monthcalendar(yr, month_num)
        fridays    = [w[4] for w in cal if w[4] != 0]
        expiry_day = fridays[2] if len(fridays) >= 3 else fridays[-1]
        expiry     = f"{yr}-{month_num:02d}-{expiry_day:02d}"

        strike = float(strike_raw.replace(',', '')) if strike_raw else None
        contract_label = (f"{month_abbr} '{str(yr)[2:]} ${int(strike)}C"
                          if strike else f"{month_abbr} '{str(yr)[2:]} ???C")

        # Find nearest ticker in the 200 chars before this match
        pre_text     = plain[max(0, m.start()-200):m.start()]
        ticker_match = None
        for t in KNOWN_TICKERS:
            if re.search(rf'\b{re.escape(t)}\b', pre_text, re.IGNORECASE):
                ticker_match = t
                break

        if ticker_match and strike:
            picks.append({
                "ticker":   ticker_match,
                "contract": contract_label,
                "strike":   strike,
                "expiry":   expiry,
                "alert":    None,
            })

    # Deduplicate by (ticker, contract)
    seen, deduped = set(), []
    for p in picks:
        key = (p.get('ticker'), p.get('contract'))
        if key not in seen:
            seen.add(key)
            deduped.append(p)
    return deduped


def scan_new_class_videos(page, seen_slugs):
    """
    Scan EYL catalog for new Troy Millings Stock Options class videos.
    Returns list of {slug, href, title, date_str} dicts not yet in seen_slugs.
    """
    page.goto(EYL_CATALOG, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(2000)

    links = page.evaluate("""
        () => Array.from(document.querySelectorAll('a'))
            .map(a => ({href: a.href, text: (a.textContent || '').trim()}))
            .filter(l => l.text.toLowerCase().includes('stock options master class')
                      && l.href.includes('/programs/'))
    """)

    new_items = []
    for link in links:
        href = link['href'].split('?')[0]
        slug = href.split('/programs/')[-1]
        if slug and slug not in seen_slugs:
            dm = re.search(r'(\d{1,2})/(\d{1,2})/(\d{2,4})', link['text'])
            if dm:
                month, day, yr = dm.groups()
                yr_full  = 2000 + int(yr) if len(yr) == 2 else int(yr)
                date_str = f"{yr_full}-{int(month):02d}-{int(day):02d}"
            else:
                date_str = None
            new_items.append({'slug': slug, 'href': href,
                               'title': link['text'], 'date_str': date_str})
    return new_items


def run_video_pipeline(page):
    """
    Scan EYL University catalog for new class recordings, download their
    auto-generated captions (WebVTT from UScreen CDN), parse for picks,
    and update troy-classes.json.  Called with an already-logged-in page.
    """
    print("\n🎬 EYL Video Pipeline — checking for new class recordings...")

    state      = load_state()
    seen_slugs = set(state.get("seen_video_slugs", []))

    try:
        new_videos = scan_new_class_videos(page, seen_slugs)
    except Exception as e:
        print(f"  ⚠  Failed to scan catalog: {e}")
        return []

    if not new_videos:
        print("  ✓ No new class videos found")
        return []

    print(f"  Found {len(new_videos)} new video(s)")
    all_picks_by_date = {}

    for item in new_videos:
        print(f"\n  📺 Processing: {item['title']}")
        try:
            page.goto(item['href'], wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(3000)

            # Extract VTT URL from the <track> element UScreen injects
            vtt_url = page.evaluate("""
                () => {
                    const t = document.querySelector('track[kind="metadata"]')
                           || document.querySelector('track');
                    return t ? t.src : null;
                }
            """)

            seen_slugs.add(item['slug'])   # mark seen regardless of outcome

            if not vtt_url:
                print(f"  ⚠  No captions found")
                continue

            print(f"  📄 Fetching captions...")
            # Fetch via page context (benefits from session auth if CDN requires it)
            safe_url = vtt_url.replace("'", "\\'")
            vtt_text = page.evaluate(f"""
                async () => {{
                    const r = await fetch('{safe_url}');
                    if (!r.ok) return null;
                    return await r.text();
                }}
            """)

            if not vtt_text:
                print(f"  ⚠  Failed to download captions")
                continue

            print(f"  ✓ Captions: {len(vtt_text):,} chars")

            picks = parse_vtt_for_picks(vtt_text)
            if picks:
                print(f"  🎯 Picks detected: {[p['ticker'] for p in picks]}")
            else:
                print(f"  ℹ  No clear new picks in transcript (review/discussion class)")

            cd = item['date_str'] or date.today().isoformat()
            if cd not in all_picks_by_date:
                all_picks_by_date[cd] = {'title': item['title'], 'picks': []}
            all_picks_by_date[cd]['picks'].extend(picks)

        except Exception as e:
            print(f"  ⚠  Error: {e}")
            seen_slugs.discard(item['slug'])   # allow retry next run

    # Persist seen slugs
    state["seen_video_slugs"] = list(seen_slugs)
    save_state(state)

    # Update classes.json for each newly discovered class date
    total_added = []
    for class_date, info in all_picks_by_date.items():
        label = datetime.strptime(class_date, '%Y-%m-%d').strftime('%b %d, %Y')
        added = update_classes_json(
            info['picks'],
            class_date_str=class_date,
            class_label=label,
            notes=f"Auto-detected from EYL video captions: {info['title']}",
        )
        total_added.extend(added)

        # Even if no picks, ensure a class entry exists so it's visible
        if not added:
            _ensure_class_entry(class_date, label, info['title'])

    return total_added


def _ensure_class_entry(class_date_str, label, title):
    """Create a stub class entry in troy-classes.json if one doesn't exist yet."""
    if not os.path.exists(CLASSES_PATH):
        return
    with open(CLASSES_PATH) as f:
        data = json.load(f)
    classes = data.get("classes", [])
    if any(c["id"] == class_date_str for c in classes):
        return   # already exists
    classes.insert(0, {
        "id":     class_date_str,
        "date":   label,
        "label":  f"{datetime.strptime(class_date_str, '%Y-%m-%d').strftime('%b %Y')} Class",
        "notes":  f"Auto-logged from EYL video: {title}. No new picks extracted — review/discussion session.",
        "stocks": [],
    })
    data["classes"] = classes
    with open(CLASSES_PATH, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  📋 Created stub class entry for {label}")


# ─── MAIN SCRAPER ────────────────────────────────────────────────────────────
def run():
    if not EYL_EMAIL or not EYL_PASSWORD:
        print("⚠  EYL_EMAIL / EYL_PASSWORD not set.")
        print("   Go to GitHub → your repo → Settings → Secrets → add both.")
        sys.exit(0)

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        print("⚠  Playwright not installed. Run: pip install playwright && playwright install chromium")
        sys.exit(1)

    state = load_state()
    last_seen = state.get("last_post_ids", [])   # list of post IDs / text hashes we've processed
    all_new_picks = []

    print(f"\n🔍 EYL Watcher — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx     = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()

        # ── Log in ──────────────────────────────────────────────
        print("  Logging into EYL University...")
        try:
            page.goto(EYL_LOGIN, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            # Fill email
            email_sel = 'input[type="email"], input[name="email"], input[placeholder*="email" i]'
            page.wait_for_selector(email_sel, timeout=10000)
            page.fill(email_sel, EYL_EMAIL)

            # Fill password
            pw_sel = 'input[type="password"], input[name="password"]'
            page.fill(pw_sel, EYL_PASSWORD)

            # Submit
            btn_sel = 'button[type="submit"], input[type="submit"], button:text("Log in"), button:text("Sign in")'
            page.click(btn_sel)
            page.wait_for_timeout(3000)

            # Verify login
            if "login" in page.url or "sign-in" in page.url:
                print("  ⚠  Login may have failed — check EYL_EMAIL / EYL_PASSWORD secrets")
                browser.close()
                sys.exit(0)
            print("  ✓ Logged in")

        except PWTimeout:
            print("  ⚠  Login page timed out")
            browser.close()
            sys.exit(0)

        # ── Video pipeline (new class recordings) ────────────────
        video_added = run_video_pipeline(page)

        # ── Scrape community channels ─────────────────────────────
        channels_to_check = [
            (EYL_BOARD,   "investors-group"),
            (EYL_GENERAL, "general"),
        ]

        for url, channel_name in channels_to_check:
            print(f"\n  Checking #{channel_name}...")
            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(2000)

                # Scroll to load more posts
                for _ in range(3):
                    page.keyboard.press("End")
                    page.wait_for_timeout(1000)

                # Get all text blocks that look like posts
                # Try multiple selectors for different community platform layouts
                post_selector = (
                    'article, '
                    '[data-post-id], '
                    '[class*="post"], '
                    '[class*="message"], '
                    '[class*="comment"], '
                    '[class*="feed-item"], '
                    '[class*="space-post"]'
                )

                posts = page.query_selector_all(post_selector)
                if not posts:
                    # Fallback: grab all paragraphs/divs with substantial text
                    posts = page.query_selector_all('div p, div[class*="content"]')

                print(f"  Found {len(posts)} post elements")

                for post_el in posts[:40]:   # check most recent 40
                    try:
                        full_text = post_el.inner_text()
                    except:
                        continue

                    if len(full_text) < 30:
                        continue

                    # Check if this post is from Troy M.
                    # Look for his name in the post element (author span, profile link, etc.)
                    post_html = ""
                    try:
                        post_html = post_el.inner_html()
                    except:
                        pass

                    is_troy_post = (
                        "troy m" in full_text.lower()[:300] or
                        "troy millings" in full_text.lower()[:300] or
                        "troy m." in post_html.lower()[:500]
                    )

                    # Also catch posts where community members share Troy's picks
                    is_class_related = any(kw in full_text.lower() for kw in CLASS_KEYWORDS)
                    mentions_troy     = "troy" in full_text.lower()

                    if not (is_troy_post or (is_class_related and mentions_troy)):
                        continue

                    # Dedup by text hash
                    post_hash = str(hash(full_text[:200]))
                    if post_hash in last_seen:
                        continue

                    print(f"\n  📌 Relevant post found:")
                    print(f"     {full_text[:200].replace(chr(10), ' ')}...")

                    # Extract tickers
                    text_upper = full_text.upper()
                    tickers_found = {t for t in KNOWN_TICKERS
                                     if re.search(rf'\b{re.escape(t)}\b', text_upper)}
                    print(f"     Tickers: {tickers_found}")

                    # Parse contracts
                    contracts = parse_contracts_from_text(full_text)
                    if contracts:
                        print(f"     Contracts parsed: {contracts}")
                        all_new_picks.extend(contracts)
                    elif tickers_found:
                        # No contract details parseable — add tickers without contract info
                        for t in tickers_found:
                            all_new_picks.append({"ticker": t, "contract": None, "strike": None,
                                                  "expiry": None, "alert": None})

                    last_seen.append(post_hash)

            except PWTimeout:
                print(f"  ⚠  Channel {channel_name} timed out")
            except Exception as e:
                print(f"  ⚠  Error on {channel_name}: {e}")

        browser.close()

    # ── Process community board findings ─────────────────────────
    state["last_post_ids"] = last_seen[-200:]
    save_state(state)

    if all_new_picks:
        # Deduplicate picks by ticker (keep the one with most info)
        by_ticker = {}
        for p in all_new_picks:
            t = p["ticker"]
            if t not in by_ticker or (p.get("alert") and not by_ticker[t].get("alert")):
                by_ticker[t] = p

        today       = date.today().isoformat()
        today_label = date.today().strftime("%b %d, %Y")
        newly_added = update_classes_json(
            list(by_ticker.values()),
            class_date_str=today,
            class_label=today_label,
            notes=f"Auto-detected from EYL community board on {today_label}.",
        )

        if newly_added:
            body = (
                f"Auto-detected {len(newly_added)} new pick(s) from Troy's EYL posts:\n"
                f"{', '.join(newly_added)}\n\n"
                "troy-classes.json updated. Dashboard will refresh on next price run.\n"
                "⚠️ Verify the contract details in troy-classes.json — auto-parsing may miss specifics."
            )
            send_push("🆕 New Troy Picks (Community Board)", body, priority="high",
                      tags=("school", "chart_with_upwards_trend"))
            print(f"\n  ✅ Added from community board: {newly_added}")
        else:
            print("\n  ℹ  Community posts found but no new tickers to add")
    else:
        print("\n  ✓ No new Troy community posts detected")

    # ── Summary ───────────────────────────────────────────────────
    if video_added:
        send_push("🎬 New Class Video Processed",
                  f"EYL video pipeline added {len(video_added)} pick(s): {', '.join(video_added)}\n"
                  "Check troy-classes.json and verify contract details.",
                  priority="high", tags=("movie_camera", "chart_with_upwards_trend"))

    print("\n✅ EYL watcher done.\n")


if __name__ == "__main__":
    run()
