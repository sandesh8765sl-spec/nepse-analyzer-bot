"""News and events engine.

Turns raw announcements and headlines into a short, clean list:

* each item gets ONE event type (config/events.yaml) with its usual price impact;
* duplicates are merged (same story from two sources, or the same event repeated);
* only fresh items are kept;
* per stock there is one net statement, so the bot never says "positive" and
  "negative" about the same stock in the same message.

Also builds the calendar of upcoming book closures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from typing import Any, Iterable

from nepse_bot.settings import CONFIG_DIR, load_yaml
from nepse_bot.trading_calendar import TradingCalendar

EVENTS_CONFIG_PATH = CONFIG_DIR / "events.yaml"
IMPACT_MARK = {"positive": "🟢", "negative": "🔴", "mixed": "🟡", "neutral": "⚪"}


@lru_cache(maxsize=1)
def events_config() -> dict[str, Any]:
    try:
        return load_yaml(EVENTS_CONFIG_PATH)
    except FileNotFoundError:
        return {}


@dataclass(frozen=True)
class Event:
    """One cleaned news item."""

    symbol: str | None
    title: str
    day: str                 # YYYY-MM-DD it was published
    source: str
    kind: str                # event type key
    label: str
    impact: str              # positive / negative / mixed / neutral
    note: str
    routine: bool

    @property
    def mark(self) -> str:
        return IMPACT_MARK.get(self.impact, "⚪")


def classify(title: str) -> str:
    """The event type of a title: the first type whose keyword appears as a whole word or phrase."""
    text = " " + re.sub(r"[^a-z0-9]+", " ", title.lower()) + " "
    for key, spec in (events_config().get("types") or {}).items():
        for keyword in spec.get("keywords") or []:
            if f" {keyword.lower()}" in text:
                return key
    return "other"


def _spec(kind: str) -> dict[str, Any]:
    config = events_config()
    return (config.get("types") or {}).get(kind) or config.get("default") or {}


def _words(title: str, symbol: str | None) -> frozenset[str]:
    text = re.sub(r"\[[^\]]*\]", " ", title.lower())
    words = set(re.findall(r"[a-z0-9]+", text))
    if symbol:
        words.discard(symbol.lower())
    return frozenset(word for word in words if len(word) > 2)


def _same_story(a: frozenset[str], b: frozenset[str], threshold: float) -> bool:
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= threshold


def build_events(rows: Iterable[dict[str, Any]], today: str) -> list[Event]:
    """Clean raw rows (symbol, title, published_at, source) into fresh, de-duplicated events, newest first."""
    config = events_config()
    default_fresh = int(config.get("fresh_days", 7))
    window = int(config.get("duplicate_window_days", 5))
    similarity = float(config.get("headline_similarity", 0.6))
    current = date.fromisoformat(today)
    candidates: list[tuple[Event, frozenset[str]]] = []
    for row in rows:
        title = " ".join(str(row.get("title") or "").split())
        day = str(row.get("published_at") or "")[:10]
        try:
            published = date.fromisoformat(day)
        except ValueError:
            continue                                    # undated items cannot be shown as fresh
        kind = classify(title)
        spec = _spec(kind)
        age = (current - published).days
        if not title or age < 0 or age > int(spec.get("fresh_days", default_fresh)):
            continue
        symbol = (row.get("symbol") or None) and str(row["symbol"]).upper()
        event = Event(
            symbol=symbol, title=title, day=day, source=str(row.get("source") or "unknown source"), kind=kind,
            label=str(spec.get("label", "Other news")), impact=str(spec.get("impact", "neutral")),
            note=str(spec.get("note", "")), routine=bool(spec.get("routine", False)),
        )
        candidates.append((event, _words(title, symbol)))
    candidates.sort(key=lambda pair: pair[0].day, reverse=True)
    kept: list[tuple[Event, frozenset[str]]] = []
    for event, words in candidates:
        duplicate = False
        for other, other_words in kept:
            if event.symbol != other.symbol:
                continue
            close_in_time = abs((date.fromisoformat(other.day) - date.fromisoformat(event.day)).days) <= window
            same_event = event.symbol is not None and event.kind == other.kind and event.kind != "other" and not event.routine
            if close_in_time and (same_event or _same_story(words, other_words, similarity)):
                duplicate = True
                break
        if not duplicate:
            kept.append((event, words))
    return [event for event, _ in kept]


def load_events(db: Any, today: str, symbols: Iterable[str] | None = None) -> list[Event]:
    """Fresh events from the database's news table (company announcements and saved headlines)."""
    oldest = (date.fromisoformat(today) - timedelta(days=45)).isoformat()
    sql, params = "SELECT symbol, title, source, published_at FROM news WHERE published_at >= ?", [oldest]
    if symbols is not None:
        wanted = sorted(set(symbols))
        if not wanted:
            return []
        sql += f" AND symbol IN ({','.join('?' for _ in wanted)})"
        params.extend(wanted)
    return build_events(db.query(sql + " ORDER BY published_at DESC", params), today)


