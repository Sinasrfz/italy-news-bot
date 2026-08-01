import hashlib
import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
REVIEW_CHANNEL_ID = os.environ["REVIEW_CHANNEL_ID"]

STATE_FILE = Path(".data/seen.json")
MAX_ITEMS_PER_RUN = 8
KEEP_HASHES = 3000
RECENT_HOURS = 12
REQUEST_TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
    )
}

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
        "name": "Google News Lavoro",
        "url": "https://news.google.com/rss/search?q=lavoro+Italia+when%3A1d&hl=it&gl=IT&ceid=IT%3Ait",
    },
    {
        "name": "Google News Trasporti",
        "url": "https://news.google.com/rss/search?q=sciopero+trasporti+Italia+when%3A1d&hl=it&gl=IT&ceid=IT%3Ait",
    },
    {
        "name": "Protezione Civile",
        "url": "https://api.protezionecivile.it/default/dpcPortalGenerateRss?categoria=notizia",
    },
]

KEYWORDS = [
    "italia", "italiano", "italiana", "roma", "milano", "napoli", "torino",
    "governo", "parlamento", "senato", "immigrazione", "migranti", "permesso",
    "cittadinanza", "sciopero", "trasporti", "treno", "aeroporto", "lavoro",
    "economia", "inflazione", "terremoto", "allerta", "maltempo", "incendio",
    "sanità", "scuola", "università", "pensione", "bonus", "affitto",
]

translator = GoogleTranslator(source="it", target="fa")


def load_seen() -> list[str]:
    if not STATE_FILE.exists():
        return []
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
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


def truncate(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[:limit].rsplit(" ", 1)[0] + "…"


def entry_time(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)


def relevant(title: str, summary: str) -> bool:
    text = f"{title} {summary}".lower()
    return any(word in text for word in KEYWORDS)


def make_hash(title: str, link: str) -> str:
    raw = f"{title.lower().strip()}|{link.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def safe_translate(text: str) -> str:
    text = clean_text(text)
    if not text:
        return ""
    try:
        translated = translator.translate(text)
        return clean_text(translated)
    except Exception as exc:
        print(f"Translation failed: {exc}")
        return text


def resolve_url(url: str) -> str:
    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        return response.url
    except Exception:
        return url


def extract_article_data(url: str) -> dict:
    result = {
        "final_url": url,
        "image": "",
        "description": "",
        "site_name": "",
    }

    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        response.raise_for_status()
        result["final_url"] = response.url

        soup = BeautifulSoup(response.text, "html.parser")

        def meta_content(*selectors):
            for selector in selectors:
                tag = soup.select_one(selector)
                if tag and tag.get("content"):
                    return clean_text(tag["content"])
            return ""

        result["image"] = meta_content(
            'meta[property="og:image"]',
            'meta[name="twitter:image"]',
            'meta[property="twitter:image"]',
        )
        result["description"] = meta_content(
            'meta[property="og:description"]',
            'meta[name="description"]',
            'meta[name="twitter:description"]',
        )
        result["site_name"] = meta_content(
            'meta[property="og:site_name"]',
            'meta[name="application-name"]',
        )

        if result["image"]:
            result["image"] = urljoin(response.url, result["image"])

    except Exception as exc:
        print(f"Article extraction failed for {url}: {exc}")

    return result


def build_summary(title: str, rss_summary: str, page_description: str) -> str:
    candidates = [
        clean_text(page_description),
        clean_text(rss_summary),
    ]

    source_text = next((text for text in candidates if len(text) >= 60), "")
    if not source_text:
        source_text = title

    source_text = truncate(source_text, 900)
    translated = safe_translate(source_text)

    if translated == title:
        return translated

    return truncate(translated, 700)


def telegram_request(method: str, *, data=None, json_data=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    response = requests.post(
        url,
        data=data,
        json=json_data,
        timeout=40,
    )
    if not response.ok:
        print(f"Telegram error {response.status_code}: {response.text}")
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload)
    return payload


def send_news(item: dict) -> None:
    title_fa = html.escape(item["title_fa"])
    summary_fa = html.escape(item["summary_fa"])
    source = html.escape(item["source_name"])
    article_url = html.escape(item["article_url"], quote=True)

    date_text = "نامشخص"
    if item["published"]:
        date_text = item["published"].astimezone().strftime("%d/%m/%Y %H:%M")

    caption = (
        f"🇮🇹 <b>{title_fa}</b>\n\n"
        f"{summary_fa}\n\n"
        f"📰 منبع: {source}\n"
        f"🕒 {date_text}\n"
        f"🔗 <a href=\"{article_url}\">مشاهده خبر اصلی</a>\n\n"
        f"وضعیت: منتظر بررسی"
    )

    if item["image_url"]:
        try:
            telegram_request(
                "sendPhoto",
                data={
                    "chat_id": REVIEW_CHANNEL_ID,
                    "photo": item["image_url"],
                    "caption": truncate(caption, 1000),
                    "parse_mode": "HTML",
                },
            )
            return
        except Exception as exc:
            print(f"Photo send failed: {exc}")

    telegram_request(
        "sendMessage",
        json_data={
            "chat_id": REVIEW_CHANNEL_ID,
            "text": caption,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
    )


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
                "feed_name": feed_info["name"],
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


def main():
    seen = load_seen()
    seen_set = set(seen)
    new_hashes = []
    sent = 0

    for raw_item in collect_entries():
        item_hash = make_hash(raw_item["title"], raw_item["link"])
        if item_hash in seen_set:
            continue

        article_url = resolve_url(raw_item["link"])
        page = extract_article_data(article_url)

        title_fa = safe_translate(raw_item["title"])
        summary_fa = build_summary(
            raw_item["title"],
            raw_item["summary"],
            page["description"],
        )

        source_name = page["site_name"] or raw_item["feed_name"]

        item = {
            "title_fa": title_fa,
            "summary_fa": summary_fa,
            "source_name": source_name,
            "article_url": page["final_url"] or article_url,
            "image_url": page["image"],
            "published": raw_item["published"],
        }

        try:
            send_news(item)
            seen_set.add(item_hash)
            new_hashes.append(item_hash)
            sent += 1
        except Exception as exc:
            print(f"Failed to send item: {exc}")

        time.sleep(2)

        if sent >= MAX_ITEMS_PER_RUN:
            break

    save_seen(seen + new_hashes)
    print(f"Sent {sent} new Persian items.")


if __name__ == "__main__":
    main()
