#!/usr/bin/env python3
"""
NAMAM Talent Scout - v1
=======================

Zoekt dagelijks opkomende Nederlandse pop-acts via de Spotify Web API en zet ze
in een browseable lijst die je 's ochtends op je telefoon kunt inzien.

Waarom deze aanpak (belangrijk om te weten):
- Sinds eind 2024 geeft Spotify de eigen editorial-playlists (Viral 50, Top 50,
  New Music Friday) NIET meer vrij via de API voor nieuwe apps: die geven een 404.
  We kunnen die lijsten dus niet direct uitlezen. Dat is geen verlies, want die
  vonden toch vooral acts die al te groot of getekend waren.
- Wat WEL werkt en wat deze scout gebruikt: catalogus-search op recente NL-releases,
  plus per artiest de cijfers (volgers, populariteit, genres), de discografie
  (aantal releases) en de labelregel per release voor de skip-check. Precies de
  velden die de kleine, nog onzichtbare acts oppakken.
- Monthly listeners (ML) staan NIET in de API. De scout filtert daarom op VOLGERS.
  Volgers liggen doorgaans lager dan ML, dus de follower-range hieronder (1K-75K)
  overlapt ruwweg met jouw ML-richtgetal van 5K-50K en pakt bewust ook nog iets
  vroegere acts mee.

Gebruik:
    python scout.py            # echte run tegen Spotify (heeft internet + creds nodig)
    python scout.py --demo     # demo zonder API, genereert scout.html met voorbeelddata
    python scout.py --max 400  # begrens hoeveel artiesten er per run worden verwerkt

Credentials: zet SPOTIFY_CLIENT_ID en SPOTIFY_CLIENT_SECRET in een .env bestand
(zie .env.example). Nooit hardcoden, dan kun je de secret ook makkelijk roteren.
"""

import argparse
import base64
import datetime as dt
import html
import json
import os
import sys
import time
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass  # dotenv is optioneel; env-vars mogen ook direct gezet zijn


# ======================================================================
# CONFIG - alles wat je wilt tweaken staat hier, op een centrale plek.
# ======================================================================

# --- Filtercriteria (defaults uit de skill) ---
FOLLOWERS_MIN = 1_000        # onder deze grens: te klein / niet te beoordelen
FOLLOWERS_MAX = 75_000       # boven deze grens: te groot / waarschijnlijk al te ver
POPULARITY_MIN = 20          # Spotify popularity score, filtert dode profielen.
                             #   Zet op ~10 als je nog vroegere/verse debuten wilt zien;
                             #   20 is strenger maar mist soms een heel vers debuut.
RELEASE_COUNT_MAX = 20       # proxy voor vroege carriere

# Genres: minstens een hiervan moet in de artiest-genres zitten.
# LET OP: heel kleine/nieuwe artiesten hebben vaak NOG GEEN genres bij Spotify.
# Die worden daarom niet automatisch weggegooid, maar meegenomen met een
# "genre onbekend" vlag als ze via een pop-zoekterm binnenkwamen (zie SOFT_GENRE).
GENRE_ALLOW = [
    "pop", "nederpop", "dutch pop", "hollandse", "levenslied",
    "volks", "carnaval", "dutch indie", "indie pop", "singer-songwriter",
]
SOFT_GENRE = True            # True = artiesten zonder genres niet blind weggooien

# Skip als het label van de nieuwste release een van deze bevat (major / grote indie).
SKIP_LABELS = [
    "atlantic", "warner records", "warner music", "columbia", "rca",
    "interscope", "republic", "def jam", "epic", "universal", "sony music",
    "polydor", "capitol", "island", "spinnin", "cloud 9", "cloud9",
    "8ball", "top notch", "noah's ark", "bearsuit",
]

# Hiphop/rap/dance/volkszang willen we NIET als hoofdgenre. Zit een van deze
# termen in de genres, dan valt de artiest af (tenzij er ook een duidelijk
# pop-genre in zit).
GENRE_BLOCK = [
    "hip hop", "hip-hop", "rap", "drill", "trap",
    "house", "techno", "edm", "hardstyle", "gabber", "dnb", "drum and bass",
    "schlager", "carnaval",  # carnaval staat bewust ook in ALLOW: zet 'm hier weg
                             # als je wel carnaval wilt meenemen.
]

