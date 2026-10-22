#!/usr/bin/env python3
"""Search Vinted and notify users about newly listed matching items in real time."""
import argparse
import email.utils
import html
import json
import logging
import smtplib
import sys
import time
from email.message import EmailMessage
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

import Config

# Mappatura webhook dedicati per ciascun canale Discord
BRAND_WEBHOOKS = {
    "scuffers": "https://discord.com/api/webhooks/1562768393575079946/6GGNmryFIWm8PHja91CXxBM2feYNZvr92JfbyoyrBV-3NjB8N6uw06OzXsSP6q84o1_b/slack",
    "reaven": "https://discord.com/api/webhooks/1562768580750090260/iDRYBS4r2RQO1K-ZnuYXVemKKT_zxcj16_lzRIrHFkz1yvmatF5o6C2gbzgEHsdDbszr/slack",
    "fivefourfive": "https://discord.com/api/webhooks/1562768783297216572/0ophTnzbWiBbzyqMXW41Tko5PlADLn3mN-_L7Ay0Rql9l_4QseNs24YhdC9tfq4sUAjc/slack",
    "corvidae": "https://discord.com/api/webhooks/1562768921059401732/174iHhaQSIea1PXB5Dt5bUDKv7HXRsaKPsvQyj0rKkIKheOIUg7nw_JTQ74199TL3GgT/slack",
    "perfect": "https://discord.com/api/webhooks/1562769028878176287/3Ts4QjnGuhNxoxPOUxcIXdXAO_JW30ewPPP8lEgmRU0T9XySCKm3RwC_ckMrfmf53c_R/slack",
    "ebtm": "https://discord.com/api/webhooks/1562769178195394694/tDN20DDfPNU3Y3Zm7pr0D5rBFEezct4o3u5z00E6iPprALzC4VtvbckV1P_V7owAzWOt/slack",
    "5mc2": "https://discord.com/api/webhooks/1562769327118094376/mjwFLPWjffWjI8Hm-33wXspwL6LoOlOy0ZdC6dnRqkV4zSjiZiOxrGSVQHorrIADGuRE/slack",
    "deputy department": "https://discord.com/api/webhooks/1562769484433850388/uYWTKEiAWM0tdiaNBw0_cYHl0xCUDWgriQe-FVoOX4RB0gk7aOS6vngCwzvzxO42DvB2/slack",
}

PROJECT_DIR = Path(__file__).resolve().parent
ITEMS_DATABASE_PATH = PROJECT_DIR / "vinted_items.txt"
LOG_PATH = PROJECT_DIR / "vinted_scanner.log"
REQUEST_TIMEOUT_SECONDS = 30
CHECK_INTERVAL_SECONDS = 60

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
    """Configure a rotating log file relative to this script."""
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


def parse_arguments():
    """Parse command-line options."""
    parser = argparse.ArgumentParser(
        description="Search Vinted for new items and send notifications."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show new items without sending notifications",
    )
    return parser.parse_args()


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

    for required_key in ("page", "per_page", "order", "filters"):
        if required_key not in query:
            raise ValueError(f"missing required query key: {required_key}")

    filters = query["filters"]
    params = {
        key: value
        for key, value in query.items()
        if key != "filters" and value not in (None, "")
    }
    for filter_code, filter_ids in filters.items():
        normalized_ids = [str(filter_id) for filter_id in filter_ids]
        if normalized_ids:
            params[f"attribute_ids[{filter_code}]"] = ",".join(normalized_ids)

    return params


def load_analyzed_items(database_path=ITEMS_DATABASE_PATH):
    """Load item IDs, creating the local database if it does not exist."""
    database_path.touch(exist_ok=True)
    with database_path.open("r", encoding="utf-8", errors="ignore") as database:
        return {line.strip() for line in database if line.strip()}


def save_analyzed_item(item_id, database_path=ITEMS_DATABASE_PATH):
    """Append an item ID to the local database."""
    with database_path.open("a", encoding="utf-8") as database:
        database.write(f"{item_id}\n")


