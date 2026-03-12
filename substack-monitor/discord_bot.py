"""
Discord Bot — Trade Assistant
==============================
Commands:
  !levels          — S/R levels for all tickers (weekly + monthly pivot points)
  !levels NVDA     — S/R levels for a specific ticker
  !summary         — Latest SmartReversals post summary
  !summary optionsai — Latest OptionsAI post summary
  !help            — Show available commands
"""

import csv
import json
import os
import subprocess
import sys
import time
import base64
import asyncio
import requests
import yfinance as yf
import anthropic
import discord
import psutil
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

CONFIG_FILE  = "config.json"
COOKIES_FILE = "cookies.json"

with open(CONFIG_FILE) as f:
    cfg = json.load(f)

# ---------------------------------------------------------------------------
# Bot management helpers
# ---------------------------------------------------------------------------
PYTHON      = r"C:\Program Files\Python311\python.exe"
ETH_BOT_DIR = r"C:\Users\Administrator\Desktop\projects\eth-options-bot"

BOT_SCRIPTS = {
    "eth":     ("run_live_0dte.py",     ETH_BOT_DIR, r"logs\live_0dte.log"),
    "eth0dte": ("run_live_0dte.py",     ETH_BOT_DIR, r"logs\live_0dte.log"),
    "eth7dte": ("run_live.py",          ETH_BOT_DIR, r"logs\live.log"),
    "btc":     ("run_live_btc_0dte.py", ETH_BOT_DIR, r"logs\live_btc_0dte.log"),
}

TRADE_CSVS = {
    "ETH 0DTE": os.path.join(ETH_BOT_DIR, "data", "live_0dte_trades.csv"),
    "ETH 7DTE": os.path.join(ETH_BOT_DIR, "data", "live_trades.csv"),
    "BTC 0DTE": os.path.join(ETH_BOT_DIR, "data", "live_btc_0dte_trades.csv"),
}

def _is_running(script: str) -> bool:
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if "python" in (proc.info["name"] or "").lower():
                if script in " ".join(proc.info["cmdline"] or []):
                    return True
        except Exception:
            pass
    return False

def _start_bot(script: str, cwd: str, log_rel: str) -> bool:
    log_path = os.path.join(cwd, log_rel)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    try:
        with open(log_path, "a") as lf:
            subprocess.Popen([PYTHON, script], cwd=cwd, stdout=lf, stderr=lf,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        time.sleep(3)
        return _is_running(script)
    except Exception:
        return False

def _stop_bot(script: str):
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if "python" in (proc.info["name"] or "").lower():
                if script in " ".join(proc.info["cmdline"] or []):
                    proc.terminate()
        except Exception:
            pass

def _parse_pnl(val):
    try:
        return float(str(val).replace("$","").replace("+","").strip())
    except Exception:
        return 0.0

def get_bot_status() -> str:
    lines = ["**Bot Status**", "```"]
    all_scripts = [
        ("ETH 0DTE", "run_live_0dte.py"),
        ("ETH 7DTE", "run_live.py"),
        ("BTC 0DTE", "run_live_btc_0dte.py"),
    ]
    for name, script in all_scripts:
        status = "RUNNING" if _is_running(script) else "STOPPED"
        icon   = "[+]" if status == "RUNNING" else "[-]"
        lines.append(f"{icon} {name:<10} {status}")
    lines.append("```")
    return "\n".join(lines)

def get_pnl_summary() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"**P&L Summary — {today}**", "```"]
    grand_today = grand_total = 0.0
    for name, csv_path in TRADE_CSVS.items():
        trades = []
        try:
            with open(csv_path, newline="") as f:
                trades = list(csv.DictReader(f))
        except FileNotFoundError:
            pass
        today_pnl = sum(_parse_pnl(t.get("pnl_usd","0")) for t in trades if t.get("exit_time","").startswith(today))
        total_pnl = sum(_parse_pnl(t.get("pnl_usd","0")) for t in trades)
        wins      = sum(1 for t in trades if _parse_pnl(t.get("pnl_usd","0")) > 0)
        win_pct   = f"{100*wins/len(trades):.0f}%" if trades else "N/A"
        grand_today += today_pnl
        grand_total += total_pnl
        lines.append(f"{name:<10} Today: {today_pnl:+.2f}  Total: {total_pnl:+.2f}  Win: {win_pct}")
    lines.append("-" * 42)
    lines.append(f"{'Combined':<10} Today: {grand_today:+.2f}  Total: {grand_total:+.2f}")
    lines.append("```")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# S/R Level helpers (from sr_levels.py)
