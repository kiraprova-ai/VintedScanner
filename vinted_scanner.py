import os
import re
import json
import time
import random
import logging
from datetime import datetime, timezone
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

VINTED_BASE_URL = "https://www.vinted.it"
CHECK_INTERVAL_SECONDS = 30

PAROLE_ESCLUSE = {
    "cap", "hat", "gorro", "gorra", "cappello", "berretto", "beanie",
    "gloves", "guanti", "guantes", "gants",
    "scarf", "sciarpa", "bufanda", "echarpe"
}

PAROLE_VESTITI = {
    "jacket", "giacca", "chaqueta", "blouson", "bomber", "cappotto", "coat",
    "fleece", "pile", "polar", "forro",
    "pant", "pants", "pantalone", "pantaloni", "pantalon", "pantalones",
    "hoodie", "hoodies", "felpa", "felpe", "sweatshirt", "sudadera",
    "tee", "t-shirt", "tshirt", "maglia", "maglietta", "camiseta"
}

WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
}


def build_api_headers(session):
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.vinted.it/catalog",
        "Origin": "https://www.vinted.it",
    }


def initialize_vinted_session():
    session = requests.Session()
    logging.info("Inizializzazione sessione Vinted...")
    try:
        resp = session.get(VINTED_BASE_URL, headers=WEB_HEADERS, timeout=15)
        if resp.status_code == 200:
            logging.info("Sessione Vinted inizializzata con successo.")
        else:
            logging.warning("Inizializzazione sessione Vinted fallita: codice %s", resp.status_code)
    except Exception as exc:
        logging.error("Errore inizializzazione sessione Vinted: %s", exc)
    return session


def load_webhooks_mapping():
    raw = os.getenv("DISCORD_WEBHOOKS_JSON", "")
    if raw.strip():
        try:
            return json.loads(raw)
        except Exception as exc:
            logging.error("Errore nel parsing di DISCORD_WEBHOOKS_JSON: %s", exc)
    return {}


def normalize_item(item):
    item_id = str(item.get("id", ""))
    title = str(item.get("title", "")).strip()
    brand = str(item.get("brand_title", "")).strip()
    price = item.get("total_item_price") or item.get("price", "N/D")
    currency = item.get("currency", "EUR")
    url = item.get("url", "")
    if url and not url.startswith("http"):
        url = f"https://www.vinted.it{url}"

    photo = item.get("photo", {}) or {}
    image_url = photo.get("url") or photo.get("full_size_url", "")

    user = item.get("user", {}) or {}
    seller = user.get("login", "Privato")

    status = str(item.get("status", "")).strip()
    if not status:
        status = "Disponibile"

    return {
        "id": item_id,
        "title": title,
        "brand": brand,
        "price": f"{price} {currency}" if price != "N/D" else "N/D",
        "url": url,
        "image_url": image_url,
        "seller": seller,
        "condition": status,
    }


def is_relevant_item(normalized, query_text):
    target = query_text.lower().strip()
    title = normalized["title"].lower().replace("ü", "u")
    brand = normalized["brand"].lower().strip().replace("ü", "u")

    # Gestione FiveFourFive / 545
    if "five" in target or "545" in target:
        if "fivefourfive" in brand or "five four five" in brand or "545" in brand:
            return True
        if "fivefourfive" in title or "five four five" in title or re.search(r"\b545\b", title):
            return True

    # Gestione Perfect
    if target == "perfect":
        if "perfect" in brand:
            return True

        words = set(re.findall(r"\b\w+\b", title))
        if any(bad in words for bad in PAROLE_ESCLUSE):
            return False

        has_perfect_word = bool(re.search(r"\bperfect\b", title))
        has_clothing_term = any(c in words or c in title for c in PAROLE_VESTITI)
        return has_perfect_word and has_clothing_term

    # Gestione Stussy
    if target in ("stussy", "stüssy"):
        return "stussy" in brand or bool(re.search(r"\bstussy\b", title))

    if target in brand:
        return True

    pattern = rf"\b{re.escape(target)}\b"
    return bool(re.search(pattern, title))


