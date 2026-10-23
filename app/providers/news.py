import re

import feedparser

from ..schemas import NewsItem


class NewsProvider:
    """Google News RSS headlines for a symbol — free, no API key."""

    RSS_URL = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

    def fetch(self, symbol: str, limit: int = 5) -> list[NewsItem]:
        feed = feedparser.parse(self.RSS_URL.format(q=symbol))
        items = []
        for entry in feed.entries[:limit]:
            source = getattr(entry, "source", None)
            items.append(NewsItem(
                title=entry.get("title", ""),
                link=entry.get("link", ""),
                source=source.title if source else "",
                published=entry.get("published", ""),
            ))
        return items


class SentimentAnalyzer:
    """Naive headline-keyword sentiment.

    (ponytail: heuristic scoring; upgrade path = a real sentiment model or LLM
    scoring, keeping the same analyze() interface.)
    """

    POSITIVE = {"beat", "beats", "surge", "surges", "rally", "rallies", "record", "upgrade",
                "upgrades", "outperform", "buy", "bullish", "growth", "gain", "gains", "jump",
                "jumps", "soar", "soars", "win", "wins", "strong", "profit", "profits", "boost"}
    NEGATIVE = {"miss", "misses", "plunge", "plunges", "drop", "drops", "fall", "falls",
                "downgrade", "downgrades", "sell", "bearish", "weak", "loss", "losses", "cut",
                "cuts", "slump", "slumps", "warning", "probe", "lawsuit", "layoff", "layoffs",
                "investigation", "decline", "declines"}

    def score_text(self, text: str) -> float:
        words = set(re.findall(r"[a-z]+", text.lower()))
        pos = len(words & self.POSITIVE)
        neg = len(words & self.NEGATIVE)
        return (pos - neg) / max(1, pos + neg)

    def analyze(self, items: list[NewsItem]) -> tuple[float, list[str]]:
        """Returns (aggregate score in [-1, 1], headline titles)."""
        scores = [self.score_text(i.title) for i in items]
        score = round(sum(scores) / len(scores), 3) if scores else 0.0
        return score, [i.title for i in items]