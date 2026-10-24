#!/usr/bin/env python3
"""Vinted -> Discord.

Controlla Vinted a ripetizione e manda su Discord (un canale per brand)
gli annunci appena pubblicati che corrispondono ai brand in Config.py.

Uso:
    python vinted_scanner.py          avvia il bot
    python vinted_scanner.py --test   manda un messaggio di prova in ogni canale
"""
import json
import logging
import os
import random
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

import Config


PROJECT_DIR = Path(__file__).resolve().parent
ITEMS_DATABASE_PATH = PROJECT_DIR / "vinted_items.txt"
LOG_PATH = PROJECT_DIR / "vinted_scanner.log"

REQUEST_TIMEOUT_SECONDS = 10          # ricerche su Vinted e invii a Discord
SESSION_INIT_TIMEOUT_SECONDS = 25     # apertura sessione (pagine più pesanti)
CHECK_INTERVAL_SECONDS = getattr(Config, "check_interval_seconds", 5)
MAX_WORKERS = getattr(Config, "max_workers", 3)
DEFAULT_PER_PAGE = getattr(Config, "default_per_page", 30)
SESSION_MAX_AGE_SECONDS = 30 * 60
SESSION_REFRESH_COOLDOWN_SECONDS = 30
RATE_LIMIT_PAUSE_SECONDS = 60
HEARTBEAT_SECONDS = 10 * 60

# Marche "vuote": quando la marca dell'annuncio è una di queste, il brand
# va cercato nel titolo.
BLANK_BRANDS = {
    "", "n/d", "senza marca", "altro", "vintage", "other", "unbranded",
    "no brand", "otro", "sin marca", "autre", "sans marque",
}

# Nota mostrata sugli annunci "loose" (brand non nel titolo/marca)
LOOSE_NOTE = "Brand non indicato nel titolo/marca: trovato dalla ricerca di Vinted (forse è nella descrizione)."

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


# ---------------------------------------------------------------
#  Utilità
# ---------------------------------------------------------------
def configure_logging():
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        level=logging.INFO,
        handlers=[
            logging.StreamHandler(sys.stdout),
            RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=3),
        ],
    )


def normalize_text(text):
    """Minuscolo e senza accenti: 'Stüssy' -> 'stussy', 'Pantalón' -> 'pantalon'."""
    text = unicodedata.normalize("NFKD", str(text or "")).lower()
    return "".join(ch for ch in text if not unicodedata.combining(ch)).strip()


def load_webhooks():
    """Ritorna (dizionario canale -> URL, webhook di riserva)."""
    webhooks = {}
    configured = getattr(Config, "webhooks", None)
    if isinstance(configured, dict):
        webhooks.update(configured)

    raw = os.getenv("DISCORD_WEBHOOKS_JSON", "").strip()
    if raw:
        try:
            webhooks.update(json.loads(raw))
        except Exception as exc:
            logging.error("Errore parsing DISCORD_WEBHOOKS_JSON: %s", exc)

    lookup = {str(key).strip().lower(): value for key, value in webhooks.items() if value}
    default_webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    return lookup, default_webhook


def webhook_for(channel, lookup, default_webhook):
    return lookup.get(channel.strip().lower()) or default_webhook


