# Message informatif sur la vérification automatique
AUTO_CHECK_FOOTER = """🔄 <b>Vérification automatique toutes les 10 minutes.</b>
🔔 <b>Un message est envoyé uniquement si un prix change.</b>

⚠️ Bug ou non-réponse ? Envoie <b>/start</b> pour relancer le bot."""

# RoulerSansPleurer_France_Fouine_modifie.py
import asyncio
import html
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import quote, quote_plus

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
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

# Aucun secret en clair : tout vient des secrets GitHub (variables d'environnement).
TOKEN = os.getenv("ROULER_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
if not TOKEN:
    raise SystemExit("❌ Secret ROULER_TOKEN manquant (GitHub > Settings > Secrets and variables > Actions).")
DATA_URL = (
    "https://data.economie.gouv.fr/api/explore/v2.1/catalog/"
    "datasets/prix-des-carburants-en-france-flux-instantane-v2/records"
)
GEO_URL = "https://geo.api.gouv.fr"

# Les URL de l'API Telegram contiennent le token du bot : on empêche les bibliothèques HTTP
# de les écrire dans les logs (visibles dans GitHub Actions).
for _noisy in ("httpx", "httpcore", "aiohttp"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
# Lien public (pas un secret) vers le profil d'assistance technique.
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/Idravisse777")

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

FUEL_FIELD = {
    "Gazole": "gazole_prix",
    "SP95-E10": "e10_prix",
    "SP95": "sp95_prix",
    "SP98": "sp98_prix",
    "E85": "e85_prix",
    "GPLc": "gplc_prix",
}
# Nom du carburant tel qu'il apparaît dans les champs de rupture de l'API.
FUEL_API_NAME = {
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

# ───────────────── Durcissement sécurité ─────────────────
MAX_CITY_LEN = 60
ALLOWED_RADII = {1, *RADII_KM}
_GEO_CODE_RE = re.compile(r"[0-9A-Za-z]{2,5}")


def valid_fuel(value):
    """Seuls les carburants proposés par les boutons sont acceptés."""
    return isinstance(value, str) and value in FUEL_FIELD


def valid_geo_code(value):
    """Codes région / département / commune : lettres et chiffres uniquement."""
    return bool(_GEO_CODE_RE.fullmatch(str(value or "")))


def geo_where(p):
    """Clause géographique de l'API prix, construite uniquement à partir de nombres."""
    lon, lat, radius = float(p["lon"]), float(p["lat"]), int(p["radius"])
    if not (math.isfinite(lon) and math.isfinite(lat)):
        raise ValueError("coordonnées invalides")
    return f"within_distance(geom, geom'POINT({lon} {lat})', {radius} km)"


# Anti-flood : 20 requêtes / 10 s / chat (le propriétaire est exempté).
FLOOD_MAX, FLOOD_WINDOW = 20, 10.0
_FLOOD = {}


def flooded(chat_id):
    now = time.time()
    start, count = _FLOOD.get(chat_id, (now, 0))
    if now - start > FLOOD_WINDOW:
        start, count = now, 0
    count += 1
    _FLOOD[chat_id] = (start, count)
    if len(_FLOOD) > 5000:
        for k in [k for k, (t, _) in _FLOOD.items() if now - t > FLOOD_WINDOW]:
            _FLOOD.pop(k, None)
    return count > FLOOD_MAX


def entry_mode_keyboard():
    """Clavier normal (non-inline) : seul ce type de clavier peut
    demander la position GPS de l'utilisateur via Telegram."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📍 Partager ma position (plus précis)", request_location=True)],
            [KeyboardButton("🗺️ Choisir ma ville manuellement")],
            [KeyboardButton("🏠 Accueil")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
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
            check=False, capture_output=True, timeout=60,
        )
        subprocess.run(
            ["git", "config", "user.name", "RoulerSansPleurer Bot"],
            check=False, capture_output=True, timeout=60,
        )
        subprocess.run(
            ["git", "add", str(USERS_FILE)],
            check=False, capture_output=True, timeout=60,
        )
        commit = subprocess.run(
            ["git", "commit", "-m", "Mise a jour automatique des utilisateurs"],
            capture_output=True, text=True, timeout=60,
        )
        if commit.returncode == 0:
            push = subprocess.run(
                ["git", "push"],
                capture_output=True, text=True, timeout=60,
            )
            if push.returncode == 0:
                _last_git_push = now
            else:
                print("[GIT] push echoue :", push.stderr.strip())
    except Exception as exc:
        print("[GIT] erreur :", exc)


def load_users():
    global users
    if not USERS_FILE.exists():
        return
    text = USERS_FILE.read_text(encoding="utf-8").strip()

    try:
        data = json.loads(text) if text else {}
        if not isinstance(data, dict):
            raise ValueError("format inattendu")
        users = data
    except Exception as exc:
        # Fichier corrompu : on le met de côté au lieu de l'écraser silencieusement au prochain enregistrement.
        backup = USERS_FILE.with_name(f"{USERS_FILE.name}.corrompu-{int(time.time())}")
        try:
            USERS_FILE.replace(backup)
        except OSError:
            pass
        print(f"[DATA] fichier utilisateurs illisible ({exc}) -> mis de côté : {backup}")
        users = {}


def save_users():
    raw = json.dumps(users, ensure_ascii=False, indent=2)
    # Écriture atomique : un arrêt en plein enregistrement ne peut plus laisser un fichier tronqué.
    tmp = USERS_FILE.with_name(USERS_FILE.name + ".tmp")
    tmp.write_text(raw, encoding="utf-8")
    os.replace(tmp, USERS_FILE)
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
            "joined_at": time.strftime("%Y-%m-%d %H:%M"),
            "saved": [],
            "role": "user",
            "banned": False,
        }
    users[key].setdefault("mode_fouine", False)
    users[key].setdefault("joined_at", None)
    users[key].setdefault("saved", [])
    users[key].setdefault("role", "user")
    users[key].setdefault("banned", False)
    users[key].setdefault("notifications_enabled", True)
    users[key].setdefault("quiet_mode", False)
    users[key].setdefault("quiet_start", "22:00")
    users[key].setdefault("quiet_end", "08:00")
    users[key].setdefault("quiet_timezone", "Europe/Paris")
    users[key].setdefault("pending_notifications", [])
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
            f"{GEO_URL}/regions/{quote(str(region_code), safe='')}/departements",
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
            f"{GEO_URL}/departements/{quote(str(department_code), safe='')}/communes",
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


def is_admin(chat_id):
    """Seul le chat défini dans le secret CHAT_ID est administrateur."""
    return bool(CHAT_ID) and str(chat_id) == CHAT_ID


def _as_set(value):
    if not value:
        return set()
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = re.split(r"[;,|]", str(value))
    return {str(i).strip().lower() for i in items if str(i).strip()}


def rupture_kind(f, fuel):
    """Retourne None (dispo), 'temporaire', 'définitive' ou 'rupture' pour ce carburant."""
    name = FUEL_API_NAME[fuel].lower()
    names = {name, fuel.lower()}
    if names & _as_set(f.get("carburants_rupture_definitive")):
        return "définitive"
    if names & _as_set(f.get("carburants_rupture_temporaire")):
        return "temporaire"
    if names & _as_set(f.get("carburants_indisponibles")):
        return "rupture"
    prefix = FUEL_FIELD[fuel][:-5]
    rtype = f.get(f"{prefix}_rupture_type")
    if rtype:
        return "définitive" if "def" in str(rtype).lower() else "temporaire"
    return None


async def shortages_for_user(p, show=3):
    """Stations en rupture du carburant choisi dans le rayon. None si indisponible."""
    try:
        label = FUEL_API_NAME[p["fuel"]]
        where = geo_where(p) + f' and carburants_indisponibles like "{label}"'
        async with aiohttp.ClientSession() as session:
            data = await get_json(session, DATA_URL, {"where": where, "limit": 50})
        stations = []
        for rec in data.get("results", []):
            f = rec.get("fields", rec)
            geom = f.get("geom")
            try:
                lon, lat = float(geom[0]), float(geom[1])
                dist = haversine_km(p["lat"], p["lon"], lat, lon)
            except Exception:
                dist = None
            stations.append({
                "ville": f.get("ville", ""),
                "adresse": f.get("adresse", ""),
                "cp": f.get("cp", ""),
                "distance": dist,
                "kind": rupture_kind(f, p["fuel"]) or "rupture",
                "others": sorted(
                    {x.strip() for x in re.split(r"[;,|]", ";".join(
                        f.get("carburants_indisponibles") if isinstance(f.get("carburants_indisponibles"), list)
                        else [str(f.get("carburants_indisponibles") or "")]
                    )) if x.strip()} - {FUEL_API_NAME[p["fuel"]]}
                ),
            })
        stations.sort(key=lambda s: (s["distance"] is None, s["distance"] or 0))
        return {"total": data.get("total_count", len(stations)), "stations": stations[:show]}
    except Exception as exc:
        print("[RUPTURES] indisponible :", exc)
        return None


def shortage_block(p, shortages):
    if not shortages or not shortages.get("total"):
        return ""
    text = (
        f"🚨 <b>Rupture de {html.escape(p['fuel'])}</b> : "
        f"{shortages['total']} station(s) dans ton rayon\n"
    )
    for s in shortages["stations"]:
        dist = f" • {s['distance']:.1f} km" if s["distance"] is not None else ""
        kind = "" if s["kind"] == "rupture" else f" ({s['kind']})"
        text += (
            f"⛔ {html.escape(s['ville'])} — {html.escape(s['adresse'])}"
            f"{dist}{kind}\n"
        )
    return text + "\n"


def _minutes_of_day(value):
    try:
        hour, minute = map(int, str(value).split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour * 60 + minute
    except (TypeError, ValueError):
        return None


def quiet_now(profile, now=None):
    """Vrai si l'utilisateur est dans sa plage de silence (heure locale)."""
    if not profile.get("quiet_mode"):
        return False
    try:
        tz = ZoneInfo(profile.get("quiet_timezone") or "Europe/Paris")
    except Exception:
        tz = ZoneInfo("Europe/Paris")
    now = now or datetime.now(tz)
    current = now.hour * 60 + now.minute
    start = _minutes_of_day(profile.get("quiet_start", "22:00"))
    end = _minutes_of_day(profile.get("quiet_end", "08:00"))
    if start is None or end is None or start == end:
        return False
    if start < end:
        return start <= current < end
    return current >= start or current < end


def notifications_allowed(profile):
    return bool(profile.get("notifications_enabled", True)) and not quiet_now(profile)


def notifications_keyboard(profile):
    all_on = bool(profile.get("notifications_enabled", True))
    night_on = bool(profile.get("quiet_mode", False))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            ("🔔 Notifications : activées" if all_on else "🔕 Notifications : désactivées"),
            callback_data="NOTIF:ALL")],
        [InlineKeyboardButton(
            ("🌙 Mode nuit : activé" if night_on else "🌙 Mode nuit : désactivé"),
            callback_data="NOTIF:NIGHT")],
        [InlineKeyboardButton("🕒 Modifier les horaires", callback_data="NOTIF:HOURS")],
        [InlineKeyboardButton("↩️ Horaires par défaut (22h–08h)", callback_data="NOTIF:RESET")],
        [InlineKeyboardButton("🏠 Retour à l'accueil", callback_data="MENU")],
    ])


