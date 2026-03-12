"""
Fetch full paid SmartReversals content using Playwright (JS-rendered).
Finds the latest post, loads it with cookies, extracts full text, summarizes with Claude.
"""

import json
import sys
import time
import anthropic
import requests
from playwright.sync_api import sync_playwright

COOKIES_FILE = "cookies.json"
CONFIG_FILE  = "config.json"

SYSTEM_PROMPT = """You are a professional financial analyst.
You will be given the content of a paid SmartReversals newsletter post.

Produce a concise summary with:

1. MARKET OVERVIEW (2-3 sentences -- current market conditions and bias)

2. KEY LEVELS
   For each instrument (SPX, QQQ, Mag 7 stocks, ETH, etc.):
   - Monthly levels (support / resistance)
   - Weekly levels (support / resistance)
   - Key pivot / line in sand
   - Target up / Target down

3. PRIME TRADE SETUPS (if any -- entry, stop, target)

4. KEY TAKEAWAYS (3-5 bullet points)

Extract all numbers, price levels, and percentages mentioned.
Keep it concise -- this goes to Telegram and Discord."""


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_cookies():
    with open(COOKIES_FILE) as f:
        return json.load(f)


def get_latest_post_url(cookies_list):
    """Use requests to get the latest post slug."""
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    for c in cookies_list:
        session.cookies.set(
            c["name"], c["value"],
            domain=c.get("domain", ".smartreversals.com"),
            path=c.get("path", "/"),
        )
    resp = session.get("https://www.smartreversals.com/api/v1/posts?limit=1&offset=0", timeout=15)
    resp.raise_for_status()
    posts = resp.json()
    if not posts:
        raise RuntimeError("No posts returned from SmartReversals API")
    post = posts[0]
    url = post.get("canonical_url") or f"https://www.smartreversals.com/p/{post['slug']}"
    return post.get("title", "Untitled"), url, post.get("post_date", "")[:10]


def fetch_full_content_playwright(post_url: str, cookies_list: list) -> str:
    """Use Playwright to load the JS-rendered paid post and extract full text."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )

        # Load cookies into browser context
        pw_cookies = []
        for c in cookies_list:
            domain = c.get("domain", "www.smartreversals.com")
            # Playwright requires domain without leading dot for non-http-only
            cookie = {
                "name":  c["name"],
                "value": c["value"],
                "domain": domain,
                "path":  c.get("path", "/"),
                "secure": c.get("secure", False),
                "httpOnly": c.get("httpOnly", False),
            }
            if c.get("expirationDate"):
                cookie["expires"] = int(c["expirationDate"])
            if c.get("sameSite"):
                ss = c["sameSite"].capitalize()
                if ss in ("Strict", "Lax", "None"):
                    cookie["sameSite"] = ss
            pw_cookies.append(cookie)

        context.add_cookies(pw_cookies)
        page = context.new_page()

        print(f"Loading: {post_url}")
        page.goto(post_url, wait_until="domcontentloaded", timeout=30000)

        # Wait for paid content to render (look for typical Substack body)
        try:
            page.wait_for_selector(".available-content, .body, article", timeout=15000)
        except Exception:
            pass

        # Extra wait for JS to populate
        time.sleep(3)

        # Extract full page text
        content = page.evaluate("""() => {
            // Remove nav, footer, headers
            ['nav', 'footer', 'header', '.subscribe-widget', '.paywall'].forEach(sel => {
                document.querySelectorAll(sel).forEach(el => el.remove());
            });
            // Get main article content
            const article = document.querySelector('article') ||
                            document.querySelector('.available-content') ||
                            document.querySelector('main') ||
                            document.body;
            return article ? article.innerText : document.body.innerText;
        }""")

        browser.close()
        return content


def send_discord(webhook_url: str, message: str) -> bool:
    if not webhook_url:
        return False
    chunks = [message[i:i+1900] for i in range(0, len(message), 1900)]
    ok = True
    for chunk in chunks:
        try:
            resp = requests.post(webhook_url, json={"content": chunk}, timeout=10)
            ok = ok and resp.ok
        except Exception as exc:
            print(f"Discord error: {exc}")
            ok = False
    return ok


def send_telegram(token: str, chat_id: str, message: str) -> bool:
    url    = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
    ok     = True
    for chunk in chunks:
        try:
            resp = requests.post(url, json={
                "chat_id": chat_id, "text": chunk,
            }, timeout=10)
            ok = ok and resp.ok
        except Exception as exc:
            print(f"Telegram error: {exc}")
            ok = False
    return ok


def main():
    cfg          = load_config()
    cookies_list = load_cookies()

    print("Fetching latest SmartReversals post...")
    title, post_url, pub_date = get_latest_post_url(cookies_list)
    print(f"Title: {title}")
    print(f"URL:   {post_url}")
    print(f"Date:  {pub_date}")

    print("\nLoading full content with Playwright...")
    full_text = fetch_full_content_playwright(post_url, cookies_list)

    # Check if we got substantive content
    if len(full_text) < 500:
        print(f"WARNING: Content looks short ({len(full_text)} chars) -- may not be authenticated")
    else:
        print(f"Content: {len(full_text)} chars")

    # Show a preview
    print("\n--- CONTENT PREVIEW (first 800 chars) ---")
    print(full_text[:800])
    print("---")

    print("\nSummarizing with Claude...")
    client   = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"PUBLICATION: SmartReversals\nPOST TITLE: {title}\nDATE: {pub_date}\n\nPOST CONTENT:\n{full_text[:10000]}"
        }],
    )
    summary = response.content[0].text

    print("\n=== SUMMARY ===")
    print(summary)

    msg = (
        f"**[SmartReversals] {title}**\n"
        f"*{pub_date}*\n"
        f"{post_url}\n"
        f"{'='*40}\n\n"
        f"{summary}"
    )

    # Send to Discord
    discord_url = cfg.get("discord_webhook", "")
    if discord_url:
        ok = send_discord(discord_url, msg)
        print(f"\nDiscord: {'OK' if ok else 'FAILED'}")

    # Send to Telegram
    tel_msg = f"[SmartReversals] {title}\n{pub_date}\n{post_url}\n{'='*40}\n\n{summary}"
    ok = send_telegram(cfg["telegram_token"], cfg["telegram_chat_id"], tel_msg)
    print(f"Telegram: {'OK' if ok else 'FAILED'}")


if __name__ == "__main__":
    main()