# ---------------------------------------------------------------------------

TICKERS = {
    "SPX": "^GSPC", "NDX": "^NDX", "DJI": "^DJI", "IWM": "IWM",
    "SPY": "SPY",   "QQQ": "QQQ",
    "AAPL": "AAPL", "MSFT": "MSFT", "NVDA": "NVDA", "META": "META",
    "AMZN": "AMZN", "GOOG": "GOOG", "TSLA": "TSLA", "PLTR": "PLTR",
    "BTC": "BTC-USD", "ETH": "ETH-USD",
}

def pivot_levels(high, low, close):
    pp = (high + low + close) / 3
    return {
        "R3": high + 2 * (pp - low),
        "R2": pp + (high - low),
        "R1": (2 * pp) - low,
        "PP": pp,
        "S1": (2 * pp) - high,
        "S2": pp - (high - low),
        "S3": low - 2 * (high - pp),
    }

def fmt(val, is_crypto=False):
    if is_crypto: return f"{val:,.0f}"
    if val >= 1000: return f"{val:,.1f}"
    return f"{val:.2f}"

def fetch_ohlc(symbol, period, interval):
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=True)
        if hasattr(df.columns, "droplevel"):
            try: df.columns = df.columns.droplevel(1)
            except Exception: pass
        if df is None or len(df) < 2:
            return None
        prev = df.iloc[-2]
        return float(prev["High"]), float(prev["Low"]), float(prev["Close"])
    except Exception:
        return None

def fetch_price(symbol):
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="1d")
        return float(hist["Close"].iloc[-1]) if not hist.empty else None
    except Exception:
        return None

def get_levels_text(name):
    name = name.upper()
    symbol = TICKERS.get(name)
    if not symbol:
        # Try as direct yfinance symbol
        symbol = name

    is_crypto = name in ("BTC", "ETH")
    w = fetch_ohlc(symbol, "1mo", "1wk")
    m = fetch_ohlc(symbol, "6mo", "1mo")
    price = fetch_price(symbol)

    if not w or not m:
        return f"Could not fetch data for **{name}**. Check the ticker symbol."

    wl = pivot_levels(*w)
    ml = pivot_levels(*m)
    w_pos = "ABOVE" if price and price > wl["PP"] else "BELOW"
    m_pos = "ABOVE" if price and price > ml["PP"] else "BELOW"
    cur = fmt(price, is_crypto) if price else "N/A"

    lines = [
        f"**{name}** — Price: {cur}",
        f"vs CWL: **{w_pos}** | vs CML: **{m_pos}**",
        "```",
        f"{'Level':<5}  {'Weekly':>10}  {'Monthly':>10}",
        "-" * 30,
    ]
    for label in ["R3", "R2", "R1", "PP", "S1", "S2", "S3"]:
        marker = " <--" if label == "PP" else ""
        lines.append(f"{label:<5}  {fmt(wl[label], is_crypto):>10}  {fmt(ml[label], is_crypto):>10}{marker}")
    lines.append("```")
    return "\n".join(lines)

def get_all_levels_text():
    sections = {
        "INDICES & ETFs": ["SPX", "NDX", "IWM", "SPY", "QQQ"],
        "MEGACAPS":       ["AAPL", "MSFT", "NVDA", "META", "AMZN", "GOOG", "TSLA", "PLTR"],
        "CRYPTO":         ["BTC", "ETH"],
    }
    now = datetime.now(timezone.utc).strftime("%b %d, %Y %H:%M UTC")
    blocks = [f"**S/R LEVELS — {now}**", "=" * 38]
    for section, names in sections.items():
        blocks.append(f"\n**── {section} ──**")
        for name in names:
            blocks.append(get_levels_text(name))
    return blocks

# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------

SUMMARY_PROMPT = """You are an expert options trader and financial analyst.
Produce a concise Discord-ready summary with:
1. SUMMARY (2-3 sentences)
2. TRADE DETAILS (if any — underlying, strategy, expiry, strikes, credit/debit, max profit/loss, breakeven, TP%, SL, rationale)
3. KEY TAKEAWAYS (3-5 bullet points)
If no trade details, skip section 2. Extract numbers from charts if visible."""

def get_summary(pub_name="smartreversals"):
    pub_name = pub_name.lower()
    publications = cfg.get("publications", [])
    pub = next((p for p in publications if pub_name in p["name"].lower()), None)
    if not pub:
        return f"Unknown publication: {pub_name}. Try: smartreversals, optionsai"

    pub_url = pub["url"]
    display = pub["name"]

    # Fetch latest post info
    try:
        with open(COOKIES_FILE) as f:
            cookies_list = json.load(f)
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})
        for c in cookies_list:
            session.cookies.set(c["name"], c["value"],
                domain=c.get("domain", ".substack.com"), path=c.get("path", "/"))
        posts = session.get(f"{pub_url}/api/v1/posts?limit=1", timeout=15).json()
        post = posts[0]
        title    = post.get("title", "Untitled")
        slug     = post.get("slug", "")
        post_url = post.get("canonical_url") or f"{pub_url}/p/{slug}"
        pub_date = post.get("post_date", "")[:10]
    except Exception as e:
        return f"Failed to fetch post list: {e}"

    # Fetch full content
    body_html = ""
    image_urls = []

    if "smartreversals" in pub_url:
        # Use Playwright for JS-rendered paid content
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    viewport={"width": 1400, "height": 900}
                )
                pw_cookies = []
                for c in cookies_list:
                    cookie = {"name": c["name"], "value": c["value"],
                              "domain": c.get("domain", "www.smartreversals.com"),
                              "path": c.get("path", "/")}
                    if c.get("expirationDate"):
                        cookie["expires"] = int(c["expirationDate"])
                    pw_cookies.append(cookie)
                context.add_cookies(pw_cookies)
                page = context.new_page()
                page.goto(post_url, wait_until="networkidle", timeout=45000)
                for scroll in range(0, 15000, 500):
                    page.evaluate(f"window.scrollTo(0, {scroll})")
                    time.sleep(0.15)
                time.sleep(3)
                text = page.evaluate(
                    '() => { const a = document.querySelector("article") || document.body; return a.innerText; }'
                )
                imgs = page.evaluate('''() => Array.from(document.querySelectorAll("img"))
                    .map(i => ({src: i.src, w: i.naturalWidth, h: i.naturalHeight}))
                    .filter(i => i.w >= 1000 && i.h >= 400)
                    .map(i => i.src)''')
                browser.close()
            image_urls = imgs[:4]
        except Exception as e:
            text = f"Could not load content: {e}"
    else:
        # Regular Substack API
        try:
            full = session.get(f"{pub_url}/api/v1/posts/{slug}", timeout=15).json()
            body_html = full.get("body_html", "") or ""
            soup = BeautifulSoup(body_html, "html.parser")
            image_urls = [img.get("src", "") for img in soup.find_all("img")
                          if img.get("src", "").startswith("http")][:4]
            for tag in soup(["script", "style"]):
                tag.decompose()
            text = "\n".join(l for l in soup.get_text(separator="\n").splitlines() if l.strip())
        except Exception as e:
            text = f"Could not fetch content: {e}"

    # Build Claude content
    content = [{"type": "text",
                 "text": f"PUBLICATION: {display}\nPOST TITLE: {title}\nDATE: {pub_date}\n\nCONTENT:\n{text[:10000]}"}]
    for url in image_urls:
        try:
            r = requests.get(url, timeout=10)
            ct = r.headers.get("content-type", "image/png").split(";")[0].strip()
            if ct not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                ct = "image/png"
            b64 = base64.standard_b64encode(r.content).decode()
            content.append({"type": "image", "source": {"type": "base64", "media_type": ct, "data": b64}})
        except Exception:
            pass

    try:
        claude = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
        resp = claude.messages.create(
            model="claude-sonnet-4-6", max_tokens=2000,
            system=SUMMARY_PROMPT,
            messages=[{"role": "user", "content": content}]
        )
        summary = resp.content[0].text
    except Exception as e:
        summary = f"Claude error: {e}"

    return f"**[{display}] {title}**\n*{pub_date}* | {post_url}\n{'='*38}\n\n{summary}"

