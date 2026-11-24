"""LLM-basierte Signal-Extraktion ueber eine OpenAI-kompatible API (z.B. OpenCode Zen)."""
from __future__ import annotations

import json
import logging
import re
import time

from openai import OpenAI

from . import config

log = logging.getLogger("llm")

BASE_PROMPT = """You are a disciplined trading-signal extractor for a PAPER-TRADING research experiment.
You receive CONTEXT (your current portfolio, recent market prices, your recent signals, and previous posts from this source)
plus a batch of NEW POSTS from ONE data source. Decide which US-listed stocks/ETFs (tradable on NYSE/NASDAQ/ARCA)
are likely to move because of what the NEW POSTS say, over the next few days.

Rules:
- Base new signals on the NEW POSTS; use previous posts only to spot trends, escalations, or shifts in tone (do not re-trigger on old posts alone).
- Use the current portfolio: if new posts turn bearish on a held position or invalidate its thesis, emit a "sell" signal. Avoid duplicate "buy" signals for positions already held.
- Use recent market prices to avoid chasing tickers that already rallied heavily OR blindly catching falling knives that are actively crashing without a concrete positive catalyst.
- Use only tickers that really exist on US exchanges. Use plain symbols (e.g. "AAPL", "GLD"). No options, no crypto pairs.
- "buy" = expect price to rise. "sell" = expect price to fall (only exits held positions, no direct shorting). For clearly bearish market/index/bond views, you may instead emit a "buy" on liquid 1x inverse ETFs: SH (Short S&P 500), PSQ (Short Nasdaq-100), TBF (Short 20+Y Treasuries), DOG (Short Dow).
- confidence is 0.0-1.0. Be conservative: most posts are noise; 0.65-0.71 = moderate (10% size), 0.72-0.79 = strong (15% size), >=0.80 = very clear high-conviction catalyst (20% size).
- horizon_days: realistic expected holding period in TRADING days (2-5 for short-term momentum/news, 10-25 for macro trends). The engine uses this to exit stagnant positions.
- reason: max. 25 words, in German, referencing the post(s).
- If nothing is actionable, return an empty list.
- Respond with ONLY a JSON object, no markdown: {"signals":[{"ticker":"...","action":"buy|sell","confidence":0.0,"horizon_days":5,"reason":"...","post_ids":["..."]}]}
"""

SOURCE_HINTS = {
    "wsb": """Source: Reddit r/wallstreetbets. Either raw hot/rising posts (with score/comment counts) OR an aggregated
snapshot of ticker mention counts (mentions now vs. 24h ago, upvotes, rank change).
CRITICAL WARNING FOR MENTION SNAPSHOTS: ApeWisdom counts ticker mentions WITHOUT sentiment! Crashes, panic, earnings misses,
and jokes generate huge mention spikes just like rallies do.
- NEVER emit a "buy" solely because mentions spiked on a falling stock (negative 1d/5d price action) — that is catching a falling knife! Require either positive/stabilizing price momentum OR an overwhelming, sustained breakout in both mentions and upvotes.
- IGNORE ticker collisions with common English words or Reddit/options slang (e.g. HR, NYT, DTE/0DTE, ET, YOU, AI, ON, ALL, FOR, LOW, NOW, COST, OPEN, LOVE, REAL, DD). Only signal genuine, actively traded WSB stocks/ETFs.
- Broad index ETFs (SPY, QQQ) are discussed constantly - only signal them on extreme changes.
- Ignore memes without a ticker, loss/gain porn without forward-looking view, and penny stocks under ~$2.""",
    "truth": """Source: Truth Social posts by Donald J. Trump.
Identify concrete market implications: named companies, sectors (tariffs, defense, energy, pharma, autos, chips, crypto),
policy announcements, or sharp market/oil/rate commentary. Map bullish impacts to liquid US stocks or sector ETFs (e.g. XLE, ITA, SMH, XLV)
and clearly bearish broad-market/index views to 1x inverse ETFs (SH, PSQ, TBF) when appropriate.
Ignore purely political posts without economic content.""",
    "armstrong": """Source: Martin Armstrong's blog (armstrongeconomics.com) - macro/cycle analysis.
Translate macro views (equities, gold, bonds/interest rates, dollar, euro, oil, emerging markets) into positions in the
ALLOWED UNIVERSE only. Use 1x inverse ETFs (SH, PSQ, TBF, DOG) for clearly bearish equity/bond cycle calls.
Prefer liquid ETFs with meaningful movement over ultra-flat currency proxies unless the macro catalyst is major.
Longer horizons (10-25 trading days) are typical. Only signal when the post states a directional view.""",
}