# --- Zoektermen voor discovery ---
# Track-searches met year:/tag:new vangen VERSE releases; artist-searches vangen
# breder. Genre-tags zijn benaderend; Spotify kent niet elk label even netjes toe.
CURRENT_YEAR = dt.date.today().year
YEARS = f"{CURRENT_YEAR-1}-{CURRENT_YEAR}"

# PRIMAIRE discovery: platte Nederlandstalige seed-woorden ZONDER field-filters.
# Deze kunnen niet 400'en (Spotify's search field-filters zoals year:/genre:/
# tag:new geven op de huidige API vaak HTTP 400). Met market=NL en Nederlandse
# woorden skewen de resultaten sterk naar NL-acts. Vroege-carriere-filtering
# gebeurt daarna client-side (followers-band + release_count<=20 + genre_ok).
KEYWORD_QUERIES = [
    "liefde", "alleen", "zonder jou", "blijf bij mij", "thuis",
    "voor altijd", "jij en ik", "hart", "verliefd", "mijn liefde",
    "samen", "nooit meer", "vanavond", "droom", "gevoel",
    "dansen", "hou van jou", "voor jou", "mooie dag", "terug",
]
# BONUS discovery: field-filtered track-searches. Als Spotify hier 400 op geeft
# vangt get() dat af (None -> overslaan); de KEYWORD_QUERIES leveren dan alsnog.
FILTERED_TRACK_QUERIES = [
    f'genre:"dutch pop" year:{YEARS}',
    f'genre:"nederpop" year:{YEARS}',
    f'genre:"dutch indie" year:{YEARS}',
]
# BONUS discovery: genre artist-searches (ook best-effort; 400 -> overslaan).
ARTIST_QUERIES = [
    'genre:"nederpop"', 'genre:"dutch pop"', 'genre:"dutch indie"',
]

MARKET = "NL"
MAX_ARTISTS_PER_RUN_DEFAULT = 600   # veiligheidsplafond op API-verkeer per run

# --- Bestanden ---
BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "watchlist.json"
HTML_PATH = BASE / "scout.html"

TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"


# ======================================================================
# SPOTIFY API LAAG
# ======================================================================

class Spotify:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = None
        self._expires_at = 0
        self.session = requests.Session()

    def _get_token(self):
        auth = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        r = self.session.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"Inloggen bij Spotify mislukt ({r.status_code}). "
                f"Check je Client ID en Secret. Antwoord: {r.text[:200]}"
            )
        data = r.json()
        self._token = data["access_token"]
        self._expires_at = time.time() + data.get("expires_in", 3600) - 60

    def _headers(self):
        if not self._token or time.time() >= self._expires_at:
            self._get_token()
        return {"Authorization": f"Bearer {self._token}"}

    def get(self, path, params=None, _retry=0):
        url = path if path.startswith("http") else f"{API}{path}"
        r = self.session.get(url, headers=self._headers(), params=params, timeout=30)

        if r.status_code == 429:  # rate limited
            wait = int(r.headers.get("Retry-After", "2")) + 1
            time.sleep(wait)
            return self.get(path, params, _retry)
        if r.status_code == 401 and _retry < 1:  # token verlopen
            self._token = None
            return self.get(path, params, _retry + 1)
        if r.status_code == 404:
            return None  # bv. editorial playlist (verwacht) of verwijderd item
        if r.status_code >= 400:
            # niet fataal: log en ga door, zodat een run niet op 1 item klapt
            print(f"  ! {r.status_code} op {url} params={params} -> {r.text[:300]}", file=sys.stderr)
            return None
        return r.json()


# ======================================================================
# DISCOVERY + FILTERING
# ======================================================================

