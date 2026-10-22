#!/usr/bin/env python3
"""Search Vinted and notify users about newly listed matching items in real time."""
import argparse
import datetime
import email.utils
import html
import json
import logging
import os
import re
import smtplib
import sys
import time
from email.message import EmailMessage
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

import Config

# Caricamento sicuro dei webhook dalla variabile d'ambiente di Render
raw_webhooks = os.environ.get("DISCORD_WEBHOOKS_JSON", "{}")
try:
    BRAND_WEBHOOKS = json.loads(raw_webhooks)
except Exception:
    BRAND_WEBHOOKS = {}

PROJECT_DIR = Path(__file__).resolve().parent
ITEMS_DATABASE_PATH = PROJECT_DIR / "vinted_items.txt"
LOG_PATH = PROJECT_DIR / "vinted_scanner.log"
REQUEST_TIMEOUT_SECONDS = 30
CHECK_INTERVAL_SECONDS = 30

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

PAROLE_VESTITI = (
    "felpa", "felpe", "hoodie", "hoodies", "crewneck", "zip",
    "sweatshirt", "sweater", "maglia", "maglione", "maglietta",
    "t-shirt", "tshirt", "tee", "pantalone", "pantaloni", "tuta",
    "tracksuit", "jogger", "jeans", "giacca", "giubbotto", "jacket"
)

PAROLE_ESCLUSE = (
    "condition", "conditions", "condizioni", "condizione", "stato", "shape",
    "cd", "dvd", "vinile", "vinyl", "album", "disco", "musica", "music",
    "cover", "film", "movie", "book", "libro", "ps4", "ps5", "xbox", "game"
)

STATUS_MAP = {
    "1": "Nuovo con cartellino",
    "2": "Nuovo senza cartellino",
    "3": "Ottime condizioni",
    "4": "Buone condizioni",
    "5": "Discreto",
    "new_with_tags": "Nuovo con cartellino",
    "new_without_tags": "Nuovo senza cartellino",
    "very_good": "Ottime condizioni",
    "good": "Buone condizioni",
    "satisfactory": "Discreto",
}


class CatalogError(RuntimeError):
    """Raised when Vinted does not return a valid catalogue response."""