def net_impact(events: Iterable[Event]) -> tuple[str, str]:
    """One impact word and one sentence for a stock's events, so the message cannot contradict itself."""
    impacts = {event.impact for event in events if not event.routine and event.impact != "neutral"}
    if not impacts:
        return "neutral", "nothing here usually moves the price"
    if impacts == {"positive"}:
        return "positive", "usually supportive for the price"
    if impacts == {"negative"}:
        return "negative", "usually a drag on the price"
    return "mixed", "pulls both ways, so the bot takes no side on it"


def by_symbol(events: Iterable[Event]) -> dict[str, list[Event]]:
    grouped: dict[str, list[Event]] = {}
    for event in events:
        if event.symbol:
            grouped.setdefault(event.symbol, []).append(event)
    return grouped


def event_lines(events: list[Event], limit: int = 6, include_routine: bool = False) -> list[str]:
    """Plain-text lines for a message: one block per stock, one net statement each."""
    lines: list[str] = []
    grouped = by_symbol([event for event in events if include_routine or not event.routine])
    ordered = sorted(grouped.items(), key=lambda pair: max(event.day for event in pair[1]), reverse=True)
    for symbol, items in ordered[:limit]:
        impact, sentence = net_impact(items)
        labels = "; ".join(dict.fromkeys(f"{event.label} ({event.day[5:]})" for event in items))
        lines.append(f"{IMPACT_MARK[impact]} {symbol}: {labels} - {sentence}.")
    return lines


# ------------------------------------------------------------------ calendar
@dataclass(frozen=True)
class ClosureItem:
    """An upcoming book closure for one stock."""

    symbol: str
    day: str                     # book closure date
    trading_days_left: int
    last_buy_day: str            # last trading day a purchase still gets the payout
    cash_percent: float | None
    bonus_percent: float | None
    right_ratio: float | None

    def price_after(self, price: float) -> float | None:
        """The price the market normally opens at after this book closure."""
        return expected_adjustment(price, self.cash_percent, self.bonus_percent, self.right_ratio)

    @property
    def payout(self) -> str:
        parts = []
        if self.cash_percent:
            parts.append(f"{self.cash_percent:g}% cash")
        if self.bonus_percent:
            parts.append(f"{self.bonus_percent:g}% bonus")
        if self.right_ratio:
            parts.append(f"right shares {self.right_ratio:g} per share held")
        return " + ".join(parts) or "details not published"


def expected_adjustment(price: float, cash_percent: float | None = None, bonus_percent: float | None = None,
                        right_ratio: float | None = None, right_price: float | None = None) -> float | None:
    """Theoretical price after a book closure (None when nothing adjusts the price).

    Cash dividend: the price drops by the dividend (percent of the Rs 100 face value).
    Bonus b%: price / (1 + b/100). Right shares r per share at the issue price: (price + r x issue price) / (1 + r).
    """
    if not price or not (cash_percent or bonus_percent or right_ratio):
        return None
    value = float(price) - float(cash_percent or 0.0)
    if bonus_percent:
        value /= 1 + float(bonus_percent) / 100.0
    if right_ratio:
        value = (value + float(right_ratio) * float(right_price or 100.0)) / (1 + float(right_ratio))
    return round(value, 1)