def send_slack_message(item_title, item_price, item_url, item_image, matched_query=None):
    """Send Discord/Slack notification to the specific brand webhook."""
    webhook_url = BRAND_WEBHOOKS.get(str(matched_query).lower().strip())
    if not webhook_url:
        webhook_url = getattr(Config, "slack_webhook_url", "")

    if not webhook_url:
        return True

    message_lines = [f"*{item_title}*", f"🏷️ {item_price}"]
    if matched_query:
        message_lines.append(f"🔍 Brand: {matched_query}")
    message_lines.append(f"🔗 {item_url}")
    if item_image:
        message_lines.append(f"📷 {item_image}")

    try:
        response = requests.post(
            webhook_url,
            json={"text": "\n".join(message_lines)},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        logging.info("Discord notification sent for [%s]", matched_query)
        return True
    except requests.exceptions.RequestException as error:
        logging.error("Error sending Slack notification: %s", error)
        return False


def get_catalog_items(session, catalog_url, params, api_headers):
    """Return catalogue items or raise CatalogError for an invalid response."""
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


def normalize_item(item):
    """Normalize item details."""
    item_id = item.get("id")
    item_title = item.get("title")
    item_url = item.get("url")
    if not item_id or not item_title or not item_url:
        return None

    full_url = urljoin(f"{Config.vinted_url.rstrip('/')}/", str(item_url))
    price_data = item.get("price") or {}
    amount = price_data.get("amount")
    currency = price_data.get("currency_code", "EUR")
    price = f"{amount} {currency}" if amount else "N/A"
    photo_data = item.get("photo") or {}

    return {
        "id": str(item_id),
        "title": item_title,
        "price": price,
        "url": full_url,
        "image": photo_data.get("full_size_url"),
    }


def process_item(item, analyzed_items, dry_run, is_initial_run=False, matched_query=None):
    """Process item: register it, but notify only after initial sync."""
    normalized = normalize_item(item)
    if not normalized or normalized["id"] in analyzed_items:
        return True

    # Se e' il primo avvio, memorizza senza inviare notifiche spam
    if is_initial_run:
        save_analyzed_item(normalized["id"])
        analyzed_items.add(normalized["id"])
        return True

    if dry_run:
        print(f"[DRY-RUN] {normalized['title']} - {normalized['price']}")
        succeeded = True
    else:
        succeeded = send_slack_message(
            normalized["title"],
            normalized["price"],
            normalized["url"],
            normalized["image"],
            matched_query,
        )

    if succeeded:
        save_analyzed_item(normalized["id"])
        analyzed_items.add(normalized["id"])
    return succeeded


def initialize_vinted_session(marketplace_url):
    """Initialize session."""
    session = requests.Session()
    session.get(marketplace_url, headers=WEB_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    return session


def main(dry_run=False):
    configure_logging()
    catalog_url = build_catalog_url(Config.vinted_url)
    queries = [build_catalog_params(q) for q in Config.queries]
    api_headers = build_api_headers(Config.vinted_url)
    analyzed_items = load_analyzed_items()

    logging.info("Avvio sessione Vinted...")
    session = initialize_vinted_session(Config.vinted_url)

    is_initial_run = True
    logging.info("Scanner avviato. Modalità: Sincronizzazione iniziale silenziosa...")

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
                            dry_run,
                            is_initial_run=is_initial_run,
                            matched_query=matched_brand,
                        )
                except CatalogError as err:
                    logging.warning("Errore interrogazione [%s]: %s", matched_brand, err)
                    time.sleep(2)

            if is_initial_run:
                is_initial_run = False
                logging.info("Sincronizzazione completata! Da adesso solo nuovi drop in tempo reale.")

            time.sleep(CHECK_INTERVAL_SECONDS)

        except requests.exceptions.RequestException:
            logging.info("Rinnovamento sessione...")
            try:
                session = initialize_vinted_session(Config.vinted_url)
            except Exception:
                pass
            time.sleep(15)
        except Exception as error:
            logging.error("Errore imprevisto nel ciclo: %s", error)
            time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    arguments = parse_arguments()
    sys.exit(main(dry_run=arguments.dry_run))
