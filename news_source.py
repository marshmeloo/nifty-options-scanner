"""
Lightweight news tracking: pulls recent headlines from a few free,
public RSS feeds and tags them against keyword categories that
historically move the NIFTY (RBI policy, Fed/FOMC, Union Budget,
geopolitical shocks, crude oil, inflation prints, SEBI/regulatory action).

This is NOT sentiment analysis or an LLM read of the news -- it's
deliberately simple keyword tagging, the same "spreadsheet of what
matters" philosophy as trade_tracker.py's tag-adjustment loop. A
headline matching "RBI" + "repo rate" doesn't tell you which way the
market will move, just that today is a day where volatility risk is
elevated and position sizing / conviction bar should probably be more
conservative -- see config.NEWS_RISK_BLOCKS_NEW_TRADES and
risk_checker.py for how that gets used.

Honesty note on the feed URLs: these are public RSS feeds from Indian
financial news publishers, commonly available without a key. Feed
paths/structures are controlled entirely by the publisher and can change
without notice -- if a feed starts coming back empty, check this list
first rather than assuming your keyword categories are wrong. Each feed
is fetched independently and a dead one is skipped, not fatal.
"""

import re
from datetime import datetime, timedelta

import requests
import xml.etree.ElementTree as ET

import config as cfg

FEEDS = {
    "Economic Times Markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "Moneycontrol Business": "https://www.moneycontrol.com/rss/business.xml",
    "Business Standard Markets": "https://www.business-standard.com/rss/markets-106.rss",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# category -> (risk weight, keywords). Keywords are matched case-insensitively
# as whole words/phrases against headline + summary text. Weights are
# somewhat arbitrary or added up against NEWS_RISK_ELEVATED_THRESHOLD --
# tune both to taste, there's no "correct" calibration for this.
EVENT_CATEGORIES = {
    "rbi_monetary_policy": (3, ["RBI", "repo rate", "monetary policy committee", "MPC meeting", "reverse repo"]),
    "global_central_bank": (3, ["Federal Reserve", "FOMC", "Fed rate", "Fed chair", "rate hike", "rate cut"]),
    "union_budget": (3, ["union budget", "budget session", "fiscal deficit", "finance minister budget"]),
    "geopolitical": (3, ["war", "ceasefire", "sanctions", "missile", "border tension", "military strike", "conflict escalates"]),
    "crude_oil_shock": (2, ["crude oil surge", "crude oil plunge", "OPEC", "oil prices spike", "Brent crude"]),
    "inflation_growth_data": (2, ["CPI inflation", "WPI inflation", "GDP growth", "IIP data", "PMI data"]),
    "regulatory_action": (2, ["SEBI", "circuit breaker", "F&O ban", "margin requirement", "trading halt"]),
    "elections": (2, ["assembly election", "election results", "exit poll", "general election"]),
}


def _fetch_feed(name: str, url: str) -> list:
    """
    Fetch and parse one RSS feed. Returns a list of {"title", "summary",
    "link", "source"} dicts. Raises on failure -- caller catches per-feed.
    """
    resp = requests.get(url, headers=_HEADERS, timeout=getattr(cfg, "NEWS_REQUEST_TIMEOUT", 10))
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        summary = (item.findtext("description") or "").strip()
        summary = re.sub("<[^<]+?>", "", summary)  # strip any embedded HTML tags
        link = (item.findtext("link") or "").strip()
        if title:
            items.append({"title": title, "summary": summary, "link": link, "source": name})
    return items


def get_headlines() -> list:
    """
    Fetch headlines from every configured feed. A feed that fails is
    skipped silently (logged by the caller if it wants) -- one dead feed
    shouldn't blank out news coverage from the others.
    """
    headlines = []
    for name, url in FEEDS.items():
        try:
            headlines.extend(_fetch_feed(name, url))
        except Exception:
            continue
    return headlines


def _matches(text: str, keyword: str) -> bool:
    return re.search(r"\b" + re.escape(keyword) + r"\b", text, re.IGNORECASE) is not None


def tag_headlines(headlines: list) -> list:
    """
    Tag each headline with any matching event categories. Returns only
    headlines that matched at least one category -- most day-to-day
    market chatter won't match anything, which is the point.
    """
    tagged = []
    for h in headlines:
        text = f"{h['title']} {h['summary']}"
        matched_categories = []
        for category, (weight, keywords) in EVENT_CATEGORIES.items():
            if any(_matches(text, kw) for kw in keywords):
                matched_categories.append(category)
        if matched_categories:
            tagged.append({**h, "categories": matched_categories})
    return tagged


def assess_news_risk(tagged_headlines: list) -> dict:
    """
    Roll matched headlines up into a single risk read for the day: total
    weight across every DISTINCT category that matched at least once
    (a category firing 5 times isn't 5x the risk of it firing once --
    it's still one theme), compared against
    config.NEWS_RISK_ELEVATED_THRESHOLD.
    """
    categories_hit = set()
    for h in tagged_headlines:
        categories_hit.update(h["categories"])

    total_weight = sum(EVENT_CATEGORIES[c][0] for c in categories_hit)
    threshold = getattr(cfg, "NEWS_RISK_ELEVATED_THRESHOLD", 3)

    return {
        "level": "elevated" if total_weight >= threshold else "normal",
        "total_weight": total_weight,
        "categories_hit": sorted(categories_hit),
        "headline_count": len(tagged_headlines),
    }


def get_news_flags() -> dict:
    """
    One-call convenience: fetch, tag, and assess. Returns
    {"risk": {...from assess_news_risk}, "headlines": [...tagged, capped]}.
    On total failure (all feeds down), returns a "normal"/unknown-flavored
    result rather than raising -- news being unavailable shouldn't halt
    the rest of the pipeline, it just means this one input is missing.
    """
    try:
        headlines = get_headlines()
        tagged = tag_headlines(headlines)
        risk = assess_news_risk(tagged)
        return {"risk": risk, "headlines": tagged[: getattr(cfg, "NEWS_MAX_HEADLINES_SHOWN", 10)]}
    except Exception as e:
        return {
            "risk": {"level": "unknown", "total_weight": 0, "categories_hit": [], "headline_count": 0, "error": str(e)},
            "headlines": [],
        }
