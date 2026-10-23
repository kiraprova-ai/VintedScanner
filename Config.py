# ==============================================================
#  CONFIGURAZIONE - Vinted -> Discord
#  Per aggiungere o togliere brand basta modificare questo file.
#  Non serve toccare vinted_scanner.py.
# ==============================================================

vinted_url = "https://www.vinted.it"
vinted_locale = "it-IT"

# --- Velocità --------------------------------------------------
# Secondi di pausa tra un giro completo di controlli e il successivo.
# Più basso = notifiche più veloci, ma se esageri Vinted può bloccarti.
# Se nei log vedi spesso "429" o "troppe richieste", alza questo numero (5 o 8).
check_interval_seconds = 3

# Quante ricerche su Vinted partono insieme (2-4 va bene).
max_workers = 3

# Quanti annunci (i più recenti) si leggono per ogni ricerca.
default_per_page = 30

# --- Webhook Discord (opzionale) --------------------------------
# Il bot legge i webhook dalle variabili d'ambiente DISCORD_WEBHOOKS_JSON
# e DISCORD_WEBHOOK_URL. Se preferisci, puoi incollarli qui.
# La chiave deve essere uguale al "channel" del brand. Non condividere questo file
# con nessuno se ci scrivi dentro i webhook reali.
webhooks = {
    # "scuffers": "https://discord.com/api/webhooks/...",
}


# --- Brand -------------------------------------------------------
# channel        nome del canale (uguale alla chiave del webhook)
# searches       cosa viene cercato su Vinted (una richiesta per ogni voce)
# names          nomi che indicano SICURAMENTE il brand, in marca o titolo.
#                Spazi e trattini sono tolleranti: "deputy department"
#                trova anche "DeputyDepartment" e "Deputy-Department".
# weak_names     nomi ambigui (es. "supreme", "perfect"): nel titolo valgono
#                solo se la marca è vuota/"Altro" oppure è una collab
#                ("Nike x Supreme"). Se la marca dell'annuncio è proprio
#                quel nome, vale sempre.
# exclude_words  se una di queste parole è nel titolo, l'annuncio si scarta
# require_words  (solo per match dal titolo) serve almeno una di queste parole
# ignore_phrases frasi da ignorare nel titolo (es. "perfect condition")
# per_page       (facoltativo) quanti annunci leggere per questo brand
# brand_ids      (avanzato, facoltativo) ID della marca su Vinted: aggiunge una
#                ricerca che pesca tutti gli annunci con quella marca,
#                anche se il titolo non la nomina. L'ID si vede nell'URL
#                di Vinted dopo aver filtrato per marca: brand_ids[]=NUMERO

PERFECT_EXCLUDE = [
    "cap", "caps", "hat", "hats", "gorro", "gorra",
    "cappello", "cappellino", "cappellini", "cappelli", "berretto", "berretti", "beanie",
    "gloves", "guanti", "guantes", "gants",
    "scarf", "sciarpa", "sciarpe", "bufanda", "echarpe",
    "cover", "custodia", "case", "scarpe", "shoes", "sneakers",
]

PERFECT_REQUIRE = [
    "tuta", "tracksuit", "chandal", "survetement",
    "hoodie", "hoodies", "felpa", "felpe", "sweatshirt", "sudadera",
    "pant", "pants", "pantalone", "pantaloni", "pantalon", "pantalones",
    "jogger", "joggers",
    "jacket", "jackets", "giacca", "giacche", "chaqueta", "blouson", "bomber",
    "cappotto", "coat", "fleece", "pile",
    "tee", "tees", "t-shirt", "tshirt", "maglia", "maglietta", "camiseta",
]

PERFECT_IGNORE = [
    "perfect condition", "perfect conditions", "perfect cond", "perfect state",
    "perfect shape", "perfect fit", "perfect size", "perfect for", "perfect gift",
    "perfect quality", "perfect match", "perfect look", "perfect piece",
    "perfect layer", "in perfect",
]

BRANDS = [
    {
        "channel": "scuffers",
        "searches": ["scuffers"],
        "names": ["scuffers"],
    },
    {
        "channel": "reaven",
        "searches": ["reaven"],
        "names": ["reaven"],
    },
    {
        "channel": "corvidae",
        "searches": ["corvidae"],
        "names": ["corvidae"],
    },
    {
        "channel": "fivefourfive",
        "searches": ["fivefourfive", "545"],
        "names": ["fivefourfive", "five four five"],
        "weak_names": ["545"],
    },
    {
        "channel": "5mc2",
        "searches": ["5mc2"],
        "names": ["5mc2", "5 mc 2"],
    },
    {
        "channel": "perfect",
        "searches": ["perfect"],
        "per_page": 50,  # "perfect" esce in tantissimi titoli: leggiamo più annunci
        "weak_names": ["perfect"],
        "exclude_words": PERFECT_EXCLUDE,
        "require_words": PERFECT_REQUIRE,
        "ignore_phrases": PERFECT_IGNORE,
    },
    {
        "channel": "ebtm",
        "searches": ["ebtm"],
        "names": ["ebtm"],
    },
    {
        "channel": "deputy department",
        "searches": ["deputy department"],
        "names": ["deputy department"],
    },
    {
        "channel": "supreme",
        "searches": ["supreme"],
        "weak_names": ["supreme"],
    },
    {
        "channel": "formy studio",
        "searches": ["formy studio"],
        "names": ["formy studio"],
    },
    {
        "channel": "himes",
        "searches": ["himes"],
        "names": ["himes"],
    },
    {
        "channel": "trendt vision",
        "searches": ["trendt vision"],
        "names": ["trendt vision"],
    },
    {
        "channel": "stussy",
        "searches": ["stussy"],
        "weak_names": ["stussy"],
    },
]
