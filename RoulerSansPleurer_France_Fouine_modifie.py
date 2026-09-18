# Message informatif sur la vérification automatique
AUTO_CHECK_FOOTER = """🔄 <b>Vérification automatique toutes les 10 minutes.</b>
🔔 <b>Un message est envoyé uniquement si un prix change.</b>

⚠️ Bug ou non-réponse ? Envoie <b>/start</b> pour relancer le bot."""

# bot_carburant_france.py
import asyncio
import html
import json
import math
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import quote_plus

import aiohttp
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

TOKEN = os.getenv("ROULER_TOKEN", "").strip()
DATA_URL = (
    "https://data.economie.gouv.fr/api/explore/v2.1/catalog/"
    "datasets/prix-des-carburants-en-france-flux-instantane-v2/records"
)
GEO_URL = "https://geo.api.gouv.fr"

TOP_N = 5
RADII_KM = [5, 10, 20, 40, 100]
REFRESH_SECONDS = 600
USERS_FILE = Path("utilisateurs_carburant.json")

FUEL_LABELS = {
    "Gazole": "Gazole",
    "SP95-E10": "E10",
    "SP95": "SP95",
    "SP98": "SP98",
    "E85": "E85",
    "GPLc": "GPLc",
}

REGION_PAGE_SIZE = 6
DEPT_PAGE_SIZE = 8
CITY_PAGE_SIZE = 8

CHOOSE_ENTRY, CHOOSE_REGION, CHOOSE_DEPARTMENT, ENTER_CITY, CHOOSE_RADIUS, CHOOSE_FUEL, MAIN_MENU = range(7)


def entry_mode_keyboard():
    """Clavier normal (non-inline) : seul ce type de clavier peut
    demander la position GPS de l'utilisateur via Telegram."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📍 Partager ma position (plus précis)", request_location=True)],
            [KeyboardButton("🗺️ Choisir ma ville manuellement")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

users = {}
data_lock = asyncio.Lock()

# Persistance : le fichier utilisateurs est commité et poussé vers le
# dépôt Git dès qu'il change, pour survivre aux redémarrages de la VM
# GitHub Actions (limite dure de 6h par exécution). Throttle a 60s pour
# ne pas multiplier les push si plusieurs sauvegardes arrivent d'affilée.
GIT_PUSH_MIN_INTERVAL = 60  # secondes
_last_git_push = 0.0


def git_commit_and_push():
    global _last_git_push
    now = time.time()
    if now - _last_git_push < GIT_PUSH_MIN_INTERVAL:
        return
    try:
        subprocess.run(
            ["git", "config", "user.email", "bot@users.noreply.github.com"],
            check=False, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "RoulerSansPleurer Bot"],
            check=False, capture_output=True,
        )
        subprocess.run(
            ["git", "add", str(USERS_FILE)],
            check=False, capture_output=True,
        )
        commit = subprocess.run(
            ["git", "commit", "-m", "Mise a jour automatique des utilisateurs"],
            capture_output=True, text=True,
        )
        if commit.returncode == 0:
            push = subprocess.run(
                ["git", "push"],
                capture_output=True, text=True,
            )
            if push.returncode == 0:
                _last_git_push = now
            else:
                print("[GIT] push echoue :", push.stderr.strip())
    except Exception as exc:
        print("[GIT] erreur :", exc)


def load_users():
    global users
    if USERS_FILE.exists():
        try:
            users = json.loads(USERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            users = {}


def save_users():
    USERS_FILE.write_text(
        json.dumps(users, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    git_commit_and_push()


def user_profile(chat_id):
    key = str(chat_id)
    if key not in users:
        users[key] = {
            "chat_id": chat_id,
            "username": None,
            "first_name": None,
            "last_name": None,
            "location_source": None,
            "latitude": None,
            "longitude": None,
            "location_updated_at": None,
            "region": None,
            "region_code": None,
            "department": None,
            "department_code": None,
            "city": None,
            "lat": None,
            "lon": None,
            "radius": 40,
            "fuel": "Gazole",
            "sort_by": "price",
            "last_signature": None,
            "sort_mode": "price",
            "mode_fouine": False,
        }
    users[key].setdefault("mode_fouine", False)
    users[key].setdefault("username", None)
    users[key].setdefault("first_name", None)
    users[key].setdefault("last_name", None)
    users[key].setdefault("location_source", None)
    users[key].setdefault("latitude", users[key].get("lat"))
    users[key].setdefault("longitude", users[key].get("lon"))
    users[key].setdefault("location_updated_at", None)
    return users[key]


def update_user_identity(user, profile):
    """Conserve l'identité Telegram dans le même JSON que les abonnés."""
    if not user:
        return
    profile["username"] = user.username
    profile["first_name"] = user.first_name
    profile["last_name"] = user.last_name


