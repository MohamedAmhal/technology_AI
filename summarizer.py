"""Résumé d'articles via l'API Gemini (Google AI Studio), avec sortie JSON structurée."""

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from dotenv import load_dotenv

from collector import Article

load_dotenv(Path(__file__).parent / ".env")

API_KEY = os.getenv("GEMINI_API_KEY")
# Modèles tentés dans l'ordre : si le premier épuise ses retries (saturation 503…), on passe au suivant.
MODELS = [
    os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
    os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.7-flash"),
]
API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Free tier Gemini : ~10 requêtes/minute → 6 s entre deux appels.
PAUSE_BETWEEN_CALLS = 6
MAX_RETRIES = 3
RETRY_BASE_DELAY = 10

CATEGORIES = ["IA", "Hardware", "Logiciel", "Sécurité", "Startups", "Science", "Société", "Autre"]

log = logging.getLogger("veille.summarizer")

PROMPT_TEMPLATE = """Tu es un assistant de veille technologique pour un lecteur francophone.
Analyse cet article et réponds UNIQUEMENT avec un objet JSON contenant :
- "titre" : le titre traduit/reformulé en français, clair et informatif
- "resume" : un résumé en 2-3 phrases, en français
- "pourquoi_important" : 1-2 phrases expliquant pourquoi c'est important (ou pas)
- "categorie" : une seule valeur parmi {categories}
- "note_importance" : un entier de 1 (anecdotique) à 10 (majeur)

Article :
Titre : {title}
Source : {source}
Extrait : {excerpt}
Lien : {link}

essayer de simplifier le texte et expliquer les termes techniques si possible, mais ne pas inventer d'informations. Ne pas inclure de liens dans le résumé. Ne pas inclure de texte hors du JSON, 
"""

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "titre": {"type": "STRING"},
        "resume": {"type": "STRING"},
        "pourquoi_important": {"type": "STRING"},
        "categorie": {"type": "STRING", "enum": CATEGORIES},
        "note_importance": {"type": "INTEGER"},
    },
    "required": ["titre", "resume", "pourquoi_important", "categorie", "note_importance"],
}


@dataclass
class Summary:
    titre: str
    resume: str
    pourquoi_important: str
    categorie: str
    note_importance: int


def _call_gemini(prompt: str, model: str) -> str:
    """Appelle l'API avec retries (backoff exponentiel, Retry-After respecté sur 429)."""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
            "temperature": 0.3,
            # Pas de "thinking" pour un simple résumé : réponse bien plus rapide,
            # et évite les réponses interminables quand le modèle est sous charge.
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                API_URL_TEMPLATE.format(model=model),
                params={"key": API_KEY},
                json=payload,
                timeout=60,
            )
            if response.status_code == 200:
                return response.json()["candidates"][0]["content"]["parts"][0]["text"]
            if response.status_code not in (429, 500, 502, 503, 504):
                # Erreur non récupérable (clé invalide, modèle inconnu…) : inutile de retenter
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
            delay = RETRY_BASE_DELAY * 2 ** (attempt - 1)
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                delay = max(delay, int(retry_after))
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
        except (requests.RequestException, KeyError, IndexError) as e:
            last_error = repr(e)
            delay = RETRY_BASE_DELAY * 2 ** (attempt - 1)
        if attempt < MAX_RETRIES:
            log.warning("⏳ %s — retry %d/%d dans %ds", last_error, attempt, MAX_RETRIES, delay)
            time.sleep(delay)
    raise RuntimeError(f"Appel Gemini échoué après {MAX_RETRIES} tentatives : {last_error}")


def _parse_summary(raw: str) -> Summary:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    data = json.loads(text)
    return Summary(
        titre=str(data["titre"]).strip(),
        resume=str(data["resume"]).strip(),
        pourquoi_important=str(data["pourquoi_important"]).strip(),
        categorie=data["categorie"] if data["categorie"] in CATEGORIES else "Autre",
        note_importance=min(10, max(1, int(data["note_importance"]))),
    )


def summarize(article: Article) -> Summary | None:
    """Résume un article ; renvoie None si l'appel ou le parsing échoue définitivement."""
    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY manquante dans .env (clé à créer sur https://aistudio.google.com/apikey)")
    prompt = PROMPT_TEMPLATE.format(
        categories=", ".join(CATEGORIES),
        title=article.title,
        source=article.source,
        excerpt=article.excerpt or "(pas d'extrait)",
        link=article.link,
    )
    last_error: Exception | None = None
    for model in MODELS:
        try:
            return _parse_summary(_call_gemini(prompt, model))
        except (RuntimeError, json.JSONDecodeError, KeyError, ValueError) as e:
            last_error = e
            if model != MODELS[-1]:
                log.warning("🔀 %s indisponible, bascule sur %s", model, MODELS[MODELS.index(model) + 1])
    log.error("❌ Résumé impossible pour « %s » : %s", article.title[:60], last_error)
    return None


def summarize_all(articles: list[Article]) -> list[tuple[Article, Summary]]:
    """Résume une liste d'articles, avec une pause entre chaque appel (rate limit free tier)."""
    results = []
    for i, article in enumerate(articles):
        if i > 0:
            time.sleep(PAUSE_BETWEEN_CALLS)
        summary = summarize(article)
        if summary:
            results.append((article, summary))
    return results


if __name__ == "__main__":
    from collector import collect
    from storage import filter_new

    articles = filter_new(collect())[:3]
    print(f"Test sur {len(articles)} articles…\n")
    for article, s in summarize_all(articles):
        print(f"[{s.categorie}] {s.note_importance}/10 — {s.titre}")
        print(f"  {s.resume}")
        print(f"  💡 {s.pourquoi_important}")
        print(f"  🔗 {article.link}\n")
