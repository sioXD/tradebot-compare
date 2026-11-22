# Tradingbot-Vergleich: WSB vs. Truth Social vs. Armstrong Economics

Drei unabhängige Bots handeln mit **Paper Money** über Alpaca. Jeder nutzt nur seine eigene Datenquelle und hat ein eigenes Paper-Konto. Die Handelsregeln sind bei allen gleich, damit sich nur die Datenquelle unterscheidet.

| Bot | Quelle | Abruf |
|---|---|---|
| `wsb` | r/wallstreetbets: Standard ist die ApeWisdom-Ticker-Statistik (kein Account nötig), optional die offizielle Reddit-API | 15 min (stündlicher Snapshot) |
| `truth` | Trump-Posts auf Truth Social über einen RSS-Feed (Standard: trumpstruth.org) | 5 min |
| `armstrong` | armstrongeconomics.com RSS, handelt nur Makro-ETFs | 60 min |

Ein LLM (OpenCode Zen) macht aus den Posts Signale der Form `Ticker / buy|sell / Confidence`. Die Engine setzt sie nach festen Regeln um. Sie kauft nur (Long-only) und shortet nicht. Ein Sell-Signal schließt also nur eine bestehende Position. Weitere Regeln: 10 % des Equity pro Position, höchstens 8 Positionen, Stop-Loss 8 %, Take-Profit 20 % und eine maximale Haltedauer.

## Einrichtung

1. **Alpaca:** Lege im Paper-Dashboard insgesamt **3 Paper-Konten** an (Kontoauswahl oben links). Erzeuge für jedes Konto einen API-Key.
2. **Reddit (optional):** Neue API-Apps gibt es nur noch auf Antrag. Ohne Zugang nutzt der Bot automatisch ApeWisdom.
3. **OpenCode:** API-Key und Modell-ID aus https://opencode.ai/console holen.
4. `.env.example` nach `.env` kopieren und ausfüllen.
5. Verbindung testen: `docker compose run --rm dashboard python -m tradebots.check`
6. Starten: `docker compose up -d --build`
7. Dashboard öffnen: `http://<server-ip>:8501/trade/` (im LAN) oder `https://sioxd.duckdns.org/trade/` (von überall)

## Zugriff von unterwegs

Das Dashboard läuft hinter dem bereits vorhandenen nginx (DuckDNS + Let's Encrypt):

```
Internet → Router :443 → nginx → dashboard:8501   (https://sioxd.duckdns.org/trade/)
                              → api:8000           (https://sioxd.duckdns.org/api/)
```

- **Router:** nur **443** ist freigegeben. Port **8501 nicht** freigeben.
- **Login:** Die Webseite fragt nur nach einem **Passwort** (kein Benutzername).
  Das Passwort ist `API_TOKEN` aus `.env` und gilt gleichzeitig als Widget-Token.
  Die Session läuft als HttpOnly-Cookie 30 Tage; Abmelden über `/logout`.
- Login-Seite/Service: `api/auth.py` (Container `auth`), nginx-Regeln in
  `../languagetool-auth-main/nginx/tradebot.conf`.
- Nach Änderungen an der nginx-Config: `cd ../languagetool-auth-main && docker compose up -d`.

Passwort ändern (wird ausschließlich in der `.env` gesetzt):

```bash
nano .env                     # API_TOKEN selbst setzen
docker compose up -d api auth # Container neu erstellen
```

Falls Compose nicht neu erstellt: `docker compose up -d --force-recreate api auth`.
Alte Login-Sessions werden dabei automatisch ungültig.

nginx löst `tradebot-auth`, `tradebot-api` und `tradebot-dashboard` über den
Docker-DNS auf (mit 10 s Cache). Ein nginx-Reload nach dem Neuerstellen der
Container ist deshalb **nicht** nötig.

Prüfen, ob der Container das Passwort richtig gelesen hat (gibt das Passwort nicht aus):

```bash
# Login: erwartet 303
curl -s -o /dev/null -w '%{http_code}\n' \
  --data-urlencode "password=$(docker compose exec -T auth printenv API_TOKEN)" \
  https://sioxd.duckdns.org/auth

# API: erwartet 200
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $(docker compose exec -T auth printenv API_TOKEN)" \
  https://sioxd.duckdns.org/api/summary
```

### JSON-API (für Widgets/Scripts)

```bash
PASS=$(docker compose exec -T auth printenv API_TOKEN)
curl -H "Authorization: Bearer $PASS" https://sioxd.duckdns.org/api/summary
curl -H "Authorization: Bearer $PASS" "https://sioxd.duckdns.org/api/equity?bot=wsb&limit=200"
```

`/api/summary` liefert je Bot Equity, Rendite, Max Drawdown, Trades, offene Positionen,
Cash und das Alter des letzten Snapshots.

### Scriptable-Widget (iPad/iPhone)

1. Passwort holen: `docker compose exec -T auth printenv API_TOKEN`
2. In `scripts/TradebotWidget.js` bei `PASSWORD` eintragen.
3. Datei in Scriptable öffnen, Widget hinzufügen und das Script auswählen.
4. Kleine, mittlere und große Widget-Größe werden unterstützt; offline wird der letzte
   Stand aus dem Cache angezeigt.

Docker muss beim Booten automatisch starten (`sudo systemctl enable docker`). Wegen `restart: unless-stopped` laufen die Bots dann nach dem Hochfahren um 8 Uhr von selbst wieder.

## Server-Zeiten

US-Börse: **15:30–22:00 Uhr** deutscher Zeit. Der Server sollte frühestens um **22:15** ausgehen.
Posts, die nachts erscheinen, werden beim Start nachgeholt, wenn sie jünger als `MAX_ITEM_AGE_HOURS` sind. Die Signale warten dann in einer Warteschlange bis zur Börseneröffnung.

## Befehle

```bash
docker compose logs -f bot-truth     # Live-Log eines Bots
docker compose ps                    # Status
docker compose down                  # stoppen
```

> Nur Paper Trading (`paper=True` ist fest im Code). Das ist keine Anlageberatung.
