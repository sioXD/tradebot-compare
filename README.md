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
7. Dashboard öffnen: `http://<server-ip>:8501`

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