def notifications_text(profile):
    enabled = "activées" if profile.get("notifications_enabled", True) else "désactivées"
    night = "activé" if profile.get("quiet_mode", False) else "désactivé"
    return (
        "🔔 <b>Mes notifications</b>\n\n"
        f"Notifications générales : <b>{enabled}</b>\n"
        f"Mode nuit : <b>{night}</b>\n"
        f"Plage silencieuse : <b>{profile.get('quiet_start', '22:00')} – {profile.get('quiet_end', '08:00')}</b>\n\n"
        "Pendant le mode nuit, les alertes de prix sont mises en attente puis envoyées à la fin de la plage silencieuse.\n"
        "Pour modifier les horaires, touche le bouton dédié puis envoie par exemple <code>22:30-07:45</code>."
    )


async def notifications_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat.id
    profile = user_profile(chat_id)
    action = q.data.split(":", 1)[1]
    if action == "OPEN":
        await q.answer()
        await q.edit_message_text(
            notifications_text(profile), parse_mode=ParseMode.HTML,
            reply_markup=notifications_keyboard(profile),
        )
        return
    if action == "ALL":
        profile["notifications_enabled"] = not profile.get("notifications_enabled", True)
        save_users()
        await q.answer("Préférences enregistrées")
    elif action == "NIGHT":
        profile["quiet_mode"] = not profile.get("quiet_mode", False)
        save_users()
        await q.answer("Mode nuit activé" if profile["quiet_mode"] else "Mode nuit désactivé")
    elif action == "HOURS":
        context.user_data.pop("awaiting_notification_end", None)
        context.user_data["awaiting_notification_start"] = True
        await q.answer()
        await q.message.reply_text(
            "🕒 <b>Choisis le début de ta plage silencieuse.</b>\n"
            "Envoie une heure au format <code>22:30</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    elif action == "RESET":
        profile["quiet_start"], profile["quiet_end"] = "22:00", "08:00"
        save_users()
        await q.answer("Horaires réinitialisés")
    await q.edit_message_text(
        notifications_text(profile), parse_mode=ParseMode.HTML,
        reply_markup=notifications_keyboard(profile),
    )


async def notification_hours_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").strip()
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", raw)
    if not match:
        await update.message.reply_text(
            "❌ Heure invalide. Utilise le format <code>HH:MM</code>, par exemple <code>22:30</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    value = f"{int(match.group(1)):02d}:{match.group(2)}"

    if context.user_data.get("awaiting_notification_start"):
        context.user_data["notification_start_draft"] = value
        context.user_data.pop("awaiting_notification_start", None)
        context.user_data["awaiting_notification_end"] = True
        await update.message.reply_text(
            f"✅ Début enregistré : <b>{value}</b>.\n"
            "Maintenant, choisis la fin de ta plage silencieuse au format <code>HH:MM</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not context.user_data.get("awaiting_notification_end"):
        return

    start = context.user_data.get("notification_start_draft")
    end = value
    if not start:
        context.user_data.pop("awaiting_notification_end", None)
        await update.message.reply_text("La configuration a expiré. Relance « Modifier les horaires ».")
        return
    if start == end:
        await update.message.reply_text(
            "❌ Le début et la fin ne peuvent pas être identiques. Choisis une autre heure de fin (format <code>HH:MM</code>).",
            parse_mode=ParseMode.HTML,
        )
        return

    profile = user_profile(update.effective_chat.id)
    profile["quiet_start"], profile["quiet_end"] = start, end
    profile["quiet_mode"] = True
    save_users()
    context.user_data.pop("awaiting_notification_end", None)
    context.user_data.pop("notification_start_draft", None)
    await update.message.reply_text(
        "✅ <b>Ta plage silencieuse est enregistrée.</b>\n\n" + notifications_text(profile),
        parse_mode=ParseMode.HTML,
        reply_markup=notifications_keyboard(profile),
    )


def main_keyboard(chat_id=None):
    admin_row = (
        [[InlineKeyboardButton("⚙️ Administration", callback_data="ADM:HOME")]]
        if is_staff(chat_id) else []
    )
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⛽ Voir les prix", callback_data="PRICES"),
            InlineKeyboardButton("📍 Plus proche", callback_data="NEAREST"),
        ],
        [InlineKeyboardButton("💶 Moins cher", callback_data="PRICES")],
        [InlineKeyboardButton("🚨 Stations en rupture", callback_data="RUPTURES")],
        [InlineKeyboardButton("🔄 Actualiser maintenant", callback_data="PRICES")],
        [InlineKeyboardButton("⚙️ Modifier ma localisation", callback_data="SET_LOCATION")],
        [InlineKeyboardButton("⛽ Modifier le carburant", callback_data="SET_FUEL")],
        [
            InlineKeyboardButton("💾 Enregistrer cette recherche", callback_data="SAVE_SEARCH"),
        ],
        [InlineKeyboardButton("📂 Mes recherches", callback_data="MY_SEARCHES")],
        [InlineKeyboardButton("🦡 Mode Fouine", callback_data="FOUINE")],
        [InlineKeyboardButton("🔔 Mes notifications", callback_data="NOTIF:OPEN")],
    ] + admin_row)