async def get_json(session, url, params=None):
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as r:
        r.raise_for_status()
        return await r.json()


async def get_regions():
    async with aiohttp.ClientSession() as session:
        return await get_json(
            session,
            f"{GEO_URL}/regions",
            {"fields": "nom,code"},
        )


async def get_departments(region_code):
    async with aiohttp.ClientSession() as session:
        return await get_json(
            session,
            f"{GEO_URL}/regions/{region_code}/departements",
            {"fields": "nom,code"},
        )


async def find_city(city_name, department_code):
    """Recherche toutes les communes correspondant au texte saisi."""
    async with aiohttp.ClientSession() as session:
        data = await get_json(
            session,
            f"{GEO_URL}/communes",
            {
                "codeDepartement": department_code,
                "nom": city_name,
                "boost": "population",
                "fields": "nom,code,centre,codesPostaux",
                "limit": 50,
            },
        )
        return data


async def get_all_cities(department_code):
    async with aiohttp.ClientSession() as session:
        return await get_json(
            session,
            f"{GEO_URL}/departements/{department_code}/communes",
            {
                "fields": "nom,code,centre,codesPostaux",
                "limit": 1000,
            },
        )


def region_keyboard(regions, page=0):
    start = page * REGION_PAGE_SIZE
    chunk = regions[start:start + REGION_PAGE_SIZE]
    rows = [
        [InlineKeyboardButton(r["nom"], callback_data=f"REG:{r['code']}:{r['nom']}")]
        for r in chunk
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"REGPAGE:{page-1}"))
    if start + REGION_PAGE_SIZE < len(regions):
        nav.append(InlineKeyboardButton("➡️", callback_data=f"REGPAGE:{page+1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


def department_keyboard(depts, page=0):
    start = page * DEPT_PAGE_SIZE
    chunk = depts[start:start + DEPT_PAGE_SIZE]
    rows = [
        [InlineKeyboardButton(d["nom"], callback_data=f"DEP:{d['code']}:{d['nom']}")]
        for d in chunk
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"DEPPAGE:{page-1}"))
    if start + DEPT_PAGE_SIZE < len(depts):
        nav.append(InlineKeyboardButton("➡️", callback_data=f"DEPPAGE:{page+1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


def radius_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{r} km", callback_data=f"RAD:{r}") for r in RADII_KM],
        [InlineKeyboardButton("📍 Plus proche", callback_data="RAD:1")],
    ])


def fuel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⛽ Gazole", callback_data="FUEL:Gazole")],
        [InlineKeyboardButton("🟢 SP95-E10", callback_data="FUEL:SP95-E10")],
        [InlineKeyboardButton("🟡 SP95", callback_data="FUEL:SP95")],
        [InlineKeyboardButton("🔵 SP98", callback_data="FUEL:SP98")],
        [InlineKeyboardButton("🟣 E85", callback_data="FUEL:E85")],
        [InlineKeyboardButton("🟠 GPLc", callback_data="FUEL:GPLc")],
    ])