# ---------------------------------------------------------------
#  Pertinenza: decide a quali canali appartiene un annuncio
# ---------------------------------------------------------------
class BrandMatcher:
    def __init__(self, cfg):
        self.channel = cfg["channel"]
        self.names = [self._prepare_name(n) for n in cfg.get("names", [])]
        self.weak_names = [self._prepare_name(n) for n in cfg.get("weak_names", [])]
        self.exclude_words = [self._word_regex(w) for w in cfg.get("exclude_words", [])]
        self.require_words = [self._word_regex(w) for w in cfg.get("require_words", [])]
        self.ignore_phrases = [normalize_text(p) for p in cfg.get("ignore_phrases", [])]
        self.loose = bool(cfg.get("loose", False))
        self.collab_patterns = [self._collab_regex(n) for n in cfg.get("weak_names", [])]

    @staticmethod
    def _prepare_name(name):
        tokens = re.findall(r"[a-z0-9]+", normalize_text(name))
        body = r"[\s\-_.]*".join(re.escape(t) for t in tokens)
        pattern = re.compile(r"(?<![a-z0-9])" + body + r"(?![a-z0-9])")
        return pattern, "".join(tokens)

    @staticmethod
    def _collab_regex(name):
        """Riconosce 'Nike x Supreme' / 'Supreme × Nike' (la x accanto al nome) oppure la parola 'collab'."""
        tokens = re.findall(r"[a-z0-9]+", normalize_text(name))
        body = r"[\s\-_.]*".join(re.escape(t) for t in tokens)
        cross = r"(?:(?<![a-z0-9])x(?![a-z0-9])|×)"
        return re.compile(
            rf"{cross}\s*(?<![a-z0-9]){body}(?![a-z0-9])"
            rf"|(?<![a-z0-9]){body}(?![a-z0-9])\s*{cross}(?!\s*(?:xs|s|m|l|xl|xxl|small|medium|large|taglia|size)(?![a-z0-9]))"
            r"|collab"
        )

    @staticmethod
    def _word_regex(word):
        return re.compile(r"(?<![a-z0-9])" + re.escape(normalize_text(word)) + r"(?![a-z0-9])")

    def blocked(self, item):
        """True se il titolo contiene una parola da escludere (vale anche in modalità loose)."""
        title = normalize_text(item["title"])
        return any(rx.search(title) for rx in self.exclude_words)

    def matches(self, item):
        brand = normalize_text(item["brand"])
        title = normalize_text(item["title"])
        brand_compact = re.sub(r"[^a-z0-9]", "", brand)

        # Parole da escludere sempre (es. cappellini per il brand Perfect)
        if any(rx.search(title) for rx in self.exclude_words):
            return False

        # 1) La marca dichiarata nell'annuncio è questo brand
        for pattern, compact in self.names:
            if brand_compact == compact or pattern.search(brand):
                return True
        for _, compact in self.weak_names:
            if brand_compact == compact:
                return True

        # 2) Il nome compare nel titolo
        clean_title = title
        for phrase in self.ignore_phrases:
            clean_title = clean_title.replace(phrase, " ")

        in_title_strong = any(p.search(clean_title) for p, _ in self.names)
        in_title_weak = any(p.search(clean_title) for p, _ in self.weak_names)
        if not (in_title_strong or in_title_weak):
            return False

        # Nome ambiguo: se l'annuncio ha già un'altra marca vera, passa solo se è una collab
        if in_title_weak and not in_title_strong:
            if brand not in BLANK_BRANDS and not any(rx.search(title) for rx in self.collab_patterns):
                return False

        if self.require_words and not any(rx.search(title) for rx in self.require_words):
            return False
        return True


# ---------------------------------------------------------------
#  Vinted
# ---------------------------------------------------------------
class VintedError(Exception):
    pass


class SessionExpired(VintedError):
    pass


class RateLimited(VintedError):
    pass


def build_catalog_url(marketplace_url):
    parsed_url = urlsplit(marketplace_url)
    hostname = parsed_url.hostname

    if parsed_url.scheme != "https" or not hostname:
        raise ValueError("vinted_url must be a valid HTTPS URL")

    if hostname.startswith("www."):
        hostname = hostname[4:]

    if not hostname.startswith("vinted."):
        raise ValueError("vinted_url must point to a Vinted marketplace")

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


def build_catalog_params(search):
    params = {"order": "newest_first", "page": 1, "per_page": search["per_page"]}
    if search.get("text"):
        params["search_text"] = search["text"]
    if search.get("brand_ids"):
        params["brand_ids"] = ",".join(str(b) for b in search["brand_ids"])
    return params