async def open_user_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Retourne au menu utilisateur sans relancer la configuration ni modifier les recherches."""
    chat_id = update.effective_chat.id
    p = user_profile(chat_id)
    if not p.get("city") or p.get("lat") is None or not p.get("fuel"):
        message = (
            "🏠 <b>Accueil</b>\n\n"
            "Ton espace n'est pas encore configuré. Choisis ta position pour commencer."
        )
        if update.callback_query:
            q = update.callback_query
            await q.answer()
            await q.edit_message_text(message, parse_mode=ParseMode.HTML)
            await q.message.reply_text("👇 Choisis une option :", reply_markup=entry_mode_keyboard())
        else:
            await update.message.reply_text(message, parse_mode=ParseMode.HTML,
                                            reply_markup=entry_mode_keyboard())
        return CHOOSE_ENTRY

    if update.callback_query:
        q = update.callback_query
        await q.answer()
        await q.edit_message_text(
            profile_text(p),
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(chat_id),
        )
    else:
        await update.message.reply_text(
            profile_text(p),
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(chat_id),
        )
    return MAIN_MENU


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
        reply_markup=main_keyboard(q.message.chat.id),
    )
    return MAIN_MENU


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    context.user_data.pop("adm_awaiting", None)
    is_new = str(chat_id) not in users
    p = user_profile(chat_id)
    update_user_identity(update.effective_user, p)
    save_users()

    # Notification au propriétaire quand quelqu'un rejoint le bot.
    if is_new and CHAT_ID and not is_admin(chat_id):
        try:
            await context.bot.send_message(
                chat_id=int(CHAT_ID),
                text=(
                    "🆕 <b>Nouvel utilisateur</b>\n\n"
                    f"👤 {html.escape(user_label(p))}\n"
                    f"🆔 <code>{chat_id}</code>\n"
                    f"👥 Total : {len(users)}"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👤 Voir le profil", callback_data=f"ADM:U:{chat_id}")]
                ]),
            )
        except Exception as exc:
            print("[BOT] notification nouvel utilisateur impossible :", exc)

    # Message principal : texte + boutons larges (assistance / administration).
    await update.message.reply_text(
        "🇫🇷 <b>ROULER SANS PLEURER — FRANCE</b>\n\n"
        "📍 Comment veux-tu renseigner ta position ?\n"
        "Le partage GPS donne des résultats plus précis que le choix manuel "
        "d'une ville.\n\n"
        "⛑️ Un souci ou une question ?\n⚡️ BY DRISSI",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⛽ Ouvrir le panneau carburants", callback_data="USER_HOME")]]
            + [[InlineKeyboardButton("🔔 Mes notifications", callback_data="NOTIF:OPEN")]]
            + [[InlineKeyboardButton("⛑️ Assistance technique", url=SUPPORT_URL)]]
            + ([[InlineKeyboardButton("⚙️ Administration", callback_data="ADM:HOME")]]
               if is_staff(chat_id) else [])
        ),
    )
    # Telegram n'autorise qu'un seul type de clavier par message : les boutons
    # de position (clavier normal) sont donc portés par ce court message.
    await update.message.reply_text(
        "👇 Choisis une option :",
        reply_markup=entry_mode_keyboard(),
    )
    return CHOOSE_ENTRY


async def entry_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    regions = await get_regions()
    context.user_data["regions"] = regions
    context.user_data["region_page"] = 0

    await update.message.reply_text(
        "D'accord, on part sur la sélection manuelle.",
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

    parts = q.data.split(":", 2)
    if len(parts) != 3 or not valid_geo_code(parts[1]):
        return CHOOSE_REGION
    _, code, name = parts
    name = name[:60]
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

    parts = q.data.split(":", 2)
    if len(parts) != 3 or not valid_geo_code(parts[1]):
        return CHOOSE_DEPARTMENT
    _, code, name = parts
    name = name[:60]
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
    if context.user_data.get("adm_awaiting"):
        return ENTER_CITY  # le texte est destiné à une annonce admin
    city_name = update.message.text.strip()
    if len(city_name) < 2:
        await update.message.reply_text("❌ Nom de ville trop court. Réessaie.")
        return ENTER_CITY
    if len(city_name) > MAX_CITY_LEN:
        await update.message.reply_text("❌ Nom de ville trop long. Réessaie.")
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
    if radius not in ALLOWED_RADII:
        return CHOOSE_RADIUS

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
    if not valid_fuel(fuel):
        return CHOOSE_FUEL

    p = user_profile(q.message.chat.id)
    p["fuel"] = fuel
    p["last_signature"] = None
    save_users()

    await q.edit_message_text(
        profile_text(p),
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(q.message.chat.id),
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
    fuel_field = FUEL_FIELD[p["fuel"]]

    # IMPORTANT : l'API utilise geom en vraies coordonnées GPS
    # (longitude, latitude), tandis que les champs latitude/longitude
    # historiques sont stockés sous forme entière (ex. 4818300).
    where = geo_where(p)

    params = {
        "where": where,
        "limit": 100,
        "order_by": f"{fuel_field} asc",
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

        # Station en rupture pour ce carburant : on ne la propose pas.
        if rupture_kind(f, p["fuel"]):
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


def format_prices(p, stations, sort_by="price", shortages=None):
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
            + shortage_block(p, shortages) +
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

    text += shortage_block(p, shortages)
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
    shortages = await shortages_for_user(p)
    await q.edit_message_text(
        format_prices(p, stations, sort_by="distance", shortages=shortages),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=main_keyboard(q.message.chat.id),
    )
    return MAIN_MENU


async def show_ruptures(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Recherche des ruptures…")
    p = user_profile(q.message.chat.id)
    if not p["city"] or p["lat"] is None:
        await q.edit_message_text("❌ Configure d'abord ta localisation avec /start.")
        return MAIN_MENU

    sh = await shortages_for_user(p, show=10)
    if sh is None:
        text = "❌ Impossible de récupérer les ruptures actuellement.\nRéessaie dans quelques secondes."
    elif not sh["total"]:
        text = (
            f"✅ <b>Aucune rupture de {html.escape(p['fuel'])}</b> signalée "
            f"dans un rayon de {p['radius']} km autour de {html.escape(p['city'])}."
        )
    else:
        text = (
            f"🚨 <b>Ruptures de {html.escape(p['fuel'])}</b>\n"
            f"📍 {html.escape(p['city'])} + rayon de {p['radius']} km\n"
            f"{sh['total']} station(s) concernée(s) :\n\n"
        )
        for st in sh["stations"]:
            dist = f" • {st['distance']:.1f} km" if st["distance"] is not None else ""
            kind = "" if st["kind"] == "rupture" else f" ({st['kind']})"
            text += (
                f"⛔ <b>{html.escape(st['ville'])}</b>{dist}{kind}\n"
                f"📍 {html.escape(st['adresse'])}, {html.escape(st['cp'])}\n"
            )
            if st["others"]:
                text += f"Aussi indisponible : {html.escape(', '.join(st['others']))}\n"
            text += f'<a href="{maps_url(st)}">🗺️ Ouvrir dans Google Maps</a>\n\n'
        if sh["total"] > len(sh["stations"]):
            text += f"… et {sh['total'] - len(sh['stations'])} autre(s), plus éloignée(s).\n"
    await q.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=main_keyboard(q.message.chat.id),
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

    shortages = await shortages_for_user(p)
    await q.edit_message_text(
        format_prices(p, stations, shortages=shortages),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=main_keyboard(q.message.chat.id),
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


# ───────────────────────── Administration ─────────────────────────
# Hiérarchie : owner (secret CHAT_ID, intouchable) > admin > moderator > user.
# Le rôle "owner" ne vient QUE du secret CHAT_ID : aucune valeur du fichier JSON
# ne peut le donner. Un rôle ne peut jamais agir sur un rôle égal ou supérieur.
ROLE_RANK = {"user": 0, "moderator": 1, "admin": 2, "owner": 3}
ROLE_NAME = {
    "owner": "Propriétaire",
    "admin": "Administrateur",
    "moderator": "Modérateur",
    "user": "Utilisateur",
}
ROLE_ICON = {"owner": "💎", "admin": "👑", "moderator": "🛡️", "user": "👤"}
ADM_PAGE_SIZE = 8


def role_of(chat_id):
    if is_admin(chat_id):
        return "owner"
    p = users.get(str(chat_id))
    if not p or p.get("banned"):
        return "user"
    r = p.get("role")
    return r if r in ("moderator", "admin") else "user"


def rank_of(chat_id):
    return ROLE_RANK[role_of(chat_id)]


def is_staff(chat_id):
    return rank_of(chat_id) >= 1


def user_label(p):
    name = " ".join(x for x in [p.get("first_name"), p.get("last_name")] if x).strip()
    tag = f"@{p['username']}" if p.get("username") else ""
    label = " ".join(x for x in [name, tag] if x)
    return label or f"ID {p.get('chat_id')}"


def _is_owner_id(target_id):
    return bool(CHAT_ID) and str(target_id) == CHAT_ID


def can_ban(actor_id, target_id):
    if _is_owner_id(target_id) or str(actor_id) == str(target_id):
        return False
    ar = rank_of(actor_id)
    return ar >= 1 and ar > rank_of(target_id)


def can_delete(actor_id, target_id):
    if _is_owner_id(target_id) or str(actor_id) == str(target_id):
        return False
    ar = rank_of(actor_id)
    return ar >= 2 and ar > rank_of(target_id)


def can_set_role(actor_id, target_id, new_role):
    if new_role not in ("user", "moderator", "admin"):
        return False
    if _is_owner_id(target_id) or str(actor_id) == str(target_id):
        return False
    t = users.get(str(target_id))
    if not t or (t.get("banned") and new_role != "user"):
        return False
    ar, tr = rank_of(actor_id), rank_of(target_id)
    if ar >= 3:
        return True
    if ar == 2:
        return new_role in ("user", "moderator") and tr < 2
    return False


async def block_banned(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bloque toute interaction d'un utilisateur exclu (jamais le propriétaire)."""
    chat = update.effective_chat
    if not chat:
        return
    # Le bot ne fonctionne qu'en conversation privée.
    if chat.type != "private":
        try:
            if update.callback_query:
                await update.callback_query.answer("Utilise ce bot en conversation privée.", show_alert=True)
        except Exception:
            pass
        raise ApplicationHandlerStop
    if is_admin(chat.id):
        return
    if flooded(chat.id):
        try:
            if update.callback_query:
                await update.callback_query.answer()
        except Exception:
            pass
        raise ApplicationHandlerStop
    p = users.get(str(chat.id))
    if p and p.get("banned"):
        try:
            if update.callback_query:
                await update.callback_query.answer("⛔ Accès refusé.", show_alert=True)
            elif update.message:
                await update.message.reply_text("⛔️ Ton accès au bot a été révoqué.")
        except Exception:
            pass
        raise ApplicationHandlerStop