def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⛽ Voir les prix", callback_data="PRICES"),
            InlineKeyboardButton("📍 Plus proche", callback_data="NEAREST"),
        ],
        [InlineKeyboardButton("💶 Moins cher", callback_data="PRICES")],
        [InlineKeyboardButton("🔄 Actualiser maintenant", callback_data="PRICES")],
        [InlineKeyboardButton("⚙️ Modifier ma localisation", callback_data="SET_LOCATION")],
        [InlineKeyboardButton("⛽ Modifier le carburant", callback_data="SET_FUEL")],
        [InlineKeyboardButton("🦡 Mode Fouine", callback_data="FOUINE")],
    ])


async def toggle_fouine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    p = user_profile(update.effective_chat.id)
    p["mode_fouine"] = not p.get("mode_fouine", False)
    save_users()

    status = "🟢 ACTIVÉ" if p["mode_fouine"] else "🔴 DÉSACTIVÉ"
    await q.edit_message_text(
        f"🦡 <b>Mode Fouine : {status}</b>\n\n"
        + (
            "Tu recevras les prix toutes les 10 minutes, même si aucun prix n'a changé."
            if p["mode_fouine"]
            else
            "Retour au mode normal : tu recevras uniquement les alertes lorsqu'un prix change."
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )
    return MAIN_MENU


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    p = user_profile(chat_id)
    update_user_identity(update.effective_user, p)
    save_users()

    await update.message.reply_text(
        "🇫🇷 <b>ROULER SANS PLEURER — FRANCE</b>\n\n"
        "📍 Comment veux-tu renseigner ta position ?\n"
        "Le partage GPS donne des résultats plus précis que le choix manuel "
        "d'une ville.",
        parse_mode=ParseMode.HTML,
        reply_markup=entry_mode_keyboard(),
    )
    return CHOOSE_ENTRY


async def entry_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    regions = await get_regions()
    context.user_data["regions"] = regions
    context.user_data["region_page"] = 0

    await update.message.reply_text(
        "D'accord, on part sur la sélection manuelle.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text(
        "📍 <b>Choisis ta région :</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=region_keyboard(regions),
    )
    return CHOOSE_REGION


async def entry_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    p = user_profile(update.effective_chat.id)
    update_user_identity(update.effective_user, p)

    # Position GPS précise partagée volontairement par l'utilisateur.
    p["lat"] = loc.latitude
    p["lon"] = loc.longitude
    p["latitude"] = loc.latitude
    p["longitude"] = loc.longitude
    p["location_source"] = "telegram_gps"
    p["location_updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    # Reverse geocoding best-effort pour retrouver un nom de ville a afficher.
    city_name = "Ma position"
    try:
        async with aiohttp.ClientSession() as session:
            data = await get_json(
                session,
                f"{GEO_URL}/communes",
                {
                    "lat": loc.latitude,
                    "lon": loc.longitude,
                    "fields": "nom,code,codeDepartement,codeRegion",
                },
            )
            if data:
                city_name = data[0]["nom"]
                p["department_code"] = data[0].get("codeDepartement")
                p["region_code"] = data[0].get("codeRegion")
    except Exception:
        pass

    p["city"] = city_name
    save_users()

    await update.message.reply_text(
        f"📍 Position enregistrée : <b>{html.escape(city_name)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text(
        "📏 <b>Choisis le rayon de recherche :</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=radius_keyboard(),
    )
    return CHOOSE_RADIUS


async def region_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    page = int(q.data.split(":")[1])
    context.user_data["region_page"] = page
    await q.edit_message_reply_markup(
        reply_markup=region_keyboard(context.user_data["regions"], page)
    )
    return CHOOSE_REGION


async def choose_region(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    _, code, name = q.data.split(":", 2)
    p = user_profile(q.message.chat.id)
    p["region"] = name
    p["region_code"] = code

    depts = await get_departments(code)
    context.user_data["departments"] = depts
    context.user_data["department_page"] = 0

    await q.edit_message_text(
        f"🇫🇷 Région : <b>{html.escape(name)}</b>\n\n"
        "📍 Choisis ton département :",
        parse_mode=ParseMode.HTML,
        reply_markup=department_keyboard(depts),
    )
    return CHOOSE_DEPARTMENT


async def department_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    page = int(q.data.split(":")[1])
    context.user_data["department_page"] = page
    await q.edit_message_reply_markup(
        reply_markup=department_keyboard(context.user_data["departments"], page)
    )
    return CHOOSE_DEPARTMENT


async def choose_department(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    _, code, name = q.data.split(":", 2)
    p = user_profile(q.message.chat.id)
    p["department"] = name
    p["department_code"] = code

    await q.edit_message_text(
        f"📍 Département : <b>{html.escape(name)}</b>\n\n"
        "🏙️ Écris le nom de ta ville, ou affiche toutes les communes :\n"
        "Exemple : <code>Grenoble</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Toutes les villes", callback_data="ALLCITIES:0")]
        ]),
    )
    return ENTER_CITY