def discover_artist_ids(sp, max_artists):
    """Verzamel kandidaat-artiest-IDs via search. Editorial playlists gebruiken
    we bewust niet (die geven 404 voor nieuwe apps)."""
    ids = {}
    full_objs = {}  # volledige artiest-objecten uit /search (voor filtering)

    # DIAGNOSE: kaalste mogelijke search-call (alleen q + type, geen limit).
    probe = sp.get("/search", {"q": "test", "type": "track"})
    n_probe = len(((probe or {}).get("tracks", {}) or {}).get("items", []))
    print(f"  [probe] kale search q=test type=track -> {n_probe} tracks", file=sys.stderr)

    def add_from_tracks(query):
        offset = 0
        while offset < 100 and len(ids) < max_artists:  # 5 paginas van 20
            data = sp.get("/search", {
                "q": query, "type": "track", "offset": offset,
            })
            items = (data or {}).get("tracks", {}).get("items", [])
            if not items:
                break
            for tr in items:
                for a in tr.get("artists", []):
                    ids.setdefault(a["id"], query)
            offset += 20
            time.sleep(0.1)

    def add_from_artists(query):
        offset = 0
        while offset < 100 and len(ids) < max_artists:
            data = sp.get("/search", {
                "q": query, "type": "artist", "offset": offset,
            })
            items = (data or {}).get("artists", {}).get("items", [])
            if not items:
                break
            for a in items:
                ids.setdefault(a["id"], query)
                full_objs[a["id"]] = a  # /search geeft volledig profiel terug
            offset += 20
            time.sleep(0.1)

    # Artist-search eerst: die geeft een volledig profiel (followers/genres/
    # popularity) terug, zodat we niet afhankelijk zijn van de batch-endpoint
    # /v1/artists (die op deze app 403 Forbidden geeft).
    for q in ARTIST_QUERIES:
        if len(ids) >= max_artists:
            break
        add_from_artists(q)
    for q in KEYWORD_QUERIES + FILTERED_TRACK_QUERIES:
        if len(ids) >= max_artists:
            break
        add_from_tracks(q)

    print(f"  {len(ids)} unieke kandidaat-artiesten verzameld via search")
    print(f"  {len(full_objs)} met volledig profiel (via artist-search)")
    return ids, full_objs


def genre_ok(genres):
    """Return (keep, reason). Soft: lege genres worden niet blind geweigerd."""
    g = [x.lower() for x in genres]
    if any(any(b in x for b in GENRE_BLOCK) for x in g) and not \
       any(any(a == x or a in x for a in ["pop", "nederpop", "dutch pop", "indie pop"]) for x in g):
        return False, "geblokkeerd genre (hiphop/dance/volkszang)"
    if not g:
        return (SOFT_GENRE, "genre onbekend bij Spotify - zelf checken")
    if any(any(a in x for a in GENRE_ALLOW) for x in g):
        return True, "genre-match"
    return False, "genre past niet"


def latest_label(sp, artist_id):
    """Label van de nieuwste release, als proxy voor de master (P-regel)."""
    albums = sp.get(f"/artists/{artist_id}/albums", {
        "include_groups": "single,album", "market": MARKET, "limit": 50,
    })
    items = (albums or {}).get("items", [])
    release_count = len(items)
    if not items:
        return release_count, None, None
    # sorteer op release_date, nieuwste eerst
    items.sort(key=lambda a: a.get("release_date", ""), reverse=True)
    newest = items[0]
    alb = sp.get(f"/albums/{newest['id']}")
    label = (alb or {}).get("label")
    return release_count, label, newest.get("release_date")


def label_is_major(label):
    if not label:
        return False
    low = label.lower()
    return any(s in low for s in SKIP_LABELS)


def enrich_and_filter(sp, id_map, full_objs, max_artists):
    """Filter kandidaten en check het label. Return matches.

    We gebruiken de volledige artiest-objecten die /search (type=artist) al
    teruggeeft (followers, popularity, genres). De batch-endpoint /v1/artists
    geeft 403 Forbidden op deze app, dus die vermijden we volledig. Kandidaten
    die alleen via track-search zijn gevonden hebben geen volledig profiel en
    slaan we over."""
    all_ids = list(id_map.keys())[:max_artists]
    matches = []
    skipped_no_profile = 0

    for aid in all_ids:
        a = full_objs.get(aid)
        if not a:
            skipped_no_profile += 1
            continue
        followers = a.get("followers", {}).get("total", 0)
        popularity = a.get("popularity", 0)
        genres = a.get("genres", [])

        if not (FOLLOWERS_MIN <= followers <= FOLLOWERS_MAX):
            continue
        if popularity < POPULARITY_MIN:
            continue
        keep, greason = genre_ok(genres)
        if not keep:
            continue

        release_count, label, last_date = latest_label(sp, a["id"])
        if release_count > RELEASE_COUNT_MAX:
            continue
        if label_is_major(label):
            continue  # master zit bij een major / grote indie

        matches.append({
            "id": a["id"],
            "name": a["name"],
            "spotify_url": a["external_urls"]["spotify"],
            "followers": followers,
            "popularity": popularity,
            "genres": genres,
            "genre_note": greason,
            "release_count": release_count,
            "latest_label": label or "onbekend",
            "latest_release": last_date,
            "found_via": id_map[a["id"]],
        })
        time.sleep(0.05)

    if skipped_no_profile:
        print(f"  {skipped_no_profile} overgeslagen (alleen via track-search, geen profiel)")
    print(f"  {len(matches)} artiesten door de filters")
    return matches


