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
    # Migration : colonnes de résumé ajoutées pour le récap quotidien
    existing = {row[1] for row in conn.execute("PRAGMA table_info(seen_articles)")}
    for column, col_type in (
        ("titre_fr", "TEXT"),
        ("resume", "TEXT"),
        ("categorie", "TEXT"),
        ("note", "INTEGER"),
    ):
        if column not in existing:
            conn.execute(f"ALTER TABLE seen_articles ADD COLUMN {column} {col_type}")
    return conn


def is_seen(url: str, db_path: Path = DB_PATH) -> bool:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT 1 FROM seen_articles WHERE url = ?", (url,)).fetchone()
    return row is not None


def mark_seen(article: Article, summary=None, db_path: Path = DB_PATH) -> None:
    """À appeler une fois l'article traité, pour ne plus le revoir.

    Avec `summary` (un Summary du summarizer), le résumé est stocké aussi,
    ce qui alimente le récap quotidien.
    """
    with _connect(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO seen_articles
               (url, title, source, published_at, seen_at, titre_fr, resume, categorie, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                article.link,
                article.title,
                article.source,
                article.date.isoformat() if article.date else None,
                datetime.now(timezone.utc).isoformat(),
                summary.titre if summary else None,
                summary.resume if summary else None,
                summary.categorie if summary else None,
                summary.note_importance if summary else None,
            ),
        )


def summaries_of_day(hours: int = 24, db_path: Path = DB_PATH) -> list[dict]:
    """Les articles résumés des dernières `hours` heures, les mieux notés d'abord."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    with _connect(db_path) as conn:
        rows = conn.execute(
            """SELECT titre_fr, resume, categorie, note, source FROM seen_articles
               WHERE seen_at >= ? AND resume IS NOT NULL ORDER BY note DESC""",
            (cutoff.isoformat(),),
        ).fetchall()
    return [
        {"titre": r[0], "resume": r[1], "categorie": r[2], "note": r[3], "source": r[4]}
        for r in rows
    ]


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