async def show_city_results(update, context, page=0, edit=False):
    results = context.user_data.get("city_results", [])
    start = page * CITY_PAGE_SIZE
    chunk = results[start:start + CITY_PAGE_SIZE]
    rows = [
        [InlineKeyboardButton(c["nom"], callback_data=f"CITY:{c['code']}")]
        for c in chunk
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"CITYPAGE:{page-1}"))
    if start + CITY_PAGE_SIZE < len(results):
        nav.append(InlineKeyboardButton("➡️", callback_data=f"CITYPAGE:{page+1}"))
    if nav:
        rows.append(nav)
    markup = InlineKeyboardMarkup(rows)
    text = "🏙️ <b>Choisis ta ville :</b>\n\n" + \
           "Tu peux aussi écrire directement le nom d'une ville pour la rechercher."
    if edit:
        await update.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    return ENTER_CITY


async def city_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    page = int(q.data.split(":")[1])
    context.user_data["city_page"] = page
    return await show_city_results(q, context, page=page, edit=True)


async def all_cities(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Chargement des communes…")
    p = user_profile(q.message.chat.id)
    try:
        cities = await get_all_cities(p["department_code"])
    except Exception:
        await q.edit_message_text("❌ Impossible de charger les communes. Réessaie.")
        return ENTER_CITY

    cities = [c for c in cities if c.get("centre", {}).get("coordinates")]
    cities.sort(key=lambda c: c.get("nom", "").lower())
    context.user_data["city_results"] = cities
    return await show_city_results(q, context, page=0, edit=True)


async def enter_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    city_name = update.message.text.strip()
    if len(city_name) < 2:
        await update.message.reply_text("❌ Nom de ville trop court. Réessaie.")
        return ENTER_CITY

    p = user_profile(update.effective_chat.id)
    try:
        results = await find_city(city_name, p["department_code"])
    except Exception:
        await update.message.reply_text("❌ Impossible de contacter l'API géographique. Réessaie.")
        return ENTER_CITY

    valid = [c for c in results if c.get("centre", {}).get("coordinates")]
    if not valid:
        await update.message.reply_text(
            "❌ Aucune commune trouvée.\n"
            "Écris simplement le nom, par exemple : Grenoble"
        )
        return ENTER_CITY

    context.user_data["city_results"] = valid
    context.user_data["city_page"] = 0

    if len(valid) == 1:
        return await set_city_and_continue(update, context, valid[0])

    rows = [[InlineKeyboardButton(c["nom"], callback_data=f"CITY:{c['code']}")] for c in valid[:CITY_PAGE_SIZE]]
    if len(valid) > CITY_PAGE_SIZE:
        rows.append([InlineKeyboardButton("➡️", callback_data="CITYPAGE:1")])
    await update.message.reply_text(
        "🏙️ <b>Plusieurs communes trouvées :</b>\nChoisis la bonne ou écris un autre nom.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return ENTER_CITY


async def choose_city_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    code = q.data.split(":", 1)[1]
    c = next((x for x in context.user_data.get("city_results", []) if x.get("code") == code), None)
    if not c:
        await q.edit_message_text("❌ Choix expiré. Utilise /start.")
        return ConversationHandler.END

    p = user_profile(q.message.chat.id)
    centre = c["centre"]["coordinates"]
    p["city"] = c["nom"]
    p["lon"] = float(centre[0])
    p["lat"] = float(centre[1])
    reset_price_signature(p)
    save_users()

    await q.edit_message_text(
        f"🏙️ Ville : <b>{html.escape(c['nom'])}</b>\n\n📏 Choisis ton rayon :",
        parse_mode=ParseMode.HTML,
        reply_markup=radius_keyboard(),
    )
    return CHOOSE_RADIUS


async def set_city_and_continue(update, context, c):
    p = user_profile(update.effective_chat.id)
    centre = c["centre"]["coordinates"]
    p["city"] = c["nom"]
    p["lon"] = float(centre[0])
    p["lat"] = float(centre[1])
    reset_price_signature(p)
    save_users()

    await update.message.reply_text(
        f"🏙️ Ville : <b>{html.escape(c['nom'])}</b>\n\n"
        "📏 Choisis ton rayon :",
        parse_mode=ParseMode.HTML,
        reply_markup=radius_keyboard(),
    )
    return CHOOSE_RADIUS


async def choose_radius(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    radius = int(q.data.split(":")[1])

    p = user_profile(q.message.chat.id)
    p["radius"] = radius
    reset_price_signature(p)
    save_users()

    await q.edit_message_text(
        "⛽ <b>Choisis ton carburant :</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=fuel_keyboard(),
    )
    return CHOOSE_FUEL


async def choose_fuel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    fuel = q.data.split(":", 1)[1]

    p = user_profile(q.message.chat.id)
    p["fuel"] = fuel
    p["last_signature"] = None
    save_users()

    await q.edit_message_text(
        profile_text(p),
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )
    return MAIN_MENU


def reset_price_signature(p):
    """Force la prochaine vérification à établir une nouvelle référence sans alerte."""
    p["last_signature"] = None


def profile_text(p):
    return (
        "🇫🇷 <b>ROULER SANS PLEURER — FRANCE</b>\n\n"
        f"📍 <b>{html.escape(p['city'] or 'Non définie')}</b>\n"
        f"🗺️ {html.escape(p['department'] or '')} — {html.escape(p['region'] or '')}\n"
        f"📏 Rayon : <b>{p['radius']} km</b>\n"
        f"⛽ Carburant : <b>{html.escape(p['fuel'])}</b>\n"
    )


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


async def prices_for_user(p, sort_by="price"):
    fuel_field = {
        "Gazole": "gazole_prix",
        "SP95-E10": "e10_prix",
        "SP95": "sp95_prix",
        "SP98": "sp98_prix",
        "E85": "e85_prix",
        "GPLc": "gplc_prix",
    }[p["fuel"]]

    # IMPORTANT : l'API utilise geom en vraies coordonnées GPS
    # (longitude, latitude), tandis que les champs latitude/longitude
    # historiques sont stockés sous forme entière (ex. 4818300).
    where = (
        f"within_distance(geom, "
        f"geom'POINT({p['lon']} {p['lat']})', "
        f"{p['radius']} km)"
    )

    params = {
        "where": where,
        "limit": 100,
        "order_by": f"{fuel_field} asc",
        "select": (
            "id,latitude,longitude,geom,cp,adresse,ville,"
            f"{fuel_field},prix"
        ),
    }

    async with aiohttp.ClientSession() as session:
        data = await get_json(session, DATA_URL, params)

    results = []
    for rec in data.get("results", []):
        f = rec.get("fields", rec)
        price = f.get(fuel_field)
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue

        if price <= 0:
            continue

        # Utilise geom en priorité : [longitude, latitude] en coordonnées GPS.
        geom = f.get("geom")
        lat = lon = None
        if isinstance(geom, (list, tuple)) and len(geom) >= 2:
            try:
                lon = float(geom[0])
                lat = float(geom[1])
            except (TypeError, ValueError):
                lon = lat = None

        # Secours pour les anciennes données : latitude/longitude sont
        # généralement multipliées par 100000 dans ce dataset.
        if lat is None or lon is None:
            try:
                raw_lat = float(f.get("latitude"))
                raw_lon = float(f.get("longitude"))
                if abs(raw_lat) > 90 or abs(raw_lon) > 180:
                    raw_lat /= 100000.0
                    raw_lon /= 100000.0
                lat, lon = raw_lat, raw_lon
            except (TypeError, ValueError):
                continue

        # Protection supplémentaire contre toute coordonnée aberrante.
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue

        distance = haversine_km(p["lat"], p["lon"], lat, lon)
        results.append({
            "id": f.get("id"),
            "price": price,
            "lat": lat,
            "lon": lon,
            "distance": distance,
            "cp": f.get("cp", ""),
            "adresse": f.get("adresse", ""),
            "ville": f.get("ville", ""),
        })

    if sort_by == "distance":
        results.sort(key=lambda x: (x["distance"], x["price"]))
    else:
        results.sort(key=lambda x: (x["price"], x["distance"]))
    return results[:TOP_N]


def maps_url(station):
    query = f"{station['adresse']}, {station['cp']} {station['ville']}"
    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(query)


def format_prices(p, stations, sort_by="price"):
    fuel = p["fuel"]
    title = (
        "🇫🇷 <b>ROULER SANS PLEURER — FRANCE</b>\n"
        f"📍 {html.escape(p['city'])} + rayon de {p['radius']} km\n"
        + ("💶 Classement : du moins cher au plus cher\n\n" if sort_by == "price" else "📍 Classement : du plus proche au plus loin\n\n")
        + "━━━━━━━━━━━━━━━━━━━━\n"
    )

    if not stations:
        return (
            title + f"\n⛽ <b>{html.escape(fuel.upper())}</b>\n❌ Aucun prix disponible.\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🕐 Prix = dernier relevé disponible\n"
            + AUTO_CHECK_FOOTER
        )

    best = stations[0]["price"]
    text = title + f"\n⛽ <b>{html.escape(fuel.upper())}</b>\n"
    if sort_by == "price":
        text += f"💰 Meilleur prix : <b>{best:.3f} €/L</b>\n\n"
    else:
        text += f"📍 Station la plus proche : <b>{stations[0]['distance']:.1f} km</b>\n\n"

    for i, s in enumerate(stations, 1):
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        medal = medals.get(i, "🔷")
        text += (
            f"{medal} <b>{html.escape(s['ville'])}</b>\n"
            f"💶 <b>{s['price']:.3f} €/L</b> • "
            f"📍 {html.escape(s['adresse'])}, {html.escape(s['cp'])} "
            f"• {html.escape(s['ville'])} • {s['distance']:.1f} km\n"
        )

        text += f'<a href="{maps_url(s)}">🗺️ Ouvrir dans Google Maps</a>\n\n'

    text += (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🕐 Prix = dernier relevé disponible\n"
        + AUTO_CHECK_FOOTER
    )
    return text


async def show_nearest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Recherche des stations les plus proches…")
    p = user_profile(q.message.chat.id)
    if p.get("sort_mode") != "distance":
        p["sort_mode"] = "distance"
        reset_price_signature(p)
    save_users()
    if p["lat"] is None:
        await q.edit_message_text("❌ Configure d'abord ta localisation avec /start.")
        return MAIN_MENU
    try:
        stations = await prices_for_user(p, sort_by="distance")
    except Exception:
        await q.edit_message_text("❌ Impossible de récupérer les stations actuellement.")
        return MAIN_MENU
    await q.edit_message_text(
        format_prices(p, stations, sort_by="distance"),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=main_keyboard(),
    )
    return MAIN_MENU


async def show_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Recherche des stations…")

    p = user_profile(q.message.chat.id)
    if p.get("sort_mode") != "price":
        p["sort_mode"] = "price"
        reset_price_signature(p)
    save_users()
    if not p["city"] or p["lat"] is None:
        await q.edit_message_text("❌ Configure d'abord ta localisation avec /start.")
        return MAIN_MENU

    try:
        stations = await prices_for_user(p)
    except Exception as e:
        await q.edit_message_text(
            "❌ Impossible de récupérer les prix actuellement.\n"
            "Réessaie dans quelques secondes."
        )
        return MAIN_MENU

    await q.edit_message_text(
        format_prices(p, stations),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=main_keyboard(),
    )
    return MAIN_MENU


async def set_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "📍 Comment veux-tu renseigner ta nouvelle position ?",
        parse_mode=ParseMode.HTML,
    )
    await q.message.reply_text(
        "Choisis une option :",
        reply_markup=entry_mode_keyboard(),
    )
    return CHOOSE_ENTRY


async def set_fuel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "⛽ <b>Choisis ton carburant :</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=fuel_keyboard(),
    )
    return CHOOSE_FUEL


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Configuration annulée. Utilise /start pour recommencer.")
    return ConversationHandler.END


async def price_check_job(context: ContextTypes.DEFAULT_TYPE):
    # Une vérification toutes les 10 minutes.
    for chat_id, p in list(users.items()):
        if p.get("lat") is None or not p.get("city") or not p.get("fuel"):
            continue

        try:
            stations = await prices_for_user(p, sort_by=p.get("sort_mode", "price"))
            signature = "|".join(
                [p.get("sort_mode", "price")] +
                [f"{s['id']}:{s['price']:.3f}" for s in stations]
            )

            if p.get("last_signature") is None:
                p["last_signature"] = signature
                save_users()
                if not p.get("mode_fouine", False):
                    continue

            should_send = p.get("mode_fouine", False) or signature != p.get("last_signature")
            if should_send:
                p["last_signature"] = signature
                await context.bot.send_message(
                    chat_id=int(chat_id),
                    text=(
                        "🦡 <b>Mode Fouine — relevé des prix</b>\n\n"
                        if p.get("mode_fouine", False)
                        else
                        "🔔 <b>Mise à jour des prix</b>\n\n"
                    ) +
                    format_prices(p, stations, sort_by=p.get("sort_mode", "price")),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                save_users()
        except Exception:
            continue


async def post_init(application: Application):
    load_users()


def build_application():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSE_ENTRY: [
                MessageHandler(filters.LOCATION, entry_location),
                MessageHandler(
                    filters.Regex("^🗺️ Choisir ma ville manuellement$"),
                    entry_manual,
                ),
            ],
            CHOOSE_REGION: [
                CallbackQueryHandler(region_page, pattern=r"^REGPAGE:\d+$"),
                CallbackQueryHandler(choose_region, pattern=r"^REG:"),
            ],
            CHOOSE_DEPARTMENT: [
                CallbackQueryHandler(department_page, pattern=r"^DEPPAGE:\d+$"),
                CallbackQueryHandler(choose_department, pattern=r"^DEP:"),
            ],
            ENTER_CITY: [
                CallbackQueryHandler(choose_city_callback, pattern=r"^CITY:"),
                CallbackQueryHandler(city_page, pattern=r"^CITYPAGE:\d+$"),
                CallbackQueryHandler(all_cities, pattern=r"^ALLCITIES:\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_city),
            ],
            CHOOSE_RADIUS: [
                CallbackQueryHandler(choose_radius, pattern=r"^RAD:\d+$"),
            ],
            CHOOSE_FUEL: [
                CallbackQueryHandler(choose_fuel, pattern=r"^FUEL:"),
            ],
            MAIN_MENU: [
                CallbackQueryHandler(show_prices, pattern=r"^PRICES$"),
                CallbackQueryHandler(show_nearest, pattern=r"^NEAREST$"),
                CallbackQueryHandler(set_location, pattern=r"^SET_LOCATION$"),
                CallbackQueryHandler(set_fuel, pattern=r"^SET_FUEL$"),
                CallbackQueryHandler(toggle_fouine, pattern=r"^FOUINE$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.job_queue.run_repeating(price_check_job, interval=REFRESH_SECONDS, first=10)
    return app


if __name__ == "__main__":
    print("🇫🇷 RoulerSansPleurer France démarré.")
    build_application().run_polling(drop_pending_updates=True)