ROLE_MESSAGES = {
    "admin": (
        "👑 <b>Tu as été nommé Administrateur.</b>\n\n"
        "Tu peux gérer les utilisateurs et les modérateurs.\n"
        "Envoie /start pour voir le bouton ⚙️ Administration."
    ),
    "moderator": (
        "🛡️ <b>Tu as été nommé Modérateur.</b>\n\n"
        "Tu peux révoquer et restaurer l'accès des utilisateurs.\n"
        "Envoie /start pour voir le bouton ⚙️ Administration."
    ),
    "user": (
        "👤 <b>Ton rôle a été retiré.</b>\n\n"
        "Tu es redevenu utilisateur lambda."
    ),
}


async def notify_user(context, target_id, text):
    """Prévient la personne concernée (échec silencieux si le bot est bloqué)."""
    try:
        await context.bot.send_message(
            chat_id=int(target_id), text=text, parse_mode=ParseMode.HTML)
    except Exception as exc:
        print(f"[BOT] notification à {target_id} impossible :", exc)


def panel_text():
    total = len(users)
    banned = sum(1 for p in users.values() if p.get("banned"))
    staff = sum(1 for p in users.values() if p.get("role") in ("moderator", "admin"))
    return (
        "⚙️ <b>Administration</b>\n\n"
        f"👥 Utilisateurs : <b>{total}</b>\n"
        f"⛔️ Révoqués : <b>{banned}</b>\n"
        f"👑🛡️ Équipe (modérateurs / admins) : <b>{staff}</b>"
    )


