"""
Substack Monitor — Multi-Publication
======================================
Monitors multiple Substack publications for new posts.
Reads text + images, extracts option trade details, sends summary to Telegram.
"""

import json
import sys
import time
import base64
import logging
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import anthropic

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("monitor.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

CONFIG_FILE  = "config.json"
SEEN_FILE    = "seen_posts.json"
COOKIES_FILE = "cookies.json"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)

def load_seen() -> set:
    if Path(SEEN_FILE).exists():
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()

def save_seen(seen: set) -> None:
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f)

# ---------------------------------------------------------------------------
# Substack Auth (cookie-based)
# ---------------------------------------------------------------------------

def load_cookies(session: requests.Session) -> bool:
    if not Path(COOKIES_FILE).exists():
        logger.error("cookies.json not found.")
        return False
    try:
        with open(COOKIES_FILE) as f:
            cookies = json.load(f)
        for c in cookies:
            session.cookies.set(
                c["name"], c["value"],
                domain=c.get("domain", ".substack.com"),
                path=c.get("path", "/"),
            )
        logger.info("Loaded %d cookies from %s", len(cookies), COOKIES_FILE)
        return True
    except Exception as exc:
        logger.error("Failed to load cookies: %s", exc)
        return False


def verify_login(session: requests.Session) -> bool:
    try:
        resp = session.get("https://substack.com/api/v1/user/login", timeout=10)
        if resp.ok and resp.json().get("id"):
            logger.info("Authenticated as %s", resp.json().get("email", "unknown"))
            return True
        logger.warning("Auth check status %s -- will attempt anyway.", resp.status_code)
        return True
    except Exception as exc:
        logger.warning("Could not verify login: %s", exc)
        return True

# ---------------------------------------------------------------------------
# Fetch posts
# ---------------------------------------------------------------------------

