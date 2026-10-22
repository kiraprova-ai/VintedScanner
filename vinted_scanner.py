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
CHECK_INTERVAL_SECONDS = 10  # Ciclo rapido per drop in tempo reale

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

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
}


def load_webhooks_mapping():
    raw = os.getenv("DISCORD_WEBHOOKS_JSON", "")
    if raw.strip():
        try:
            return json.loads(raw)
        except Exception as exc:
            logging.error("Errore parsing DISCORD_WEBHOOKS_JSON: %s", exc)
    return {}


def create_vinted_session():
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    try:
        res = session.get(VINTED_BASE_URL, timeout=10)
        if res.status_code == 200:
            logging.info("Sessione Vinted inizializzata con cookie.")
        else:
            logging.warning("Inizializzazione Vinted: HTTP %s", res.status_code)
    except Exception as exc:
        logging.error("Errore connessione iniziale Vinted: %s", exc)
    return session


def fetch_items(session, search_text):
    url = f"{VINTED_BASE_URL}/api/v2/catalog/items"
    params = {
        "search_text": search_text,
        "order": "newest_first",
        "page": 1,
        "per_page": 20
    }
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{VINTED_BASE_URL}/catalog?search_text={requests.utils.quote(search_text)}",
        "x-anon-id": session.cookies.get("anon_id", "")
    }

    try:
        resp = session.get(url, params=params, headers=headers, timeout=10)
        if resp.status_code in (401, 403, 404):
            session.get(f"{VINTED_BASE_URL}/catalog", timeout=10)
            headers["x-anon-id"] = session.cookies.get("anon_id", "")
            resp = session.get(url, params=params, headers=headers, timeout=10)

        if resp.status_code == 200:
            return resp.json().get("items", [])
        return []
    except Exception as exc:
        logging.error("Errore fetch per '%s': %s", search_text, exc)
        return []


def is_relevant_item(normalized, query_text):
    target = query_text.lower().strip()
    title = normalized["title"].lower().replace("ü", "u")
    brand = normalized["brand"].lower().strip().replace("ü", "u")

    # Regola per FiveFourFive / 545
    if "five" in target or "545" in target:
        if "fivefourfive" in brand or "five four five" in brand or "545" in brand:
            return True
        if "fivefourfive" in title or "five four five" in title or re.search(r"\b545\b", title):
            return True

    # Regola per Perfect
    if target == "perfect":
        if "perfect" in brand:
            return True

        words = set(re.findall(r"\b\w+\b", title))
        if any(bad in words for bad in PAROLE_ESCLUSE):
            return False

        has_perfect_word = bool(re.search(r"\bperfect\b", title))
        has_clothing_term = any(c in words or c in title for c in PAROLE_VESTITI)
        return has_perfect_word and has_clothing_term

    # Regola per Stussy
    if target in ("stussy", "stüssy"):
        return "stussy" in brand or bool(re.search(r"\bstussy\b", title))

    # Regola generale per tutti gli altri brand
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
        response = requests.post(webhook_url, json=payload, timeout=8)
        if response.status_code in (200, 204):
            logging.info("Notifica Discord inviata all'istante per ID %s", item["id"])
        elif response.status_code == 429:
            retry_after = response.json().get("retry_after", 3)
            time.sleep(retry_after)
            requests.post(webhook_url, json=payload, timeout=8)
    except Exception as exc:
        logging.error("Errore invio notifica Discord: %s", exc)


def parse_vinted_item(item):
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


def main():
    import Config

    queries = getattr(Config, "queries", getattr(Config, "SEARCH_QUERIES", []))
    webhooks_map = load_webhooks_mapping()
    default_webhook = os.getenv("DISCORD_WEBHOOK_URL", "")

    session = create_vinted_session()
    seen_ids = set()
    is_initial_run = True

    logging.info("Sincronizzazione rapida del catalogo attuale...")

    while True:
        try:
            for q in queries:
                search_text = q.get("search_text", "")
                channel_key = q.get("channel_key", search_text)
                
                # Cerca prima la chiave specifica, poi il default
                webhook_url = webhooks_map.get(channel_key) or default_webhook

                if not webhook_url:
                    continue

                items = fetch_items(session, search_text)

                for raw_item in items:
                    norm = parse_vinted_item(raw_item)
                    item_id = norm["id"]

                    if item_id in seen_ids:
                        continue

                    seen_ids.add(item_id)

                    # Durante il primissimo giro memorizza e basta, poi invia subito
                    if is_initial_run:
                        continue

                    if is_relevant_item(norm, search_text):
                        send_discord_notification(webhook_url, norm)

                time.sleep(1.0)  # Pausa breve per reattività massima

            if is_initial_run:
                is_initial_run = False
                logging.info("Sincronizzazione completata! Monitoraggio drop attivo in tempo reale.")

        except Exception as exc:
            logging.error("Errore nel ciclo: %s", exc)
            session = create_vinted_session()

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
