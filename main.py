import hashlib
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
REVIEW_CHANNEL_ID = os.environ["REVIEW_CHANNEL_ID"]

STATE_FILE = Path(".data/seen.json")
MAX_ITEMS_PER_RUN = 10
KEEP_HASHES = 2500
RECENT_HOURS = 8

FEEDS = [
    {
        "name": "Google News Italia",
        "url": "https://news.google.com/rss/search?q=Italia+when%3A1d&hl=it&gl=IT&ceid=IT%3Ait",
    },
    {
        "name": "Google News Immigrazione",
        "url": "https://news.google.com/rss/search?q=immigrazione+Italia+when%3A1d&hl=it&gl=IT&ceid=IT%3Ait",
    },
    {
        "name": "Protezione Civile",
        "url": "https://api.protezionecivile.it/default/dpcPortalGenerateRss?categoria=notizia",
    },
    {
        "name": "Protezione Civile - Comunicati",
        "url": "https://api.protezionecivile.it/default/dpcPortalGenerateRss?categoria=comunicato_stampa",
    },
]

KEYWORDS = [
    "italia", "italiano", "italiana", "roma", "milano", "napoli", "torino",
    "governo", "parlamento", "senato", "meloni", "immigrazione", "migranti",
    "permesso", "cittadinanza", "sciopero", "trasporti", "treno", "aeroporto",
    "lavoro", "economia", "inflazione", "terremoto", "allerta", "maltempo",
    "protezione civile", "incendio", "sanità", "scuola", "università",
]


def load_seen() -> list[str]:
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_seen(items: list[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(items[-KEEP_HASHES:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def entry_time(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)


def relevant(title: str, summary: str) -> bool:
    text = f"{title} {summary}".lower()
    return any(keyword in text for keyword in KEYWORDS)


def make_hash(title: str, link: str) -> str:
    raw = f"{title.lower().strip()}|{link.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def send_to_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={
            "chat_id": REVIEW_CHANNEL_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload)


def collect_entries():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=RECENT_HOURS)
    collected = []

    for feed_info in FEEDS:
        feed = feedparser.parse(feed_info["url"])
        for entry in feed.entries:
            title = clean_text(entry.get("title", ""))
            summary = clean_text(entry.get("summary", ""))
            link = entry.get("link", "").strip()
            published = entry_time(entry)

            if not title or not link:
                continue
            if published and published < cutoff:
                continue
            if feed_info["name"].startswith("Google") and not relevant(title, summary):
                continue

            collected.append({
                "source": feed_info["name"],
                "title": title,
                "summary": summary,
                "link": link,
                "published": published,
            })

    collected.sort(
        key=lambda item: item["published"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return collected


def format_message(item) -> str:
    title = html.escape(item["title"])
    source = html.escape(item["source"])
    link = html.escape(item["link"], quote=True)
    date_text = ""
    if item["published"]:
        date_text = item["published"].astimezone().strftime("%d/%m/%Y %H:%M")

    return (
        f"📰 <b>{title}</b>\n\n"
        f"منبع جمع‌آوری: {source}\n"
        f"زمان: {date_text or 'نامشخص'}\n\n"
        f"🔗 <a href=\"{link}\">باز کردن خبر اصلی</a>\n\n"
        f"وضعیت: منتظر بررسی و ترجمه"
    )


def main():
    seen = load_seen()
    seen_set = set(seen)
    new_hashes = []
    sent = 0

    for item in collect_entries():
        item_hash = make_hash(item["title"], item["link"])
        if item_hash in seen_set:
            continue

        send_to_telegram(format_message(item))
        seen_set.add(item_hash)
        new_hashes.append(item_hash)
        sent += 1

        if sent >= MAX_ITEMS_PER_RUN:
            break

    save_seen(seen + new_hashes)
    print(f"Sent {sent} new items.")


if __name__ == "__main__":
    main()