def _client() -> OpenAI:
    if not config.LLM_API_KEY or not config.LLM_MODELS:
        raise RuntimeError("LLM_API_KEY und LLM_MODEL muessen in .env gesetzt sein")
    return OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY, timeout=120, max_retries=1)


# Modell -> Zeitpunkt, bis zu dem es pausiert wird (nach Limit-/Guthaben-Fehler)
_cooldown_until: dict[str, float] = {}
_LIMIT_HINTS = ("quota", "limit", "credit", "balance", "insufficient", "exhausted", "billing", "payment")


def _is_limit_error(e: Exception) -> bool:
    status = getattr(e, "status_code", None)
    if status in (402, 403, 429):
        return True
    return any(h in str(e).lower() for h in _LIMIT_HINTS)


def _chat(messages: list[dict], parse_json: bool, **kw):
    """Probiert die Modelle der Reihe nach. Ein Modell, das an ein Limit stoesst,
    wird fuer LLM_COOLDOWN_MINUTES pausiert und danach automatisch wieder bevorzugt."""
    client = _client()
    errors = []
    now = time.time()
    candidates = [m for m in config.LLM_MODELS if _cooldown_until.get(m, 0) <= now] or config.LLM_MODELS[-1:]
    for model in candidates:
        try:
            resp = client.chat.completions.create(model=model, messages=messages, **kw)
            text = resp.choices[0].message.content or ""
            result = _parse_json(text or "{}") if parse_json else text
            if model != config.LLM_MODELS[0]:
                log.info("LLM-Fallback genutzt: %s", model)
            return result, model
        except json.JSONDecodeError as e:
            errors.append(f"{model}: ungueltiges JSON")  # Modell lieferte Murks -> naechstes probieren
            log.warning("%s lieferte kein gueltiges JSON: %s", model, e)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{model}: {e}")
            if _is_limit_error(e):
                _cooldown_until[model] = time.time() + config.LLM_COOLDOWN_MINUTES * 60
                log.warning("%s am Limit -> %d min Pause, wechsle Modell", model, config.LLM_COOLDOWN_MINUTES)
            else:
                log.warning("%s Fehler: %s", model, e)
    raise RuntimeError("Alle LLM-Modelle fehlgeschlagen: " + " | ".join(errors))


def _parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))


def analyze(bot: str, items: list[dict], universe: list[str], context: str = "") -> list[dict]:
    """Gibt validierte Signale zurueck (ohne Tradable-Check, der passiert im Broker)."""
    if not items:
        return []
    system = BASE_PROMPT + "\n" + SOURCE_HINTS[bot]
    if universe:
        system += f"\nALLOWED UNIVERSE (use ONLY these tickers): {', '.join(universe)}"

    lines = []
    for it in items:
        body = (it.get("text") or "")[:1800]
        lines.append(f"--- post_id={it['item_id']} | {it.get('created_utc','')} | {it.get('author','')}\nTITLE: {it.get('title','')}\n{body}")
    parts = []
    if context:
        parts.append("=== CONTEXT ===\n" + context)
    parts.append("=== NEW POSTS TO ANALYZE ===\n" + "\n\n".join(lines))
    user = "\n\n".join(parts)

    data, model = _chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        parse_json=True,
        temperature=0.1,
    )
    if not isinstance(data, dict):
        data = {}

    out = []
    for s in data.get("signals", []):
        try:
            ticker = str(s["ticker"]).upper().strip().lstrip("$")
            action = str(s["action"]).lower().strip()
            conf = max(0.0, min(1.0, float(s.get("confidence", 0))))
        except (KeyError, ValueError, TypeError):
            continue
        if action not in ("buy", "sell") or not re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", ticker):
            continue
        if bot == "wsb" and ticker in config.WSB_BLACKLIST:
            continue
        if universe and ticker not in universe:
            continue
        out.append(
            dict(
                ticker=ticker,
                action=action,
                confidence=conf,
                horizon_days=int(s.get("horizon_days") or 3),
                reason=str(s.get("reason", ""))[:500],
                post_ids=[str(p) for p in s.get("post_ids", [])],
            )
        )
    return out


def ping() -> str:
    """Testet JEDES konfigurierte Modell einzeln."""
    client = _client()
    out = []
    for m in config.LLM_MODELS:
        try:
            r = client.chat.completions.create(model=m, messages=[{"role": "user", "content": "Antworte nur mit: OK"}])
            out.append(f"{m}='{(r.choices[0].message.content or '').strip()}'")
        except Exception as e:  # noqa: BLE001
            out.append(f"{m}=FEHLER({str(e)[:120]})")
    return ", ".join(out)
