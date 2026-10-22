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

VINTED_DOMAIN = "www.vinted.it"
CHECK_INTERVAL_SECONDS = 8

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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Origin": f"https://{VINTED_DOMAIN}",
    "Referer": f"https://{VINTED_DOMAIN}/catalog",
    "Connection": "keep-alive"
}


def load_webhooks_mapping():
    raw = os.getenv("DISCORD_WEBHOOKS_JSON", "")
    if raw.strip():
        try:
            return json.loads(raw)
        except Exception as exc:
            logging.error("Errore parsing DISCORD_WEBHOOKS_JSON: %s", exc)
    return {}


class VintedClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.init_session()

    def init_session(self):
        try:
            # Visita prima la home per il set-cookie principale (_vinted_fr_session)
            resp = self.session.get(f"https://{VINTED_DOMAIN}/", timeout=15)
            # Seconda chiamata su catalog per abilitare l'anon_id
            self.session.get(f"https://{VINTED_DOMAIN}/catalog", timeout=15)
            logging.info("Sessione Vinted inizializzata. Cookie: %s", list(self.session.cookies.keys()))
        except Exception as e:
            logging.error("Errore inizializzazione Vinted: %s", e)

    def search(self, search_text):
        # Proviamo entrambi gli endpoint noti di Vinted (items e catalog/items)
        endpoints = [
            f"https://{VINTED_DOMAIN}/api/v2/catalog/items",
            f"https://{VINTED_DOMAIN}/api/v2/items"
        ]
        
        params = {
            "search_text": search_text,
            "order": "newest_first",
            "page": 1,
            "per_page": 20
        }
        
        # Inietta l'anon_id se presente nei cookie
        anon_id = self.session.cookies.get("anon_id", "")
        if anon_id:
            self.session.headers["x-anon-id"] = anon_id
            
        for url in endpoints:
            try:
                r = self.session.get(url, params=params, timeout=15)
                if r.status_code == 200:
                    data = r.json()
                    return data.get("items", [])
                elif r.status_code in (401, 403):
                    self.init_session()
            except Exception:
                continue

        return []


def is_relevant_item(normalized, query_text):
    target = query_text.lower().strip()
    title = normalized["title"].lower().replace("ü", "u")
    brand = normalized["brand"].lower().strip().replace("ü", "u")

    # 545 / FiveFourFive
    if "five" in target or "545" in target:
        if "fivefourfive" in brand or "five four five" in brand or "545" in brand:
            return True
        if "fivefourfive" in title or "five four five" in title or re.search(r"\b545\b", title):
            return True

    # Perfect
    if target == "perfect":
        if "perfect" in brand:
            return True
        words = set(re.findall(r"\b\w+\b", title))
        if any(bad in words for bad in PAROLE_ESCLUSE):
            return False
        has_perfect_word = bool(re.search(r"\bperfect\b", title))
        has_clothing_term = any(c in words or c in title for c in PAROLE_VESTITI)
        return has_perfect_word and has_clothing_term

    # Stussy
    if target in ("stussy", "stüssy"):
        return "stussy" in brand or bool(re.search(r"\bstussy\b", title))

    if target in brand or target in title:
        return True

    return False


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
        resp = requests.post(webhook_url, json=payload, timeout=8)
        if resp.status_code in (200, 204):
            logging.info("Notifica inviata su Discord per '%s'", item["title"])
        elif resp.status_code == 429:
            retry_after = resp.json().get("retry_after", 3)
            time.sleep(retry_after)
            requests.post(webhook_url, json=payload, timeout=8)
    except Exception as exc:
        logging.error("Errore invio Discord: %s", exc)


def parse_vinted_item(item):
    item_id = str(item.get("id", ""))
    title = str(item.get("title", "")).strip()
    brand = str(item.get("brand_title", "")).strip()
    price = item.get("total_item_price") or item.get("price", "N/D")
    currency = item.get("currency", "EUR")
    url = item.get("url", "")
    if url and not url.startswith("http"):
        url = f"https://{VINTED_DOMAIN}{url}"

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

    client = VintedClient()
    seen_ids = set()
    is_initial_run = True

    logging.info("Sincronizzazione catalogo...")

    while True:
        try:
            for q in queries:
                search_text = q.get("search_text", "")
                channel_key = q.get("channel_key", search_text)
                webhook_url = webhooks_map.get(channel_key) or default_webhook

                if not webhook_url:
                    continue

                items = client.search(search_text)

                if not is_initial_run:
                    logging.info("[%s] Articoli ricevuti: %d", search_text, len(items))

                for raw_item in items:
                    norm = parse_vinted_item(raw_item)
                    item_id = norm["id"]

                    if item_id in seen_ids:
                        continue

                    seen_ids.add(item_id)

                    if is_initial_run:
                        continue

                    logging.info("DROP TROVATO: %s (%s)", norm["title"], norm["brand"])

                    if is_relevant_item(norm, search_text):
                        send_discord_notification(webhook_url, norm)

                time.sleep(1.0)

            if is_initial_run:
                is_initial_run = False
                logging.info("Sincronizzazione completata! Elementi memorizzati: %d", len(seen_ids))

        except Exception as exc:
            logging.error("Errore nel loop: %s", exc)

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