def panel_keyboard(actor_id):
    rows = [[InlineKeyboardButton("👥 Utilisateurs", callback_data="ADM:LIST:0")]]
    if is_admin(actor_id):
        rows.append([InlineKeyboardButton("📢 Annonce à tous", callback_data="ADM:ANN")])
    # Redémarrage accessible à toute l'équipe (admin et modérateur).
    if is_staff(actor_id):
        rows.append([InlineKeyboardButton("🔄 Redémarrer le bot", callback_data="ADM:RESTART")])
    return InlineKeyboardMarkup(rows)


def _sorted_user_ids():
    def key(item):
        cid, p = item
        return (-rank_of(cid), p.get("joined_at") or "", cid)
    return [cid for cid, _ in sorted(users.items(), key=key)]


def list_view(actor_id, page):
    ids = _sorted_user_ids()
    pages = max(1, math.ceil(len(ids) / ADM_PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    chunk = ids[page * ADM_PAGE_SIZE:(page + 1) * ADM_PAGE_SIZE]
    rows = []
    for cid in chunk:
        p = users[cid]
        icon = "⛔️" if p.get("banned") else ROLE_ICON[role_of(cid)]
        rows.append([InlineKeyboardButton(
            f"{icon} {user_label(p)}"[:45], callback_data=f"ADM:U:{cid}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"ADM:LIST:{page-1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"ADM:LIST:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("↩️ Retour", callback_data="ADM:PANEL")])
    text = f"👥 <b>Utilisateurs</b> ({len(ids)}) — page {page+1}/{pages}"
    return text, InlineKeyboardMarkup(rows)


def detail_view(actor_id, tid):
    p = users[tid]
    role = role_of(tid)
    status = "⛔️ Accès révoqué" if p.get("banned") else "✅ Actif"
    text = (
        f"{ROLE_ICON[role]} <b>{html.escape(user_label(p))}</b>\n\n"
        f"ID : <code>{html.escape(str(tid))}</code>\n"
        f"Rôle : {ROLE_NAME[role]}\n"
        f"Statut : {status}\n"
        f"Inscrit : {html.escape(p.get('joined_at') or 'inconnu')}\n"
        f"Ville : {html.escape(p.get('city') or '—')}\n"
        f"Carburant : {html.escape(p.get('fuel') or '—')}"
    )
    rows = []
    if can_ban(actor_id, tid):
        if p.get("banned"):
            rows.append([InlineKeyboardButton("✅ Restaurer l'accès", callback_data=f"ADM:UNBAN:{tid}")])
        else:
            rows.append([InlineKeyboardButton("⛔️ Révoquer l'accès", callback_data=f"ADM:BAN:{tid}")])
    if role != "moderator" and can_set_role(actor_id, tid, "moderator"):
        rows.append([InlineKeyboardButton("🛡️ Nommer modérateur", callback_data=f"ADM:ROLE:{tid}:moderator")])
    if role != "admin" and can_set_role(actor_id, tid, "admin"):
        rows.append([InlineKeyboardButton("👑 Nommer administrateur", callback_data=f"ADM:ROLE:{tid}:admin")])
    if role != "user" and can_set_role(actor_id, tid, "user"):
        rows.append([InlineKeyboardButton("⬇️ Retirer le rôle", callback_data=f"ADM:ROLE:{tid}:user")])
    if can_delete(actor_id, tid):
        rows.append([InlineKeyboardButton("🗑️ Supprimer", callback_data=f"ADM:DEL:{tid}")])
    rows.append([InlineKeyboardButton("↩️ Liste", callback_data="ADM:LIST:0")])
    return text, InlineKeyboardMarkup(rows)


async def _show(q, text, markup):
    await q.edit_message_text(
        text, parse_mode=ParseMode.HTML,
        disable_web_page_preview=True, reply_markup=markup,
    )


async def unpin_update_job(context: ContextTypes.DEFAULT_TYPE):
    """Retire une annonce épinglée après 24 h, sans toucher à une épingle plus récente."""
    data = context.job.data or {}
    chat_id = int(data.get("chat_id"))
    message_id = int(data.get("message_id"))
    profile = users.get(str(chat_id))
    if not profile or int(profile.get("update_pin_message_id") or 0) != message_id:
        return  # cette annonce a déjà été remplacée par une mise à jour plus récente
    try:
        await context.bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)
    except Exception as exc:
        print(f"[BOT] retrait automatique de l'épingle impossible ({chat_id}): {exc}")
        return
    profile.pop("update_pin_message_id", None)
    profile.pop("update_pin_expires_at", None)
    save_users()


def schedule_update_unpin(application, chat_id, message_id, expires_at):
    """Programme le retrait d'une épingle, avec un nom unique par message."""
    delay = max(1, float(expires_at) - time.time())
    application.job_queue.run_once(
        unpin_update_job,
        when=delay,
        data={"chat_id": int(chat_id), "message_id": int(message_id)},
        name=f"update-unpin-{chat_id}-{message_id}",
    )


async def adm_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    actor = q.from_user.id
    if not is_staff(actor):
        await q.answer("⛔ Réservé à l'équipe d'administration.", show_alert=True)
        return
    parts = q.data.split(":")
    act = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else None

    # Ouvre le panneau dans un NOUVEAU message : le menu principal reste intact.
    if act == "HOME":
        await q.answer()
        await q.message.reply_text(
            panel_text(), parse_mode=ParseMode.HTML, reply_markup=panel_keyboard(actor))
        return
    if act == "PANEL":
        await q.answer()
        await _show(q, panel_text(), panel_keyboard(actor))
        return
    if act == "LIST":
        await q.answer()
        text, markup = list_view(actor, int(arg or 0))
        await _show(q, text, markup)
        return

    # ── Redémarrage du bot (admin et modérateur) ──
    if act == "RESTART":
        await q.answer()
        await _show(
            q,
            "🔄 <b>Redémarrer le bot ?</b>\n\n"
            "Le bot va se relancer. Les utilisateurs pourront le réutiliser "
            "après quelques secondes.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirmer le redémarrage", callback_data="ADM:RESTARTOK")],
                [InlineKeyboardButton("❌ Annuler", callback_data="ADM:PANEL")],
            ]),
        )
        return
    if act == "RESTARTOK":
        # Double vérification : seuls les admins/modérateurs peuvent relancer.
        if not is_staff(actor):
            await q.answer("⛔ Réservé à l'équipe d'administration.", show_alert=True)
            return
        await q.answer("🔄 Redémarrage en cours…")
        try:
            await q.edit_message_text(
                "🔄 <b>Redémarrage du bot en cours…</b>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        await asyncio.sleep(1)
        # Remplace le processus Python courant : même script, mêmes secrets et même environnement.
        os.execv(sys.executable, [sys.executable] + sys.argv)
        return

    # ── Annonce (propriétaire uniquement) ──
    if act in ("ANN", "ANNSEND", "ANNCANCEL"):
        if not is_admin(actor):
            await q.answer("⛔ Réservé au propriétaire.", show_alert=True)
            return
        if act == "ANN":
            await q.answer()
            context.user_data["adm_awaiting"] = "announce"
            await _show(
                q,
                "📢 <b>Annonce à tous les utilisateurs</b>\n\n"
                "Écris ton message maintenant (un seul message). "
                "Tu pourras le relire avant l'envoi.",
                InlineKeyboardMarkup([[InlineKeyboardButton("❌ Annuler", callback_data="ADM:ANNCANCEL")]]),
            )
        elif act == "ANNCANCEL":
            await q.answer("Annulé")
            context.user_data.pop("adm_awaiting", None)
            context.user_data.pop("announce_html", None)
            await _show(q, panel_text(), panel_keyboard(actor))
        else:
            await q.answer("Envoi en cours…")
            body = context.user_data.pop("announce_html", None)
            if not body:
                await _show(q, "❌ Annonce expirée. Recommence.", panel_keyboard(actor))
                return
            sent = failed = pinned = pin_failed = 0
            for cid, p in list(users.items()):
                if p.get("banned"):
                    continue
                try:
                    message = await context.bot.send_message(
                        chat_id=int(cid),
                        text="📢 <b>Annonce</b>\n\n" + body,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                    sent += 1
                    # L'annonce reste un message normal même si l'épinglage est refusé.
                    try:
                        await context.bot.pin_chat_message(
                            chat_id=int(cid),
                            message_id=message.message_id,
                            disable_notification=True,
                        )
                        old_message_id = p.get("update_pin_message_id")
                        expires_at = time.time() + 24 * 60 * 60
                        p["update_pin_message_id"] = message.message_id
                        p["update_pin_expires_at"] = expires_at
                        save_users()
                        # Remplace l'ancienne épingle uniquement après la réussite de la nouvelle.
                        if old_message_id and int(old_message_id) != message.message_id:
                            try:
                                await context.bot.unpin_chat_message(
                                    chat_id=int(cid), message_id=int(old_message_id)
                                )
                            except Exception as exc:
                                print(f"[BOT] ancienne épingle impossible à retirer ({cid}): {exc}")
                        schedule_update_unpin(context.application, int(cid), message.message_id, expires_at)
                        pinned += 1
                    except Exception as exc:
                        pin_failed += 1
                        print(f"[BOT] épinglage impossible pour {cid}: {exc}")
                except Exception:
                    failed += 1
                await asyncio.sleep(0.05)  # reste sous les limites d'envoi de Telegram
            await _show(
                q,
                f"✅ Annonce envoyée à <b>{sent}</b> utilisateur(s).\n"
                f"📌 Épingles actives : <b>{pinned}</b> (24 h)."
                + (f"\n⚠️ {pin_failed} épinglage(s) impossible(s) (droits Telegram ou restriction du chat)." if pin_failed else "")
                + (f"\n⚠️ {failed} envoi(s) impossible(s) (bot bloqué ou chat supprimé)." if failed else ""),
                panel_keyboard(actor),
            )
        return

    # ── Actions sur un utilisateur ──
    tid = arg
    if tid not in users:
        await q.answer("Utilisateur introuvable.", show_alert=True)
        text, markup = list_view(actor, 0)
        await _show(q, text, markup)
        return

    if act == "U":
        await q.answer()
        text, markup = detail_view(actor, tid)
        await _show(q, text, markup)
    elif act in ("BAN", "UNBAN"):
        if not can_ban(actor, tid):
            await q.answer("⛔ Tu n'as pas le droit d'agir sur cet utilisateur.", show_alert=True)
            return
        users[tid]["banned"] = (act == "BAN")
        if act == "BAN":
            users[tid]["last_signature"] = None
        save_users()
        await q.answer("⛔️ Accès révoqué" if act == "BAN" else "✅ Accès restauré")
        await notify_user(
            context, tid,
            "⛔️ <b>Ton accès au bot a été révoqué.</b>" if act == "BAN"
            else "✅ <b>Ton accès au bot a été restauré.</b>\nEnvoie /start pour reprendre.",
        )
        text, markup = detail_view(actor, tid)
        await _show(q, text, markup)
    elif act == "ROLE":
        new_role = parts[3] if len(parts) > 3 else ""
        if not can_set_role(actor, tid, new_role):
            await q.answer("⛔ Tu n'as pas le droit de faire ça.", show_alert=True)
            return
        users[tid]["role"] = new_role
        save_users()
        await q.answer(f"{ROLE_NAME[new_role]} ✔")
        await notify_user(context, tid, ROLE_MESSAGES[new_role])
        text, markup = detail_view(actor, tid)
        await _show(q, text, markup)
    elif act == "DEL":
        if not can_delete(actor, tid):
            await q.answer("⛔ Tu n'as pas le droit de supprimer cet utilisateur.", show_alert=True)
            return
        await q.answer()
        await _show(
            q,
            f"🗑️ Supprimer <b>{html.escape(user_label(users[tid]))}</b> ?\n"
            "Son profil sera effacé (il pourra se réinscrire avec /start).",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Oui, supprimer", callback_data=f"ADM:DELOK:{tid}")],
                [InlineKeyboardButton("❌ Non", callback_data=f"ADM:U:{tid}")],
            ]),
        )
    elif act == "DELOK":
        if not can_delete(actor, tid):
            await q.answer("⛔ Tu n'as pas le droit de supprimer cet utilisateur.", show_alert=True)
            return
        users.pop(tid, None)
        save_users()
        await q.answer("🗑️ Supprimé")
        await notify_user(
            context, tid,
            "🗑️ <b>Ton profil a été supprimé.</b>\nTu peux te réinscrire avec /start.")
        text, markup = list_view(actor, 0)
        await _show(q, text, markup)
    else:
        await q.answer()


async def admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Réceptionne les horaires de notification ou le texte d'une annonce."""
    if (
        context.user_data.get("awaiting_notification_start")
        or context.user_data.get("awaiting_notification_end")
    ):
        await notification_hours_text(update, context)
        return
    if context.user_data.get("adm_awaiting") != "announce":
        return
    if not is_admin(update.effective_chat.id):
        context.user_data.pop("adm_awaiting", None)
        return
    context.user_data.pop("adm_awaiting", None)
    context.user_data["announce_html"] = update.message.text_html
    total = sum(1 for p in users.values() if not p.get("banned"))
    await update.message.reply_text(
        "📢 <b>Aperçu de l'annonce :</b>\n\n" + update.message.text_html,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Envoyer à {total} utilisateur(s)", callback_data="ADM:ANNSEND")],
            [InlineKeyboardButton("❌ Annuler", callback_data="ADM:ANNCANCEL")],
        ]),
    )