@dataclass(frozen=True)
class CorporateEvent:
    """A known upcoming corporate event: shown however old the announcement is, until it has happened."""

    symbol: str
    kind: str                    # right / bonus / dividend / agm / book_closure / merger
    text: str                    # e.g. "right shares 0.5 per share held (50%) at Rs 100"
    announced: str | None
    closure_day: str | None      # book closure date, None while it is not fixed
    adjusts_price: bool
    price_after: float | None    # expected price after the book closure, from the last close

    def line(self) -> str:
        when = f"book closure {self.closure_day}" if self.closure_day else "book closure date not fixed yet"
        if self.kind in {"agm", "merger"}:
            when = f"announced {self.announced}" if self.announced else "announced"
        elif self.announced:
            when += f" (announced {self.announced})"
        text = f"{self.symbol}: {self.text} - {when}"
        if self.price_after:
            text += f". Expected price after the book closure: about {self.price_after:,.0f}"
        return text


NEWS_KIND_TO_ACTION = {"right_share": "right", "dividend": "dividend", "book_closure": "book_closure", "agm": "agm", "merger": "merger"}


def corporate_events(db: Any, today: str, symbols: Iterable[str] | None = None, prices: dict[str, float] | None = None) -> list[CorporateEvent]:
    """Every known corporate event that has not happened yet (right share, bonus, dividend, AGM, book closure).

    Unlike the news list this does not expire after a few days: an approved right share stays
    listed until its book closure has passed. Sources: the corporate-actions table (open or
    proposed items, and any book closure still ahead) and company announcements.
    """
    config = events_config()
    known_days = int(config.get("known_event_days", 240))
    meeting_days = int(config.get("meeting_days", 45))
    current = date.fromisoformat(today)
    oldest = (current - timedelta(days=known_days)).isoformat()
    # A dividend with no book closure date long after its announcement is a stale source record, not a pending event.
    oldest_dividend = (current - timedelta(days=int(config.get("known_dividend_days", 120)))).isoformat()
    wanted = sorted(set(symbols)) if symbols is not None else None
    if wanted is not None and not wanted:
        return []
    clause = f" AND symbol IN ({','.join('?' for _ in wanted)})" if wanted else ""
    extra = list(wanted or [])
    prices = prices or {}

    def last_close(symbol: str) -> float | None:
        if symbol not in prices:
            value = db.scalar("SELECT close FROM daily_prices WHERE symbol = ? ORDER BY date DESC LIMIT 1", (symbol,))
            prices[symbol] = float(value) if value else 0.0
        return prices[symbol] or None

    grouped: dict[tuple[str, str | None], dict[str, Any]] = {}
    for row in db.query(
        "SELECT symbol, action_type, ratio, amount, announcement_date, book_closure_date, status FROM corporate_actions "
        "WHERE (book_closure_date >= ? OR (book_closure_date IS NULL AND COALESCE(status, '') != 'Closed' "
        "AND (announcement_date IS NULL OR announcement_date >= ?)))" + clause,
        [today, oldest, *extra],
    ):
        if row["action_type"] != "right" and not row["book_closure_date"] and (row["announcement_date"] or "") < oldest_dividend:
            continue
        entry = grouped.setdefault((row["symbol"], row["book_closure_date"]), {"announced": None})
        if row["announcement_date"]:
            entry["announced"] = max(entry["announced"] or "", row["announcement_date"])
        if row["action_type"] == "cash_dividend":
            entry["cash"] = row["amount"]
        elif row["action_type"] == "bonus":
            entry["bonus"] = row["ratio"]
        elif row["action_type"] == "right":
            entry["right"], entry["right_price"] = row["ratio"], row["amount"]
    items: list[CorporateEvent] = []
    covered: set[tuple[str, str]] = set()
    for (symbol, closure_day), entry in grouped.items():
        price = last_close(symbol)
        if entry.get("right"):
            ratio = float(entry["right"])
            text = f"right shares {ratio:g} per share held ({ratio * 100:.0f}%) at Rs {float(entry.get('right_price') or 100):g}"
            after = expected_adjustment(price or 0.0, right_ratio=ratio, right_price=entry.get("right_price"))
            items.append(CorporateEvent(symbol, "right", text, entry["announced"], closure_day, True, after))
            covered.add((symbol, "right"))
        if entry.get("cash") or entry.get("bonus"):
            parts = [f"{float(entry['bonus']):g}% bonus shares"] if entry.get("bonus") else []
            if entry.get("cash"):
                parts.append(f"{float(entry['cash']):g}% cash dividend")
            after = expected_adjustment(price or 0.0, entry.get("cash"), entry.get("bonus"))
            items.append(CorporateEvent(symbol, "bonus" if entry.get("bonus") else "dividend", " + ".join(parts), entry["announced"], closure_day, True, after))
            covered.add((symbol, "dividend"))
            if closure_day:
                covered.add((symbol, "book_closure"))

    # Announcements: an approved right share or a called AGM that the corporate-actions table does not carry (yet).
    done = {
        (row["symbol"], "right" if row["action_type"] == "right" else "dividend"): row["last"]
        for row in db.query(
            "SELECT symbol, action_type, MAX(book_closure_date) AS last FROM corporate_actions "
            "WHERE book_closure_date < ? AND book_closure_date >= ?" + clause + " GROUP BY symbol, action_type",
            [today, oldest, *extra],
        )
    }
    seen: set[tuple[str, str]] = set()
    for row in db.query(
        "SELECT symbol, title, published_at FROM news WHERE symbol IS NOT NULL AND published_at >= ?" + clause + " ORDER BY published_at DESC",
        [oldest, *extra],
    ):
        kind = NEWS_KIND_TO_ACTION.get(classify(str(row["title"] or "")))
        symbol, day = row["symbol"], str(row["published_at"] or "")[:10]
        if kind is None or (symbol, kind) in covered or (symbol, kind) in seen:
            continue
        if kind in {"agm", "book_closure", "merger"} and day < (current - timedelta(days=meeting_days)).isoformat():
            continue
        if kind == "agm" and any(word in str(row["title"]).lower() for word in ("minute", "elected", "held on")):
            continue                                     # minutes of a meeting that was already held
        if kind in {"right", "dividend"} and (done.get((symbol, kind)) or "") >= day:
            continue                                     # that event's book closure has already passed
        seen.add((symbol, kind))
        title = " ".join(str(row["title"]).replace(f"[{symbol}]", "").split())
        items.append(CorporateEvent(symbol, kind, title[:110], day, None, kind in {"right", "dividend", "book_closure"}, None))
    items.sort(key=lambda item: (item.closure_day or "9999", item.symbol))
    return items


