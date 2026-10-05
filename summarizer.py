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

PROMPT_TEMPLATE = """Tu es un assistant de veille techno pour un lecteur francophone pressé.
Objectif : qu'il comprenne ET retienne l'info en 10 secondes, comme si tu la racontais à un ami.

Règles d'écriture strictes :
- phrases courtes (15 mots max), une seule idée par phrase
- mots simples, pas de jargon ; si un terme technique est indispensable, explique-le en 3-4 mots
- commence par le fait principal, jamais par le contexte
- ne rien inventer : uniquement ce que dit l'article ; pas de liens dans le texte

Réponds UNIQUEMENT avec un objet JSON contenant :
- "titre" : titre en français, 10 mots max, qui dit le fait essentiel
- "resume" : 4 à 6 phrases donnant une vue complète de la news : d'abord le fait principal, puis les détails clés (chiffres, acteurs, comment ça marche), puis le contexte utile pour comprendre. Toujours des phrases courtes et simples.
- "pourquoi_important" : 1 à 2 phrases simples : ce que ça change concrètement.
- "categorie" : une seule valeur parmi {categories}
- "note_importance" : un entier de 1 (anecdotique) à 10 (majeur)

Article :
Titre : {title}
Source : {source}
Extrait : {excerpt}
Lien : {link}
"""

DIGEST_PROMPT = """Tu es un assistant de veille techno. Voici les articles résumés aujourd'hui.
Écris le récap du jour pour un lecteur francophone pressé, à lire en 30 secondes.

Règles :
- "titre" : un titre de récap court (8 mots max) qui capture LA tendance du jour
- "points" : 3 à 5 points, un par info majeure, dans cet ordre : IA d'abord, puis informatique (logiciel, hardware, sécurité), puis le reste
- chaque point : 1 phrase simple de 20 mots max, mémorisable, avec le fait + le détail marquant
- ignore les articles anecdotiques ; ne rien inventer

Articles du jour :
{articles}
"""

DIGEST_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "titre": {"type": "STRING"},
        "points": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["titre", "points"],
}

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


def _call_gemini(prompt: str, model: str, schema: dict = RESPONSE_SCHEMA) -> str:
    """Appelle l'API avec retries (backoff exponentiel, Retry-After respecté sur 429)."""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
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


def make_daily_digest(items: list[dict]) -> dict | None:
    """Condense les résumés du jour en un récap {titre, points}. None si échec."""
    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY manquante dans .env")
    lines = "\n".join(
        f"- [{it['categorie']}] ({it['note']}/10) {it['titre']} : {it['resume']}" for it in items
    )
    prompt = DIGEST_PROMPT.format(articles=lines)
    last_error: Exception | None = None
    for model in MODELS:
        try:
            raw = _call_gemini(prompt, model, schema=DIGEST_SCHEMA).strip()
            if raw.startswith("```"):
                raw = raw.strip("`").removeprefix("json").strip()
            data = json.loads(raw)
            if data.get("titre") and data.get("points"):
                return data
            raise ValueError(f"récap incomplet : {data}")
        except (RuntimeError, json.JSONDecodeError, ValueError) as e:
            last_error = e
            if model != MODELS[-1]:
                log.warning("🔀 %s indisponible, bascule sur %s", model, MODELS[MODELS.index(model) + 1])
    log.error("❌ Récap du jour impossible : %s", last_error)
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