# ======================================================================
# DATABASE (dedupe, status behouden, nieuw markeren)
# ======================================================================

def load_db():
    if DB_PATH.exists():
        return json.loads(DB_PATH.read_text(encoding="utf-8"))
    return {}


def save_db(db):
    DB_PATH.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")


def merge(db, matches):
    """Voeg nieuwe matches toe. Bestaande status NOOIT overschrijven."""
    today = dt.date.today().isoformat()
    new_count = 0
    for m in matches:
        aid = m["id"]
        if aid in db:
            db[aid]["last_seen"] = today
            db[aid]["times_seen"] = db[aid].get("times_seen", 1) + 1
            db[aid]["is_new"] = False
            # cijfers verversen, status met rust laten
            for k in ("followers", "popularity", "release_count",
                      "latest_label", "latest_release", "genres", "genre_note"):
                db[aid][k] = m[k]
        else:
            m.update({
                "status": "new",       # jij zet dit handmatig om: reviewing/contacted/signed/passed
                "detected_on": today,
                "last_seen": today,
                "times_seen": 1,
                "is_new": True,
            })
            db[aid] = m
            new_count += 1
    print(f"  {new_count} nieuwe naam/namen sinds vorige run")
    return db


# ======================================================================
# HTML MORNING VIEW (mobielvriendelijk, read-only)
# ======================================================================

def render_html(db):
    rows = list(db.values())
    # nieuw eerst, daarna kleinste follower-aantal eerst (= vroegste fase)
    rows.sort(key=lambda r: (not r.get("is_new"), r.get("followers", 0)))

    run = dt.datetime.now().strftime("%d-%m-%Y %H:%M")
    total = len(rows)
    new_total = sum(1 for r in rows if r.get("is_new"))

    def esc(x):
        return html.escape(str(x)) if x is not None else ""

    cards = []
    for r in rows:
        badge = '<span class="new">NIEUW</span>' if r.get("is_new") else ""
        genres = ", ".join(r.get("genres", [])) or "geen genre-tag"
        status = esc(r.get("status", "new"))
        cards.append(f"""
        <div class="card status-{status}">
          <div class="top">
            <a href="{esc(r['spotify_url'])}" target="_blank" class="name">{esc(r['name'])}</a>
            {badge}
          </div>
          <div class="meta">
            ~{r.get('followers', 0):,} volgers &middot; populariteit {esc(r.get('popularity'))} &middot;
            {esc(r.get('release_count'))} releases
          </div>
          <div class="row"><span class="k">genre:</span> {esc(genres)} ({esc(r.get('genre_note'))})</div>
          <div class="row"><span class="k">master (label nieuwste release):</span> {esc(r.get('latest_label'))} &mdash; P-regel zelf aftikken in de Spotify-desktopapp.</div>
          <div class="row"><span class="k">publishing:</span> niet in de API &mdash; zelf checken via BUMA/Stemra.</div>
          <div class="row"><span class="k">management / booking:</span> niet in de API &mdash; handmatig checken (socials, bio, boekingssites).</div>
          <div class="row muted">gevonden via: {esc(r.get('found_via'))} &middot; eerste detectie {esc(r.get('detected_on'))} &middot; {esc(r.get('times_seen'))}x gezien &middot; status: {status}</div>
        </div>""")

    body = "\n".join(cards) if cards else "<p>Nog geen matches. Draai de scout eerst.</p>"

    return f"""<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NAMAM Scout</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0; background: #0f1115; color: #e7e9ee; }}
  header {{ position: sticky; top: 0; background: #171a21; padding: 16px 18px; border-bottom: 1px solid #2a2f3a; }}
  header h1 {{ font-size: 18px; margin: 0 0 4px; }}
  header .sub {{ font-size: 13px; color: #9aa3b2; }}
  .legend {{ font-size: 12px; color: #8b93a3; padding: 10px 18px; border-bottom: 1px solid #2a2f3a; }}
  .wrap {{ padding: 14px; max-width: 720px; margin: 0 auto; }}
  .card {{ background: #171a21; border: 1px solid #2a2f3a; border-radius: 12px; padding: 14px; margin-bottom: 12px; }}
  .card.status-passed {{ opacity: .5; }}
  .card.status-signed {{ border-color: #2f7d4f; }}
  .card.status-contacted {{ border-color: #b8862b; }}
  .top {{ display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }}
  .name {{ font-size: 17px; font-weight: 600; color: #7ab8ff; text-decoration: none; }}
  .new {{ font-size: 10px; font-weight: 700; letter-spacing: .5px; background: #2f7d4f; color: #fff; padding: 2px 6px; border-radius: 6px; }}
  .meta {{ font-size: 13px; color: #c7cdd8; margin-bottom: 8px; }}
  .row {{ font-size: 13px; margin: 4px 0; color: #cfd5e0; line-height: 1.35; }}
  .row .k {{ color: #8b93a3; }}
  .row.muted {{ color: #7d8698; font-size: 12px; margin-top: 8px; }}
</style>
</head>
<body>
  <header>
    <h1>NAMAM Scout</h1>
    <div class="sub">Run {run} &middot; {total} in de lijst &middot; {new_total} nieuw &middot; gesorteerd nieuw eerst, dan kleinste eerst.</div>
  </header>
  <div class="legend">
    Andere-partij-status (label op master is de eerste indicatie hieronder). Publishing en management/booking staan niet in de API en zet je er handmatig bij.
    Status pas je aan in watchlist.json (new / reviewing / contacted / signed / passed); die blijft bij de volgende run staan.
  </div>
  <div class="wrap">
    {body}
  </div>
</body>
</html>"""


