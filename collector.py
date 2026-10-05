"""Collecte d'articles depuis une liste de flux RSS définie dans sources.yaml."""

import html
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import mktime

import feedparser
import yaml

CONFIG_PATH = Path(__file__).parent / "sources.yaml"
EXCERPT_MAX_LENGTH = 1500
FETCH_TIMEOUT_SECONDS = 10


@dataclass
class Article:
    title: str
    link: str
    date: datetime | None
    source: str
    excerpt: str


def load_sources(config_path: Path = CONFIG_PATH) -> list[dict]:
    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config.get("sources", [])


def _parse_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return datetime.fromtimestamp(mktime(parsed), tz=timezone.utc)
    return None


def _make_excerpt(entry) -> str:
    raw = entry.get("summary", "") or ""
    text = html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > EXCERPT_MAX_LENGTH:
        text = text[:EXCERPT_MAX_LENGTH].rsplit(" ", 1)[0] + "…"
    return text


def collect(config_path: Path = CONFIG_PATH) -> list[Article]:
    """Lit tous les flux de sources.yaml et renvoie les articles, plus récents d'abord."""
    # feedparser utilise urllib sans timeout : un flux muet bloquerait la collecte
    socket.setdefaulttimeout(FETCH_TIMEOUT_SECONDS)
    articles: list[Article] = []
    for source in load_sources(config_path):
        feed = feedparser.parse(source["url"])
        if feed.bozo and not feed.entries:
            print(f"⚠️  Flux illisible, ignoré : {source['name']} ({feed.bozo_exception})")
            continue
        for entry in feed.entries:
            articles.append(
                Article(
                    title=entry.get("title", "").strip(),
                    link=entry.get("link", ""),
                    date=_parse_date(entry),
                    source=source["name"],
                    excerpt=_make_excerpt(entry),
                )
            )
    articles.sort(key=lambda a: a.date or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return articles


if __name__ == "__main__":
    for article in collect():
        date_str = article.date.strftime("%Y-%m-%d %H:%M") if article.date else "date inconnue"
        print(f"[{article.source}] {date_str} — {article.title}")
        print(f"  {article.link}")
        if article.excerpt:
            print(f"  {article.excerpt}")
        print()

