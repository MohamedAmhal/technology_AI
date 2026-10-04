"""Mémoire des articles déjà traités (SQLite), pour éviter les doublons entre deux runs."""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from collector import Article

DB_PATH = Path(__file__).parent / "articles.db"
MAX_AGE_HOURS = 24
# On garde les URLs vues plus longtemps que la fenêtre de fraîcheur, sinon un
# article encore présent dans le flux redeviendrait « nouveau » après purge.
RETENTION_DAYS = 7


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_articles (
            url TEXT PRIMARY KEY,
            title TEXT,
            source TEXT,
            published_at TEXT,
            seen_at TEXT NOT NULL
        )
        """
    )
    return conn


def is_seen(url: str, db_path: Path = DB_PATH) -> bool:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT 1 FROM seen_articles WHERE url = ?", (url,)).fetchone()
    return row is not None


def mark_seen(article: Article, db_path: Path = DB_PATH) -> None:
    """À appeler une fois l'article traité, pour ne plus le revoir."""
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_articles (url, title, source, published_at, seen_at) VALUES (?, ?, ?, ?, ?)",
            (
                article.link,
                article.title,
                article.source,
                article.date.isoformat() if article.date else None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def filter_new(
    articles: list[Article],
    max_age_hours: int = MAX_AGE_HOURS,
    db_path: Path = DB_PATH,
) -> list[Article]:
    """Renvoie les articles publiés dans les dernières `max_age_hours` et jamais vus.

    Un article sans date est écarté : impossible de garantir sa fraîcheur.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    recent = [a for a in articles if a.date and a.date >= cutoff]
    with _connect(db_path) as conn:
        seen = {
            row[0]
            for row in conn.execute(
                f"SELECT url FROM seen_articles WHERE url IN ({','.join('?' * len(recent))})",
                [a.link for a in recent],
            )
        } if recent else set()
    return [a for a in recent if a.link not in seen]


def purge_old(retention_days: int = RETENTION_DAYS, db_path: Path = DB_PATH) -> int:
    """Supprime les entrées vues il y a plus de `retention_days` jours. Renvoie le nombre supprimé."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    with _connect(db_path) as conn:
        cursor = conn.execute("DELETE FROM seen_articles WHERE seen_at < ?", (cutoff.isoformat(),))
    return cursor.rowcount


if __name__ == "__main__":
    from collector import collect

    purged = purge_old()
    new_articles = filter_new(collect())
    print(f"{len(new_articles)} nouveaux articles (<{MAX_AGE_HOURS}h), {purged} entrées purgées\n")
    for article in new_articles:
        print(f"[{article.source}] {article.date:%Y-%m-%d %H:%M} — {article.title}")
        mark_seen(article)
