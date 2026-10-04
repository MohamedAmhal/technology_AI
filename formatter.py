"""Mise en forme des résumés en messages HTML pour Telegram (parse_mode=HTML)."""

import html
import re

from collector import Article
from summarizer import Summary

MIN_IMPORTANCE = 7

CATEGORY_EMOJIS = {
    "IA": "🤖",
    "Hardware": "🔧",
    "Logiciel": "💻",
    "Sécurité": "🔒",
    "Startups": "🚀",
    "Science": "🔬",
    "Société": "🌍",
    "Autre": "📰",
}


def _escape(text: str) -> str:
    """Échappe <, > et & — obligatoire, sinon Telegram rejette le message."""
    return html.escape(text, quote=False)


def _hashtag(text: str) -> str:
    """Transforme un libellé en hashtag Telegram valide (alphanumérique, sans accents gênants)."""
    tag = re.sub(r"[^0-9A-Za-zÀ-ÿ]+", "", text)
    return f"#{tag}" if tag else ""


def is_important(summary: Summary, min_importance: int = MIN_IMPORTANCE) -> bool:
    return summary.note_importance >= min_importance


def filter_important(
    items: list[tuple[Article, Summary]],
    min_importance: int = MIN_IMPORTANCE,
) -> list[tuple[Article, Summary]]:
    """Ne garde que l'essentiel, trié par note décroissante."""
    kept = [(a, s) for a, s in items if is_important(s, min_importance)]
    kept.sort(key=lambda pair: pair[1].note_importance, reverse=True)
    return kept


def format_message(article: Article, summary: Summary) -> str:
    """Construit le message HTML Telegram pour un article résumé."""
    emoji = CATEGORY_EMOJIS.get(summary.categorie, "📰")
    stars = "⭐" * summary.note_importance if summary.note_importance >= 9 else ""
    hashtags = " ".join(filter(None, [_hashtag(summary.categorie), _hashtag(article.source)]))

    lines = [
        f"{emoji} <b>{_escape(summary.titre)}</b> {stars}".rstrip(),
        "",
        _escape(summary.resume),
        "",
        f"💡 <i>{_escape(summary.pourquoi_important)}</i>",
        "",
        f"📊 Importance : <b>{summary.note_importance}/10</b> · 📌 {_escape(article.source)}",
        f'🔗 <a href="{html.escape(article.link, quote=True)}">Lire l’article</a>',
        "",
        hashtags,
    ]
    return "\n".join(lines).strip()


if __name__ == "__main__":
    from datetime import datetime, timezone

    article = Article(
        title="AI <beats> humans & co",
        link="https://example.com/article?a=1&b=2",
        date=datetime.now(timezone.utc),
        source="Hacker News",
        excerpt="",
    )
    summary = Summary(
        titre="Une IA bat <enfin> les humains & surprend tout le monde",
        resume="Résumé avec des caractères spéciaux : <, > et &. Deuxième phrase.",
        pourquoi_important="Parce que ça montre que l'échappement < & > fonctionne.",
        categorie="IA",
        note_importance=9,
    )
    print(format_message(article, summary))
    print("\n--- filtre ---")
    low = Summary("Peu important", "R.", "P.", "Autre", 4)
    kept = filter_important([(article, summary), (article, low)])
    print(f"{len(kept)} message(s) gardé(s) sur 2 (seuil ≥ {MIN_IMPORTANCE})")