# ───────────────── Recherches enregistrées (plusieurs par personne) ─────────────────
MAX_SAVED = 5
SAVED_FIELDS = ("city", "lat", "lon", "radius", "fuel", "department", "region")


def search_key(s):
    try:
        return (s.get("fuel"), round(float(s["lat"]), 4), round(float(s["lon"]), 4), s.get("radius"))
    except (TypeError, ValueError, KeyError):
        return None


def search_name(s):
    return f"{s.get('fuel', '?')} · {s.get('city', '?')} · {s.get('radius', '?')} km"


async def save_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    p = user_profile(q.message.chat.id)
    if not p.get("city") or p.get("lat") is None or not p.get("fuel"):
        await q.answer("Configure d'abord ta recherche (ville + carburant).", show_alert=True)
        return MAIN_MENU
    saved = p.setdefault("saved", [])
    key = search_key(p)
    if any(search_key(e) == key for e in saved):
        await q.answer("✅ Cette recherche est déjà enregistrée.", show_alert=True)
        return MAIN_MENU
    if len(saved) >= MAX_SAVED:
        await q.answer(
            f"Maximum {MAX_SAVED} recherches. Supprime-en une dans « Mes recherches ».",
            show_alert=True,
        )
        return MAIN_MENU
    entry = {k: p.get(k) for k in SAVED_FIELDS}
    entry["id"] = str(max([int(e["id"]) for e in saved if str(e.get("id", "")).isdigit()] + [0]) + 1)
    entry["last_signature"] = None
    saved.append(entry)
    save_users()
    await q.answer(f"💾 Enregistrée : {search_name(entry)}", show_alert=True)
    return MAIN_MENU


