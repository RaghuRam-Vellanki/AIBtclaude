"""
modules/news_feed.py
Lightweight RSS scraper for Indian market news.
Stdlib-only (urllib + xml.etree), no feedparser dep.
5-minute disk cache so we don't hammer the upstream feeds.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

logger = logging.getLogger("news_feed")

# (label, RSS URL, asset relevance: any|nifty|crypto|gold)
NIFTY_FEEDS: list[tuple[str, str]] = [
    ("Moneycontrol – Markets",
     "https://www.moneycontrol.com/rss/marketreports.xml"),
    ("Moneycontrol – Business",
     "https://www.moneycontrol.com/rss/business.xml"),
    ("ET Markets",
     "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Livemint – Markets",
     "https://www.livemint.com/rss/markets"),
    ("Reuters India – Business",
     "https://news.google.com/rss/search?q=NIFTY+OR+BSE+OR+NSE+when:1d&hl=en-IN&gl=IN&ceid=IN:en"),
]

XAU_FEEDS: list[tuple[str, str]] = [
    ("Kitco News",
     "https://www.kitco.com/rss/KitcoNews.xml"),
    ("Investing.com Gold",
     "https://news.google.com/rss/search?q=gold+price+OR+XAUUSD+when:1d&hl=en-US&gl=US&ceid=US:en"),
]

BTC_FEEDS: list[tuple[str, str]] = [
    ("CoinDesk",
     "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml"),
    ("Google News BTC",
     "https://news.google.com/rss/search?q=bitcoin+OR+BTC+when:1d&hl=en-US&gl=US&ceid=US:en"),
]


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _parse_pubdate(s: str | None) -> str | None:
    """Try multiple RSS date formats. Return ISO-8601 string or None."""
    if not s:
        return None
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s.strip(), fmt).astimezone(timezone.utc).isoformat()
        except Exception:
            continue
    return None


def _fetch_feed(url: str, timeout: int = 6) -> list[dict[str, Any]]:
    """Fetch RSS/Atom and return a list of {title, link, source, published, summary}."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36"
                ),
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except Exception as exc:
        logger.debug("feed fetch failed for %s: %s", url, exc)
        return []

    try:
        root = ET.fromstring(data)
    except Exception as exc:
        logger.debug("feed parse failed for %s: %s", url, exc)
        return []

    items: list[dict[str, Any]] = []
    # RSS 2.0
    for item in root.iter("item"):
        title = _strip_html((item.findtext("title") or "")[:300])
        link = (item.findtext("link") or "").strip()
        desc = _strip_html((item.findtext("description") or ""))[:280]
        pub = _parse_pubdate(item.findtext("pubDate"))
        if title and link:
            items.append({
                "title": title,
                "link": link,
                "summary": desc,
                "published": pub,
            })

    # Atom
    if not items:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//a:entry", ns):
            title = _strip_html((entry.findtext("a:title", default="", namespaces=ns) or "")[:300])
            link_el = entry.find("a:link", ns)
            link = link_el.attrib.get("href", "") if link_el is not None else ""
            summary = _strip_html(
                entry.findtext("a:summary", default="", namespaces=ns) or
                entry.findtext("a:content", default="", namespaces=ns) or ""
            )[:280]
            pub = _parse_pubdate(entry.findtext("a:updated", namespaces=ns) or
                                 entry.findtext("a:published", namespaces=ns))
            if title and link:
                items.append({
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "published": pub,
                })

    return items[:25]


def _feeds_for(asset: str) -> list[tuple[str, str]]:
    a = (asset or "").lower()
    if a == "nifty":
        return NIFTY_FEEDS
    if a == "xau":
        return XAU_FEEDS
    return BTC_FEEDS


_CACHE_DIR = Path(__file__).resolve().parent.parent / "logs"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(asset: str) -> Path:
    return _CACHE_DIR / f".news_cache_{asset}.json"


def get_headlines(
    asset: str = "nifty",
    limit: int = 20,
    ttl_sec: int = 300,
) -> dict[str, Any]:
    """Return {fetched_at, items: [...]} for the asset. Cached for `ttl_sec`."""
    asset = (asset or "nifty").lower()
    cache_file = _cache_path(asset)

    # Try cache
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            age = time.time() - cached.get("ts", 0)
            if age < ttl_sec and cached.get("items"):
                return {
                    "fetched_at": cached.get("fetched_at"),
                    "age_sec": int(age),
                    "items": cached["items"][:limit],
                    "cached": True,
                }
        except Exception:
            pass

    items: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for source, url in _feeds_for(asset):
        feed_items = _fetch_feed(url)
        for it in feed_items:
            t = it["title"].lower().strip()
            if t in seen_titles:
                continue
            seen_titles.add(t)
            it["source"] = source
            items.append(it)

    # Sort newest first when timestamps available
    def _sort_key(it: dict[str, Any]) -> str:
        return it.get("published") or ""
    items.sort(key=_sort_key, reverse=True)

    payload = {
        "ts": time.time(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "items": items[: max(limit, 30)],
    }
    try:
        cache_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("news cache write failed: %s", exc)

    return {
        "fetched_at": payload["fetched_at"],
        "age_sec": 0,
        "items": payload["items"][:limit],
        "cached": False,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    out = get_headlines("nifty", limit=10, ttl_sec=0)
    print(f"fetched_at={out['fetched_at']}  count={len(out['items'])}")
    for it in out["items"]:
        print(f"  • [{it['source']}] {it['title'][:90]}")