# ---------------------------------------------------------------------------
# Discord Bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

HELP_TEXT = """**Trade Bot Commands:**
`!levels` — S/R levels for all tickers
`!levels NVDA` — S/R levels for a specific ticker (e.g. AAPL, MSFT, BTC)
`!summary` — Latest SmartReversals post summary
`!summary optionsai` — Latest OptionsAI post summary
`!portfolio` — Live OptionsAI $30K portfolio positions + P&L
`!ask <question>` — Ask anything about recent newsletter posts
  Examples:
  • `!ask what trades were recommended this week?`
  • `!ask what were the SPX levels from SmartReversals?`

**Bot Management:**
`!status` — Show running/stopped state of all trading bots
`!pnl` — P&L summary (today + all-time) for all bots
`!start <bot>` — Start a bot (eth, eth0dte, eth7dte, btc)
`!stop <bot>` — Stop a bot (eth, eth0dte, eth7dte, btc)

`!help` — Show this message"""

@client.event
async def on_ready():
    print(f"Bot logged in as {client.user}")

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    content = message.content.strip()
    if not content.startswith("!"):
        return

    parts = content.split(maxsplit=1)
    cmd   = parts[0].lower()
    arg   = parts[1].strip() if len(parts) > 1 else ""

    # !help
    if cmd == "!help":
        await message.channel.send(HELP_TEXT)
        return

    # !levels [ticker]
    if cmd == "!levels":
        await message.channel.send("Fetching levels...")
        loop = asyncio.get_event_loop()
        if arg:
            text = await loop.run_in_executor(None, get_levels_text, arg)
            await message.channel.send(text[:1900])
        else:
            blocks = await loop.run_in_executor(None, get_all_levels_text)
            chunk = ""
            for block in blocks:
                if len(chunk) + len(block) + 2 > 1900:
                    await message.channel.send(chunk)
                    chunk = block
                else:
                    chunk += "\n" + block
            if chunk:
                await message.channel.send(chunk)
        return

    # !portfolio
    if cmd == "!portfolio":
        await message.channel.send("Fetching live portfolio... (~10s)")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, fetch_portfolio)
        for i in range(0, len(result), 1900):
            await message.channel.send(result[i:i+1900])
        return

    # !ask <question>
    if cmd == "!ask":
        if not arg:
            await message.channel.send("Usage: `!ask <your question>`\nExample: `!ask what positions are open in the OptionsAI portfolio?`")
            return
        await message.channel.send(f"Searching recent posts to answer: *{arg}*... (may take ~30s)")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, answer_question, arg)
        for i in range(0, len(result), 1900):
            await message.channel.send(result[i:i+1900])
        return

    # !summary [pub]
    if cmd == "!summary":
        pub = arg if arg else "smartreversals"
        await message.channel.send(f"Fetching latest {pub} summary... (may take ~30s)")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, get_summary, pub)
        # Send in chunks
        for i in range(0, len(result), 1900):
            await message.channel.send(result[i:i+1900])
        return

    # !status
    if cmd == "!status":
        result = get_bot_status()
        await message.channel.send(result)
        return

    # !pnl
    if cmd == "!pnl":
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, get_pnl_summary)
        await message.channel.send(result)
        return

    # !start <bot>
    if cmd == "!start":
        bot_key = arg.lower()
        if not bot_key or bot_key not in BOT_SCRIPTS:
            keys = ", ".join(BOT_SCRIPTS.keys())
            await message.channel.send(f"Usage: `!start <bot>` — Available bots: {keys}")
            return
        script, cwd, log_rel = BOT_SCRIPTS[bot_key]
        if _is_running(script):
            await message.channel.send(f"`{bot_key}` is already running.")
            return
        await message.channel.send(f"Starting `{bot_key}`...")
        loop = asyncio.get_event_loop()
        ok = await loop.run_in_executor(None, _start_bot, script, cwd, log_rel)
        if ok:
            await message.channel.send(f"`{bot_key}` started successfully.")
        else:
            await message.channel.send(f"Failed to start `{bot_key}`. Check logs.")
        return

    # !stop <bot>
    if cmd == "!stop":
        bot_key = arg.lower()
        if not bot_key or bot_key not in BOT_SCRIPTS:
            keys = ", ".join(BOT_SCRIPTS.keys())
            await message.channel.send(f"Usage: `!stop <bot>` — Available bots: {keys}")
            return
        script, cwd, log_rel = BOT_SCRIPTS[bot_key]
        if not _is_running(script):
            await message.channel.send(f"`{bot_key}` is not running.")
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _stop_bot, script)
        await message.channel.send(f"`{bot_key}` stopped.")
        return

