"""Datenquellen. Jede Quelle liefert eine Liste von Items:
{item_id, created_utc (ISO), author, title, text, url}
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone

import feedparser
import requests

from . import config

_TAG_RE = re.compile(r"<[^>]+>")
_TRUTH_MEDIA_RE = re.compile(r"^(?:Image|Video|Quote|Link|ReTruth)\s+post\s+from\s+", re.IGNORECASE)


def is_junk_item(bot: str, item: dict) -> bool:
    """Filtert offensichtlich inhaltsleere Posts (Medien-Platzhalter, Paywall-Teaser) vor dem LLM."""
    title = (item.get("title") or "").strip()
    text = (item.get("text") or "").strip()
    if bot == "truth":
        if _TRUTH_MEDIA_RE.match(title):
            body = text.replace(title, "").strip()
            if len(body) < 30:
                return True
        if not title and len(text) < 20:
            return True
    elif bot == "armstrong":
        if title.upper().startswith("PRIVATE BLOG") and len(text) < 350:
            return True
    return False


def _clean(s: str | None) -> str:
    if not s:
        return ""
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _fetch_feed(url: str) -> list[dict]:
    resp = requests.get(url, headers={"User-Agent": config.HTTP_USER_AGENT}, timeout=30)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    items = []
    for e in feed.entries:
        ts = e.get("published_parsed") or e.get("updated_parsed")
        created = datetime(*ts[:6], tzinfo=timezone.utc).isoformat() if ts else datetime.now(timezone.utc).isoformat()
        content = ""
        if e.get("content"):
            content = e.content[0].get("value", "")
        content = content or e.get("summary", "")
        items.append(
            dict(
                item_id=str(e.get("id") or e.get("link")),
                created_utc=created,
                author=e.get("author", ""),
                title=_clean(e.get("title")),
                text=_clean(content),
                url=e.get("link", ""),
            )
        )
    return items


def fetch_truth() -> list[dict]:
    return _fetch_feed(config.TRUTH_FEED_URL)


def fetch_armstrong() -> list[dict]:
    return _fetch_feed(config.ARMSTRONG_FEED_URL)


def fetch_wsb() -> list[dict]:
    """Offizielle Reddit-API, falls Zugangsdaten vorhanden - sonst ApeWisdom (oeffentliche WSB-Statistik)."""
    if config.REDDIT_CLIENT_ID:
        return fetch_wsb_reddit()
    return fetch_wsb_apewisdom()


def fetch_wsb_apewisdom() -> list[dict]:
    """ApeWisdom zaehlt Ticker-Erwaehnungen auf r/wallstreetbets (letzte 24h).
    Pro Stunde ein Snapshot-Item -> LLM wertet die Veraenderungen aus."""
    url = "https://apewisdom.io/api/v1.0/filter/wallstreetbets/page/1"
    resp = requests.get(url, headers={"User-Agent": config.HTTP_USER_AGENT}, timeout=30)
    resp.raise_for_status()
    raw_rows = resp.json().get("results", [])
    rows = [r for r in raw_rows if str(r.get("ticker") or "").upper() not in config.WSB_BLACKLIST][:40]
    now = datetime.now(timezone.utc)
    lines = []
    for r in rows:
        m, m24 = int(r.get("mentions") or 0), int(r.get("mentions_24h_ago") or 0)
        chg = f"{(m / m24 - 1) * 100:+.0f}%" if m24 else "neu"
        lines.append(
            f"#{r.get('rank')} {r.get('ticker')} ({_clean(r.get('name'))}): mentions={m} (24h ago {m24}, {chg}), "
            f"upvotes={r.get('upvotes')}, rank 24h ago={r.get('rank_24h_ago')}"
        )
    return [
        dict(
            item_id=f"apewisdom-{now:%Y%m%d%H}",
            created_utc=now.isoformat(),
            author="apewisdom.io",
            title=f"WSB Ticker-Erwaehnungen Top {len(rows)} (Snapshot {now:%Y-%m-%d %H:00} UTC)",
            text="\n".join(lines),
            url="https://apewisdom.io/wallstreetbets/",
        )
    ]


def fetch_wsb_reddit() -> list[dict]:
    import praw  # lazy import

    reddit = praw.Reddit(
        client_id=config.REDDIT_CLIENT_ID,
        client_secret=config.REDDIT_CLIENT_SECRET,
        user_agent=config.REDDIT_USER_AGENT,
        check_for_async=False,
    )
    sub = reddit.subreddit("wallstreetbets")
    seen, items = set(), []
    for listing in (sub.hot(limit=60), sub.rising(limit=25)):
        for p in listing:
            if p.id in seen or p.stickied:
                continue
            seen.add(p.id)
            items.append(
                dict(
                    item_id=p.id,
                    created_utc=datetime.fromtimestamp(p.created_utc, tz=timezone.utc).isoformat(),
                    author=str(p.author) if p.author else "",
                    title=p.title,
                    text=f"[score={p.score}, comments={p.num_comments}, flair={p.link_flair_text}] {(p.selftext or '')[:1500]}",
                    url=f"https://reddit.com{p.permalink}",
                )
            )
    return items


SOURCES = {"wsb": fetch_wsb, "truth": fetch_truth, "armstrong": fetch_armstrong}