def send_discord_notification(webhook_url, item):
    embed = {
        "title": f"👕 {item['title']}",
        "url": item["url"],
        "color": 3447003,
        "description": (
            f"🏷️ **Prezzo:** {item['price']}\n"
            f"👕 **Brand:** {item['brand']}\n"
            f"✨ **Condizioni:** {item['condition']}\n"
            f"👤 **Venditore:** {item['seller']}\n\n"
            f"🛒 **[APRI L'ANNUNCIO SU VINTED]({item['url']})**"
        ),
        "footer": {
            "text": "Vinted Steals • Drop in tempo reale"
        },
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    if item.get("image_url"):
        embed["image"] = {"url": item["image_url"]}

    payload = {
        "username": "Vinted Steals",
        "embeds": [embed]
    }

    try:
        response = requests.post(webhook_url, json=payload, timeout=10)
        if response.status_code in (200, 204):
            logging.info("Notifica Discord inviata per %s", item["id"])
        elif response.status_code == 429:
            retry_after = response.json().get("retry_after", 5)
            time.sleep(retry_after)
            requests.post(webhook_url, json=payload, timeout=10)
        else:
            logging.error("Errore invio Discord: %s - %s", response.status_code, response.text)
    except Exception as exc:
        logging.error("Eccezione durante invio Discord: %s", exc)


def fetch_vinted_items(session, query_dict):
    search_text = query_dict.get("search_text", "")
    params = {
        "search_text": search_text,
        "order": query_dict.get("order", "newest_first"),
        "page": query_dict.get("page", 1),
        "per_page": query_dict.get("per_page", 20),
    }

    # Prova prima con l'endpoint di ricerca standard
    api_url = f"{VINTED_BASE_URL}/api/v2/catalog/items"
    headers = build_api_headers(session)

    try:
        response = session.get(api_url, headers=headers, params=params, timeout=15)
        
        # Se risponde 404, fallback sull'endpoint /api/v2/items
        if response.status_code == 404:
            fallback_url = f"{VINTED_BASE_URL}/api/v2/items"
            response = session.get(fallback_url, headers=headers, params=params, timeout=15)

        if response.status_code in (401, 403):
            logging.warning("Sessione Vinted scaduta (%s). Reinizializzazione...", response.status_code)
            return None

        if response.status_code != 200:
            logging.warning("Risposta imprevista da Vinted per '%s': codice %s", search_text, response.status_code)
            return []

        data = response.json()
        return data.get("items", [])
    except Exception as exc:
        logging.error("Errore chiamata Vinted per '%s': %s", search_text, exc)
        return []


def run():
    import Config

    queries = getattr(Config, "queries", getattr(Config, "SEARCH_QUERIES", []))
    webhooks_map = load_webhooks_mapping()
    session = initialize_vinted_session()
    seen_ids = set()
    is_initial_run = True

    logging.info("Sincronizzazione iniziale degli annunci attuali in corso...")

    while True:
        try:
            for q in queries:
                search_text = q.get("search_text", "")
                channel_key = q.get("channel_key", search_text)
                webhook_url = webhooks_map.get(channel_key) or os.getenv("DISCORD_WEBHOOK_URL")

                if not webhook_url:
                    continue

                items = fetch_vinted_items(session, q)
                if items is None:
                    session = initialize_vinted_session()
                    continue

                for raw_item in items:
                    norm = normalize_item(raw_item)
                    item_id = norm["id"]

                    if item_id in seen_ids:
                        continue

                    seen_ids.add(item_id)

                    if is_initial_run:
                        continue

                    if is_relevant_item(norm, search_text):
                        send_discord_notification(webhook_url, norm)
                        time.sleep(1)

                time.sleep(random.uniform(2.0, 3.5))

            if is_initial_run:
                is_initial_run = False
                logging.info("Sincronizzazione iniziale completata. Da adesso verranno notificati i nuovi drop.")

        except Exception as exc:
            logging.error("Errore durante il ciclo di scansione: %s", exc)

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
