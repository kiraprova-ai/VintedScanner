#!/usr/bin/env python3
"""Search Vinted and notify users about newly listed matching items."""
import email.utils
import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

import Config


PROJECT_DIR = Path(__file__).resolve().parent
ITEMS_DATABASE_PATH = PROJECT_DIR / "vinted_items.txt"
LOG_PATH = PROJECT_DIR / "vinted_scanner.log"
REQUEST_TIMEOUT_SECONDS = 25
CHECK_INTERVAL_SECONDS = 6

# Filtro rigido a 5 minuti (300 secondi): annunci più vecchi vengono scartati all'istante
MAX_ITEM_AGE_SECONDS = 300

# Parole escluse per il brand 'Perfect' (no accessori non voluti)
PERFECT_PAROLE_ESCLUSE = {
    "cap", "hat", "gorro", "gorra", "cappello", "berretto", "beanie",
    "gloves", "guanti", "guantes", "gants",
    "scarf", "sciarpa", "bufanda", "echarpe",
    "cover", "custodia", "case", "scarpe", "shoes", "sneakers"
}

# Parole chiave d'abbigliamento per 'Perfect' (tute, felpe, giacche, pantaloni)
PERFECT_PAROLE_VESTITI = {
    "tuta", "tracksuit", "chandal", "survetement",
    "hoodie", "hoodies", "felpa", "felpe", "sweatshirt", "sudadera",
    "pant", "pants", "pantalone", "pantaloni", "pantalon", "pantalones", "jogger", "joggers",
    "jacket", "giacca", "chaqueta", "blouson", "bomber", "cappotto", "coat", "fleece", "pile",
    "tee", "t-shirt", "tshirt", "maglia", "maglietta", "camiseta"
}

WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) "
        "Gecko/20100101 Firefox/128.0"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,image/png,image/svg+xml,*/*;q=0.8"
    ),
    "Accept-Language": "it-IT,it;q=0.8,en-US;q=0.5,en;q=0.3",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "cross-site",
    "Sec-GPC": "1",
    "Priority": "u=0, i",
    "Pragma": "no-cache",
    "Cache-Control": "no-cache",
}


def configure_logging():
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        level=logging.INFO,
        handlers=[
            logging.StreamHandler(sys.stdout),
            RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=3)
        ]
    )


def load_webhooks_mapping():
    raw = os.getenv("DISCORD_WEBHOOKS_JSON", "")
    if raw.strip():
        try:
            return json.loads(raw)
        except Exception as exc:
            logging.error("Errore parsing DISCORD_WEBHOOKS_JSON: %s", exc)
    return {}


def build_catalog_url(marketplace_url):
    parsed_url = urlsplit(marketplace_url)
    hostname = parsed_url.hostname

    if parsed_url.scheme != "https" or not hostname:
        raise ValueError("vinted_url must be a valid HTTPS URL")

    if hostname.startswith("www."):
        hostname = hostname[4:]

    if not hostname.startswith("vinted."):
        raise ValueError("vinted_url must point to a Vinted marketplace")

    return urlunsplit(
        ("https", f"api.{hostname}", "/svc-catalogue/items", "", "")
    )


def build_api_headers(marketplace_url):
    marketplace_url = marketplace_url.rstrip("/")
    api_headers = {
        "User-Agent": WEB_HEADERS["User-Agent"],
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": WEB_HEADERS["Accept-Language"],
        "Platform": "web",
        "x-next-app": "marketplace-web",
        "Origin": marketplace_url,
        "Referer": f"{marketplace_url}/",
    }

    locale = getattr(Config, "vinted_locale", None)
    if locale:
        api_headers["Locale"] = locale

    return api_headers


def build_catalog_params(query, page=1):
    if not isinstance(query, dict):
        raise ValueError("each entry in queries must be a dictionary")

    filters = query.get("filters", {})
    params = {
        key: value
        for key, value in query.items()
        if key not in ("filters", "channel_key") and value not in (None, "")
    }

    params["order"] = "newest_first"
    params["page"] = page
    params["per_page"] = query.get("per_page", 20)

    if isinstance(filters, dict):
        for filter_code, filter_ids in filters.items():
            if isinstance(filter_ids, (list, tuple)):
                normalized_ids = [str(f_id) for f_id in filter_ids if f_id]
                if normalized_ids:
                    params[f"attribute_ids[{filter_code}]"] = ",".join(normalized_ids)

    channel_key = query.get("channel_key", query.get("search_text", "default"))
    return params, channel_key


def load_analyzed_items(database_path=ITEMS_DATABASE_PATH):
    database_path.touch(exist_ok=True)
    with database_path.open("r", encoding="utf-8", errors="ignore") as database:
        return {line.strip() for line in database if line.strip()}


def save_analyzed_item(item_id, database_path=ITEMS_DATABASE_PATH):
    with database_path.open("a", encoding="utf-8") as database:
        database.write(f"{item_id}\n")


def send_discord_notification(webhook_url, item):
    embed = {
        "title": f"👕 {item['title']}",
        "url": item["url"],
        "color": 0x00d2d3,
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

    if item.get("image"):
        embed["image"] = {"url": item["image"]}

    payload = {
        "username": "Captain Hook",
        "embeds": [embed]
    }

    try:
        response = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        if response.status_code in (200, 204):
            logging.info("Notifica inviata con successo: %s", item["title"])
            return True
        elif response.status_code == 429:
            retry_after = response.json().get("retry_after", 2)
            time.sleep(retry_after)
            requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
            return True
        else:
            logging.error("Errore invio Discord (%s): %s", response.status_code, response.text)
            return False
    except Exception as exc:
        logging.error("Eccezione invio Discord: %s", exc)
        return False


def get_catalog_items(session, catalog_url, params, api_headers):
    try:
        response = session.get(
            catalog_url,
            params=params,
            headers=api_headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as error:
        raise RuntimeError(f"Chiamata fallita: {error}") from error

    try:
        data = response.json()
    except ValueError as error:
        raise RuntimeError("Risposta non JSON") from error

    items = data.get("items")
    if not isinstance(items, list):
        raise RuntimeError("Formato items non valido")
    return items


def get_item_real_age_seconds(session, item_id):
    """Interroga direttamente i dettagli del capo per calcolare l'orario effettivo di caricamento."""
    try:
        url = f"https://www.vinted.it/api/v2/items/{item_id}"
        resp = session.get(url, headers=WEB_HEADERS, timeout=8)
        if resp.status_code == 200:
            data = resp.json().get("item", {})
            created_at = data.get("created_at_ts")
            if not created_at and "created_at" in data:
                raw_date = data.get("created_at")
                if isinstance(raw_date, str):
                    try:
                        dt = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                        created_at = int(dt.timestamp())
                    except Exception:
                        pass
            if created_at:
                now_ts = int(datetime.now(timezone.utc).timestamp())
                return now_ts - int(created_at)
    except Exception as exc:
        logging.debug("Impossibile recuperare timestamp dettagliato per item %s: %s", item_id, exc)
    return None


def normalize_item(item):
    if not isinstance(item, dict):
        return None

    item_id = item.get("id")
    item_title = item.get("title")
    item_url = item.get("url")
    if item_id is None or not item_title or not item_url:
        return None

    item_url = urljoin(f"{Config.vinted_url.rstrip('/')}/", str(item_url))

    price_data = item.get("price") or {}
    if isinstance(price_data, dict):
        amount = price_data.get("amount")
        currency = price_data.get("currency_code", "EUR")
        item_price = f"{amount} {currency}" if amount else "N/A"
    else:
        item_price = f"{price_data} EUR"

    photo_data = item.get("photo") or {}
    if not isinstance(photo_data, dict):
        photo_data = {}

    brand_title = item.get("brand_title") or "N/D"
    user_data = item.get("user") or {}
    seller = user_data.get("login") or "Privato"
    condition = item.get("status") or item.get("subtitle") or "Ottime condizioni"

    return {
        "id": str(item_id),
        "title": item_title,
        "brand": brand_title,
        "price": item_price,
        "condition": condition,
        "seller": seller,
        "url": item_url,
        "image": photo_data.get("full_size_url") or photo_data.get("url"),
    }


def is_relevant_item(item, search_text):
    target = search_text.lower().strip().replace("ü", "u").replace("ú", "u").replace("ù", "u")
    raw_title = item["title"].lower()
    raw_brand = item["brand"].lower().strip()

    title = raw_title.replace("ü", "u").replace("ú", "u").replace("ù", "u")
    brand = raw_brand.replace("ü", "u").replace("ú", "u").replace("ù", "u")

    # Match 545 / FiveFourFive
    if "545" in target or "five" in target:
        if "fivefourfive" in brand or "five four five" in brand or "545" in brand:
            return True
        if "fivefourfive" in title or "five four five" in title or re.search(r"\b545\b", title):
            return True
        return False

    # Filtro 'Perfect'
    if target == "perfect":
        if "perfect fit" in brand or "perfectfit" in brand:
            return False

        words = set(re.findall(r"\b\w+\b", title))
        if any(bad in words for bad in PERFECT_PAROLE_ESCLUSE):
            return False

        if brand == "perfect":
            return True

        has_perfect = bool(re.search(r"\bperfect\b", title))
        has_apparel = any(c in words or c in title for c in PERFECT_PAROLE_VESTITI)
        return has_perfect and has_apparel

    # Filtro rigoroso Supreme
    if target == "supreme":
        if brand == "supreme":
            return True
        if brand not in ("", "n/d", "senza marca", "vintage", "altro"):
            return False
        return bool(re.search(r"\bsupreme\b", title))

    # Filtro rigoroso Stüssy / Stussy
    if target in ("stussy", "stüssy"):
        if "stussy" in brand:
            return True
        if brand not in ("", "n/d", "senza marca", "vintage", "altro"):
            return False
        return bool(re.search(r"\bstussy\b", title))

    # Regola generale (Scuffers, Corvidae, ecc.)
    if target in brand or target in title:
        return True

    return False


def initialize_vinted_session(marketplace_url):
    session = requests.Session()
    session.get(marketplace_url, headers=WEB_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    session.get(f"{marketplace_url.rstrip('/')}/catalog", headers=WEB_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    return session


def main():
    configure_logging()
    catalog_url = build_catalog_url(Config.vinted_url)
    api_headers = build_api_headers(Config.vinted_url)
    webhooks_map = load_webhooks_mapping()
    default_webhook = os.getenv("DISCORD_WEBHOOK_URL", "")

    analyzed_items = load_analyzed_items()

    session = initialize_vinted_session(Config.vinted_url)
    anon_id = session.cookies.get("anon_id")
    if anon_id:
        api_headers["X-Anon-Id"] = anon_id

    # WARMUP INIZIALE: Registra le prime 2 pagine di annunci già presenti per escluderli
    logging.info("--- AVVIO WARMUP: Archiviazione annunci pre-esistenti ---")
    for q in Config.queries:
        for page in (1, 2):
            params, channel_key = build_catalog_params(q, page=page)
            search_text = params.get("search_text", channel_key)
            try:
                items = get_catalog_items(session, catalog_url, params, api_headers)
                for raw in items:
                    item = normalize_item(raw)
                    if item:
                        analyzed_items.add(item["id"])
                        save_analyzed_item(item["id"])
            except Exception as exc:
                logging.warning("[%s - pag %d] Errore warmup: %s", search_text, page, exc)
            time.sleep(0.3)

    logging.info("--- WARMUP CONCLUSO: %d annunci in archivio. Monitoraggio drop attivi avviato! ---", len(analyzed_items))

    # LOOP REAL-TIME
    while True:
        try:
            for q in Config.queries:
                params, channel_key = build_catalog_params(q, page=1)
                search_text = params.get("search_text", channel_key)
                webhook_url = webhooks_map.get(channel_key) or default_webhook

                try:
                    items = get_catalog_items(session, catalog_url, params, api_headers)
                except Exception as err:
                    logging.warning("[%s] Errore API: %s. Refresh sessione...", search_text, err)
                    session = initialize_vinted_session(Config.vinted_url)
                    anon_id = session.cookies.get("anon_id")
                    if anon_id:
                        api_headers["X-Anon-Id"] = anon_id
                    continue

                for raw in items:
                    item = normalize_item(raw)
                    if not item:
                        continue

                    item_id = item["id"]

                    # 1. Se l'ID è già stato visto, skip
                    if item_id in analyzed_items:
                        continue

                    # Archivia subito per non ricontrollarlo
                    analyzed_items.add(item_id)
                    save_analyzed_item(item_id)

                    # 2. Controllo pertinenza (brand, parole escluse, filtri)
                    if not is_relevant_item(item, search_text):
                        continue

                    # 3. VERIFICA RIGIDA DELL'ETÀ REALE DELL'ANNUNCIO (Massimo 5 minuti / 300 secondi)
                    age_seconds = get_item_real_age_seconds(session, item_id)
                    if age_seconds is not None and age_seconds > MAX_ITEM_AGE_SECONDS:
                        logging.info("Scartato annuncio vecchio (%d sec fa): %s", age_seconds, item["title"])
                        continue

                    logging.info("🔥 DROP REALE IN DIRETTA: %s (Brand: %s)", item["title"], item["brand"])
                    if webhook_url:
                        send_discord_notification(webhook_url, item)

                time.sleep(0.5)

        except Exception as exc:
            logging.error("Errore loop scansione: %s", exc)

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
