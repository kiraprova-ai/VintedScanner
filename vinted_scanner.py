#!/usr/bin/env python3
"""Search Vinted and notify users about newly listed matching items."""
import email.utils
import html
import json
import logging
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

import Config


PROJECT_DIR = Path(__file__).resolve().parent
ITEMS_DATABASE_PATH = PROJECT_DIR / "vinted_items.txt"
LOG_PATH = PROJECT_DIR / "vinted_scanner.log"
REQUEST_TIMEOUT_SECONDS = 30
CHECK_INTERVAL_SECONDS = 15

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

LEGACY_QUERY_KEYS = {
    "brand_ids",
    "catalog_ids",
    "color_ids",
    "material_ids",
    "size_ids",
    "status_ids",
}

CATALOG_QUERY_KEYS = {
    "channel_key",
    "currency",
    "filters",
    "order",
    "page",
    "per_page",
    "price_from",
    "price_to",
    "search_text",
}

CATALOG_ORDER_VALUES = {
    "newest_first",
    "price_high_to_low",
    "price_low_to_high",
    "relevance",
}


class CatalogError(RuntimeError):
    """Raised when Vinted does not return a valid catalogue response."""


def configure_logging():
    """Configure stream and file logging."""
    logging.basicConfig(
        format=(
            "%(asctime)s [%(levelname)s] %(message)s"
        ),
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
    """Build the country-specific catalogue API URL."""
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
    """Return headers for the Vinted web catalogue API."""
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
    """Validate a configured query and convert its filters to API parameters."""
    if not isinstance(query, dict):
        raise ValueError("each entry in queries must be a dictionary")

    for required_key in ("page", "per_page", "order"):
        if required_key not in query:
            raise ValueError(f"missing required query key: {required_key}")

    filters = query.get("filters", {})
    params = {
        key: value
        for key, value in query.items()
        if key not in ("filters", "channel_key") and value not in (None, "")
    }

    if isinstance(filters, dict):
        for filter_code, filter_ids in filters.items():
            if isinstance(filter_ids, (list, tuple)):
                normalized_ids = [str(f_id) for f_id in filter_ids if f_id]
                if normalized_ids:
                    params[f"attribute_ids[{filter_code}]"] = ",".join(normalized_ids)

    return params, query.get("channel_key", query.get("search_text", "default"))


def load_analyzed_items(database_path=ITEMS_DATABASE_PATH):
    """Load item IDs from local file."""
    database_path.touch(exist_ok=True)
    with database_path.open("r", encoding="utf-8", errors="ignore") as database:
        return {line.strip() for line in database if line.strip()}


def save_analyzed_item(item_id, database_path=ITEMS_DATABASE_PATH):
    """Append an item ID to the local database."""
    with database_path.open("a", encoding="utf-8") as database:
        database.write(f"{item_id}\n")


def send_discord_notification(webhook_url, item):
    """Send item notification to Discord."""
    embed = {
        "title": f"👕 {item['title']}",
        "url": item["url"],
        "color": 3447003,
        "description": (
            f"🏷️ **Prezzo:** {item['price']}\n"
            f"👕 **Brand:** {item['brand']}\n\n"
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
        "username": "Vinted Steals",
        "embeds": [embed]
    }

    try:
        response = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        if response.status_code in (200, 204):
            logging.info("Notifica Discord recapitata: %s", item["title"])
            return True
        elif response.status_code == 429:
            retry_after = response.json().get("retry_after", 3)
            time.sleep(retry_after)
            requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
            return True
        else:
            logging.error("Errore invio Discord (%s): %s", response.status_code, response.text)
            return False
    except Exception as exc:
        logging.error("Eccezione durante l'invio su Discord: %s", exc)
        return False


def get_catalog_items(session, catalog_url, params, api_headers):
    """Return catalogue items using Drego's official endpoint."""
    try:
        response = session.get(
            catalog_url,
            params=params,
            headers=api_headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as error:
        raise CatalogError(f"Vinted catalogue request failed: {error}") from error

    try:
        data = response.json()
    except ValueError as error:
        raise CatalogError(
            f"Vinted returned a non-JSON response (HTTP {response.status_code})"
        ) from error

    items = data.get("items")
    if not isinstance(items, list):
        raise CatalogError("Vinted response does not contain a valid 'items' list")
    return items


def normalize_item(item):
    """Normalize item data."""
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
        currency = price_data.get("currency_code")
        item_price = f"{amount} {currency}" if amount and currency else "N/A"
    else:
        item_price = str(price_data)

    photo_data = item.get("photo") or {}
    if not isinstance(photo_data, dict):
        photo_data = {}

    brand_title = item.get("brand_title") or ""

    return {
        "id": str(item_id),
        "title": item_title,
        "brand": brand_title,
        "price": item_price,
        "url": item_url,
        "image": photo_data.get("full_size_url"),
    }


def is_relevant_item(item, search_text):
    target = search_text.lower().strip()
    title = item["title"].lower().replace("ü", "u")
    brand = item["brand"].lower().strip().replace("ü", "u")

    # Match specifico per 545 / FiveFourFive
    if "545" in target or "five" in target:
        if "fivefourfive" in brand or "five four five" in brand or "545" in brand:
            return True
        if "fivefourfive" in title or "five four five" in title or re.search(r"\b545\b", title):
            return True

    if target in brand or target in title:
        return True

    return False


def initialize_vinted_session(marketplace_url):
    """Initialize an anonymous Vinted web session."""
    session = requests.Session()
    session.get(
        marketplace_url,
        headers=WEB_HEADERS,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    session.get(
        f"{marketplace_url.rstrip('/')}/catalog",
        headers=WEB_HEADERS,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    return session


def main():
    configure_logging()
    catalog_url = build_catalog_url(Config.vinted_url)
    api_headers = build_api_headers(Config.vinted_url)
    webhooks_map = load_webhooks_mapping()
    default_webhook = os.getenv("DISCORD_WEBHOOK_URL", "")

    parsed_queries = [build_catalog_params(q) for q in Config.queries]
    analyzed_items = load_analyzed_items()

    session = initialize_vinted_session(Config.vinted_url)
    anon_id = session.cookies.get("anon_id")
    if anon_id:
        api_headers["X-Anon-Id"] = anon_id

    logging.info("Sincronizzazione catalogo Vinted...")
    is_initial_run = True

    while True:
        try:
            for params, channel_key in parsed_queries:
                search_text = params.get("search_text", channel_key)
                webhook_url = webhooks_map.get(channel_key) or default_webhook

                try:
                    items = get_catalog_items(session, catalog_url, params, api_headers)
                except CatalogError as err:
                    logging.warning("[%s] Errore API: %s. Reinizializzazione sessione...", search_text, err)
                    session = initialize_vinted_session(Config.vinted_url)
                    anon_id = session.cookies.get("anon_id")
                    if anon_id:
                        api_headers["X-Anon-Id"] = anon_id
                    continue

                if not is_initial_run:
                    logging.info("[%s] Articoli ricevuti da Vinted: %d", search_text, len(items))

                for raw in items:
                    item = normalize_item(raw)
                    if not item or item["id"] in analyzed_items:
                        continue

                    analyzed_items.add(item["id"])
                    save_analyzed_item(item["id"])

                    if is_initial_run:
                        continue

                    if is_relevant_item(item, search_text):
                        logging.info("--> NUOVO DROP RILEVATO: %s (Brand: %s)", item["title"], item["brand"])
                        if webhook_url:
                            send_discord_notification(webhook_url, item)

                time.sleep(1.5)

            if is_initial_run:
                is_initial_run = False
                logging.info("Sincronizzazione completata! Articoli in cache: %d", len(analyzed_items))

        except Exception as exc:
            logging.error("Errore generale scansione: %s", exc)

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