def searches_view(p):
    saved = p.get("saved", [])
    active = search_key(p)
    rows = []
    for e in saved:
        mark = "✅ " if search_key(e) == active else ""
        rows.append([
            InlineKeyboardButton(f"{mark}⛽ {search_name(e)}"[:50], callback_data=f"SS:{e['id']}"),
            InlineKeyboardButton("🗑️", callback_data=f"SD:{e['id']}"),
        ])
    rows.append([InlineKeyboardButton("↩️ Retour au menu", callback_data="MENU")])
    if saved:
        text = (
            "📂 <b>Mes recherches</b>\n\n"
            "Touche une recherche pour voir les prix tout de suite.\n"
            "Tu recevras aussi une alerte quand les prix de chacune changent.\n"
            f"({len(saved)}/{MAX_SAVED})"
        )
    else:
        text = (
            "📂 <b>Mes recherches</b>\n\n"
            "Aucune recherche enregistrée.\n"
            "Configure une ville et un carburant, puis touche "
            "« 💾 Enregistrer cette recherche »."
        )
    return text, InlineKeyboardMarkup(rows)


async def my_searches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    p = user_profile(q.message.chat.id)
    text, markup = searches_view(p)
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    return MAIN_MENU


async def select_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    p = user_profile(q.message.chat.id)
    sid = q.data.split(":", 1)[1]
    entry = next((e for e in p.get("saved", []) if str(e.get("id")) == sid), None)
    if not entry:
        await q.answer("Recherche introuvable.", show_alert=True)
        text, markup = searches_view(p)
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return MAIN_MENU
    for k in SAVED_FIELDS:
        p[k] = entry.get(k)
    p["latitude"], p["longitude"] = p.get("lat"), p.get("lon")
    p["sort_mode"] = "price"
    reset_price_signature(p)
    save_users()
    return await show_prices(update, context)


async def delete_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    p = user_profile(q.message.chat.id)
    sid = q.data.split(":", 1)[1]
    p["saved"] = [e for e in p.get("saved", []) if str(e.get("id")) != sid]
    save_users()
    await q.answer("🗑️ Supprimée")
    text, markup = searches_view(p)
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    return MAIN_MENU


async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    p = user_profile(q.message.chat.id)
    await q.edit_message_text(
        profile_text(p),
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(q.message.chat.id),
    )
    return MAIN_MENU


async def price_check_job(context: ContextTypes.DEFAULT_TYPE):
    # Une vérification toutes les 10 minutes : recherche active + recherches enregistrées.
    for chat_id, profile in list(users.items()):
        if profile.get("banned"):
            continue

        # Envoie les alertes accumulées dès la fin du mode nuit.
        pending = profile.get("pending_notifications", [])
        if pending and notifications_allowed(profile):
            try:
                digest = "🌅 <b>Récapitulatif de tes alertes pendant le mode nuit</b>\n\n" + "\n\n".join(pending[-20:])
                await context.bot.send_message(
                    chat_id=int(chat_id), text=digest,
                    parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                )
                profile["pending_notifications"] = []
                save_users()
            except Exception as exc:
                print(f"[NOTIF] envoi du récapitulatif impossible ({chat_id}) : {exc}")

        searches = []
        seen = set()
        if profile.get("lat") is not None and profile.get("city") and profile.get("fuel"):
            searches.append((profile, True))
            seen.add(search_key(profile))
        for e in profile.get("saved", []):
            k = search_key(e)
            if k is None or k in seen or e.get("lat") is None:
                continue
            seen.add(k)
            searches.append((e, False))

        for p, is_active in searches:
            try:
                fouine = bool(profile.get("mode_fouine", False)) and is_active
                sort_mode = p.get("sort_mode", "price")
                stations = await prices_for_user(p, sort_by=sort_mode)
                shortages = await shortages_for_user(p)
                signature = "|".join(
                    [sort_mode] +
                    [f"{s['id']}:{s['price']:.3f}" for s in stations] +
                    ([f"R:{shortages['total']}"] if shortages and shortages.get("total") else [])
                )

                if p.get("last_signature") is None:
                    p["last_signature"] = signature
                    save_users()
                    if not fouine:
                        continue

                if fouine or signature != p.get("last_signature"):
                    p["last_signature"] = signature
                    alert_text = (
                        "🦡 <b>Mode Fouine — relevé des prix</b>\n\n"
                        if fouine
                        else
                        "🔔 <b>Mise à jour des prix</b>\n\n"
                    ) + format_prices(p, stations, sort_by=sort_mode, shortages=shortages)
                    if not profile.get("notifications_enabled", True):
                        # Désactivation générale : ne pas envoyer ni mettre en attente.
                        pass
                    elif quiet_now(profile):
                        # Mode nuit : conserve les alertes pour le récapitulatif du matin.
                        queue = profile.setdefault("pending_notifications", [])
                        queue.append(alert_text)
                        profile["pending_notifications"] = queue[-20:]
                    else:
                        await context.bot.send_message(
                            chat_id=int(chat_id), text=alert_text,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                        )
                    save_users()
            except Exception as exc:
                print(f"[CRON] recherche ignorée ({chat_id}) : {exc!r}")
                continue


