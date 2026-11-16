"""Plain-text questions, mapped to the right command with a short explanation.

    "why is NABIL falling"        -> a short explanation from the engine + fresh events
    "news NABIL"                  -> /news NABIL
    "what should I watch today"   -> /watch (with the top candidates)
    "how is the market"           -> /market
    "NABIL"                       -> /stock NABIL

Simple word matching only; nothing is sent to an outside service.
"""

from __future__ import annotations

import re
from html import escape
from typing import Any

SKIP_WORDS = {
    "WHY", "IS", "ARE", "THE", "WHAT", "HOW", "NEWS", "FALLING", "RISING", "DOWN", "UP", "TODAY", "SHOULD", "WATCH", "MARKET",
    "STOCK", "ON", "FOR", "OF", "ABOUT", "ANY", "LATEST", "GOING", "DROPPING", "DOING", "PRICE", "SHARE", "A", "I", "IT", "TO", "DO",
    "DID", "DOES", "HAS", "NOW", "SO", "MUCH", "AND", "IN", "ME", "TELL", "SHOW", "BUY", "SELL", "WITH", "HAPPENING", "HAPPENED",
}


def find_symbol(text: str, known: set[str]) -> str | None:
    """The first word of the message that is a listed symbol."""
    words = re.findall(r"[A-Za-z0-9]{2,10}", text.upper())
    for word in words:
        if word in known and word not in SKIP_WORDS:
            return word
    for word in words:                      # a symbol that is also a common word, e.g. "UP"... only if nothing else matched
        if word in known:
            return word
    return None


def route(text: str, known: set[str]) -> tuple[str, str | None] | None:
    """Understand a plain message. Returns (intent, symbol) or None.

    Intents: why / news / watch / market / stock.
    """
    lower = " ".join(text.lower().split())
    symbol = find_symbol(text, known)
    bare = re.sub(r"[^A-Za-z0-9]", "", text).upper()
    if bare in known and len(text.split()) == 1:
        return "stock", bare
    if re.search(r"\bwhy\b", lower) or re.search(r"\b(falling|rising|dropping|crashing|going (up|down)|fell|rose|dropped|jumped)\b", lower):
        return ("why", symbol) if symbol else ("market", None)
    if re.search(r"\bnews\b|\bannouncement", lower) and symbol:
        return "news", symbol
    if re.search(r"what.*\b(watch|buy|look at)\b|\bwatch\b.*today|\bwatchlist\b|\bwatch list\b|\bideas?\b|\bcandidates?\b", lower):
        return "watch", None
    if re.search(r"\bmarket\b|\bnepse\b|\bindex\b", lower):
        return "market", None
    if symbol and len(text.split()) <= 4:
        return "stock", symbol
    return None


def explain_move(item: Any, scan: Any, frame_changes: dict[str, float | None], event_lines: list[str]) -> str:
    """A short, honest answer to "why is X falling / rising": what the data shows, not a guess at the cause."""
    change = item.change_percent
    direction = "fell" if (change or 0) < 0 else "rose"
    lines = [f"❓ <b>Why is {escape(item.symbol)} moving?</b> — data at {scan.as_of} close"]
    if change is None:
        moves = ["no change figure for the last session"]
    elif abs(change) < 0.05:
        moves = ["was unchanged on the last session"]
    else:
        moves = [f"{direction} {abs(change):.1f}% on the last session"]
    for label, key in (("5 days", "roc5"), ("20 days", "roc20")):
        value = frame_changes.get(key)
        if value is not None:
            moves.append(f"{value:+.1f}% over {label}")
    lines.append(f"{escape(item.symbol)} " + ", ".join(moves) + f". Last price {item.close:,.1f}.")
    sector = item.sector_state
    if sector is not None and sector.return_20d is not None:
        lines.append(f"Its sector ({escape(item.sector)}) is {sector.label}: {sector.return_20d:+.1f}% in 20 days. Market: {scan.regime.label}.")
    lines.append("")
    lines.append("<b>What the bot sees</b>")
    shown = (item.negatives[:3] + item.positives[:1]) if (change or 0) < 0 else (item.positives[:3] + item.negatives[:1])
    lines.extend(f"• {escape(text)}" for text in shown or ["nothing stands out in the price data"])
    lines.extend(f"⚠️ {escape(flag)}" for flag in item.flags)
    lines.append("")
    lines.append("<b>Fresh news and events</b>")
    lines.extend([f"• {escape(text)}" for text in event_lines[:4]] or ["• Nothing fresh in the bot's database. Try /news " + escape(item.symbol) + " for the news portals."])
    lines.append("")
    lines.append(f"Verdict: <b>{item.verdict}</b> (score {item.score:.0f}/100). Full analysis: /stock {escape(item.symbol)}")
    lines.append("The bot shows what the data says; it cannot know why buyers or sellers acted.")
    return "\n".join(lines)