# ---------------------------------------------------------------------------
# !ask — fetch recent posts from all pubs and answer with Claude
# ---------------------------------------------------------------------------

PORTFOLIO_URL = "https://portfolio-30k-production.up.railway.app"
PORTFOLIO_EMAIL = "basa.kireeti@gmail.com"

def fetch_portfolio() -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{PORTFOLIO_URL}/login", wait_until="networkidle", timeout=30000)
        page.fill("input[type=email]", PORTFOLIO_EMAIL)
        page.click("button")
        time.sleep(4)
        # Click the tab using JS to avoid navigation timeout
        page.evaluate("document.querySelector('button[data-tab=\"positions\"]').click()")
        time.sleep(3)
        text = page.inner_text("body")
        browser.close()

    # Parse into structured message
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # Extract summary numbers
    summary = {}
    for i, line in enumerate(lines):
        if "TOTAL COST BASIS" in line and i+1 < len(lines):
            summary["cost"] = lines[i+1]
        if "TOTAL PORTFOLIO PROFIT" in line and i+2 < len(lines):
            summary["profit"] = lines[i+2]
        if "DATA:" in line:
            summary["date"] = line.replace("DATA:", "").strip()

    # Find positions table (after the header row)
    try:
        header_idx = next(i for i, l in enumerate(lines) if "SYMBOL" in l and "TYPE" in l)
        table_lines = lines[header_idx:]
    except StopIteration:
        table_lines = []

    # Build output
    date  = summary.get("date", "")
    cost  = summary.get("cost", "")
    profit = summary.get("profit", "")

    out = [
        f"**OptionsAI $30K Portfolio** | Data: {date}",
        f"Cost Basis: **{cost}** | Total Return: **{profit}**",
        "```",
        f"{'Symbol':<6} {'Type':<20} {'Strikes':<12} {'Expiry':<13} {'DTE':<5} {'P&L':>10} {'%Max':>7}",
        "-" * 75,
    ]

    # Parse rows — skip header and leg rows (starting with └)
    i = 0
    positions = []
    while i < len(table_lines):
        line = table_lines[i]
        if line.startswith("└") or "SYMBOL" in line or "All Types" in line:
            i += 1
            continue
        parts = line.split("\t")
        if len(parts) >= 10:
            symbol  = parts[0].strip()
            ptype   = parts[1].strip()[:18]
            strikes = parts[2].strip()
            expiry  = parts[3].strip()
            dte     = parts[4].strip()
            pnl     = parts[9].strip()
            pct     = parts[11].strip() if len(parts) > 11 else ""
            positions.append((symbol, ptype, strikes, expiry, dte, pnl, pct))
            out.append(f"{symbol:<6} {ptype:<20} {strikes:<12} {expiry:<13} {dte:<5} {pnl:>10} {pct:>7}")
        i += 1

    out.append("```")
    if not positions:
        out.append("*(Could not parse position rows — check the site directly)*")

    return "\n".join(out)


