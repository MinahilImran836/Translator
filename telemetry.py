"""Prometheus metrics for the translation pipeline (see main.run_translation)."""

from typing import Dict, Optional

from prometheus_client import Counter, Histogram

TRANSLATIONS = Counter(
    "translations_total",
    "Total number of completed translations",
    ["source_lang", "target_lang"],
)

LATENCY = Histogram(
    "translation_latency_seconds",
    "End-to-end translation latency in seconds (from run_translation() start to done)",
)

QUALITY = Histogram(
    "translation_quality_score",
    "Heuristic quality score (0-100) from heuristic_feedback() per translation",
    buckets=(0, 20, 40, 50, 60, 70, 80, 90, 95, 100),
)

CACHE_HITS = Counter(
    "cache_hits_total",
    "Translations served from the in-memory response cache",
)

INTEGRITY_FAILURES = Counter(
    "integrity_failures_total",
    "Translations that failed the local placeholder integrity check",
)

SECURITY_FLAGS = Counter(
    "security_flags_total",
    "Translations flagged as containing instruction-like (prompt-injection) text",
)

TRANSLATION_ERRORS = Counter(
    "translation_errors_total",
    "Translations that raised a TranslationError (rate limit, upstream error, empty output)",
    ["status"],
)

MODEL_FALLBACK_TOTAL = Counter(
    "model_fallback_total",
    "Translations served by a fallback model instead of the primary OPENROUTER_MODEL",
)


def _counter_total(counter: Counter) -> float:
    # Sums every label combination; ignores the internal "_created" timestamp sample that
    # collect() also returns alongside the actual value.
    return sum(s.value for s in counter.collect()[0].samples if not s.name.endswith("_created"))


def _histogram_avg(hist: Histogram) -> Optional[float]:
    samples = hist.collect()[0].samples
    total = next((s.value for s in samples if s.name.endswith("_sum")), None)
    count = next((s.value for s in samples if s.name.endswith("_count")), None)
    return (total / count) if count else None


def snapshot() -> Dict[str, Optional[float]]:
    """Current values read straight from these metrics — the same numbers /metrics exposes
    to Prometheus, as plain JSON. Used by GET /admin/health so the admin page still works
    even if Grafana/Prometheus themselves aren't running."""
    return {
        "translations_total": _counter_total(TRANSLATIONS),
        "translation_errors_total": _counter_total(TRANSLATION_ERRORS),
        "cache_hits_total": _counter_total(CACHE_HITS),
        "integrity_failures_total": _counter_total(INTEGRITY_FAILURES),
        "security_flags_total": _counter_total(SECURITY_FLAGS),
        "model_fallback_total": _counter_total(MODEL_FALLBACK_TOTAL),
        "avg_latency_seconds": _histogram_avg(LATENCY),
        "avg_quality_score": _histogram_avg(QUALITY),
    }