class VintedClient:
    def __init__(self):
        self.base = Config.vinted_url.rstrip("/")
        self.catalog_url = build_catalog_url(Config.vinted_url)
        self.api_headers = build_api_headers(Config.vinted_url)
        self.session = None
        self.created_at = 0.0
        self.last_refresh_attempt = 0.0
        self.refresh()

    def refresh(self):
        self.last_refresh_attempt = time.time()
        session = requests.Session()
        session.get(self.base, headers=WEB_HEADERS, timeout=SESSION_INIT_TIMEOUT_SECONDS)
        session.get(f"{self.base}/catalog", headers=WEB_HEADERS, timeout=SESSION_INIT_TIMEOUT_SECONDS)
        anon_id = session.cookies.get("anon_id")
        if anon_id:
            self.api_headers["X-Anon-Id"] = anon_id
        self.session = session
        self.created_at = time.time()
        logging.info("Sessione Vinted pronta")

    def safe_refresh(self):
        """Rinnova la sessione, ma al massimo una volta ogni 30 secondi."""
        if time.time() - self.last_refresh_attempt < SESSION_REFRESH_COOLDOWN_SECONDS:
            return
        try:
            self.refresh()
        except Exception as exc:
            logging.warning("Rinnovo sessione fallito: %s", exc)

    def fetch(self, params):
        time.sleep(random.uniform(0, 0.3))  # evita raffiche tutte nello stesso istante
        try:
            response = self.session.get(
                self.catalog_url,
                params=params,
                headers=self.api_headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.exceptions.RequestException as error:
            raise VintedError(f"Chiamata fallita: {error}") from error

        if response.status_code == 429:
            raise RateLimited("429 troppe richieste")
        if response.status_code in (401, 403):
            raise SessionExpired(f"HTTP {response.status_code}")

        try:
            response.raise_for_status()
            data = response.json()
        except (requests.exceptions.RequestException, ValueError) as error:
            raise VintedError(f"Risposta non valida: {error}") from error

        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise VintedError("Formato items non valido")
        return items


def format_price(price_data):
    if isinstance(price_data, dict):
        amount = price_data.get("amount")
        currency = price_data.get("currency_code") or "EUR"
    else:
        amount, currency = price_data, "EUR"
    if amount in (None, ""):
        return "N/A"
    try:
        amount = f"{float(amount):.2f}"
    except (TypeError, ValueError):
        amount = str(amount)
    return f"{amount} {'€' if currency == 'EUR' else currency}"


def listing_age_text(raw):
    """Quanto tempo fa è stata caricata la foto (indicativo). None se Vinted non lo dice."""
    photo = raw.get("photo")
    if not isinstance(photo, dict):
        return None
    high_res = photo.get("high_resolution")
    timestamp = high_res.get("timestamp") if isinstance(high_res, dict) else None
    try:
        minutes = (time.time() - int(timestamp)) / 60
    except (TypeError, ValueError):
        return None
    if minutes < 0 or minutes > 7 * 24 * 60:
        return None
    if minutes < 1:
        return "meno di 1 minuto fa"
    if minutes < 60:
        return f"{int(minutes)} min fa"
    return f"{int(minutes // 60)} h fa"


def normalize_item(item):
    if not isinstance(item, dict):
        return None

    item_id = item.get("id")
    item_title = item.get("title")
    item_url = item.get("url")
    if item_id is None or not item_title or not item_url:
        return None

    item_url = urljoin(f"{Config.vinted_url.rstrip('/')}/", str(item_url))

    photo_data = item.get("photo")
    if not isinstance(photo_data, dict):
        photo_data = {}

    user_data = item.get("user")
    if not isinstance(user_data, dict):
        user_data = {}

    return {
        "id": str(item_id),
        "title": item_title,
        "brand": item.get("brand_title") or "N/D",
        "price": format_price(item.get("price") or {}),
        "size": item.get("size_title") or "",
        "condition": item.get("status") or item.get("subtitle") or "N/D",
        "seller": user_data.get("login") or "Privato",
        "url": item_url,
        "image": photo_data.get("full_size_url") or photo_data.get("url"),
        "age_text": listing_age_text(item),
    }


# ---------------------------------------------------------------
#  Annunci già visti
# ---------------------------------------------------------------
def load_seen(database_path=ITEMS_DATABASE_PATH):
    database_path.touch(exist_ok=True)
    with database_path.open("r", encoding="utf-8", errors="ignore") as database:
        return {line.strip() for line in database if line.strip()}


def save_seen(item_ids, database_path=ITEMS_DATABASE_PATH):
    if not item_ids:
        return
    with database_path.open("a", encoding="utf-8") as database:
        database.write("".join(f"{item_id}\n" for item_id in item_ids))


def prune_seen_file(database_path=ITEMS_DATABASE_PATH, max_entries=20000, keep_entries=10000):
    """Mantiene il file di testo leggero ed evita il consumo eccessivo di spazio e RAM."""
    try:
        if not database_path.exists():
            return
        with database_path.open("r", encoding="utf-8", errors="ignore") as database:
            lines = [line.strip() for line in database if line.strip()]
        if len(lines) > max_entries:
            kept = lines[-keep_entries:]
            with database_path.open("w", encoding="utf-8") as database:
                database.write("".join(f"{item_id}\n" for item_id in kept))
            logging.info("Pulizia archivio completata: mantenuti gli ultimi %d ID (file snello).", len(kept))
    except Exception as exc:
        logging.warning("Errore pulizia archivio ID: %s", exc)


# ---------------------------------------------------------------
#  Discord
# ---------------------------------------------------------------
def post_webhook(webhook_url, payload):
    """Ritorna 'ok' (inviato), 'error' (richiesta/URL non valido: inutile riprovare)
    oppure 'retry' (problema temporaneo: conviene riprovare dopo)."""
    for _ in range(3):
        try:
            response = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.exceptions.RequestException as exc:
            logging.error("Eccezione invio Discord: %s", exc)
            time.sleep(1)
            continue

        if response.status_code in (200, 204):
            return "ok"
        if response.status_code == 429:
            try:
                wait = float(response.json().get("retry_after", 2))
            except (ValueError, TypeError, AttributeError):
                wait = 2.0
            time.sleep(min(wait, 10) + 0.1)
            continue
        if 400 <= response.status_code < 500:
            logging.error("Errore Discord (%s): %s", response.status_code, response.text[:200])
            return "error"
        logging.error("Errore Discord (%s), riprovo", response.status_code)
        time.sleep(1)
    return "retry"


def build_embed(item, note=None):
    lines = [
        f"🏷️ **Prezzo:** {item['price']}",
        f"👕 **Brand:** {item['brand']}",
    ]
    if item.get("size"):
        lines.append(f"📏 **Taglia:** {item['size']}")
    lines.append(f"✨ **Condizioni:** {item['condition']}")
    lines.append(f"👤 **Venditore:** {item['seller']}")
    if item.get("age_text"):
        lines.append(f"⏱️ **Caricato:** {item['age_text']}")
    if note:
        lines.append("")
        lines.append(f"🔎 *{note}*")
    lines.append("")
    lines.append(f"🛒 **[APRI L'ANNUNCIO SU VINTED]({item['url']})**")

    embed = {
        "title": f"👕 {item['title']}"[:250],
        "url": item["url"],
        # azzurro = brand certo; arancione = da verificare (modalità loose)
        "color": 0xF39C12 if note else 0x00D2D3,
        "description": "\n".join(lines),
        "footer": {"text": "Vinted Steals • Drop in tempo reale"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if item.get("image"):
        embed["image"] = {"url": item["image"]}
    return embed


def send_discord_notification(webhook_url, item, note=None):
    return post_webhook(webhook_url, {"username": "Captain Hook", "embeds": [build_embed(item, note)]})


# ---------------------------------------------------------------
#  Ciclo di scansione
# ---------------------------------------------------------------
def build_searches(brands_cfg):
    searches = []
    for cfg in brands_cfg:
        per_page = cfg.get("per_page", DEFAULT_PER_PAGE)
        for text in cfg.get("searches", []):
            searches.append({"text": text, "per_page": per_page, "label": text, "channel": cfg["channel"]})
        if cfg.get("brand_ids"):
            searches.append({
                "brand_ids": cfg["brand_ids"],
                "per_page": per_page,
                "label": f"{cfg['channel']} (brand_ids)",
                "channel": cfg["channel"],
            })
    return searches


def fetch_all(client, executor, searches):
    futures = [(s, executor.submit(client.fetch, build_catalog_params(s))) for s in searches]
    results = []
    for search, future in futures:
        try:
            results.append((search, future.result(), None))
        except Exception as exc:
            results.append((search, None, exc))
    return results


def handle_fetch_errors(client, results):
    """Gestisce gli errori dei fetch. Ritorna i secondi extra da aspettare."""
    errors = [(s, e) for s, _, e in results if e is not None]
    if not errors:
        return 0

    if any(isinstance(e, RateLimited) for _, e in errors):
        logging.warning(
            "Vinted risponde 'troppe richieste' (429): pausa di %d secondi. "
            "Se succede spesso, alza check_interval_seconds in Config.py.",
            RATE_LIMIT_PAUSE_SECONDS,
        )
        return RATE_LIMIT_PAUSE_SECONDS

    if any(isinstance(e, SessionExpired) for _, e in errors):
        logging.warning("Sessione Vinted scaduta o rifiutata, la rinnovo...")
        client.safe_refresh()
        return 0

    for search, error in errors[:3]:
        logging.warning("[%s] Errore chiamata: %s", search["label"], error)
    if len(errors) == len(results):
        logging.warning("Tutte le ricerche sono fallite (connessione?). Riprovo tra poco.")
        client.safe_refresh()
        return 15
    return 0


def process_fresh_items(fresh, origins, barrier, matchers, webhooks, default_webhook, sent, warned, stats):
    """Valuta gli annunci nuovi e li manda ai canali giusti.
    'origins' dice quali canali hanno trovato ogni annuncio con la propria ricerca.
    Ritorna gli ID da considerare 'fatti' (da non rivalutare)."""
    done = []
    for item_id, raw in sorted(fresh.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0):
        try:
            numeric_id = int(item_id) if item_id.isdigit() else 0

            # Annuncio pubblicato prima dell'avvio del bot: ignoralo
            if barrier and numeric_id and numeric_id <= barrier:
                done.append(item_id)
                continue

            stats["new"] += 1
            item = normalize_item(raw)
            if not item:
                done.append(item_id)
                continue

            # (canale, nota): nota=None -> brand certo; con nota -> da verificare (modalità loose)
            targets = [(m.channel, None) for m in matchers if m.matches(item)]
            sure = {channel for channel, _ in targets}
            for m in matchers:
                if (m.loose and m.channel not in sure
                        and m.channel in origins.get(item_id, ())
                        and not m.blocked(item)):
                    targets.append((m.channel, LOOSE_NOTE))

            if not targets:
                done.append(item_id)
                continue

            all_done = True
            for channel, note in targets:
                if (item_id, channel) in sent:
                    continue
                webhook_url = webhook_for(channel, webhooks, default_webhook)
                if not webhook_url:
                    if channel not in warned:
                        logging.warning("Nessun webhook configurato per il canale '%s': annunci non inviati", channel)
                        warned.add(channel)
                    continue

                logging.info(
                    "NUOVO [%s]%s %s (marca: %s, ID %s)",
                    channel, " (da verificare)" if note else "", item["title"], item["brand"], item_id,
                )
                result = send_discord_notification(webhook_url, item, note)
                if result == "retry":
                    all_done = False
                    continue
                sent.add((item_id, channel))
                if result == "ok":
                    stats["sent"] += 1

            if all_done:
                done.append(item_id)
        except Exception as exc:
            # Un annuncio "strano" non deve mai bloccare tutti quelli dopo di lui
            logging.error("Errore sull'annuncio %s: %s", item_id, exc)
            done.append(item_id)
    return done


def load_brands_config():
    brands = getattr(Config, "BRANDS", None)
    if not brands:
        raise SystemExit("Config.py non contiene la lista BRANDS")
    return brands


def run_test(matchers, webhooks, default_webhook):
    print("Invio un messaggio di prova in ogni canale...")
    for matcher in matchers:
        url = webhook_for(matcher.channel, webhooks, default_webhook)
        if not url:
            print(f"  ✗ {matcher.channel}: nessun webhook configurato")
            continue
        result = post_webhook(url, {
            "username": "Captain Hook",
            "content": f"✅ Test: il canale **{matcher.channel}** è collegato correttamente.",
        })
        print(f"  {'✓' if result == 'ok' else '✗'} {matcher.channel}" + ("" if result == "ok" else f" ({result})"))
        time.sleep(0.5)


def main():
    configure_logging()
    brands_cfg = load_brands_config()
    matchers = [BrandMatcher(cfg) for cfg in brands_cfg]
    searches = build_searches(brands_cfg)
    webhooks, default_webhook = load_webhooks()

    if "--test" in sys.argv:
        run_test(matchers, webhooks, default_webhook)
        return

    missing = [m.channel for m in matchers if not webhook_for(m.channel, webhooks, default_webhook)]
    if missing:
        logging.warning("Canali senza webhook (gli annunci non verranno inviati): %s", ", ".join(missing))

    seen = load_seen()

    client = None
    while client is None:
        try:
            client = VintedClient()
        except Exception as exc:
            logging.error("Impossibile collegarsi a Vinted: %s. Riprovo tra 10 secondi.", exc)
            time.sleep(10)

    sent = set()
    warned = set()
    stats = {"cycles": 0, "new": 0, "sent": 0}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # ANCORAGGIO: registra gli annunci già presenti e fissa l'ID massimo.
        # Su Vinted gli ID crescono con il tempo: tutto ciò che ha ID <= barrier
        # è stato pubblicato prima dell'avvio del bot e non va notificato.
        barrier = 0
        logging.info("Avvio: registro gli annunci già presenti...")
        while True:
            results = fetch_all(client, executor, searches)
            if any(error is None for _, _, error in results):
                newly_seen = []
                for _, items, error in results:
                    if error is not None:
                        continue
                    for raw in items:
                        item_id = str(raw.get("id") or "") if isinstance(raw, dict) else ""
                        if not item_id:
                            continue
                        if item_id not in seen:
                            seen.add(item_id)
                            newly_seen.append(item_id)
                        if item_id.isdigit():
                            barrier = max(barrier, int(item_id))
                save_seen(newly_seen)
                break
            logging.warning("Avvio fallito, riprovo tra poco...")
            time.sleep(max(handle_fetch_errors(client, results), 10))

        logging.info("Pronto: da questo momento notifico solo annunci con ID > %d", barrier)

        last_heartbeat = time.time()
        while True:
            pause = 0
            try:
                if time.time() - client.created_at > SESSION_MAX_AGE_SECONDS:
                    client.safe_refresh()

                results = fetch_all(client, executor, searches)
                pause = handle_fetch_errors(client, results)

                fresh = {}
                origins = {}
                for search, items, error in results:
                    if error is not None:
                        continue
                    for raw in items:
                        if not isinstance(raw, dict):
                            continue
                        item_id = str(raw.get("id") or "")
                        if item_id and item_id not in seen:
                            fresh.setdefault(item_id, raw)
                            origins.setdefault(item_id, set()).add(search["channel"])

                if fresh:
                    done = process_fresh_items(
                        fresh, origins, barrier, matchers, webhooks, default_webhook, sent, warned, stats
                    )
                    seen.update(done)
                    save_seen(done)
                if len(sent) > 5000:
                    sent.clear()
                stats["cycles"] += 1
            except Exception as exc:
                logging.error("Errore loop scansione: %s", exc)
                pause = 5

            if time.time() - last_heartbeat >= HEARTBEAT_SECONDS:
                logging.info(
                    "Bot attivo: %d giri, %d annunci nuovi visti, %d notifiche inviate (ultimi %d minuti)",
                    stats["cycles"], stats["new"], stats["sent"], HEARTBEAT_SECONDS // 60,
                )
                stats.update(cycles=0, new=0, sent=0)
                last_heartbeat = time.time()
                prune_seen_file()

            time.sleep(CHECK_INTERVAL_SECONDS + pause)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBot fermato.")