ASK_SYSTEM = """You are a financial assistant with access to recent paid newsletter content.
Answer the user's question based ONLY on the provided newsletter content.
Be specific — include exact prices, dates, trade details, portfolio positions, strikes, expiries, credits.
If the answer is not in the content, say so clearly.
Keep answers concise and well formatted for Discord."""

def fetch_pub_text(pub: dict, num_posts: int = 3) -> str:
    """Fetch text from the last N posts of a publication."""
    pub_url  = pub["url"]
    pub_name = pub["name"]
    all_text = []

    try:
        with open(COOKIES_FILE) as f:
            cookies_list = json.load(f)
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})
        for c in cookies_list:
            session.cookies.set(c["name"], c["value"],
                domain=c.get("domain", ".substack.com"), path=c.get("path", "/"))

        posts = session.get(f"{pub_url}/api/v1/posts?limit={num_posts}", timeout=15).json()
    except Exception as e:
        return f"[{pub_name}] Failed to fetch posts: {e}"

    for post in posts:
        title    = post.get("title", "Untitled")
        slug     = post.get("slug", "")
        pub_date = post.get("post_date", "")[:10]
        post_url = post.get("canonical_url") or f"{pub_url}/p/{slug}"

        if "smartreversals" in pub_url:
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    context = browser.new_context(
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                        viewport={"width": 1400, "height": 900}
                    )
                    pw_cookies = []
                    for c in cookies_list:
                        cookie = {"name": c["name"], "value": c["value"],
                                  "domain": c.get("domain", "www.smartreversals.com"),
                                  "path": c.get("path", "/")}
                        if c.get("expirationDate"):
                            cookie["expires"] = int(c["expirationDate"])
                        pw_cookies.append(cookie)
                    context.add_cookies(pw_cookies)
                    page = context.new_page()
                    page.goto(post_url, wait_until="networkidle", timeout=45000)
                    for scroll in range(0, 10000, 800):
                        page.evaluate(f"window.scrollTo(0, {scroll})")
                        time.sleep(0.1)
                    time.sleep(2)
                    text = page.evaluate(
                        '() => { const a = document.querySelector("article") || document.body; return a.innerText; }'
                    )
                    browser.close()
                all_text.append(f"--- [{pub_name}] {title} ({pub_date}) ---\n{text[:4000]}")
            except Exception as e:
                all_text.append(f"--- [{pub_name}] {title} ({pub_date}) --- [load error: {e}]")
        else:
            try:
                full      = session.get(f"{pub_url}/api/v1/posts/{slug}", timeout=15).json()
                body_html = full.get("body_html", "") or ""
                soup      = BeautifulSoup(body_html, "html.parser")
                for tag in soup(["script", "style"]):
                    tag.decompose()
                text = "\n".join(l for l in soup.get_text(separator="\n").splitlines() if l.strip())
                all_text.append(f"--- [{pub_name}] {title} ({pub_date}) ---\n{text[:4000]}")
            except Exception as e:
                all_text.append(f"--- [{pub_name}] {title} ({pub_date}) --- [load error: {e}]")

    return "\n\n".join(all_text)


def answer_question(question: str) -> str:
    """Fetch recent posts from all publications and answer the question."""
    publications = cfg.get("publications", [])

    all_content = []
    for pub in publications:
        print(f"Fetching {pub['name']} for !ask...")
        text = fetch_pub_text(pub, num_posts=3)
        all_content.append(text)

    combined = "\n\n" + "="*50 + "\n\n".join(all_content)

    try:
        claude = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
        resp = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=ASK_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"NEWSLETTER CONTENT:\n{combined[:12000]}\n\nQUESTION: {question}"
            }]
        )
        return resp.content[0].text
    except Exception as e:
        return f"Claude error: {e}"


client.run(cfg["discord_bot_token"])