def fetch_posts(session: requests.Session, pub_url: str, limit: int = 10) -> list:
    api = f"{pub_url}/api/v1/posts?limit={limit}&offset=0"
    try:
        resp = session.get(api, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error("Failed to fetch posts from %s: %s", pub_url, exc)
        return []


def fetch_post_content(session: requests.Session, pub_url: str, slug: str) -> dict:
    api = f"{pub_url}/api/v1/posts/{slug}"
    try:
        resp = session.get(api, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error("Failed to fetch post %s: %s", slug, exc)
        return {}

# ---------------------------------------------------------------------------
# Extract content
# ---------------------------------------------------------------------------

def extract_text_and_images(html: str):
    soup = BeautifulSoup(html, "html.parser")
    image_urls = [img.get("src","") for img in soup.find_all("img")
                  if img.get("src","").startswith("http")]
    for tag in soup(["script", "style"]):
        tag.decompose()
    lines = [l for l in soup.get_text(separator="\n").strip().splitlines() if l.strip()]
    return "\n".join(lines), image_urls


def image_to_base64(url: str):
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        if ct not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
            ct = "image/jpeg"
        return base64.standard_b64encode(resp.content).decode("utf-8"), ct
    except Exception as exc:
        logger.warning("Could not download image %s: %s", url, exc)
        return None

# ---------------------------------------------------------------------------
# Claude summary
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert options trader and financial analyst.
You will be given the content of a paid newsletter/Substack post.
Produce a concise Telegram-ready summary with these sections:

1. SUMMARY (2-3 sentences -- what is this post about?)

2. TRADE DETAILS (if any trades mentioned -- include ALL of the below)
   - Underlying asset (e.g. SPY, AAPL, ETH, Silver)
   - Strategy (e.g. Bull Put Spread, Iron Condor, Bull Call Spread)
   - Expiry date
   - Strikes (all legs)
   - Entry credit / debit
   - Max profit / Max loss
   - Break-even price(s)
   - Target exit (take-profit %)
   - Stop loss
   - Rationale (1-2 sentences)

3. KEY TAKEAWAYS (3-5 bullet points)

If no trade details, skip section 2.
Extract numbers from charts/screenshots if visible.
Keep it concise -- this goes to Telegram."""


def summarize_with_claude(client: anthropic.Anthropic, pub_name: str,
                           title: str, text: str, image_urls: list) -> str:
    content = [{
        "type": "text",
        "text": f"PUBLICATION: {pub_name}\nPOST TITLE: {title}\n\nPOST CONTENT:\n{text[:8000]}"
    }]
    for url in image_urls[:4]:
        result = image_to_base64(url)
        if result:
            b64data, media_type = result
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64data},
            })
            logger.info("  Image included: %s", url[:60])
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
        return response.content[0].text
    except Exception as exc:
        logger.error("Claude API error: %s", exc)
        return f"[Summary unavailable: {exc}]"

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(token: str, chat_id: str, message: str) -> bool:
    url    = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
    ok     = True
    for i, chunk in enumerate(chunks):
        prefix = f"[{i+1}/{len(chunks)}] " if len(chunks) > 1 else ""
        try:
            resp = requests.post(url, json={
                "chat_id": chat_id, "text": prefix + chunk, "parse_mode": "Markdown",
            }, timeout=10)
            if not resp.ok:
                resp = requests.post(url, json={
                    "chat_id": chat_id, "text": prefix + chunk,
                }, timeout=10)
            ok = ok and resp.ok
        except Exception as exc:
            logger.error("Telegram send error: %s", exc)
            ok = False
    return ok

# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def send_discord(webhook_url: str, message: str) -> bool:
    if not webhook_url:
        return False
    # Discord limit is 2000 chars per message
    chunks = [message[i:i+1900] for i in range(0, len(message), 1900)]
    ok = True
    for chunk in chunks:
        try:
            resp = requests.post(webhook_url, json={"content": chunk}, timeout=10)
            ok = ok and resp.ok
        except Exception as exc:
            logger.error("Discord send error: %s", exc)
            ok = False
    return ok

# ---------------------------------------------------------------------------
# Per-publication processing
# ---------------------------------------------------------------------------

def process_publication(session, pub_name, pub_url, cfg, claude, seen):
    posts = fetch_posts(session, pub_url)
    if not posts:
        logger.warning("[%s] No posts returned", pub_name)
        return

    new_posts = [p for p in posts if str(p.get("id")) not in seen]
    if not new_posts:
        logger.info("[%s] No new posts.", pub_name)
        return

    logger.info("[%s] %d new post(s) found.", pub_name, len(new_posts))

    for post in reversed(new_posts):
        post_id  = str(post.get("id"))
        slug     = post.get("slug", "")
        title    = post.get("title", "Untitled")
        post_url = post.get("canonical_url", f"{pub_url}/p/{slug}")
        pub_date = post.get("post_date", "")[:10]
        is_paid  = post.get("audience", "") in ("only_paid", "paid")

        logger.info("[%s] [%s] %s (paid=%s)", pub_name, pub_date, title, is_paid)

        full      = fetch_post_content(session, pub_url, slug)
        body_html = full.get("body_html", "") or post.get("body_html", "")

        if not body_html:
            logger.warning("  No body HTML -- may need login")
            discord_url = cfg.get("discord_webhook", "")
            if discord_url:
                send_discord(discord_url, f"**[{pub_name}] {title}**\n{pub_date}\n{post_url}\n\n_Could not fetch full content._")
            seen.add(post_id)
            save_seen(seen)
            continue

        text, image_urls = extract_text_and_images(body_html)
        logger.info("  Text: %d chars | Images: %d", len(text), len(image_urls))

        summary = summarize_with_claude(claude, pub_name, title, text, image_urls)
        label   = "PAID" if is_paid else "FREE"

        msg = (
            f"*[{pub_name}] {title}*\n"
            f"_{pub_date} | {label}_\n"
            f"{post_url}\n"
            f"{'='*40}\n\n"
            f"{summary}"
        )

        discord_url = cfg.get("discord_webhook", "")
        if discord_url:
            discord_msg = msg.replace("*", "**").replace("_", "")
            dok = send_discord(discord_url, discord_msg)
            logger.info("  Discord : %s", "OK" if dok else "FAILED")

        seen.add(post_id)
        save_seen(seen)
        time.sleep(2)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    cfg  = load_config()
    seen = load_seen()

    if "YOUR_" in cfg.get("anthropic_api_key", "YOUR_"):
        print("\nERROR: Please fill in anthropic_api_key in config.json\n")
        sys.exit(1)

    # Support both old single-url format and new multi-publication format
    publications = cfg.get("publications", [
        {"name": "OptionsAI", "url": cfg.get("substack_url", "")}
    ])

    claude  = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    if not load_cookies(session):
        print("\nERROR: Could not load cookies.json.")
        sys.exit(1)
    verify_login(session)

    interval  = cfg.get("check_interval_minutes", 60) * 60
    pub_names = ", ".join(p["name"] for p in publications)

    logger.info("="*50)
    logger.info("Substack Monitor started")
    logger.info("Publications: %s", pub_names)
    logger.info("Check every : %d minutes", cfg["check_interval_minutes"])
    logger.info("="*50)

    startup_msg = (f"Substack Monitor Started\n"
                   f"Watching: {pub_names}\n"
                   f"Checking every {cfg['check_interval_minutes']} minutes")
    if cfg.get("discord_webhook"):
        send_discord(cfg["discord_webhook"], startup_msg)

    while True:
        try:
            logger.info("Checking all publications...")
            for pub in publications:
                process_publication(session, pub["name"], pub["url"], cfg, claude, seen)
        except Exception as exc:
            logger.exception("Unexpected error: %s", exc)
        logger.info("Sleeping %d minutes...", cfg["check_interval_minutes"])
        time.sleep(interval)


if __name__ == "__main__":
    main()
