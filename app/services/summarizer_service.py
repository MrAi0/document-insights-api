import asyncio
import random
import re
from collections import Counter

from app.models import Summary

MODEL_NAME = "mock-summarizer-v1"
WORDS_PER_MINUTE = 200
MAX_SUMMARY_CHARS = 500
KEYWORD_COUNT = 5

_WORD_RE = re.compile(r"[a-z][a-z'-]+")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")

_STOPWORDS = frozenset(
    """
    a about after all also an and any are as at be been but by can could did do does
    for from had has have he her his how i if in into is it its just more most no not
    of on one or other our out over she so some such than that the their them then
    there these they this those through to too under up very was we were what when
    where which while who will with would you your
    """.split()
)


class SummarizationError(Exception):
    pass


class SummarizerService:
    """Stand-in for a real model call: slow, and fails now and then."""

    def __init__(
        self,
        min_seconds: float,
        max_seconds: float,
        failure_rate: float,
        rng: random.Random | None = None,
    ):
        self._min_seconds = min_seconds
        self._max_seconds = max_seconds
        self._failure_rate = failure_rate
        self._rng = rng or random.Random()

    async def summarize(self, content: str) -> Summary:
        await asyncio.sleep(self._rng.uniform(self._min_seconds, self._max_seconds))
        if self._rng.random() < self._failure_rate:
            raise SummarizationError("summarizer failed (simulated)")
        return build_summary(content)


def build_summary(content: str) -> Summary:
    """Cheap extractive summary: the first two sentences plus the most frequent words.

    It has to be deterministic. The cache assumes the same content always gives
    the same summary.
    """
    sentences = [s.strip() for s in _SENTENCE_END_RE.split(content) if s.strip()]
    text = " ".join(sentences[:2])
    if len(text) > MAX_SUMMARY_CHARS:
        text = text[: MAX_SUMMARY_CHARS - 3].rstrip() + "..."

    words = _WORD_RE.findall(content.lower())
    counts = Counter(w for w in words if w not in _STOPWORDS)
    word_count = len(content.split())

    return Summary(
        text=text,
        keywords=[word for word, _ in counts.most_common(KEYWORD_COUNT)],
        word_count=word_count,
        reading_time_seconds=max(1, round(word_count / WORDS_PER_MINUTE * 60)),
        model=MODEL_NAME,
    )