async def post_init(application: Application):
    load_users()
    # Restaure les retraits d'épingles après un redémarrage du bot.
    now = time.time()
    for cid, profile in list(users.items()):
        message_id = profile.get("update_pin_message_id")
        expires_at = profile.get("update_pin_expires_at")
        if not message_id or not expires_at:
            continue
        try:
            schedule_update_unpin(application, int(cid), int(message_id), float(expires_at))
        except Exception as exc:
            print(f"[BOT] restauration de l'échéance d'épingle impossible ({cid}): {exc}")
    # Notification de démarrage réservée au propriétaire, aux administrateurs
    # et aux modérateurs. Les utilisateurs classiques ne sont pas destinataires.
    recipients = {str(cid) for cid in users.keys() if is_staff(cid)}
    # Le propriétaire est ajouté même s'il n'a pas encore de profil enregistré.
    if CHAT_ID:
        recipients.add(str(CHAT_ID))
    sent = 0
    failed = 0
    for recipient in recipients:
        try:
            await application.bot.send_message(
                chat_id=int(recipient),
                text="🟢 RoulerSansPleurer démarré.",
            )
            sent += 1
        except Exception as exc:
            failed += 1
            print(f"[BOT] notification de démarrage impossible ({recipient}) : {exc}")
        await asyncio.sleep(0.05)  # reste sous la limite d'envoi de Telegram
    print(f"[BOT] notification de démarrage : {sent} envoyée(s), {failed} échec(s).")


def menu_handlers():
    return [
        CallbackQueryHandler(open_user_home, pattern=r"^USER_HOME$"),
        CallbackQueryHandler(show_prices, pattern=r"^PRICES$"),
        CallbackQueryHandler(show_nearest, pattern=r"^NEAREST$"),
        CallbackQueryHandler(show_ruptures, pattern=r"^RUPTURES$"),
        CallbackQueryHandler(set_location, pattern=r"^SET_LOCATION$"),
        CallbackQueryHandler(set_fuel, pattern=r"^SET_FUEL$"),
        CallbackQueryHandler(toggle_fouine, pattern=r"^FOUINE$"),
        CallbackQueryHandler(notifications_router, pattern=r"^NOTIF:"),
        CallbackQueryHandler(save_search, pattern=r"^SAVE_SEARCH$"),
        CallbackQueryHandler(my_searches, pattern=r"^MY_SEARCHES$"),
        CallbackQueryHandler(select_search, pattern=r"^SS:\d+$"),
        CallbackQueryHandler(delete_search, pattern=r"^SD:\d+$"),
        CallbackQueryHandler(back_to_menu, pattern=r"^MENU$"),
    ]


def build_application():
    app = Application.builder().token(TOKEN).post_init(post_init).build()

    conv = ConversationHandler(
        # Les boutons du menu sont aussi des points d'entrée : ils marchent
        # même après un redémarrage du bot, sans refaire /start.
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^🏠 Accueil$"), start),
        ] + menu_handlers(),
        states={
            CHOOSE_ENTRY: [
                MessageHandler(filters.Regex("^🏠 Accueil$"), start),
                MessageHandler(filters.LOCATION, entry_location),
                MessageHandler(
                    filters.Regex("^🗺️ Choisir ma ville manuellement$"),
                    entry_manual,
                ),
            ],
            CHOOSE_REGION: [
                MessageHandler(filters.Regex("^🏠 Accueil$"), start),
                CallbackQueryHandler(region_page, pattern=r"^REGPAGE:\d+$"),
                CallbackQueryHandler(choose_region, pattern=r"^REG:"),
            ],
            CHOOSE_DEPARTMENT: [
                MessageHandler(filters.Regex("^🏠 Accueil$"), start),
                CallbackQueryHandler(department_page, pattern=r"^DEPPAGE:\d+$"),
                CallbackQueryHandler(choose_department, pattern=r"^DEP:"),
            ],
            ENTER_CITY: [
                MessageHandler(filters.Regex("^🏠 Accueil$"), start),
                CallbackQueryHandler(choose_city_callback, pattern=r"^CITY:"),
                CallbackQueryHandler(city_page, pattern=r"^CITYPAGE:\d+$"),
                CallbackQueryHandler(all_cities, pattern=r"^ALLCITIES:\d+$"),
                MessageHandler(filters.Regex("^🏠 Accueil$"), start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_city),
            ],
            CHOOSE_RADIUS: [
                MessageHandler(filters.Regex("^🏠 Accueil$"), start),
                CallbackQueryHandler(choose_radius, pattern=r"^RAD:\d+$"),
            ],
            CHOOSE_FUEL: [
                MessageHandler(filters.Regex("^🏠 Accueil$"), start),
                CallbackQueryHandler(choose_fuel, pattern=r"^FUEL:"),
            ],
            MAIN_MENU: [
                MessageHandler(filters.Regex("^🏠 Accueil$"), start),
            ] + menu_handlers(),
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    # Garde-fou : bloque les utilisateurs exclus avant tout autre traitement.
    app.add_handler(TypeHandler(Update, block_banned), group=-1)
    # Administration : indépendante de l'état de la conversation principale.
    app.add_handler(CallbackQueryHandler(adm_router, pattern=r"^ADM:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text), group=1)
    app.add_handler(conv)
    app.job_queue.run_repeating(price_check_job, interval=REFRESH_SECONDS, first=10)
    return app


if __name__ == "__main__":
    print("🇫🇷 RoulerSansPleurer France démarré.")
    build_application().run_polling(drop_pending_updates=True)