# ======================================================================
# DEMO DATA (voor --demo, zonder API)
# ======================================================================

DEMO_MATCHES = [
    {
        "id": "demo1", "name": "Voorbeeld Artiest A",
        "spotify_url": "https://open.spotify.com/artist/xxxxxxxxxxxx",
        "followers": 8200, "popularity": 34, "genres": ["nederpop", "dutch pop"],
        "genre_note": "genre-match", "release_count": 3,
        "latest_label": "onbekend (waarschijnlijk DIY/distributeur)",
        "latest_release": "2026-05-30", "found_via": 'genre:"nederpop" year:2025-2026',
    },
    {
        "id": "demo2", "name": "Voorbeeld Artiest B",
        "spotify_url": "https://open.spotify.com/artist/yyyyyyyyyyyy",
        "followers": 3100, "popularity": 22, "genres": [],
        "genre_note": "genre onbekend bij Spotify - zelf checken", "release_count": 2,
        "latest_label": "onbekend", "latest_release": "2026-06-14",
        "found_via": "liefde year:2025-2026",
    },
]


# ======================================================================
# MAIN
# ======================================================================

def main():
    ap = argparse.ArgumentParser(description="NAMAM Talent Scout")
    ap.add_argument("--demo", action="store_true",
                    help="genereer scout.html met voorbeelddata, zonder API")
    ap.add_argument("--max", type=int, default=MAX_ARTISTS_PER_RUN_DEFAULT,
                    help="max aantal artiesten per run")
    args = ap.parse_args()

    db = load_db()

    if args.demo:
        print("Demo-modus: geen API, voorbeelddata.")
        db = merge(db, DEMO_MATCHES)
    else:
        cid = os.getenv("SPOTIFY_CLIENT_ID")
        secret = os.getenv("SPOTIFY_CLIENT_SECRET")
        if not cid or not secret:
            print("FOUT: zet SPOTIFY_CLIENT_ID en SPOTIFY_CLIENT_SECRET in .env "
                  "(zie .env.example).", file=sys.stderr)
            sys.exit(1)
        sp = Spotify(cid, secret)
        print("Ingelogd bij Spotify. Discovery...")
        id_map, full_objs = discover_artist_ids(sp, args.max)
        print("Filteren en labels checken...")
        matches = enrich_and_filter(sp, id_map, full_objs, args.max)
        db = merge(db, matches)

    save_db(db)
    HTML_PATH.write_text(render_html(db), encoding="utf-8")
    print(f"\nKlaar. Open {HTML_PATH.name} op je telefoon of computer.")
    print(f"Database: {DB_PATH.name} ({len(db)} namen).")


if __name__ == "__main__":
    main()
