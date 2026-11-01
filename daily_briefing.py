import calendar
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai import types


FEEDS = [
    "https://www.tagesschau.de/index~rss2.xml",
    "https://www.tagesschau.de/infoservices/alle-meldungen-100~rss2.xml",
]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "PersonalNewsBriefing/1.0"
NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(hours=24)


def allowed_url(url):
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname in {"tagesschau.de", "www.tagesschau.de"}
    )


def fetch(url):
    response = SESSION.get(url, timeout=30)
    response.raise_for_status()
    if not allowed_url(response.url):
        raise RuntimeError("Unexpected redirect away from Tagesschau.")
    return response


def collect_articles():
    articles = []
    seen = set()
    successful_feeds = 0

    for feed_url in FEEDS:
        try:
            feed = feedparser.parse(fetch(feed_url).content)
            if not feed.entries:
                continue
            successful_feeds += 1
        except Exception:
            print("A news feed could not be loaded.")
            continue

        for entry in feed.entries:
            link = entry.get("link", "").split("#")[0]
            if link in seen or not allowed_url(link):
                continue

            dates = [
                entry.get("published_parsed"),
                entry.get("updated_parsed"),
            ]
            dates = [
                datetime.fromtimestamp(
                    calendar.timegm(value), timezone.utc
                )
                for value in dates
                if value is not None
            ]

            if not dates:
                continue

            date = max(dates)
            if not CUTOFF <= date <= NOW:
                continue

            seen.add(link)
            articles.append({
                "title": entry.get("title", ""),
                "url": link,
                "date": date.isoformat(),
                "feed_excerpt": BeautifulSoup(
                    entry.get("summary", ""), "html.parser"
                ).get_text(" ", strip=True),
            })

    if not successful_feeds:
        raise RuntimeError("No Tagesschau feed could be read.")

    return articles[:45]


def add_article_text(article):
    try:
        soup = BeautifulSoup(
            fetch(article["url"]).content, "html.parser"
        )
        paragraphs = soup.select("p.textabsatz")

        if not paragraphs:
            container = soup.find("article") or soup.find("main")
            paragraphs = container.find_all("p") if container else []

        text = "\n".join(
            paragraph.get_text(" ", strip=True)
            for paragraph in paragraphs
            if len(paragraph.get_text(" ", strip=True)) > 60
        )

        if len(text) < 400:
            return None

        article["text"] = text[:16000]
        article["text_truncated"] = len(text) > 16000
        return article
    except Exception:
        print("An article could not be extracted; skipping it.")
        return None
    finally:
        time.sleep(0.4)


def generate_briefing(articles):
    instructions = Path("briefing_prompt.txt").read_text(
        encoding="utf-8"
    )
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    payload = {
        "current_time_utc": NOW.isoformat(),
        "window_start_utc": CUTOFF.isoformat(),
        "candidate_count": len(articles),
        "articles": articles,
    }

    response = client.models.generate_content(
        model=MODEL,
        contents=json.dumps(payload, ensure_ascii=False),
        config=types.GenerateContentConfig(
            system_instruction=(
                instructions
                + "\nUse plain text, not Markdown tables or bold markers."
                + "\nThese articles are a feed-based candidate sample, "
                "not an exhaustive archive of every article."
                + "\nIf text_truncated is true, the article text is partial."
                + "\nEnd with: «این گزارش با کمک هوش مصنوعی تهیه شده؛ "
                "برای جزئیات و بررسی صحت، منبع اصلی را بخوانید.»"
            ),
            temperature=0.2,
            max_output_tokens=24000,
        ),
    )

    if not response.text:
        raise RuntimeError("Gemini returned an empty briefing.")

    if response.candidates:
        reason = str(response.candidates[0].finish_reason)
        if "MAX_TOKENS" in reason:
            raise RuntimeError(
                "The briefing exceeded the output limit; nothing sent."
            )

    date = NOW.astimezone(ZoneInfo("Europe/Berlin")).strftime(
        "%Y-%m-%d"
    )
    return (
        f"خبرهای تاگس‌شاو | {date}\n"
        "مرور خبرهای ۲۴ ساعت گذشته از مطالب قابل دریافت\n\n"
        + response.text.strip()
    )


def split_messages(text, limit=3000):
    while text:
        if len(text) <= limit:
            yield text
            break
        end = text.rfind("\n", 0, limit)
        if end < limit // 2:
            end = limit
        yield text[:end]
        text = text[end:].lstrip()


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    parts = list(split_messages(text))

    for number, part in enumerate(parts, start=1):
        try:
            response = SESSION.post(
                endpoint,
                json={
                    "chat_id": chat_id,
                    "text": f"({number}/{len(parts)})\n\n{part}",
                    "link_preview_options": {"is_disabled": True},
                },
                timeout=30,
            )
            result = response.json()
        except Exception:
            raise RuntimeError(
                "Telegram delivery failed. Some earlier parts may "
                "have arrived; check before running again."
            ) from None

        if not result.get("ok"):
            raise RuntimeError(
                "Telegram rejected a message. Check the chat and token."
            )

        time.sleep(1.2)

    print(f"Briefing sent in {len(parts)} Telegram messages.")


def main():
    candidates = collect_articles()
    articles = []

    for candidate in candidates:
        article = add_article_text(candidate)
        if article:
            articles.append(article)
        if len(articles) >= 25:
            break

    print(f"Retrieved readable text for {len(articles)} articles.")

    if not articles:
        raise RuntimeError(
            "No readable articles from the past 24 hours. Nothing sent."
        )

    briefing = generate_briefing(articles)

    if "--send" not in sys.argv:
        print(
            "Briefing generated successfully. Nothing sent. "
            "Use --send to deliver it."
        )
        return

    send_telegram(briefing)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            f"Briefing failed ({type(error).__name__}). "
            "Check feed availability, API access, quotas, and secrets."
        )
        sys.exit(1)
