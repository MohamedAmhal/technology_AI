"""Pipeline de veille : collecter → dédoublonner → résumer → formater → envoyer sur Telegram."""

import logging
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from collector import collect
from formatter import format_digest, format_message
from storage import filter_new, mark_seen, purge_old, summaries_of_day
from summarizer import make_daily_digest, summarize_all

load_dotenv(Path(__file__).parent / ".env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MAX_ARTICLES_PER_RUN = 10  # borne le coût LLM d'un run
MAX_POSTS = 5  # nombre de messages envoyés par run (les mieux notés)

# Ordre de priorité d'envoi : l'IA d'abord, puis l'informatique, puis le reste.
CATEGORY_PRIORITY = {"IA": 0, "Logiciel": 1, "Hardware": 1, "Sécurité": 1}
OTHER_PRIORITY = 2  # Startups, Science, Société, Autre

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("veille")


def send_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, json=payload, timeout=30)
        data = response.json()
        if not data.get("ok"):
            log.error("Telegram a refusé le message : %s", data.get("description"))
            return False
        return True
    except requests.RequestException as e:
        log.error("Envoi Telegram échoué : %r", e)
        return False


def run() -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID manquants dans .env")

    purged = purge_old()
    log.info("🧹 Purge : %d entrée(s) de plus de 7 jours supprimée(s)", purged)

    articles = collect()
    log.info("📡 Collecte : %d articles sur %d sources", len(articles), len({a.source for a in articles}))

    new_articles = filter_new(articles)
    log.info("🔎 Dédoublonnage : %d nouveaux articles (<24h)", len(new_articles))
    if len(new_articles) > MAX_ARTICLES_PER_RUN:
        log.info("   (limités aux %d plus récents pour ce run)", MAX_ARTICLES_PER_RUN)
        new_articles = new_articles[:MAX_ARTICLES_PER_RUN]
    if not new_articles:
        log.info("Rien de nouveau, fin.")
        return

    summarized = summarize_all(new_articles)
    log.info("🤖 Résumés : %d/%d réussis", len(summarized), len(new_articles))

    ranked = sorted(
        summarized,
        key=lambda pair: (
            CATEGORY_PRIORITY.get(pair[1].categorie, OTHER_PRIORITY),
            -pair[1].note_importance,
        ),
    )
    to_send = ranked[:MAX_POSTS]
    log.info("⭐ Sélection : top %d sur %d (IA > informatique > reste, puis note)", len(to_send), len(ranked))
    for _, s in ranked:
        log.info("   %d/10 [%s] %s", s.note_importance, s.categorie, s.titre)

    sent = 0
    for article, summary in to_send:
        if send_telegram(format_message(article, summary)):
            sent += 1
            log.info("📬 Envoyé : %s", summary.titre)
        time.sleep(1)  # limite Telegram : ~1 msg/s par chat

    # Tout article résumé est marqué vu, avec son résumé (pour le récap du jour).
    # Un article dont le résumé a échoué reste non-vu et sera retenté au prochain run.
    for article, summary in summarized:
        mark_seen(article, summary)
    log.info("✅ Terminé : %d message(s) envoyé(s), %d article(s) marqués vus", sent, len(summarized))


def run_digest() -> None:
    """Récap de fin de journée : condense les résumés des dernières 24h en un seul message."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID manquants dans .env")

    items = summaries_of_day()
    log.info("🌙 Récap : %d article(s) résumé(s) dans les dernières 24h", len(items))
    if not items:
        log.info("Rien à récapituler, fin.")
        return

    digest = make_daily_digest(items)
    if digest and send_telegram(format_digest(digest)):
        log.info("📬 Récap envoyé : %s (%d points)", digest["titre"], len(digest["points"]))
    else:
        log.error("Récap non envoyé")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "digest":
        run_digest()
    else:
        run()