def upcoming_closures(db: Any, today: str, calendar: TradingCalendar, days: int = 30, symbols: Iterable[str] | None = None) -> list[ClosureItem]:
    """Book closures from today to ``days`` ahead, soonest first."""
    start = date.fromisoformat(today)
    rows = db.query(
        "SELECT symbol, action_type, ratio, amount, book_closure_date FROM corporate_actions "
        "WHERE book_closure_date >= ? AND book_closure_date <= ? ORDER BY book_closure_date, symbol",
        (today, (start + timedelta(days=days)).isoformat()),
    )
    wanted = set(symbols) if symbols is not None else None
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if wanted is not None and row["symbol"] not in wanted:
            continue
        entry = merged.setdefault((row["book_closure_date"], row["symbol"]), {})
        if row["action_type"] == "cash_dividend":
            entry["cash"] = row["amount"]
        elif row["action_type"] == "bonus":
            entry["bonus"] = row["ratio"]
        elif row["action_type"] == "right":
            entry["right"] = row["ratio"]
    items = []
    for (day, symbol), entry in sorted(merged.items()):
        closure = date.fromisoformat(day)
        left, cursor = 0, start
        while cursor < closure:
            cursor += timedelta(days=1)
            if calendar.is_trading_day(cursor):
                left += 1
        items.append(ClosureItem(symbol, day, left, calendar.previous_trading_day(closure).isoformat(), entry.get("cash"), entry.get("bonus"), entry.get("right")))
    return items