def configure_logging():
    handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=5_000_000,
        backupCount=5,
    )
    logging.basicConfig(
        handlers=[handler, logging.StreamHandler(sys.stdout)],
        format="%(asctime)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )


def build_catalog_url(marketplace_url):
    parsed_url = urlsplit(marketplace_url)
    hostname = parsed_url.hostname
    if not hostname:
        raise ValueError("URL Vinted non valido")
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return urlunsplit(("https", f"api.{hostname}", "/svc-catalogue/items", "", ""))


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


def build_catalog_params(query):
    for required_key in ("page", "per_page", "order", "filters"):
        if required_key not in query:
            raise ValueError(f"missing required query key: {required_key}")

    params = {
        key: value
        for key, value in query.items()
        if key != "filters" and value not in (None, "")
    }
    for filter_code, filter_ids in query["filters"].items():
        normalized_ids = [str(filter_id) for filter_id in filter_ids]
        if normalized_ids:
            params[f"attribute_ids[{filter_code}]"] = ",".join(normalized_ids)
    return params


def load_analyzed_items(database_path=ITEMS_DATABASE_PATH):
    database_path.touch(exist_ok=True)
    with database_path.open("r", encoding="utf-8", errors="ignore") as database:
        return {line.strip() for line in database if line.strip()}


def save_analyzed_item(item_id, database_path=ITEMS_DATABASE_PATH):
    with database_path.open("a", encoding="utf-8") as database:
        database.write(f"{item_id}\n")


def fetch_item_details(session, marketplace_url, item_id, api_headers):
    """Estrae i dati specifici del capo per taglia e condizioni esatte."""
    parsed_url = urlsplit(marketplace_url)
    hostname = parsed_url.hostname or "vinted.it"
    if hostname.startswith("www."):
        hostname = hostname[4:]
    endpoint = f"https://www.{hostname}/api/v2/items/{item_id}"
    
    try:
        resp = session.get(endpoint, headers=api_headers, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("item", {})
    except Exception:
        pass
    return {}


def send_discord_notification(item_title, item_price, item_url, item_image, item_size="Taglia unica", item_status="Ottime condizioni", seller_info="N/D", brand_title=None, matched_query=None):
    webhook_url = BRAND_WEBHOOKS.get(str(matched_query).lower().strip())
    if not webhook_url:
        return True

    webhook_url = webhook_url.removesuffix("/slack")
    brand_display = brand_title if brand_title else matched_query

    desc = (
        f"🏷️ **Prezzo:** {item_price}\n"
        f"📏 **Taglia:** **{item_size}**\n"
        f"👕 **Brand:** {brand_display}\n"
        f"✨ **Condizioni:** {item_status}\n"
        f"👤 **Venditore:** {seller_info}\n\n"
        f"🛒 **[APRI L'ANNUNCIO SU VINTED]({item_url})**"
    )

    embed = {
        "title": f"👕 {item_title[:240]}",
        "url": item_url,
        "description": desc,
        "color": 0x09B1BA,
        "footer": {
            "text": "Vinted Steals • Drop in tempo reale"
        },
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }

    if item_image:
        embed["image"] = {"url": item_image}

    payload = {"embeds": [embed]}

    for attempt in range(2):
        try:
            response = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)

            if response.status_code == 429:
                try:
                    data = response.json()
                    wait_time = float(data.get("retry_after", 2))
                except Exception:
                    wait_time = float(response.headers.get("Retry-After", 2))

                if wait_time > 5.0:
                    logging.warning("Rate limit prolungato su [%s] (%.1f sec). Salto per sicurezza.", matched_query, wait_time)
                    return False

                logging.warning("Rate limit su [%s]: attesa di %.2f sec...", matched_query, wait_time)
                time.sleep(wait_time + 0.5)
                continue

            response.raise_for_status()
            logging.info("Notifica inviata per [%s]: %s", matched_query, item_title)
            time.sleep(1.0)
            return True

        except requests.exceptions.RequestException as error:
            logging.error("Errore invio notifica: %s", error)
            return False

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
        data = response.json()
        return data.get("items", [])
    except Exception as error:
        raise CatalogError(f"Catalogue request error: {error}") from error


def normalize_item(item, detail=None):
    item_id = item.get("id")
    item_title = item.get("title")
    item_url = item.get("url")
    if not item_id or not item_title or not item_url:
        return None

    full_url = urljoin(f"{Config.vinted_url.rstrip('/')}/", str(item_url))

    # Prezzo
    price_data = item.get("price") or {}
    amount = price_data.get("amount") or item.get("total_item_price")
    currency = price_data.get("currency_code", "EUR")
    price = f"{amount} {currency}" if amount else "N/A"

    # Immagine
    photo_data = item.get("photo") or {}
    image_url = photo_data.get("full_size_url") or photo_data.get("url")

    # Brand
    brand_title = item.get("brand_title") or (detail.get("brand_dto", {}).get("title") if detail else "")

    # Taglia
    size = None
    if detail:
        size = detail.get("size_title") or detail.get("size")
    if not size:
        size = item.get("size_title")
    if not size and isinstance(item.get("size"), dict):
        size = item.get("size", {}).get("title")
    if not size and isinstance(item.get("size"), str):
        size = item.get("size")
    if not size and item.get("subtitle"):
        size = str(item.get("subtitle")).split("•")[0].strip()

    if not size:
        size = "Taglia unica"

    # Condizioni
    status_label = None
    if detail:
        status_label = detail.get("status")
        if not status_label:
            raw_id = str(detail.get("status_id") or "").lower()
            status_label = STATUS_MAP.get(raw_id)
    if not status_label:
        raw_status = str(item.get("status_id") or item.get("status") or "").lower()
        status_label = STATUS_MAP.get(raw_status, item.get("status"))

    if not status_label or status_label == "N/D":
        status_label = "Ottime condizioni"

    # Venditore e feedback
    user_data = item.get("user") or (detail.get("user") if detail else {}) or {}
    seller_name = user_data.get("login") or "N/D"
    feedback_count = user_data.get("feedback_count")
    feedback_reputation = user_data.get("feedback_reputation")

    if feedback_count is not None and feedback_reputation is not None:
        rating = round(float(feedback_reputation) * 5, 1)
        seller_display = f"{seller_name} ({rating}⭐ • {feedback_count})"
    elif seller_name != "N/D":
        seller_display = seller_name
    else:
        seller_display = "N/D"

    return {
        "id": str(item_id),
        "title": str(item_title),
        "brand": str(brand_title),
        "size": str(size),
        "status": str(status_label),
        "seller": str(seller_display),
        "price": price,
        "url": full_url,
        "image": image_url,
    }


def is_relevant_item(normalized, query_text):
    target = query_text.lower().strip()
    title = normalized["title"].lower().replace("ü", "u")
    brand = normalized["brand"].lower().strip().replace("ü", "u")

    # Logica per Perfect (come prima, senza blocchi su altri brand)
    if target == "perfect":
        if "perfect" in brand:
            return True

        words = set(re.findall(r"\b\w+\b", title))
        if any(bad in words for bad in PAROLE_ESCLUSE):
            return False

        has_perfect_word = bool(re.search(r"\bperfect\b", title))
        has_clothing_term = any(c in words or c in title for c in PAROLE_VESTITI)
        return has_perfect_word and has_clothing_term

    # Logica per Stussy
    if target in ("stussy", "stüssy"):
        return "stussy" in brand or bool(re.search(r"\bstussy\b", title))

    if target in brand:
        return True

    pattern = rf"\b{re.escape(target)}\b"
    return bool(re.search(pattern, title))


def process_item(item, analyzed_items, session=None, api_headers=None, is_initial_run=False, matched_query=None):
    item_id = str(item.get("id"))
    if not item_id or item_id in analyzed_items:
        return True

    save_analyzed_item(item_id)
    analyzed_items.add(item_id)

    title_pre = str(item.get("title", "")).lower().replace("ü", "u")
    brand_pre = str(item.get("brand_title", "")).lower().replace("ü", "u")
    target = matched_query.lower().strip()

    if target == "perfect":
        if "perfect" not in title_pre and "perfect" not in brand_pre:
            return True
    elif target in ("stussy", "stüssy"):
        if "stussy" not in brand_pre and "stussy" not in title_pre:
            return True
    elif target not in brand_pre and not re.search(rf"\b{re.escape(target)}\b", title_pre):
        return True

    if is_initial_run:
        return True

    # Chiamata di dettaglio per avere taglia e condizioni reali
    detail = {}
    if session and api_headers:
        detail = fetch_item_details(session, Config.vinted_url, item_id, api_headers)

    normalized = normalize_item(item, detail=detail)
    if not normalized or not is_relevant_item(normalized, matched_query):
        return True

    return send_discord_notification(
        item_title=normalized["title"],
        item_price=normalized["price"],
        item_url=normalized["url"],
        item_image=normalized["image"],
        item_size=normalized.get("size", "Taglia unica"),
        item_status=normalized.get("status", "Ottime condizioni"),
        seller_info=normalized.get("seller", "N/D"),
        brand_title=normalized["brand"],
        matched_query=matched_query,
    )


def initialize_vinted_session(marketplace_url):
    session = requests.Session()
    session.get(marketplace_url, headers=WEB_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    return session


def main():
    configure_logging()
    catalog_url = build_catalog_url(Config.vinted_url)
    queries = [build_catalog_params(q) for q in Config.queries]
    api_headers = build_api_headers(Config.vinted_url)
    analyzed_items = load_analyzed_items()

    logging.info("Inizializzazione sessione Vinted...")
    session = initialize_vinted_session(Config.vinted_url)

    is_initial_run = True
    logging.info("Sincronizzazione iniziale degli annunci attuali in corso...")

    while True:
        try:
            for params in queries:
                matched_brand = params.get("search_text", "")
                try:
                    items = get_catalog_items(session, catalog_url, params, api_headers)
                    for item in items:
                        process_item(
                            item,
                            analyzed_items,
                            session=session,
                            api_headers=api_headers,
                            is_initial_run=is_initial_run,
                            matched_query=matched_brand,
                        )
                except CatalogError as err:
                    logging.warning("Errore richiesta per [%s]: %s", matched_brand, err)
                    time.sleep(1)

            if is_initial_run:
                is_initial_run = False
                logging.info("Sincronizzazione completata! In attesa di nuovi drop in tempo reale.")

            time.sleep(CHECK_INTERVAL_SECONDS)

        except requests.exceptions.RequestException:
            logging.info("Rinnovo sessione Vinted...")
            try:
                session = initialize_vinted_session(Config.vinted_url)
            except Exception:
                pass
            time.sleep(10)
        except Exception as error:
            logging.error("Errore ciclo principale: %s", error)
            time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
