"""Pipeline de veille : collecter → dédoublonner → résumer → formater → envoyer sur Telegram."""

import logging
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from collector import collect
from formatter import format_message
from storage import filter_new, mark_seen, purge_old
from summarizer import summarize_all

load_dotenv(Path(__file__).parent / ".env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MAX_ARTICLES_PER_RUN = 10  # borne le coût LLM d'un run
MAX_POSTS = 5  # nombre de messages envoyés par run (les mieux notés)

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

    ranked = sorted(summarized, key=lambda pair: pair[1].note_importance, reverse=True)
    to_send = ranked[:MAX_POSTS]
    log.info("⭐ Sélection : top %d sur %d article(s) résumé(s)", len(to_send), len(ranked))
    for _, s in ranked:
        log.info("   %d/10 [%s] %s", s.note_importance, s.categorie, s.titre)

    sent = 0
    for article, summary in to_send:
        if send_telegram(format_message(article, summary)):
            sent += 1
            log.info("📬 Envoyé : %s", summary.titre)
        time.sleep(1)  # limite Telegram : ~1 msg/s par chat

    # Tout article résumé est marqué vu (même sous le seuil : inutile de le re-résumer).
    # Un article dont le résumé a échoué reste non-vu et sera retenté au prochain run.
    for article, _ in summarized:
        mark_seen(article)
    log.info("✅ Terminé : %d message(s) envoyé(s), %d article(s) marqués vus", sent, len(summarized))


if __name__ == "__main__":
    run()
