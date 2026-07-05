"""
Jetski Watcher Bot (GitHub Actions edition)
Scans Craigslist regions near Wilmington, DE for Yamaha WaveRunners
under $12,000, extracts engine hours, and emails alerts.

Two modes:
    python jetski_watcher.py                  -> normal scan (runs every 30 min)
    python jetski_watcher.py --daily-summary  -> emails a digest of today's finds

Email credentials come from environment variables (GitHub Secrets):
    EMAIL_FROM, EMAIL_TO, EMAIL_APP_PASSWORD
"""

import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ----------------- YOUR CRITERIA -----------------
MAX_PRICE = 12000
KEYWORD = "yamaha"
REGIONS = [
    "delaware",
    "philadelphia",
    "southjersey",
    "baltimore",
    "easternshore",
    "annapolis",
]
HOT_HOURS = 60          # listings at/under this get flagged
INSTANT_ALERTS = True   # email immediately when a new match appears
                        # (set False if you only want the daily summary)

# ----------------- EMAIL (from GitHub Secrets) -----------------
EMAIL_FROM = os.environ.get("EMAIL_FROM", "")
# EMAIL_TO can be one address or several separated by commas,
# e.g. "you@gmail.com, dad@gmail.com"
EMAIL_TO = [a.strip() for a in os.environ.get("EMAIL_TO", "").split(",") if a.strip()]
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "")
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

# ----------------- INTERNALS -----------------
BASE = Path(__file__).parent
SEEN_FILE = BASE / "seen_listings.json"
DAILY_LOG = BASE / "daily_log.json"
EASTERN = timezone(timedelta(hours=-4))  # EDT; summaries keyed to Eastern days
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}
HOURS_RE = re.compile(r"(\d{1,4})\s*(?:hours|hrs|hr)\b", re.IGNORECASE)


def today_key():
    return datetime.now(EASTERN).strftime("%Y-%m-%d")


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return default


def send_email(subject, body):
    if not (EMAIL_FROM and EMAIL_TO and EMAIL_APP_PASSWORD):
        print("[email] credentials not set; skipping email")
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(EMAIL_TO)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(EMAIL_FROM, EMAIL_APP_PASSWORD)
        s.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    print(f"[email] sent: {subject}")


def format_listing(l):
    hours = f"{l['hours']} hrs" if l.get("hours") is not None else "hours not listed"
    hot = "  *** LOW HOURS ***" if (l.get("hours") is not None and l["hours"] <= HOT_HOURS) else ""
    return f"${l['price']:,} | {hours}{hot}\n{l['title']} ({l['location']})\n{l['url']}\n"


# ----------------- SCANNING -----------------
def search_region(region):
    results = []
    for category in ("boo", "sss"):
        url = (
            f"https://{region}.craigslist.org/search/{category}"
            f"?query={KEYWORD}+waverunner&max_price={MAX_PRICE}&sort=date"
        )
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  [warn] {region}/{category}: {e}")
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        for li in soup.select("li.cl-static-search-result"):
            a = li.find("a", href=True)
            title_el = li.select_one("div.title")
            price_el = li.select_one("div.price")
            loc_el = li.select_one("div.location")
            if not a or not title_el:
                continue
            title = title_el.get_text(strip=True)
            if "yamaha" not in title.lower():
                continue
            price = int(re.sub(r"[^\d]", "", price_el.get_text(strip=True)) or 0) if price_el else 0
            if price == 0 or price > MAX_PRICE:
                continue
            results.append({
                "url": a["href"],
                "title": title,
                "price": price,
                "location": loc_el.get_text(strip=True) if loc_el else region,
                "region": region,
            })
        time.sleep(2)
    return results


def fetch_hours(listing_url):
    try:
        resp = requests.get(listing_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    body = soup.select_one("#postingbody")
    text = body.get_text(" ", strip=True) if body else soup.get_text(" ", strip=True)
    matches = [int(m) for m in HOURS_RE.findall(text) if int(m) <= 2000]
    return min(matches) if matches else None


def run_scan():
    seen = set(load_json(SEEN_FILE, []))
    daily = load_json(DAILY_LOG, {})

    all_results = []
    for region in REGIONS:
        print(f"Searching {region}...")
        all_results.extend(search_region(region))

    fresh = list({r["url"]: r for r in all_results if r["url"] not in seen}.values())

    if fresh:
        print(f"Found {len(fresh)} new listing(s). Checking hours...")
        for l in fresh:
            l["hours"] = fetch_hours(l["url"])
            l["found_at"] = datetime.now(EASTERN).strftime("%I:%M %p")
            time.sleep(2)

        fresh.sort(key=lambda l: (l["hours"] is None, l["hours"] or 0, l["price"]))

        # Add to today's log
        key = today_key()
        daily.setdefault(key, []).extend(fresh)
        # Keep only the last 7 days in the log
        cutoff = sorted(daily.keys())[-7:]
        daily = {k: v for k, v in daily.items() if k in cutoff}
        DAILY_LOG.write_text(json.dumps(daily, indent=1))

        seen.update(l["url"] for l in fresh)
        SEEN_FILE.write_text(json.dumps(sorted(seen)))

        for l in fresh:
            print(format_listing(l))

        if INSTANT_ALERTS:
            body = "\n".join(format_listing(l) for l in fresh)
            send_email(f"Jetski Watcher: {len(fresh)} new Yamaha match(es)", body)
    else:
        print("No new listings this run.")


# ----------------- DAILY SUMMARY -----------------
def run_summary():
    daily = load_json(DAILY_LOG, {})
    key = today_key()
    finds = daily.get(key, [])

    if not finds:
        body = ("No new Yamaha WaveRunners under $12,000 appeared today "
                "across Delaware, Philly, South Jersey, Baltimore, "
                "Eastern Shore, or Annapolis Craigslist.\n\n"
                "The bot is running fine - just a quiet day.")
        send_email(f"Jetski Daily Summary {key}: nothing new", body)
        return

    finds.sort(key=lambda l: (l.get("hours") is None, l.get("hours") or 0, l["price"]))
    hot = [l for l in finds if l.get("hours") is not None and l["hours"] <= HOT_HOURS]

    lines = [f"{len(finds)} new listing(s) today, sorted by lowest hours:\n"]
    if hot:
        lines.append(f">>> {len(hot)} LOW-HOUR find(s) at or under {HOT_HOURS} hrs <<<\n")
    for l in finds:
        found = l.get("found_at", "")
        lines.append(f"[{found}] " + format_listing(l))
    send_email(f"Jetski Daily Summary {key}: {len(finds)} new find(s)", "\n".join(lines))


if __name__ == "__main__":
    if "--daily-summary" in sys.argv:
        run_summary()
    else:
        run_scan()