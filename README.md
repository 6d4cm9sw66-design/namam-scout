# NAMAM Talent Scout

Zoekt opkomende Nederlandse pop-acts via de Spotify Web API en zet ze in een
lijst (scout.html) die je op je telefoon kunt inzien. Nieuwe namen staan bovenaan.

## Wat je nodig hebt
- Een computer of klein servertje met internet (dit draait niet op je telefoon).
- Python 3.10 of hoger.
- Je Spotify Client ID en Client Secret (uit developer.spotify.com/dashboard).

## Eenmalig instellen (5 minuten)
1. Zet deze map ergens neer op de computer.
2. Open een terminal in deze map en installeer de dependencies:

       pip install -r requirements.txt

   Geeft dat een "externally-managed-environment" melding, gebruik dan een venv:

       python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt

   (op Windows: `.venv\Scripts\activate` in plaats van `source ...`)

3. Kopieer `.env.example` naar `.env` en zet je Client Secret erin.
   De Client ID staat er al voor NAMAM Scout. Voorbeeld van `.env`:

       SPOTIFY_CLIENT_ID=5aca4122017743938547811ab6c952f3
       SPOTIFY_CLIENT_SECRET=jouw-secret-hier

## Draaien
Even kijken hoe de lijst eruitziet, zonder API:

    python scout.py --demo

De echte run tegen Spotify:

    python scout.py

Daarna open je `scout.html`. De resultaten komen ook in `watchlist.json`.

## Dagelijks automatisch
Zet de echte run in een dagelijkse taak:
- Mac/Linux: een cron-regel, bijvoorbeeld elke ochtend 07:00.
- Windows: Taakplanner.
- Of op een klein always-on servertje.

Vraag Nathan/Claude om de exacte cron-regel of setup als je zover bent.

## Belangrijk om te weten
- Monthly listeners staan NIET in de Spotify API. De scout filtert op VOLGERS.
  Volgers liggen lager dan ML, dus de range 1.000-75.000 volgers overlapt ruwweg
  met het ML-richtgetal van 5.000-50.000 en pakt bewust ook iets vroegere acts.
- De editorial-playlists (Viral 50, Top 50, New Music Friday) zijn sinds eind 2024
  niet meer via de API te lezen voor nieuwe apps. De scout gebruikt daarom
  catalogus-search op recente NL-releases. Dat vindt juist de kleine, nog
  onzichtbare acts die op die lijsten toch nooit stonden.
- Het label per release is de eerste indicatie voor de master (P-regel). Publishing
  (BUMA/Stemra) en management/booking staan niet in de API: die check je handmatig
  per naam. De scout zet dat er als reminder bij.
- Alle filtercriteria (follower-range, populariteit, genres, skip-labels, zoektermen)
  staan bovenin `scout.py` onder CONFIG, op een centrale plek om te tweaken.

## Status bijhouden
Elke naam krijgt in `watchlist.json` een `status`: standaard `new`. Zet die zelf om
naar `reviewing`, `contacted`, `signed` of `passed`. Bij een volgende run blijft die
status staan; nieuwe namen komen erbij en krijgen een NIEUW-label.
