#!/usr/bin/env python3
"""Build S2 signal-cycle bundle from public GitHub Pages artifacts only.

Strict source policy:
- Fetch only configured public Pages JSON/CSV artifact URLs.
- No rendered-page scraping.
- No dummy rows.
- No zero-filled coupling rows.
- Live predictions are live-state only; they are never used to compute hit/PnL.
- Market horizon lift is computed only from scored aggregate artifacts or aggregateable realized state.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "feeds.yml"
OUT_DIR = ROOT / "data" / "derived"
BUNDLE_PATH = OUT_DIR / "signal_cycle_bundle.json"
HEALTH_PATH = OUT_DIR / "source_health.json"
USER_AGENT = "Mozilla/5.0 (compatible; s2-signal-cycle-lab/2.0; +https://github.com/dream-framework/)"

TOPIC_ALIASES = {
    "markets": "Markets / Economy",
    "market": "Markets / Economy",
    "markets_economy": "Markets / Economy",
    "markets_/_economy": "Markets / Economy",
    "economy": "Markets / Economy",
    "ai": "AI / Tech",
    "ai_tech": "AI / Tech",
    "ai_/_tech": "AI / Tech",
    "tech": "AI / Tech",
    "public_health": "Public Health",
    "health": "Public Health",
    "space_science": "Space / Science",
    "space_/_science": "Space / Science",
    "space": "Space / Science",
    "science": "Space / Science",
    "culture_media": "Culture / Media",
    "culture_/_media": "Culture / Media",
    "culture": "Culture / Media",
    "media": "Culture / Media",
    "climate": "Climate / Weather",
    "climate_weather": "Climate / Weather",
    "climate_/_weather": "Climate / Weather",
    "weather": "Climate / Weather",
    "politics": "Politics / Elections",
    "elections": "Politics / Elections",
    "politics_elections": "Politics / Elections",
    "politics_/_elections": "Politics / Elections",
    "geopolitics": "Geopolitics",
    "cybersecurity": "Cybersecurity",
    "cyber": "Cybersecurity",
    "energy": "Energy",
    "general": "General",
    "quantum": "Quantum tech",
    "quantum_tech": "Quantum tech",
}

AGG_SCORE_REQUIRED = {"direction_hit", "pnl_proxy", "mae"}


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_key(value: Any) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower())).strip("_")


def get_any(d: dict[str, Any], *keys: str) -> Any:
    if not isinstance(d, dict):
        return None
    by_norm = {normalize_key(k): k for k in d.keys()}
    for key in keys:
        nk = normalize_key(key)
        if nk in by_norm:
            return d[by_norm[nk]]
    return None


def first(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if math.isfinite(v) else None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "n/a", "na", "—", "-"}:
        return None
    text = text.replace(",", "")
    neg = text.startswith("(") and text.endswith(")")
    if neg:
        text = text[1:-1]
    is_pct = text.endswith("%")
    if is_pct:
        text = text[:-1].strip()
    try:
        v = float(text)
    except ValueError:
        return None
    if neg:
        v = -v
    if not math.isfinite(v):
        return None
    return v / 100.0 if is_pct else v


def duration_to_hours(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        return v if math.isfinite(v) else None
    text = str(value).strip().lower().replace(" ", "")
    if not text or text in {"—", "-"}:
        return None
    try:
        if text.endswith(("hours", "hour")):
            return float(re.sub(r"hours?$", "", text))
        if text.endswith(("hrs", "hr")):
            return float(re.sub(r"hrs?$", "", text))
        if text.endswith("h"):
            return float(text[:-1])
        if text.endswith(("days", "day")):
            return float(re.sub(r"days?$", "", text)) * 24.0
        if text.endswith("d"):
            return float(text[:-1]) * 24.0
        return float(text)
    except ValueError:
        return None


def fmt_topic(topic: Any) -> str | None:
    if topic is None or isinstance(topic, (dict, list)):
        return None
    raw = str(topic).strip()
    if not raw:
        return None
    key = normalize_key(raw.replace("/", " / "))
    if key in TOPIC_ALIASES:
        return TOPIC_ALIASES[key]
    key2 = normalize_key(raw)
    if key2 in TOPIC_ALIASES:
        return TOPIC_ALIASES[key2]
    # Title-case snake names but preserve slash labels.
    if "_" in raw and "/" not in raw:
        return raw.replace("_", " ").title()
    return raw


def median(vals: Iterable[float | None]) -> float | None:
    clean = [float(v) for v in vals if v is not None and math.isfinite(float(v))]
    return float(statistics.median(clean)) if clean else None


def mean(vals: Iterable[float | None]) -> float | None:
    clean = [float(v) for v in vals if v is not None and math.isfinite(float(v))]
    return float(statistics.fmean(clean)) if clean else None


def mode(vals: Iterable[float | None], ndigits: int = 4) -> float | None:
    clean = [round(float(v), ndigits) for v in vals if v is not None and math.isfinite(float(v))]
    return Counter(clean).most_common(1)[0][0] if clean else None


def build_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch_url(url: str, timeout: int = 18, max_bytes: int | None = None) -> tuple[bool, str, str | None]:
    """Fetch a public artifact with network timeouts and visible progress logs.

    Required artifacts are intentionally uncapped because the upstream market
    scorecard can be large. We still avoid hangs through connect/read timeouts
    and the workflow-level timeout. Optional/debug artifacts may pass max_bytes.
    """
    started = dt.datetime.now(dt.timezone.utc)
    cap_label = "unlimited" if not max_bytes or max_bytes <= 0 else str(max_bytes)
    log(f"[FETCH] start {url} timeout={timeout}s max_bytes={cap_label}")
    try:
        with requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=(8, timeout), stream=True) as response:
            if response.status_code >= 400:
                return False, "", f"HTTP {response.status_code}"
            length_header = response.headers.get("content-length")
            if length_header:
                try:
                    length = int(length_header)
                    if max_bytes and max_bytes > 0 and length > max_bytes:
                        return False, "", f"artifact too large: {length} bytes > {max_bytes}"
                    log(f"[FETCH] length {url} bytes={length}")
                except ValueError:
                    pass
            chunks: list[bytes] = []
            total = 0
            next_progress = 25 * 1024 * 1024
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if max_bytes and max_bytes > 0 and total > max_bytes:
                    return False, "", f"artifact exceeded size cap: {total} bytes > {max_bytes}"
                if total >= next_progress:
                    elapsed = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
                    log(f"[FETCH] progress {url} bytes={total} elapsed={elapsed:.1f}s")
                    next_progress += 25 * 1024 * 1024
                chunks.append(chunk)
            text = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
            if not text.strip():
                return False, "", "empty response"
            elapsed = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
            log(f"[FETCH] ok {url} bytes={total} elapsed={elapsed:.1f}s")
            return True, text, None
    except Exception as exc:
        elapsed = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
        log(f"[FETCH] fail {url} elapsed={elapsed:.1f}s error={exc}")
        return False, "", str(exc)


def parse_json_artifact(text: str, url: str) -> tuple[Any | None, str | None]:
    try:
        return json.loads(text), None
    except Exception as exc:
        return None, f"JSON parse failed for {url}: {exc}"


def parse_csv_artifact(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        dialect = csv.Sniffer().sniff(text[:4096]) if text.strip() else csv.excel
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        return [], []
    return [dict(row) for row in reader], list(reader.fieldnames)


def iter_dicts(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from iter_dicts(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_dicts(item)


def count_json_records(obj: Any) -> int:
    """Best-effort count for context artifacts that are not fitted cycle rows.

    history.json in the cycle app is often raw story/event history. It can be
    perfectly valid and useful to the source cycle app while containing no
    beta/lambda fit rows for this coupling app.
    """
    if isinstance(obj, list):
        return len(obj)
    if isinstance(obj, dict):
        for key in ("rows", "history", "items", "stories", "events", "articles", "records"):
            value = obj.get(key)
            if isinstance(value, list):
                return len(value)
        return sum(1 for _ in iter_dicts(obj))
    return 0

def text_norm(value: Any) -> str:
    """Small safe text normalizer for topic/title comparisons."""
    if value is None:
        return ""
    text = str(value).strip().lower()
    text = text.replace("_", " ").replace("-", " ")
    text = " ".join(text.split())
    return text

def extract_news_mentions(obj: Any, source_kind: str, max_per_topic: int = 18) -> dict[str, list[dict[str, Any]]]:
    """Extract visible article/story examples from cycle JSON artifacts.

    This is intentionally best-effort and strict: it only uses text already
    present in the public cycle artifacts. It does not scrape pages, summarize
    outside content, or invent headlines. The goal is UI visibility: when the
    radar says a filament is forming, users can see which real source rows were
    underneath that topic.
    """
    topic_keys = ("topic", "Topic", "topic_label", "topic_id", "category", "sector", "channel", "metatopic")
    title_keys = ("title", "headline", "headline_text", "story_title", "name")
    summary_keys = ("summary", "description", "snippet", "abstract", "dek")
    source_keys = ("source", "publisher", "outlet", "site", "feed", "domain")
    url_keys = ("url", "link", "href")
    time_keys = ("published", "published_at", "date", "timestamp", "updated_at", "created_at")

    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[str, str]] = set()
    for d in iter_dicts(obj):
        if not isinstance(d, dict):
            continue
        topic = fmt_topic(first(*(get_any(d, k) for k in topic_keys)))
        title = first(*(get_any(d, k) for k in title_keys))
        summary = first(*(get_any(d, k) for k in summary_keys))
        url = first(*(get_any(d, k) for k in url_keys))
        source = first(*(get_any(d, k) for k in source_keys))
        published = first(*(get_any(d, k) for k in time_keys))

        # Some cycle fit rows use the topic itself as name/title. Do not show
        # those as news evidence unless they also carry a URL or summary.
        if not title and summary:
            title = str(summary)[:160]
        if not topic or not title:
            continue
        title_s = re.sub(r"\s+", " ", str(title)).strip()
        if len(title_s) < 12:
            continue
        if text_norm(title_s) == text_norm(topic) and not url and not summary:
            continue
        sig = (topic, title_s.lower())
        if sig in seen:
            continue
        seen.add(sig)
        if len(out[topic]) >= max_per_topic:
            continue
        out[topic].append({
            "title": title_s,
            "summary": re.sub(r"\s+", " ", str(summary)).strip()[:240] if summary else "",
            "source": str(source)[:80] if source else "",
            "url": str(url) if url else "",
            "published": str(published)[:80] if published else "",
            "artifact": source_kind,
        })
    return out


def merge_news_mentions(base: dict[str, list[dict[str, Any]]], new: dict[str, list[dict[str, Any]]], max_per_topic: int = 24) -> None:
    for topic, rows in (new or {}).items():
        existing_titles = {str(x.get("title", "")).lower() for x in base.get(topic, [])}
        bucket = base.setdefault(topic, [])
        for row in rows:
            title = str(row.get("title") or "").lower()
            if title and title not in existing_titles and len(bucket) < max_per_topic:
                bucket.append(row)
                existing_titles.add(title)


def stable_hash_int(*parts: Any) -> int:
    """Deterministic small hash used only for UI sample rotation."""
    text = "|".join(str(p) for p in parts if p is not None)
    digest = hashlib.blake2b(text.encode("utf-8", errors="replace"), digest_size=8).hexdigest()
    return int(digest, 16)


def news_signature(item: dict[str, Any]) -> tuple[str, str, str]:
    return (
        text_norm(item.get("title")),
        text_norm(item.get("source") or item.get("artifact")),
        text_norm(item.get("published")),
    )


def unique_news_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for sample in samples or []:
        if not isinstance(sample, dict):
            continue
        sig = news_signature(sample)
        if not sig[0] or sig in seen:
            continue
        seen.add(sig)
        out.append(sample)
    return out


def rotated_news_samples(pool: list[dict[str, Any]], row: dict[str, Any], max_samples: int) -> list[dict[str, Any]]:
    """Pick a deterministic row-specific window from a topic news bucket.

    Earlier versions attached topic_news[topic][:N] to every fitted row. That made
    every dot/circle in S2 Theater show the same headlines whenever several rows
    belonged to the same broad topic. This function keeps the data strict and real
    but rotates the public source-row bucket by the fitted row identity, so each
    circle exposes a local slice of the topic evidence instead of repeating the
    same generic block.
    """
    pool = unique_news_samples(pool)
    if not pool or max_samples <= 0:
        return []
    if len(pool) <= max_samples:
        return [dict(x) for x in pool]
    seed = stable_hash_int(
        row.get("topic"), row.get("phase"), row.get("newest_peak"), row.get("source"),
        row.get("lambda_hours"), row.get("beta"), row.get("delta_aic"), row.get("n"),
    )
    start = seed % len(pool)
    step = (seed // max(1, len(pool))) % len(pool) or 1
    while math.gcd(step, len(pool)) != 1:
        step = (step + 1) % len(pool) or 1
    picked: list[dict[str, Any]] = []
    idx = start
    for _ in range(len(pool)):
        item = dict(pool[idx])
        item["sample_scope"] = "row-rotated"
        picked.append(item)
        if len(picked) >= max_samples:
            break
        idx = (idx + step) % len(pool)
    return picked


def attach_news_samples(rows: list[dict[str, Any]], topic_news: dict[str, list[dict[str, Any]]], max_samples: int = 5) -> list[dict[str, Any]]:
    """Attach row-specific real news examples to cycle rows for theater/radar UI."""
    out: list[dict[str, Any]] = []
    for row in rows:
        r = dict(row)
        topic = str(r.get("topic") or "")
        pool = topic_news.get(topic, [])
        samples = rotated_news_samples(pool, r, max_samples=max_samples)
        r["news_samples"] = samples
        r["news_sample_scope"] = "row-specific" if samples else "none"
        r["topic_news_pool_count"] = len(unique_news_samples(pool))
        out.append(r)
    return out


def parse_cycle_row(d: dict[str, Any], source: str) -> dict[str, Any] | None:
    fit = get_any(d, "fit", "s2_fit", "retention_fit")
    fit = fit if isinstance(fit, dict) else {}
    metrics = get_any(d, "metrics", "summary")
    metrics = metrics if isinstance(metrics, dict) else {}
    topic = fmt_topic(first(
        get_any(d, "topic"), get_any(d, "Topic"), get_any(d, "name"), get_any(d, "label"),
        get_any(d, "category"), get_any(d, "sector"), get_any(d, "channel"), get_any(fit, "topic"),
    ))
    phase = first(get_any(d, "phase"), get_any(d, "Phase"), get_any(d, "verdict"), get_any(d, "status"))
    n_value = first(get_any(d, "N"), get_any(d, "n"), get_any(d, "count"), get_any(d, "rows"), get_any(d, "sample_count"), get_any(metrics, "N"))
    lambda_value = first(
        get_any(d, "lambda_q"), get_any(d, "lambda"), get_any(d, "lambda_hours"), get_any(d, "lambda_q_hours"),
        get_any(d, "tau_hours"), get_any(d, "coherence_hours"), get_any(fit, "lambda_q"), get_any(fit, "tau_hours"),
        get_any(fit, "lambda_hours"), get_any(metrics, "lambda_q"), get_any(metrics, "tau_hours"),
    )
    beta_value = first(get_any(d, "beta"), get_any(d, "Beta"), get_any(fit, "beta"), get_any(metrics, "beta"))
    half_value = first(
        get_any(d, "half"), get_any(d, "Half"), get_any(d, "half_life"), get_any(d, "half_life_hours"),
        get_any(fit, "half_life"), get_any(fit, "half_life_hours"), get_any(metrics, "half_life"),
    )
    # Keep dust strict: do not default missing dust to zero.
    dust_value = first(
        get_any(d, "dust"), get_any(d, "Dust"), get_any(d, "dust_score"), get_any(d, "residual_dust"),
        get_any(fit, "dust"), get_any(fit, "dust_score"), get_any(fit, "residual_dust"), get_any(metrics, "dust"),
    )
    delta_value = first(
        get_any(d, "delta_aic"), get_any(d, "Delta AIC"), get_any(d, "deltaAIC"), get_any(d, "delta_aic_vs_exp"),
        get_any(d, "delta_bic"), get_any(fit, "delta_aic"), get_any(fit, "delta_bic"),
        get_any(fit, "delta_aic_vs_exp"), get_any(metrics, "delta_aic"),
    )
    newest = first(
        get_any(d, "newest"), get_any(d, "peak"), get_any(d, "newest_peak"), get_any(d, "newest / peak"),
        get_any(d, "latest"), get_any(d, "timestamp"), get_any(d, "date"), get_any(d, "as_of"),
    )
    beta = finite_float(beta_value)
    lam_h = duration_to_hours(lambda_value)
    half_h = duration_to_hours(half_value)
    dust = finite_float(dust_value)
    delta = finite_float(delta_value)
    n = finite_float(n_value)
    # A usable cycle row must contain a topic and at least two fit fields, including beta or lambda.
    fit_count = sum(x is not None for x in [beta, lam_h, half_h, dust, delta])
    if not topic or fit_count < 2 or (beta is None and lam_h is None):
        return None
    beta_grid_min = finite_float(first(
        get_any(d, "beta_grid_min"), get_any(fit, "beta_grid_min"), get_any(metrics, "beta_grid_min")
    ))
    beta_at_grid_floor = first(
        get_any(d, "beta_at_grid_floor"), get_any(fit, "beta_at_grid_floor"), get_any(metrics, "beta_at_grid_floor")
    )
    beta_grid_version = first(
        get_any(d, "beta_grid_version"), get_any(fit, "beta_grid_version"), get_any(metrics, "beta_grid_version")
    )
    return {
        "topic": topic,
        "phase": str(phase) if phase is not None else "",
        "n": int(n) if n is not None and n >= 0 else None,
        "newest_peak": str(newest) if newest is not None else "",
        "lambda_hours": lam_h,
        "beta": beta,
        "half_life_hours": half_h,
        "dust": dust,
        "delta_aic": delta,
        "source": source,
        "beta_grid_min": beta_grid_min,
        "beta_at_grid_floor": beta_at_grid_floor,
        "beta_grid_version": str(beta_grid_version) if beta_grid_version is not None else "",
    }


def extract_cycle_rows(obj: Any, source: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for d in iter_dicts(obj):
        row = parse_cycle_row(d, source)
        if not row:
            continue
        sig = (
            row.get("topic"), row.get("phase"), row.get("newest_peak"),
            round(row.get("lambda_hours") or -1, 4), round(row.get("beta") or -1, 4),
            round(row.get("delta_aic") or -9999, 4), row.get("source"),
        )
        if sig in seen:
            continue
        seen.add(sig)
        rows.append(row)
    return rows


def norm_horizon(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower().replace(" ", "")
    if not text:
        return ""
    text = text.replace("horizon", "")
    if text.startswith("h") and text[1:].isdigit():
        return text
    if text.endswith("d") and text[:-1].isdigit():
        return "h" + text[:-1]
    if text.isdigit():
        return "h" + text
    match = re.search(r"(?:^|[_-])h?(\d+)(?:d)?$", text)
    if match:
        return "h" + match.group(1)
    return text


def normalize_model(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    text = re.sub(r"_?h\d+$", "", text).strip("_")
    if "baseline" in text or text in {"base", "price_baseline"}:
        return "baseline"
    if "s2" in text:
        return "s2"
    return text or "unknown"


def parse_model_horizon(row: dict[str, Any]) -> tuple[str, str]:
    mh = first(get_any(row, "model/horizon"), get_any(row, "model_horizon"), get_any(row, "name"))
    model = first(get_any(row, "model"), get_any(row, "track"))
    horizon = first(get_any(row, "horizon"), get_any(row, "target_horizon"), get_any(row, "days"))
    if mh and not model:
        raw = str(mh).strip()
        parts = raw.replace("-", "_").split("_")
        for p in reversed(parts):
            h = norm_horizon(p)
            if h.startswith("h") and h[1:].isdigit():
                horizon = horizon or h
                model = raw[: max(0, raw.lower().rfind(p.lower()))].strip("_- ") or raw
                break
        model = model or raw
    return normalize_model(model), norm_horizon(horizon)


def parse_aggregate_score_row(row: dict[str, Any], source: str) -> dict[str, Any] | None:
    model, horizon = parse_model_horizon(row)
    if model not in {"baseline", "s2"} or not horizon:
        return None
    realized = finite_float(first(get_any(row, "realized rows"), get_any(row, "realized_rows"), get_any(row, "held-out rows"), get_any(row, "held_out_rows"), get_any(row, "rows"), get_any(row, "n"), get_any(row, "count")))
    hit = finite_float(first(get_any(row, "direction hit"), get_any(row, "direction_hit"), get_any(row, "hit_rate"), get_any(row, "accuracy"), get_any(row, "direction_accuracy"), get_any(row, "hit")))
    coverage = finite_float(first(get_any(row, "coverage"), get_any(row, "signal_coverage")))
    pnl = finite_float(first(get_any(row, "PnL proxy"), get_any(row, "pnl_proxy"), get_any(row, "pnl"), get_any(row, "mean_return"), get_any(row, "avg_return"), get_any(row, "paper_return")))
    mae = finite_float(first(get_any(row, "MAE"), get_any(row, "mae"), get_any(row, "mean_abs_error"), get_any(row, "mean_absolute_error")))
    rmse = finite_float(first(get_any(row, "RMSE"), get_any(row, "rmse")))
    # Aggregate rows must contain at least a hit/pnl/mae metric. Do not accept live rows as aggregate rows.
    metric_count = sum(x is not None for x in [hit, pnl, mae, rmse])
    if metric_count < 2:
        return None
    if hit is not None and not (0 <= hit <= 1):
        return None
    return {
        "model": model,
        "horizon": horizon,
        "realized_rows": int(realized) if realized is not None and realized >= 0 else None,
        "direction_hit": hit,
        "coverage": coverage,
        "pnl_proxy": pnl,
        "mae": mae,
        "rmse": rmse,
        "source": source,
        "source_type": "aggregate",
    }


def direction_from_text(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"up", "buy", "long", "positive", "+", "1", "true", "hit"}:
        return 1
    if text in {"down", "sell", "short", "negative", "-", "-1", "false", "miss"}:
        return -1
    return None


def aggregate_realized_state(rows: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    """Aggregate individual realized prediction rows if the schema supports it.

    This is intentionally strict: rows need model, horizon, a real/predicted return or correctness field.
    Live/pending rows are ignored.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        model, horizon = parse_model_horizon(row)
        if model not in {"baseline", "s2"} or not horizon:
            continue
        status = str(first(get_any(row, "status"), get_any(row, "state"), "") or "").lower()
        if "pending" in status or "live" in status:
            continue
        actual_ret = finite_float(first(get_any(row, "actual_return"), get_any(row, "realized_return"), get_any(row, "future_return"), get_any(row, "return_actual")))
        pred_ret = finite_float(first(get_any(row, "predicted_return"), get_any(row, "pred_return"), get_any(row, "model_return"), get_any(row, "s2_pred_return"), get_any(row, "baseline_pred_return")))
        pnl = finite_float(first(get_any(row, "pnl_proxy"), get_any(row, "paper_return"), get_any(row, "realized_pnl")))
        correct_raw = first(get_any(row, "correct"), get_any(row, "direction_correct"), get_any(row, "hit"), get_any(row, "baseline_correct"), get_any(row, "s2_correct"))
        correct: bool | None = None
        if isinstance(correct_raw, bool):
            correct = correct_raw
        elif correct_raw is not None:
            txt = str(correct_raw).strip().lower()
            if txt in {"true", "1", "yes", "y", "hit"}:
                correct = True
            elif txt in {"false", "0", "no", "n", "miss"}:
                correct = False
        pred_dir = direction_from_text(first(get_any(row, "predicted_direction"), get_any(row, "direction"), get_any(row, "trade_signal"), get_any(row, "prediction")))
        actual_dir = direction_from_text(first(get_any(row, "actual_direction"), get_any(row, "realized_direction")))
        if actual_dir is None and actual_ret is not None:
            actual_dir = 1 if actual_ret >= 0 else -1
        if pred_dir is None and pred_ret is not None:
            pred_dir = 1 if pred_ret >= 0 else -1
        if correct is None and pred_dir is not None and actual_dir is not None:
            correct = pred_dir == actual_dir
        if pnl is None and actual_ret is not None and pred_dir is not None:
            pnl = actual_ret * pred_dir
        if correct is None and pnl is None and actual_ret is None:
            continue
        groups[(model, horizon)].append({"correct": correct, "pnl": pnl, "actual_ret": actual_ret, "pred_ret": pred_ret})
    out: list[dict[str, Any]] = []
    for (model, horizon), rs in groups.items():
        if len(rs) < 20:
            continue
        corrects = [1.0 if r["correct"] else 0.0 for r in rs if r.get("correct") is not None]
        pnls = [r["pnl"] for r in rs if r.get("pnl") is not None and math.isfinite(float(r["pnl"]))]
        errors = [abs(r["actual_ret"] - r["pred_ret"]) for r in rs if r.get("actual_ret") is not None and r.get("pred_ret") is not None]
        if not corrects and not pnls:
            continue
        out.append({
            "model": model,
            "horizon": horizon,
            "realized_rows": len(rs),
            "direction_hit": mean(corrects),
            "coverage": None,
            "pnl_proxy": mean(pnls),
            "mae": mean(errors),
            "rmse": math.sqrt(mean([e * e for e in errors])) if errors else None,
            "source": source,
            "source_type": "aggregated_realized_state",
        })
    return out


def extract_scorecard_rows(rows: list[dict[str, Any]], source: str, fieldnames: list[str]) -> tuple[list[dict[str, Any]], str]:
    norm_fields = {normalize_key(f) for f in fieldnames}
    aggregate_hint = bool({"model_horizon", "model/horizon", "direction_hit", "direction hit", "pnl_proxy", "pnl proxy"} & norm_fields)
    # First try explicit aggregate rows. If many rows are accepted, this is probably not an aggregate scorecard.
    aggregate_rows = [r for r in (parse_aggregate_score_row(row, source) for row in rows) if r]
    if aggregate_rows and len(aggregate_rows) <= 100:
        return aggregate_rows, "aggregate_scorecard"
    # If the file is not a compact aggregate, only aggregate realized state if sufficient fields exist.
    state_rows = aggregate_realized_state(rows, source)
    if state_rows:
        return state_rows, "aggregated_realized_state"
    if aggregate_rows:
        return [], "rejected_rowwise_score_like_file"
    return [], "schema_not_recognized"




class FastAggregator:
    def __init__(self):
        self.data: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {
            "rows": 0.0, "hit_n": 0.0, "hit_sum": 0.0, "pnl_n": 0.0, "pnl_sum": 0.0,
            "pnl_pos_n": 0.0, "pnl_pos_sum": 0.0, "pnl_neg_n": 0.0, "pnl_neg_sum": 0.0,
            "pnl_min": math.inf, "pnl_max": -math.inf,
            "mae_n": 0.0, "mae_sum": 0.0, "rmse_n": 0.0, "rmse_sq_sum": 0.0,
            "coverage_n": 0.0, "coverage_sum": 0.0,
        })

    def add(self, model: str, horizon: str, weight: float, hit: float | None = None,
            pnl: float | None = None, mae: float | None = None, rmse: float | None = None,
            coverage: float | None = None) -> None:
        if model not in {"baseline", "s2"} or not horizon:
            return
        if not weight or weight <= 0 or not math.isfinite(weight):
            weight = 1.0
        d = self.data[(model, horizon)]
        d["rows"] += weight
        if hit is not None and math.isfinite(hit):
            if 1.0 < hit <= 100.0:
                hit = hit / 100.0
            if 0.0 <= hit <= 1.0:
                d["hit_n"] += weight
                d["hit_sum"] += hit * weight
        if pnl is not None and math.isfinite(pnl):
            d["pnl_n"] += weight
            d["pnl_sum"] += pnl * weight
            d["pnl_min"] = min(d["pnl_min"], pnl)
            d["pnl_max"] = max(d["pnl_max"], pnl)
            if pnl > 0:
                d["pnl_pos_n"] += weight
                d["pnl_pos_sum"] += pnl * weight
            elif pnl < 0:
                d["pnl_neg_n"] += weight
                d["pnl_neg_sum"] += pnl * weight
        if mae is not None and math.isfinite(mae):
            if 1.0 < mae <= 100.0:
                mae = mae / 100.0
            d["mae_n"] += weight
            d["mae_sum"] += mae * weight
        if rmse is not None and math.isfinite(rmse):
            if 1.0 < rmse <= 100.0:
                rmse = rmse / 100.0
            d["rmse_n"] += weight
            d["rmse_sq_sum"] += (rmse * rmse) * weight
        if coverage is not None and math.isfinite(coverage):
            if 1.0 < coverage <= 100.0:
                coverage = coverage / 100.0
            if 0.0 <= coverage <= 1.0:
                d["coverage_n"] += weight
                d["coverage_sum"] += coverage * weight

    def rows(self, source: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for (model, horizon), d in sorted(self.data.items(), key=lambda kv: (int(kv[0][1][1:]) if kv[0][1].startswith('h') and kv[0][1][1:].isdigit() else 999, kv[0][0])):
            if d["rows"] <= 0:
                continue
            hit = d["hit_sum"] / d["hit_n"] if d["hit_n"] else None
            pnl = d["pnl_sum"] / d["pnl_n"] if d["pnl_n"] else None
            mae = d["mae_sum"] / d["mae_n"] if d["mae_n"] else None
            rmse = math.sqrt(d["rmse_sq_sum"] / d["rmse_n"]) if d["rmse_n"] else None
            coverage = d["coverage_sum"] / d["coverage_n"] if d["coverage_n"] else None
            avg_win = d["pnl_pos_sum"] / d["pnl_pos_n"] if d["pnl_pos_n"] else None
            avg_loss = d["pnl_neg_sum"] / d["pnl_neg_n"] if d["pnl_neg_n"] else None
            win_loss_ratio = (avg_win / abs(avg_loss)) if avg_win is not None and avg_loss is not None and avg_loss != 0 else None
            worst_pnl = d["pnl_min"] if math.isfinite(d["pnl_min"]) else None
            best_pnl = d["pnl_max"] if math.isfinite(d["pnl_max"]) else None
            if sum(x is not None for x in [hit, pnl, mae, rmse]) < 2:
                continue
            out.append({
                "model": model,
                "horizon": horizon,
                "realized_rows": int(round(d["rows"])),
                "direction_hit": hit,
                "coverage": coverage,
                "pnl_proxy": pnl,
                "cumulative_pnl_proxy": d["pnl_sum"] if d["pnl_n"] else None,
                "avg_win_proxy": avg_win,
                "avg_loss_proxy": avg_loss,
                "win_loss_ratio": win_loss_ratio,
                "worst_pnl_proxy": worst_pnl,
                "best_pnl_proxy": best_pnl,
                "mae": mae,
                "rmse": rmse,
                "source": source,
                "source_type": "fast_streamed_scorecard",
            })
        return out


def header_lookup(fieldnames: list[str]) -> dict[str, str]:
    return {normalize_key(name): name for name in fieldnames or []}


def get_field(row: dict[str, Any], fmap: dict[str, str], aliases: list[str]) -> Any:
    for alias in aliases:
        key = normalize_key(alias)
        real = fmap.get(key)
        if real is not None:
            return row.get(real)
    return None


def parse_model_horizon_fast(row: dict[str, Any], fmap: dict[str, str]) -> tuple[str, str]:
    mh = get_field(row, fmap, ["model/horizon", "model_horizon", "model horizon", "name", "track_horizon"])
    model = get_field(row, fmap, ["model", "track", "model_name"])
    horizon = get_field(row, fmap, ["horizon", "target_horizon", "days", "h"])
    if mh and (not model or not horizon):
        m2, h2 = parse_model_horizon({"model/horizon": mh})
        model = model or m2
        horizon = horizon or h2
    return normalize_model(model), norm_horizon(horizon)


def truthy_hit(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    text = str(value).strip().lower()
    if text in {"true", "yes", "y", "hit", "correct", "1"}:
        return 1.0
    if text in {"false", "no", "n", "miss", "wrong", "0"}:
        return 0.0
    v = finite_float(value)
    if v is None:
        return None
    if 1.0 < v <= 100.0:
        v = v / 100.0
    if 0.0 <= v <= 1.0:
        return v
    return None


def parse_scorecard_csv_fast(text: str, source: str) -> tuple[list[dict[str, Any]], str, int, list[str]]:
    """Fast one-pass parser for large scorecard CSVs.

    Avoids building a full list of 80MB+ rows and avoids the slow generic schema
    probe on every row. Live/pending rows are ignored. Returned rows are
    aggregate model/horizon scores only.
    """
    try:
        dialect = csv.Sniffer().sniff(text[:4096]) if text.strip() else csv.excel
    except csv.Error:
        dialect = csv.excel
    f = io.StringIO(text)
    reader = csv.DictReader(f, dialect=dialect)
    fields = list(reader.fieldnames or [])
    fmap = header_lookup(fields)
    agg = FastAggregator()
    raw = 0
    parsed_like = 0
    has_realized_weight = any(normalize_key(x) in fmap for x in ["realized_rows", "realized rows", "held_out_rows", "held-out rows", "n", "count"])
    for row in reader:
        raw += 1
        if raw % 100000 == 0:
            log(f"[PARSE] {source} rows={raw} aggregates={len(agg.data)}")
        model, horizon = parse_model_horizon_fast(row, fmap)
        if model not in {"baseline", "s2"} or not horizon:
            continue
        status = str(get_field(row, fmap, ["status", "state", "prediction_status", "realization_status"]) or "").lower()
        if "pending" in status or "live" in status:
            continue
        weight = finite_float(get_field(row, fmap, ["realized rows", "realized_rows", "held-out rows", "held_out_rows", "rows", "n", "count"])) if has_realized_weight else None
        if weight is None or weight <= 0:
            weight = 1.0
        hit = truthy_hit(get_field(row, fmap, ["direction hit", "direction_hit", "hit_rate", "accuracy", "direction_accuracy", "hit", "correct", "direction_correct"]))
        pnl = finite_float(get_field(row, fmap, ["PnL proxy", "pnl_proxy", "pnl", "mean_return", "avg_return", "paper_return", "realized_pnl"]))
        mae = finite_float(get_field(row, fmap, ["MAE", "mae", "mean_abs_error", "mean_absolute_error", "abs_error"]))
        rmse = finite_float(get_field(row, fmap, ["RMSE", "rmse", "root_mean_square_error"]))
        coverage = finite_float(get_field(row, fmap, ["coverage", "signal_coverage"]))
        if hit is None:
            pred_dir = direction_from_text(get_field(row, fmap, ["predicted_direction", "direction", "trade_signal", "prediction", "signal"]))
            actual_dir = direction_from_text(get_field(row, fmap, ["actual_direction", "realized_direction"]))
            actual_ret = finite_float(get_field(row, fmap, ["actual_return", "realized_return", "future_return", "return_actual"]))
            pred_ret = finite_float(get_field(row, fmap, ["predicted_return", "pred_return", "model_return", "s2_pred_return", "baseline_pred_return"]))
            if actual_dir is None and actual_ret is not None:
                actual_dir = 1 if actual_ret >= 0 else -1
            if pred_dir is None and pred_ret is not None:
                pred_dir = 1 if pred_ret >= 0 else -1
            if pred_dir is not None and actual_dir is not None:
                hit = 1.0 if pred_dir == actual_dir else 0.0
            if pnl is None and actual_ret is not None and pred_dir is not None:
                pnl = actual_ret * pred_dir
            if mae is None and actual_ret is not None and pred_ret is not None:
                mae = abs(actual_ret - pred_ret)
        if sum(x is not None for x in [hit, pnl, mae, rmse]) < 1:
            continue
        parsed_like += 1
        # If explicit aggregate rows exist, weight by realized rows. If not, each scored row is one sample.
        agg.add(model, horizon, weight, hit=hit, pnl=pnl, mae=mae, rmse=rmse, coverage=coverage)
    rows = agg.rows(source)
    mode_name = "fast_streamed_scorecard" if rows else "schema_not_recognized_fast"
    log(f"[PARSE] done {source} raw_rows={raw} parsed_metric_rows={parsed_like} aggregate_rows={len(rows)}")
    return rows, mode_name, raw, fields


def context_from_key(key: Any, ctx: dict[str, Any]) -> dict[str, Any]:
    """Infer model/horizon context from nested JSON keys such as baseline_h5 or h1."""
    next_ctx = dict(ctx)
    text = str(key or "").strip()
    if not text:
        return next_ctx
    model, horizon = parse_model_horizon({"model/horizon": text})
    if model in {"baseline", "s2"}:
        next_ctx.setdefault("model", model)
    if horizon:
        next_ctx.setdefault("horizon", horizon)
    key_model = normalize_model(text)
    if key_model in {"baseline", "s2"}:
        next_ctx.setdefault("model", key_model)
    key_h = norm_horizon(text)
    if key_h.startswith("h") and key_h[1:].isdigit():
        next_ctx.setdefault("horizon", key_h)
    return next_ctx


def iter_dicts_with_context(obj: Any, ctx: dict[str, Any] | None = None):
    """Yield dictionaries with inherited model/horizon context for nested model_comparison JSON."""
    ctx = ctx or {}
    if isinstance(obj, dict):
        merged = dict(obj)
        for k, v in ctx.items():
            merged.setdefault(k, v)
        yield merged
        for key, value in obj.items():
            yield from iter_dicts_with_context(value, context_from_key(key, ctx))
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_dicts_with_context(item, ctx)


def extract_model_comparison(obj: Any, source: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen = set()
    # First pass: ordinary rows and context-inherited nested metrics.
    for d in iter_dicts_with_context(obj):
        row = parse_aggregate_score_row(d, source)
        if not row:
            continue
        sig = (row["model"], row["horizon"], row.get("direction_hit"), row.get("pnl_proxy"), row.get("mae"), row.get("source"))
        if sig in seen:
            continue
        seen.add(sig)
        out.append(row)
    return out


def extract_live_predictions(rows: list[dict[str, Any]], source: str, max_rows: int | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    source_rows = rows if max_rows is None else rows[:max_rows]
    for row in source_rows:
        ticker = first(get_any(row, "ticker"), get_any(row, "symbol"))
        horizon = norm_horizon(first(get_any(row, "horizon"), get_any(row, "target_horizon"), get_any(row, "days")))
        if not ticker:
            continue
        pred = first(get_any(row, "prediction"), get_any(row, "direction"), get_any(row, "trade_signal"), get_any(row, "action"), get_any(row, "side"))
        prob = finite_float(first(get_any(row, "probability"), get_any(row, "probability_up"), get_any(row, "confidence"), get_any(row, "p_up")))
        # Live prediction confidence sometimes arrives as 50.12 meaning 50.12%,
        # while finite_float("50.12%") returns 0.5012. Normalize display values to 0..1.
        if prob is not None:
            if 1.0 < prob <= 100.0:
                prob = prob / 100.0
            elif 100.0 < prob <= 10000.0:
                prob = prob / 10000.0
        exp_ret = finite_float(first(get_any(row, "expected_return"), get_any(row, "predicted_return"), get_any(row, "return"), get_any(row, "pnl_proxy")))
        close = finite_float(first(get_any(row, "asof_close"), get_any(row, "last_close"), get_any(row, "close")))
        asof = first(get_any(row, "asof_date"), get_any(row, "date"), get_any(row, "last_date"))
        out.append({
            "ticker": str(ticker),
            "horizon": horizon,
            "prediction": str(pred or ""),
            "probability": prob,
            "expected_return": exp_ret,
            "asof_date": str(asof or ""),
            "asof_close": close,
            "source": source,
        })
    return out



def cycle_batch_key(row: dict[str, Any], fallback_index: int = 0) -> str:
    """Best-effort refresh key for theater replay.

    Important: do not use per-topic relative labels like "47m ago" as refresh
    keys. That creates one-point "batches" and hides the dust field. The active
    wave is one real refresh batch; archive rows use timestamp-like keys only
    when the source actually exposes one.
    """
    source = str(row.get("source") or "cycle")
    if source == "cycle_active":
        return "cycle_active:current_wave"

    raw = str(row.get("batch_id") or row.get("generated_at") or row.get("as_of") or row.get("timestamp") or row.get("date") or "").strip()
    match = re.search(r"\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+-]+Z?)?", raw)
    if match:
        return f"{source}:{match.group(0).replace('T', ' ')[:16]}"

    # If the only date-ish value is a relative per-topic label, keep the rows
    # together as one source batch rather than splitting them into singletons.
    relative = str(row.get("newest_peak") or "").strip().lower()
    if relative and any(token in relative for token in ("ago", "active", "cooling", "plateau", "tail")):
        return f"{source}:current_wave"
    return f"{source}:current_wave"


def build_theater_batches(rows: list[dict[str, Any]], max_batches: int = 5) -> list[dict[str, Any]]:
    """Build compact real-cycle replay batches for the S2 Theater.

    No dummy rows: if the source cycle artifacts do not expose fitted rows, the
    returned list is empty and the frontend displays a strict empty state.
    """
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for i, row in enumerate(rows):
        topic = row.get("topic")
        beta = row.get("beta")
        lam = row.get("lambda_hours")
        # Theater requires the same fitted-cycle essentials as the scientific views.
        if not topic or beta is None or lam is None:
            continue
        key = cycle_batch_key(row, i)
        dust = row.get("dust")
        delta = row.get("delta_aic")
        n = row.get("n")
        retained = (delta is not None and float(delta) >= 6.0 and (dust is None or float(dust) <= 0.35))
        groups[key].append({
            "topic": topic,
            "phase": row.get("phase") or "",
            "n": n,
            "newest_peak": row.get("newest_peak") or "",
            "lambda_hours": lam,
            "beta": beta,
            "half_life_hours": row.get("half_life_hours"),
            "dust": dust,
            "delta_aic": delta,
            "source": row.get("source") or "",
            "beta_grid_min": row.get("beta_grid_min"),
            "beta_at_grid_floor": row.get("beta_at_grid_floor"),
            "beta_grid_version": row.get("beta_grid_version") or "",
            "retained": retained,
            "row_uid": stable_hash_int(topic, row.get("phase"), row.get("newest_peak"), row.get("source"), lam, beta, delta, n),
            "news_sample_scope": row.get("news_sample_scope") or "",
            "topic_news_pool_count": row.get("topic_news_pool_count"),
            "news_samples": row.get("news_samples") or [],
        })
    batches: list[dict[str, Any]] = []
    for key, rs in groups.items():
        # Stable score for sorting/replay: timestamp-like keys sort lexically; fallback groups preserve source index.
        retained_count = sum(1 for r in rs if r.get("retained"))
        dust_vals = [float(r["dust"]) for r in rs if r.get("dust") is not None]
        delta_vals = [float(r["delta_aic"]) for r in rs if r.get("delta_aic") is not None]
        batches.append({
            "batch_id": key,
            "rows": sorted(rs, key=lambda r: (str(r.get("topic")), float(r.get("lambda_hours") or 0.0))),
            "row_count": len(rs),
            "retained_count": retained_count,
            "dust_median": median(dust_vals),
            "delta_aic_median": median(delta_vals),
            "source_policy": "real-cycle-batches-only",
        })
    batches.sort(key=lambda b: str(b.get("batch_id")))
    return batches[-max_batches:]

TOPIC_TICKER_MAP: dict[str, set[str]] = {
    "Markets / Economy": {"SPY", "QQQ", "DIA", "IWM", "XLF", "KRE", "TLT", "IEF", "HYG", "LQD", "GLD", "UUP", "JPM", "BAC", "WFC", "C", "GS", "MS", "BLK", "SCHW"},
    "Energy": {"XLE", "USO", "UNG", "OIH", "XOP", "CVX", "XOM", "COP", "OXY", "SLB", "HAL", "MPC", "VLO", "PSX", "KMI", "LNG", "NEE", "DUK", "SO"},
    "Cybersecurity": {"CRWD", "PANW", "FTNT", "ZS", "OKTA", "S", "CHKP", "CYBR", "NET", "TENB", "RPD", "GEN", "AKAM", "BB", "QLYS"},
    "AI / Tech": {"NVDA", "AMD", "MSFT", "GOOGL", "GOOG", "META", "AMZN", "AVGO", "TSM", "ASML", "SMCI", "ARM", "PLTR", "MU", "QCOM", "INTC", "ORCL", "CRM", "ADBE", "SNOW", "NOW"},
    "Geopolitics": {"ITA", "XAR", "LMT", "RTX", "NOC", "GD", "LHX", "BA", "KTOS", "HII", "XLE", "USO", "GLD", "UUP", "TLT"},
    "Politics / Elections": {"SPY", "XLF", "XLV", "XLE", "XLU", "ITA", "LMT", "UNH", "HUM", "CI", "PFE", "MRK", "NEE", "DUK"},
    "Public Health": {"XLV", "PFE", "MRK", "LLY", "UNH", "ABBV", "JNJ", "TMO", "ABT", "BMY", "GILD", "REGN", "CVS", "HUM", "CI", "MDT"},
    "Climate / Weather": {"XLE", "XLU", "TAN", "FAN", "ICLN", "NEE", "DUK", "SO", "AEP", "ENPH", "FSLR", "SEDG", "ADM", "DE", "MOS", "CF", "TRV", "ALL", "CB"},
    "Space / Science": {"ARKX", "ITA", "XAR", "LMT", "NOC", "RTX", "BA", "HON", "IRDM", "RKLB", "PL", "SPIR", "LHX", "TDY"},
    "Culture / Media": {"XLC", "META", "GOOGL", "GOOG", "NFLX", "DIS", "WBD", "PARA", "ROKU", "SPOT", "TTD", "SNAP", "PINS", "RBLX"},
    "Quantum tech": {"IBM", "IONQ", "RGTI", "QBTS", "GOOGL", "MSFT", "NVDA", "AMAT", "LRCX", "TER", "INTC", "QCOM"},
    "General": {"SPY", "QQQ", "IWM", "DIA", "TLT", "GLD", "UUP", "HYG", "LQD"},
}


def topic_symbol_match(topic: str, ticker: Any) -> bool:
    symbol = str(ticker or "").upper().strip()
    if not symbol:
        return False
    allowed = TOPIC_TICKER_MAP.get(str(topic or ""), set())
    return symbol in allowed


def topic_live_universe(topic: str) -> list[str]:
    return sorted(TOPIC_TICKER_MAP.get(str(topic or ""), set()))


def build_theater_trade_signals(coupling_rows: list[dict[str, Any]], live_predictions: list[dict[str, Any]], max_per_topic: int = 8) -> list[dict[str, Any]]:
    """Attach topic-specific real live prediction rows to theater topics.

    Earlier versions displayed the same top horizon tickers for every topic,
    because live_predictions has no native news-topic label. This version is
    stricter: it only links real live rows whose ticker is in a transparent
    topic watchlist. If no matching ticker exists, the tooltip says so instead
    of recycling generic tickers.
    """
    usable_couplings: list[dict[str, Any]] = []
    for c in coupling_rows or []:
        h = str(c.get("horizon") or "")
        if h == "h1" or c.get("status") not in {"candidate coupling", "mixed coupling"}:
            continue
        usable_couplings.append(c)
    if not usable_couplings or not live_predictions:
        return []

    by_horizon: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in live_predictions:
        h = str(row.get("horizon") or "")
        if not h or h == "h1":
            continue
        by_horizon[h].append(row)
    for h, rows in by_horizon.items():
        rows.sort(key=lambda r: (
            abs(float(r.get("expected_return") or 0.0)),
            float(r.get("probability") or 0.0),
        ), reverse=True)

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for c in sorted(usable_couplings, key=lambda r: float(r.get("coupling_score") or 0.0), reverse=True):
        topic = str(c.get("topic") or "")
        h = str(c.get("horizon") or "")
        if not topic or not h or (topic, h) in seen:
            continue
        seen.add((topic, h))
        matched = [row for row in by_horizon.get(h, []) if topic_symbol_match(topic, row.get("ticker"))]
        signals = []
        for row in matched[:max_per_topic]:
            signals.append({
                "ticker": row.get("ticker"),
                "horizon": h,
                "prediction": row.get("prediction"),
                "expected_return": row.get("expected_return"),
                "probability": row.get("probability"),
                "asof_date": row.get("asof_date"),
                "asof_close": row.get("asof_close"),
                "link_policy": "topic_watchlist_symbol_match",
            })
        out.append({
            "topic": topic,
            "horizon": h,
            "coupling_score": c.get("coupling_score"),
            "delta_hit": c.get("delta_hit"),
            "delta_pnl": c.get("delta_pnl"),
            "status": c.get("status"),
            "signals": signals,
            "topic_live_universe": topic_live_universe(topic)[:24],
            "source_policy": "real-live-predictions-only; topic-watchlist filtered; no generic ticker reuse",
        })
    return out


def _radar_pressure(row: dict[str, Any]) -> float:
    """Narrative pressure for the Filament Radar.

    This is an early-warning visualization score, not a trading score. It uses
    only fitted cycle-row fields already parsed from the public cycle artifacts.
    """
    dust = row.get("dust")
    dust_quality = 0.50 if dust is None else 1.0 - max(0.0, min(1.0, float(dust) / 0.50))
    delta = row.get("delta_aic") or 0.0
    support = max(0.0, min(1.0, float(delta) / 35.0))
    beta = row.get("beta")
    beta_stickiness = 0.50 if beta is None else max(0.0, min(1.0, (0.80 - float(beta)) / 0.70))
    lam = row.get("lambda_hours") or 0.0
    scale_quality = max(0.0, min(1.0, math.log1p(float(lam)) / math.log1p(168.0)))
    retained = 1.0 if (float(delta) >= 6.0 and (dust is None or float(dust) <= 0.35)) else 0.0
    return 100.0 * (0.40 * support + 0.23 * dust_quality + 0.15 * beta_stickiness + 0.12 * scale_quality + 0.10 * retained)


def _business_vector_for_topic(topic: str) -> str:
    t = str(topic or "")
    mapping = {
        "Markets / Economy": "rates, credit, banks, broad index risk",
        "Energy": "oil, gas, utilities, inflation-sensitive sectors",
        "Cybersecurity": "security software, insurers, defense, enterprise risk",
        "AI / Tech": "semis, cloud, software, capex, regulation",
        "Geopolitics": "defense, energy, currencies, risk-off assets",
        "Politics / Elections": "regulation, taxes, healthcare, energy, financials",
        "Public Health": "pharma, healthcare services, labor-sensitive sectors",
        "Climate / Weather": "energy, agriculture, insurance, logistics",
        "Space / Science": "aerospace, defense, satellites, advanced tech",
        "Culture / Media": "media platforms, consumer attention, ad-sensitive names",
        "Quantum tech": "speculative tech, semis, defense research",
        "General": "broad macro narrative field",
    }
    return mapping.get(t, "topic-linked watchlist; map to sector exposures manually")


EVENT_CLASS_RULES: list[dict[str, Any]] = [
    {
        "event_watch_class": "M&A / large strategic transaction watch",
        "impending_read": "retained business-news filament contains deal/strategic-transaction language; inspect merger, acquisition, divestiture, antitrust, financing and board-review evidence",
        "keywords": ["merger", "acquisition", "acquire", "buyout", "takeover", "deal talks", "strategic alternatives", "strategic review", "activist", "private equity", "antitrust", "divest", "spin off", "spin-off", "tender offer", "board review"],
    },
    {
        "event_watch_class": "unusual earnings / guidance surprise watch",
        "impending_read": "retained business-news filament contains earnings/guidance language; inspect revenue, margin, EPS, inventory, demand, channel checks and analyst-revision evidence",
        "keywords": ["earnings", "guidance", "revenue", "margin", "eps", "profit warning", "preannounce", "pre-announcement", "outlook", "forecast", "analyst revision", "channel check", "inventory", "demand", "pricing power", "beat estimates", "miss estimates"],
    },
    {
        "event_watch_class": "credit / liquidity stress watch",
        "impending_read": "retained filament contains credit/liquidity stress language; inspect refinancing, covenants, downgrade, default, funding, capital raise and bank/credit spillover evidence",
        "keywords": ["debt", "refinancing", "covenant", "liquidity", "downgrade", "default", "bankruptcy", "restructuring", "funding", "credit", "capital raise", "deposit", "solvency", "going concern"],
    },
    {
        "event_watch_class": "regulatory / antitrust action watch",
        "impending_read": "retained filament contains regulatory/enforcement language; inspect agency, probe, lawsuit, compliance, monopoly, antitrust and consent-order evidence",
        "keywords": ["regulator", "regulatory", "investigation", "probe", "antitrust", "ftc", "doj", "sec", "lawsuit", "compliance", "monopoly", "consent order", "enforcement", "fine", "ban", "approval"],
    },
    {
        "event_watch_class": "supply-chain disruption watch",
        "impending_read": "retained filament contains supply-chain bottleneck language; inspect shortage, supplier, shipping, port, strike, export-control, tariff and backlog evidence",
        "keywords": ["supply chain", "shortage", "supplier", "shipping", "port", "strike", "export control", "tariff", "backlog", "bottleneck", "logistics", "freight", "disruption", "factory shutdown"],
    },
    {
        "event_watch_class": "product / demand shock watch",
        "impending_read": "retained filament contains product/demand shock language; inspect recall, defect, launch, preorder, cancellation, channel inventory, adoption and customer-demand evidence",
        "keywords": ["recall", "defect", "product launch", "launch", "preorder", "pre-order", "cancellation", "cancelled", "demand shock", "demand surge", "demand slump", "channel inventory", "adoption", "customer complaints"],
    },
    {
        "event_watch_class": "capex cycle / infrastructure buildout watch",
        "impending_read": "retained filament contains capex/infrastructure language; inspect AI chips, data centers, cloud spend, factories, grid/power demand and supplier-order evidence",
        "keywords": ["capex", "capital spending", "data center", "datacenter", "ai chip", "gpu", "cloud spend", "factory", "foundry", "grid", "power demand", "infrastructure", "buildout", "orders"],
    },
    {
        "event_watch_class": "management / governance transition watch",
        "impending_read": "retained filament contains governance language; inspect CEO/CFO change, board, activist, succession, resignation, investigation and proxy evidence",
        "keywords": ["ceo", "cfo", "board", "succession", "resignation", "resigns", "activist investor", "proxy", "governance", "chairman", "executive change", "management change"],
    },
]


def _keyword_hits(text: str, keywords: list[str]) -> int:
    hay = text_norm(text)
    hits = 0
    for kw in keywords:
        needle = text_norm(kw)
        if needle and needle in hay:
            hits += 1
    return hits


def _event_watch_for_topic(topic: str, news_samples: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Research-safe event-class hint for narrative outliers.

    This labels the *kind* of future-facing shadow that a retained public-information
    filament resembles. It is deliberately not Cassandra: no certainty, no intent
    attribution, no trade instruction. Keyword hits are taken only from real source
    rows already present in cycle artifacts.
    """
    text_parts = [str(topic or "")]
    for sample in news_samples or []:
        text_parts.append(str(sample.get("title") or ""))
        text_parts.append(str(sample.get("summary") or ""))
        text_parts.append(str(sample.get("source") or ""))
    combined = " ".join(text_parts)

    best_rule: dict[str, Any] | None = None
    best_hits = 0
    for rule in EVENT_CLASS_RULES:
        hits = _keyword_hits(combined, rule["keywords"])
        if hits > best_hits:
            best_hits = hits
            best_rule = rule
    if best_rule and best_hits > 0:
        return {
            "event_watch_class": best_rule["event_watch_class"],
            "impending_read": best_rule["impending_read"],
            "event_keyword_hits": best_hits,
            "event_basis": "headline/source keyword evidence",
        }

    t = str(topic or "").lower()
    if "geopolitic" in t:
        return {"event_watch_class": "military / sanctions / risk-off watch", "impending_read": "geopolitical filament is concentrating; inspect conflict, sanctions, defense, energy and currency narratives", "event_keyword_hits": 0, "event_basis": "topic prior"}
    if "politic" in t or "election" in t:
        return {"event_watch_class": "policy / regulation watch", "impending_read": "policy narrative is concentrating; inspect regulation, taxes, elections, court and agency language", "event_keyword_hits": 0, "event_basis": "topic prior"}
    if "market" in t or "econom" in t:
        return {"event_watch_class": "economic policy / credit / rates watch", "impending_read": "macro/business filament is concentrating; inspect inflation, rates, banks, credit, earnings and broad risk narratives", "event_keyword_hits": 0, "event_basis": "topic prior"}
    if "energy" in t:
        return {"event_watch_class": "energy shock / inflation / supply watch", "impending_read": "energy filament is concentrating; inspect supply shock, oil/gas, utilities, inflation and geopolitical spillovers", "event_keyword_hits": 0, "event_basis": "topic prior"}
    if "cyber" in t:
        return {"event_watch_class": "cyber incident / infrastructure-risk watch", "impending_read": "cybersecurity filament is concentrating; inspect infrastructure, insurers, software, defense and incident-response narratives", "event_keyword_hits": 0, "event_basis": "topic prior"}
    if "health" in t:
        return {"event_watch_class": "public-health / labor-disruption watch", "impending_read": "health filament is concentrating; inspect public-health, healthcare capacity, labor and policy-response narratives", "event_keyword_hits": 0, "event_basis": "topic prior"}
    if "climate" in t or "weather" in t:
        return {"event_watch_class": "weather shock / insurance / logistics watch", "impending_read": "climate/weather filament is concentrating; inspect storms, heat, crops, insurance and logistics narratives", "event_keyword_hits": 0, "event_basis": "topic prior"}
    if "ai" in t or "tech" in t:
        return {"event_watch_class": "AI/tech capex / regulation / supply-chain watch", "impending_read": "AI/tech filament is concentrating; inspect semis, cloud capex, regulation, export controls, product demand and supply-chain narratives", "event_keyword_hits": 0, "event_basis": "topic prior"}
    if "space" in t or "science" in t:
        return {"event_watch_class": "aerospace / defense / science-policy watch", "impending_read": "space/science filament is concentrating; inspect aerospace, defense, satellites, launch, science-policy and funding narratives", "event_keyword_hits": 0, "event_basis": "topic prior"}
    return {"event_watch_class": "broad narrative-regime watch", "impending_read": "retained narrative filament is concentrating; inspect source rows for emerging real-world event class", "event_keyword_hits": 0, "event_basis": "topic prior"}

def build_narrative_radar(theater_batches: list[dict[str, Any]], theater_trade_signals: list[dict[str, Any]], max_rows: int = 24) -> list[dict[str, Any]]:
    """Build a First-Notice / rumor-field radar from real cycle batches.

    This is the core redesign: rank the small, fast, retained public shadow,
    not the obvious large public hub. The score rewards acceleration from a
    low base, dust clearing, source-diversity widening, semantic convergence,
    low saturation, and retained S2 tail strength; it subtracts an
    already-obvious-hub penalty. It does not claim access to private info,
    infer intent, or issue trades.
    """
    if not theater_batches:
        return []

    def clamp01(value: float | None) -> float:
        if value is None or not math.isfinite(float(value)):
            return 0.0
        return max(0.0, min(1.0, float(value)))

    def source_set(rows: list[dict[str, Any]]) -> set[str]:
        out: set[str] = set()
        for rr in rows:
            src = str(rr.get("source") or "").strip()
            if src:
                out.add(src)
            for sample in rr.get("news_samples") or []:
                src2 = str(sample.get("source") or sample.get("artifact") or "").strip()
                if src2:
                    out.add(src2)
        return out

    signals_by_topic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in theater_trade_signals or []:
        topic = str(item.get("topic") or "")
        if topic:
            signals_by_topic[topic].append(item)

    by_topic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for batch_index, batch in enumerate(theater_batches):
        batch_id = str(batch.get("batch_id") or f"batch_{batch_index}")
        for row in batch.get("rows", []) or []:
            topic = str(row.get("topic") or "")
            if not topic:
                continue
            r = dict(row)
            r["batch_id"] = batch_id
            r["batch_index"] = batch_index
            r["pressure"] = _radar_pressure(r)
            by_topic[topic].append(r)

    out: list[dict[str, Any]] = []
    for topic, rows in by_topic.items():
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            groups[str(r.get("batch_id"))].append(r)
        series: list[dict[str, Any]] = []
        for batch_id, rs in groups.items():
            pressures = [float(x.get("pressure") or 0.0) for x in rs]
            dusts = [float(x["dust"]) for x in rs if x.get("dust") is not None]
            deltas = [float(x["delta_aic"]) for x in rs if x.get("delta_aic") is not None]
            betas = [float(x["beta"]) for x in rs if x.get("beta") is not None]
            lambdas = [float(x["lambda_hours"]) for x in rs if x.get("lambda_hours") is not None]
            retained_count = sum(1 for x in rs if x.get("retained") or ((x.get("delta_aic") or 0) >= 6 and (x.get("dust") is None or (x.get("dust") or 0) <= 0.35)))
            srcs = source_set(rs)
            series.append({
                "batch_id": batch_id,
                "batch_index": min(int(x.get("batch_index") or 0) for x in rs),
                "pressure": sum(pressures) / len(pressures) if pressures else 0.0,
                "dust": median(dusts),
                "delta_aic": median(deltas),
                "beta": mode(betas),
                "lambda_hours": median(lambdas),
                "rows": len(rs),
                "retained_count": retained_count,
                "source_diversity": len(srcs),
            })
        series.sort(key=lambda x: (x.get("batch_index", 0), str(x.get("batch_id"))))
        if not series:
            continue

        first_s = series[0]
        last_s = series[-1]
        prev_s = series[-2] if len(series) >= 2 else first_s
        prev2_s = series[-3] if len(series) >= 3 else prev_s
        first_pressure = float(first_s.get("pressure") or 0.0)
        current_pressure = float(last_s.get("pressure") or 0.0)
        growth = current_pressure - first_pressure
        growth_multiple = current_pressure / max(1.0, first_pressure)
        recent_velocity = current_pressure - float(prev_s.get("pressure") or 0.0)
        prior_velocity = float(prev_s.get("pressure") or 0.0) - float(prev2_s.get("pressure") or 0.0)
        acceleration = recent_velocity - prior_velocity
        dust_change = (last_s.get("dust") or 0.0) - (first_s.get("dust") or 0.0) if last_s.get("dust") is not None and first_s.get("dust") is not None else None
        delta_aic_change = (last_s.get("delta_aic") or 0.0) - (first_s.get("delta_aic") or 0.0) if last_s.get("delta_aic") is not None and first_s.get("delta_aic") is not None else None
        source_diversity_now = int(last_s.get("source_diversity") or 0)
        source_diversity_first = int(first_s.get("source_diversity") or 0)
        source_diversity_growth = source_diversity_now - source_diversity_first
        retained_share = (sum(float(s.get("retained_count") or 0) for s in series) / max(1.0, sum(float(s.get("rows") or 0) for s in series)))

        signal_links = signals_by_topic.get(topic, [])
        news_samples: list[dict[str, Any]] = []
        seen_titles: set[str] = set()
        # Prefer most recent rows first so the radar shows what is brewing now.
        for rr in sorted(rows, key=lambda x: int(x.get("batch_index") or 0), reverse=True):
            for sample in rr.get("news_samples") or []:
                title = str(sample.get("title") or "").strip()
                if title and title.lower() not in seen_titles:
                    news_samples.append(sample)
                    seen_titles.add(title.lower())
                if len(news_samples) >= 6:
                    break
            if len(news_samples) >= 6:
                break

        linked_signal_count = sum(len(x.get("signals") or []) for x in signal_links)
        horizons = sorted({str(x.get("horizon") or "") for x in signal_links if x.get("horizon")})
        top_tickers: list[str] = []
        for x in signal_links:
            for sig in x.get("signals") or []:
                tic = str(sig.get("ticker") or "").strip()
                if tic and tic not in top_tickers:
                    top_tickers.append(tic)
                if len(top_tickers) >= 8:
                    break
            if len(top_tickers) >= 8:
                break

        market_hint = _business_vector_for_topic(topic)
        event_hint = _event_watch_for_topic(topic, news_samples)
        keyword_hits = int(event_hint.get("event_keyword_hits", 0) or 0)

        # Components in 0..100-ish terms, then combined below.
        acceleration_component = 100.0 * clamp01(max(0.0, acceleration) / 28.0)
        growth_component = 100.0 * clamp01((growth_multiple - 1.0) / 4.0)
        dust_clearing_component = 100.0 * clamp01((-(dust_change or 0.0)) / 0.22)
        novelty_component = 100.0 * clamp01((growth_multiple - 0.90) / 2.75) * clamp01((78.0 - current_pressure) / 62.0)
        source_diversity_component = 100.0 * clamp01((max(0, source_diversity_growth) + min(source_diversity_now, 8) * 0.35) / 5.0)
        semantic_convergence_component = 100.0 * clamp01((keyword_hits + retained_share * 2.0 + min(len(horizons), 3) * 0.45) / 4.0)
        low_saturation_bonus = 100.0 * clamp01((72.0 - current_pressure) / 58.0) * clamp01((growth_multiple - 1.0) / 3.5)
        beta = last_s.get("beta")
        beta_slow_tail = 0.50 if beta is None else clamp01((0.85 - float(beta)) / 0.75)
        lambda_quality = clamp01(math.log1p(float(last_s.get("lambda_hours") or 0.0)) / math.log1p(168.0))
        retained_tail_strength = 100.0 * clamp01(0.52 * retained_share + 0.28 * beta_slow_tail + 0.20 * lambda_quality)

        current_size = float(last_s.get("rows") or 0.0)
        current_size_norm = clamp01(current_size / 12.0)
        already_obvious_penalty = 100.0 * clamp01(
            0.32 * clamp01(current_pressure / 92.0)
            + 0.22 * clamp01(sum(float(s.get("rows") or 0) for s in series) / 46.0)
            + 0.14 * clamp01(source_diversity_now / 10.0)
            + 0.14 * clamp01(linked_signal_count / 20.0)
            + 0.10 * current_size_norm
            + 0.08 * clamp01(len(news_samples) / 6.0)
        )

        early_score = max(0.0, min(100.0,
            0.18 * acceleration_component
            + 0.18 * growth_component
            + 0.14 * dust_clearing_component
            + 0.12 * novelty_component
            + 0.11 * source_diversity_component
            + 0.10 * semantic_convergence_component
            + 0.09 * low_saturation_bonus
            + 0.08 * retained_tail_strength
            - 0.34 * already_obvious_penalty
        ))

        hub_score = max(0.0, min(100.0,
            0.44 * current_pressure
            + 0.22 * max(0.0, growth)
            + 0.14 * max(0.0, recent_velocity)
            + 18.0 * retained_share
            + min(12.0, linked_signal_count * 1.5)
        ))
        injection_score = max(0.0, min(100.0,
            0.52 * current_pressure
            + 1.15 * max(0.0, recent_velocity)
            + 0.65 * max(0.0, acceleration)
            + (10.0 if dust_change is not None and dust_change < -0.03 else 0.0)
            + min(8.0, linked_signal_count)
        ))

        is_established = already_obvious_penalty >= 55.0 and current_pressure >= 64.0 and early_score < 62.0
        if is_established:
            first_notice_status = "established hub"
        elif early_score >= 72.0 and (keyword_hits > 0 or linked_signal_count > 0):
            first_notice_status = "possible pre-event shadow"
        elif early_score >= 64.0:
            first_notice_status = "outlier filament"
        elif early_score >= 45.0:
            first_notice_status = "emerging filament"
        elif current_pressure >= 52.0:
            first_notice_status = "retained filament"
        elif current_pressure >= 35.0 or recent_velocity > 5.0:
            first_notice_status = "watch"
        else:
            first_notice_status = "dust / weak"

        # Keep legacy radar_status compatible but map it to the new intent.
        radar_status = first_notice_status
        if first_notice_status == "established hub":
            why_not_obvious = "large public hub; keep as context, not first-notice edge"
        else:
            why_bits = []
            if growth_multiple > 1.15:
                why_bits.append(f"growth from low base x{growth_multiple:.2f}")
            if acceleration > 0:
                why_bits.append(f"positive acceleration {acceleration:.1f}")
            if dust_change is not None and dust_change < 0:
                why_bits.append(f"dust clearing {dust_change:.3f}")
            if source_diversity_growth > 0:
                why_bits.append(f"source diversity +{source_diversity_growth}")
            why_bits.append(f"obvious penalty {already_obvious_penalty:.1f}")
            why_not_obvious = "; ".join(why_bits)

        public_shadow_read = (
            "public shadow of non-public accumulation" if first_notice_status in {"possible pre-event shadow", "outlier filament", "emerging filament"}
            else "public hub/context" if first_notice_status == "established hub"
            else "retained public-information field"
        )
        next_signal = "watch only"
        if first_notice_status in {"possible pre-event shadow", "outlier filament", "emerging filament"}:
            next_signal = f"watch {market_hint}; inspect source rows and non-h1 live vectors"
        elif first_notice_status == "retained filament":
            next_signal = f"retained narrative present; monitor {market_hint}"

        out.append({
            "topic": topic,
            "radar_status": radar_status,
            "first_notice_status": first_notice_status,
            "first_notice_candidate": first_notice_status in {"possible pre-event shadow", "outlier filament", "emerging filament"},
            "public_shadow_read": public_shadow_read,
            "event_watch_class": event_hint.get("event_watch_class"),
            "impending_read": event_hint.get("impending_read"),
            "event_keyword_hits": keyword_hits,
            "event_basis": event_hint.get("event_basis", "topic prior"),
            "current_pressure": current_pressure,
            "current_size": current_size,
            "growth": growth,
            "growth_multiple": growth_multiple,
            "recent_velocity": recent_velocity,
            "acceleration": acceleration,
            "dust_change": dust_change,
            "delta_aic_change": delta_aic_change,
            "source_diversity_now": source_diversity_now,
            "source_diversity_first": source_diversity_first,
            "source_diversity_growth": source_diversity_growth,
            "hub_score": hub_score,
            "injection_score": injection_score,
            "early_score": early_score,
            "first_notice_score": early_score,
            "retained_share": retained_share,
            "low_saturation_bonus": low_saturation_bonus,
            "retained_tail_strength": retained_tail_strength,
            "already_obvious_penalty": already_obvious_penalty,
            "score_components": {
                "acceleration": acceleration_component,
                "growth_rate": growth_component,
                "dust_clearing": dust_clearing_component,
                "novelty": novelty_component,
                "source_diversity_growth": source_diversity_component,
                "semantic_convergence": semantic_convergence_component,
                "low_saturation_bonus": low_saturation_bonus,
                "retained_tail_strength": retained_tail_strength,
                "already_obvious_penalty": already_obvious_penalty,
            },
            "last_beta": last_s.get("beta"),
            "last_lambda_hours": median([x.get("lambda_hours") for x in rows if x.get("lambda_hours") is not None]),
            "last_dust": last_s.get("dust"),
            "last_delta_aic": last_s.get("delta_aic"),
            "batches_seen": len(series),
            "rows_seen": len(rows),
            "business_vector": market_hint,
            "next_market_signal_watch": next_signal,
            "why_not_obvious_hub": why_not_obvious,
            "linked_live_signal_count": linked_signal_count,
            "linked_horizons": horizons,
            "top_live_tickers": top_tickers,
            "news_samples": news_samples,
            "series": series,
            "source_policy": "real-cycle-batches-only; First-Notice favors low-footprint accelerating retained filaments; no private-info claim; no inferred intent; no trade instruction",
        })
    out.sort(key=lambda r: (float(r.get("early_score") or 0.0), float(r.get("acceleration") or 0.0), float(r.get("growth_multiple") or 0.0)), reverse=True)
    return out[:max_rows]

def summarize_topics(rows: list[dict[str, Any]], beta_floor: float, legacy_beta_floor: float | None = None, expanded_beta_min: float | None = None) -> list[dict[str, Any]]:
    legacy_beta_floor = beta_floor if legacy_beta_floor is None else legacy_beta_floor
    expanded_beta_min = beta_floor if expanded_beta_min is None else expanded_beta_min
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["topic"]].append(row)
    summaries: list[dict[str, Any]] = []
    for topic, rs in grouped.items():
        betas = [r.get("beta") for r in rs if r.get("beta") is not None]
        lambdas = [r.get("lambda_hours") for r in rs if r.get("lambda_hours") is not None]
        halfs = [r.get("half_life_hours") for r in rs if r.get("half_life_hours") is not None]
        dusts_all = [r.get("dust") for r in rs if r.get("dust") is not None]
        # If the source supplies both zero and non-zero dust for same topic, prefer non-zero fitted dust values.
        dusts_pos = [d for d in dusts_all if d is not None and d > 1e-9]
        dusts = dusts_pos if dusts_pos else dusts_all
        deltas = [r.get("delta_aic") for r in rs if r.get("delta_aic") is not None]
        phases = [str(r.get("phase") or "").lower() for r in rs]
        s2_likely = sum("s2 likely" in p for p in phases)
        s2_any = sum("s2" in p for p in phases)
        beta_floor_count = sum(abs(float(b) - beta_floor) <= 1e-9 for b in betas)
        beta_floor_share = beta_floor_count / len(betas) if betas else None
        legacy_beta_floor_count = sum(abs(float(b) - legacy_beta_floor) <= 1e-9 for b in betas)
        legacy_beta_floor_share = legacy_beta_floor_count / len(betas) if betas else None
        expanded_beta_min_count = sum(abs(float(b) - expanded_beta_min) <= 1e-9 for b in betas)
        expanded_beta_min_share = expanded_beta_min_count / len(betas) if betas else None
        beta_below_legacy_share = sum(float(b) < legacy_beta_floor - 1e-9 for b in betas) / len(betas) if betas else None
        delta_m = median(deltas)
        dust_m = median(dusts)
        likely_share = s2_likely / len(rs) if rs else 0.0
        support = max(0.0, min(1.0, (delta_m or 0.0) / 25.0))
        dust_quality = None if dust_m is None else 1.0 - max(0.0, min(1.0, dust_m / 0.50))
        sample_quality = max(0.0, min(1.0, math.log10(len(rs) + 1) / 2.0))
        retained_pressure = 100.0 * (
            0.42 * support
            + 0.22 * (dust_quality if dust_quality is not None else 0.50)
            + 0.24 * likely_share
            + 0.12 * sample_quality
        )
        # beta_floor_share is the legacy 0.35-watch share, not necessarily the true grid floor.
        # After the upstream cycle app expands below 0.35, exact beta should be read from beta_mode,
        # while these shares diagnose whether old/new boundaries still dominate.
        if expanded_beta_min_share is not None and expanded_beta_min_share >= 0.75:
            beta_verdict = "new-grid-floor"
        elif legacy_beta_floor_share is not None and legacy_beta_floor_share >= 0.75 and (beta_below_legacy_share or 0.0) < 0.10:
            beta_verdict = "0.35-cluster"
        elif beta_below_legacy_share is not None and beta_below_legacy_share >= 0.25:
            beta_verdict = "below-0.35"
        else:
            beta_verdict = "varied"
        dust_audit = "ok"
        if dusts_all and not dusts_pos:
            dust_audit = "all-zero-or-missing"
        elif dusts_all and len(dusts_pos) / len(dusts_all) < 0.25:
            dust_audit = "mostly-zero"
        summaries.append({
            "topic": topic,
            "cycle_rows": len(rs),
            "s2_likely_rows": s2_likely,
            "s2_any_rows": s2_any,
            "s2_likely_share": likely_share,
            "lambda_median_hours": median(lambdas),
            "half_life_median_hours": median(halfs),
            "beta_mode": mode(betas),
            "beta_median": median(betas),
            "beta_floor_share": beta_floor_share,
            "legacy_beta_floor_share": legacy_beta_floor_share,
            "beta_035_share": legacy_beta_floor_share,
            "expanded_beta_min_share": expanded_beta_min_share,
            "new_grid_floor_share": expanded_beta_min_share,
            "beta_below_legacy_share": beta_below_legacy_share,
            "beta_min_observed": min(betas) if betas else None,
            "beta_verdict": beta_verdict,
            "dust_median": dust_m,
            "dust_nonzero_share": len(dusts_pos) / len(dusts_all) if dusts_all else None,
            "dust_audit": dust_audit,
            "delta_aic_median": delta_m,
            "retained_pressure_score": retained_pressure,
            "sources": sorted({r.get("source") for r in rs if r.get("source")}),
        })
    summaries.sort(key=lambda x: (x.get("retained_pressure_score") or 0.0), reverse=True)
    return summaries


def group_scores(rows: list[dict[str, Any]], preferred_sources: list[str]) -> list[dict[str, Any]]:
    """Deduplicate aggregate score rows by source priority + model/horizon."""
    priority = {src: idx for idx, src in enumerate(preferred_sources)}
    chosen: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r["model"], r["horizon"])
        prev = chosen.get(key)
        if prev is None or priority.get(r.get("source", ""), 99) < priority.get(prev.get("source", ""), 99):
            chosen[key] = r
    return list(chosen.values())


def summarize_market(score_rows: list[dict[str, Any]], source_label: str) -> list[dict[str, Any]]:
    by_h: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in score_rows:
        by_h[row["horizon"]].append(row)
    out: list[dict[str, Any]] = []
    for horizon, rows in by_h.items():
        base = next((r for r in rows if r["model"] == "baseline"), None)
        s2 = next((r for r in rows if r["model"] == "s2"), None)
        best = max(rows, key=lambda r: ((r.get("direction_hit") if r.get("direction_hit") is not None else -999), (r.get("pnl_proxy") if r.get("pnl_proxy") is not None else -999)))
        row = {
            "horizon": horizon,
            "models": rows,
            "score_source": source_label,
            "best_model": best.get("model"),
            "best_hit": best.get("direction_hit"),
            "best_pnl": best.get("pnl_proxy"),
            "best_mae": best.get("mae"),
            "realized_rows": max([r.get("realized_rows") or 0 for r in rows]) or None,
        }
        if base and s2:
            row.update({
                "baseline_hit": base.get("direction_hit"),
                "s2_hit": s2.get("direction_hit"),
                "delta_hit": (s2.get("direction_hit") - base.get("direction_hit")) if s2.get("direction_hit") is not None and base.get("direction_hit") is not None else None,
                "baseline_pnl": base.get("pnl_proxy"),
                "s2_pnl": s2.get("pnl_proxy"),
                "delta_pnl": (s2.get("pnl_proxy") - base.get("pnl_proxy")) if s2.get("pnl_proxy") is not None and base.get("pnl_proxy") is not None else None,
                "baseline_mae": base.get("mae"),
                "s2_mae": s2.get("mae"),
                "delta_mae": (s2.get("mae") - base.get("mae")) if s2.get("mae") is not None and base.get("mae") is not None else None,
                "baseline_avg_win": base.get("avg_win_proxy"),
                "s2_avg_win": s2.get("avg_win_proxy"),
                "baseline_avg_loss": base.get("avg_loss_proxy"),
                "s2_avg_loss": s2.get("avg_loss_proxy"),
                "baseline_win_loss_ratio": base.get("win_loss_ratio"),
                "s2_win_loss_ratio": s2.get("win_loss_ratio"),
                "baseline_cumulative_pnl": base.get("cumulative_pnl_proxy"),
                "s2_cumulative_pnl": s2.get("cumulative_pnl_proxy"),
                "delta_cumulative_pnl": (s2.get("cumulative_pnl_proxy") - base.get("cumulative_pnl_proxy")) if s2.get("cumulative_pnl_proxy") is not None and base.get("cumulative_pnl_proxy") is not None else None,
                "baseline_worst_pnl": base.get("worst_pnl_proxy"),
                "s2_worst_pnl": s2.get("worst_pnl_proxy"),
                "baseline_best_pnl": base.get("best_pnl_proxy"),
                "s2_best_pnl": s2.get("best_pnl_proxy"),
            })
        out.append(row)
    def hsort(x: dict[str, Any]) -> int:
        h = x.get("horizon", "")
        return int(h[1:]) if isinstance(h, str) and h.startswith("h") and h[1:].isdigit() else 999
    out.sort(key=hsort)
    return out


def build_pnl_audit(horizon_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create compact postfactum PnL audit rows from scored horizons.

    This is still a directional paper-PnL proxy, not executable portfolio PnL.
    It exists to show whether better direction accuracy also survives as payoff.
    """
    out: list[dict[str, Any]] = []
    for h in horizon_rows:
        out.append({
            "horizon": h.get("horizon"),
            "score_source": h.get("score_source"),
            "realized_rows": h.get("realized_rows"),
            "baseline_hit": h.get("baseline_hit"),
            "s2_hit": h.get("s2_hit"),
            "delta_hit": h.get("delta_hit"),
            "baseline_pnl": h.get("baseline_pnl"),
            "s2_pnl": h.get("s2_pnl"),
            "delta_pnl": h.get("delta_pnl"),
            "baseline_avg_win": h.get("baseline_avg_win"),
            "s2_avg_win": h.get("s2_avg_win"),
            "baseline_avg_loss": h.get("baseline_avg_loss"),
            "s2_avg_loss": h.get("s2_avg_loss"),
            "baseline_win_loss_ratio": h.get("baseline_win_loss_ratio"),
            "s2_win_loss_ratio": h.get("s2_win_loss_ratio"),
            "baseline_cumulative_pnl": h.get("baseline_cumulative_pnl"),
            "s2_cumulative_pnl": h.get("s2_cumulative_pnl"),
            "delta_cumulative_pnl": h.get("delta_cumulative_pnl"),
            "baseline_worst_pnl": h.get("baseline_worst_pnl"),
            "s2_worst_pnl": h.get("s2_worst_pnl"),
            "baseline_best_pnl": h.get("baseline_best_pnl"),
            "s2_best_pnl": h.get("s2_best_pnl"),
            "verdict": (
                "positive payoff lift" if (h.get("delta_pnl") or 0) > 0 and (h.get("delta_hit") or 0) > 0
                else "hit lift without payoff" if (h.get("delta_hit") or 0) > 0 and (h.get("delta_pnl") or 0) <= 0
                else "no S2 payoff edge"
            ),
        })
    return out


def build_coupling(topic_rows: list[dict[str, Any]], horizon_rows: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    diagnostic = {norm_horizon(h) for h in config["analysis"].get("diagnostic_horizons", [])}
    primary = {norm_horizon(h) for h in config["analysis"].get("primary_horizons", [])}
    min_pressure = float(config["analysis"].get("min_pressure_for_signal", 10.0))
    out: list[dict[str, Any]] = []
    for t in topic_rows:
        pressure = t.get("retained_pressure_score")
        if pressure is None:
            continue
        for h in horizon_rows:
            horizon = h.get("horizon")
            dh = h.get("delta_hit")
            dp = h.get("delta_pnl")
            if dh is None and dp is None:
                continue
            is_diag = horizon in diagnostic
            if is_diag:
                status = "dust diagnostic"
            elif horizon in primary and pressure >= min_pressure and (dh or 0) > 0 and (dp or 0) > 0:
                status = "candidate coupling"
            elif horizon in primary and ((dh or 0) > 0 or (dp or 0) > 0):
                status = "mixed coupling"
            elif horizon in primary:
                status = "not confirmed"
            else:
                status = "secondary horizon"
            # score is a research ranking, not a trading claim.
            edge = 0.0
            if dh is not None:
                edge += max(-0.20, min(0.20, dh)) * 3.0
            if dp is not None:
                edge += max(-0.05, min(0.05, dp)) * 10.0
            if is_diag:
                edge *= 0.0
            score = pressure * edge
            out.append({
                "topic": t.get("topic"),
                "horizon": horizon,
                "retained_pressure_score": pressure,
                "topic_lambda_hours": t.get("lambda_median_hours"),
                "topic_beta_mode": t.get("beta_mode"),
                "topic_beta_floor_share": t.get("beta_floor_share"),
                "topic_beta_035_share": t.get("legacy_beta_floor_share"),
                "topic_new_grid_floor_share": t.get("expanded_beta_min_share"),
                "topic_beta_below_035_share": t.get("beta_below_legacy_share"),
                "topic_dust_median": t.get("dust_median"),
                "topic_dust_audit": t.get("dust_audit"),
                "topic_delta_aic_median": t.get("delta_aic_median"),
                "delta_hit": dh,
                "delta_pnl": dp,
                "realized_rows": h.get("realized_rows"),
                "score_source": h.get("score_source"),
                "coupling_score": score,
                "status": status,
            })
    out.sort(key=lambda r: (r.get("status") != "candidate coupling", -(r.get("coupling_score") or -9999)))
    return out


def load_config() -> dict[str, Any]:
    with CONFIG.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    fetch_cfg = cfg.get("fetch", {}) or {}
    fetch_timeout = int(fetch_cfg.get("timeout_seconds", 18))
    raw_required_cap = fetch_cfg.get("max_bytes_required", 0)
    max_bytes_required = None if raw_required_cap in (None, "", 0, "0", "none", "unlimited") else int(raw_required_cap)
    max_bytes_optional = int(fetch_cfg.get("max_bytes_optional", 5_000_000))
    log("[BUILD] strict public-artifact bundle starting")
    log(f"[BUILD] timeout={fetch_timeout}s max_required={max_bytes_required if max_bytes_required else 'unlimited'} max_optional={max_bytes_optional}")
    health: list[dict[str, Any]] = []
    cycle_rows: list[dict[str, Any]] = []
    scorecard_rows: list[dict[str, Any]] = []
    model_comparison_rows: list[dict[str, Any]] = []
    live_predictions: list[dict[str, Any]] = []
    topic_news_samples: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for artifact in cfg["cycle"].get("required_artifacts", []):
        url = build_url(cfg["cycle"]["base_url"], artifact["path"])
        ok, text, err = fetch_url(url, timeout=fetch_timeout, max_bytes=max_bytes_required)
        record = {"group": "cycle", "kind": artifact["kind"], "url": url, "ok": ok, "rows": 0, "error": err}
        if ok:
            obj, perr = parse_json_artifact(text, url)
            if perr:
                record.update({"ok": False, "error": perr})
            else:
                news_mentions = extract_news_mentions(obj, artifact["kind"])
                merge_news_mentions(topic_news_samples, news_mentions)
                if news_mentions:
                    record["news_mentions"] = sum(len(v) for v in news_mentions.values())
                rows = extract_cycle_rows(obj, artifact["kind"])
                if rows:
                    cycle_rows.extend(rows)
                    record["rows"] = len(rows)
                    record["schema_mode"] = "cycle_fit_rows"
                elif artifact["kind"] == "cycle_history":
                    record["rows"] = 0
                    record["raw_records"] = count_json_records(obj)
                    record["schema_mode"] = "history_context_only"
                    record["note"] = "history.json loaded; it is raw story/event context, not a fitted beta/lambda cycle table"
                else:
                    record["warning"] = "JSON loaded but no usable fitted cycle rows recognized"
        health.append(record)

    for artifact in cfg["market"].get("required_artifacts", []):
        url = build_url(cfg["market"]["base_url"], artifact["path"])
        ok, text, err = fetch_url(url, timeout=fetch_timeout, max_bytes=max_bytes_required)
        kind = artifact["kind"]
        record = {"group": "market", "kind": kind, "url": url, "ok": ok, "rows": 0, "raw_rows": 0, "error": err}
        if ok:
            if artifact["path"].lower().endswith(".csv"):
                if kind in {"prediction_scorecard", "prediction_state"}:
                    parsed, mode_name, raw_rows, fields = parse_scorecard_csv_fast(text, kind)
                    record["raw_rows"] = raw_rows
                    record["fields"] = fields[:20]
                    scorecard_rows.extend(parsed)
                    record["rows"] = len(parsed)
                    record["schema_mode"] = mode_name
                    if not parsed:
                        record["warning"] = f"{kind} loaded but no scored aggregate rows recognized"
                else:
                    rows, fields = parse_csv_artifact(text)
                    record["raw_rows"] = len(rows)
                    record["fields"] = fields[:20]
                    if kind == "live_predictions":
                        parsed = extract_live_predictions(rows, kind)
                        live_predictions.extend(parsed)
                        record["rows"] = len(parsed)
                        record["schema_mode"] = "live_state_only"
                    else:
                        record["schema_mode"] = "loaded_not_used_for_score"
            elif artifact["path"].lower().endswith(".json"):
                obj, perr = parse_json_artifact(text, url)
                if perr:
                    record.update({"ok": False, "error": perr})
                else:
                    parsed = extract_model_comparison(obj, kind)
                    model_comparison_rows.extend(parsed)
                    record["rows"] = len(parsed)
                    record["schema_mode"] = "model_comparison"
                    if not parsed:
                        record["warning"] = "JSON loaded but no aggregate model comparison rows recognized"
        health.append(record)

    for artifact in cfg["market"].get("optional_artifacts", []):
        url = build_url(cfg["market"]["base_url"], artifact["path"])
        if artifact.get("disabled_by_default"):
            health.append({
                "group": "market_optional",
                "kind": artifact["kind"],
                "url": url,
                "ok": False,
                "rows": 0,
                "error": "disabled_by_default",
                "note": artifact.get("reason", "optional large artifact skipped"),
            })
            log(f"[SKIP] optional disabled {url}")
            continue
        ok, text, err = fetch_url(url, timeout=fetch_timeout, max_bytes=max_bytes_optional)
        record = {"group": "market_optional", "kind": artifact["kind"], "url": url, "ok": ok, "rows": 0, "error": err}
        if ok and artifact["path"].lower().endswith(".json"):
            obj, perr = parse_json_artifact(text, url)
            if perr:
                record.update({"ok": False, "error": perr})
            elif isinstance(obj, dict):
                record["metadata"] = {k: obj.get(k) for k in ["generated_at_utc", "latest_market_date", "requested_tickers", "successful_tickers", "quote_rows", "live_predictions", "prior_predictions_scored", "total_realized_scores"] if k in obj}
        health.append(record)

    beta_floor = float(cfg["analysis"].get("beta_floor_watch", 0.35))
    legacy_beta_floor = float(cfg["analysis"].get("legacy_beta_floor_watch", beta_floor))
    expanded_beta_min = float(cfg["analysis"].get("expanded_beta_min", 0.15))
    expanded_beta_grid = cfg["analysis"].get("expanded_beta_grid", [0.15, 0.20, 0.25, 0.30, 0.35, 0.45, 0.65, 0.85, 1.0, 1.5, 2.0])
    active_cycle_rows = [r for r in cycle_rows if r.get("source") == "cycle_active"]
    archive_cycle_rows = [r for r in cycle_rows if r.get("source") != "cycle_active"]
    # Use the current active wave for market coupling whenever it exists. The archive can contain legacy-grid fits
    # and should not overwrite newly-refit beta values from news_s2.json.
    primary_cycle_rows = active_cycle_rows if active_cycle_rows else cycle_rows
    topic_summaries = summarize_topics(primary_cycle_rows, beta_floor, legacy_beta_floor=legacy_beta_floor, expanded_beta_min=expanded_beta_min)
    archive_topic_summaries = summarize_topics(archive_cycle_rows, beta_floor, legacy_beta_floor=legacy_beta_floor, expanded_beta_min=expanded_beta_min) if archive_cycle_rows else []
    # Prefer realized live scorecard/state over backtest comparison. Use model_comparison only as a separate reference.
    scorecard_dedup = group_scores(scorecard_rows, ["prediction_scorecard", "prediction_state"])
    comparison_dedup = group_scores(model_comparison_rows, ["model_comparison"])
    selected_scores = scorecard_dedup if scorecard_dedup else comparison_dedup
    selected_source = "live_scorecard" if scorecard_dedup else ("backtest_model_comparison" if comparison_dedup else "none")
    market_horizons = summarize_market(selected_scores, selected_source)
    backtest_horizons = summarize_market(comparison_dedup, "backtest_model_comparison") if comparison_dedup else []
    coupling = build_coupling(topic_summaries, market_horizons, cfg) if market_horizons else []
    pnl_audit = build_pnl_audit(market_horizons) if market_horizons else []
    # Theater should replay the real cycle archive/current-wave rows, not only the
    # active coupling rowset. The active rowset is still used for coupling science.
    theater_source_rows = cycle_rows if cycle_rows else primary_cycle_rows
    theater_source_rows = attach_news_samples(theater_source_rows, topic_news_samples, max_samples=int(cfg.get("analysis", {}).get("theater_news_samples", 5)))
    theater_batches = build_theater_batches(theater_source_rows, max_batches=int(cfg.get("analysis", {}).get("theater_replay_batches", 5)))
    theater_trade_signals = build_theater_trade_signals(coupling, live_predictions, max_per_topic=int(cfg.get("analysis", {}).get("theater_tooltip_signals", 8)))
    narrative_radar = build_narrative_radar(theater_batches, theater_trade_signals, max_rows=int(cfg.get("analysis", {}).get("narrative_radar_rows", 24)))
    betas = [r.get("beta") for r in primary_cycle_rows if r.get("beta") is not None]
    archive_betas = [r.get("beta") for r in archive_cycle_rows if r.get("beta") is not None]
    beta_floor_share = sum(abs(float(b) - beta_floor) <= 1e-9 for b in betas) / len(betas) if betas else None
    legacy_beta_floor_share = sum(abs(float(b) - legacy_beta_floor) <= 1e-9 for b in betas) / len(betas) if betas else None
    expanded_beta_min_share = sum(abs(float(b) - expanded_beta_min) <= 1e-9 for b in betas) / len(betas) if betas else None
    archive_legacy_share = sum(abs(float(b) - legacy_beta_floor) <= 1e-9 for b in archive_betas) / len(archive_betas) if archive_betas else None
    below_legacy_share = sum(float(b) < legacy_beta_floor - 1e-9 for b in betas) / len(betas) if betas else None
    if expanded_beta_min_share is not None and expanded_beta_min_share >= float(cfg["analysis"].get("beta_floor_warning_share", 0.75)):
        beta_grid_status = "current_wave_new_floor_locked"
    elif below_legacy_share is not None and below_legacy_share >= 0.25:
        beta_grid_status = "current_wave_refit_below_035"
    elif legacy_beta_floor_share is not None and legacy_beta_floor_share >= float(cfg["analysis"].get("beta_floor_warning_share", 0.75)):
        beta_grid_status = "current_wave_035_cluster"
    else:
        beta_grid_status = "current_wave_varied"
    dust_values = [r.get("dust") for r in primary_cycle_rows if r.get("dust") is not None]
    dust_nonzero_values = [d for d in dust_values if d and d > 1e-9]

    candidate_count = sum(1 for r in coupling if r.get("status") == "candidate coupling")
    primary_nonh1 = [h for h in market_horizons if h.get("horizon") != "h1"]
    verdict = "waiting for scored market artifacts"
    if market_horizons and candidate_count:
        verdict = "candidate coupling found"
    elif market_horizons:
        verdict = "sources loaded; no confirmed advanced coupling"

    live_horizon_counts = []
    if live_predictions:
        hc = Counter((r.get("horizon") or "unknown") for r in live_predictions)
        live_horizon_counts = [{"horizon": h, "rows": n} for h, n in sorted(hc.items(), key=lambda kv: int(kv[0][1:]) if isinstance(kv[0], str) and kv[0].startswith("h") and kv[0][1:].isdigit() else 999)]

    chart_status = {
        "coupling_chart": "scored_coupling" if any(r.get("status") != "dust diagnostic" for r in coupling) else ("cycle_pressure_only" if topic_summaries else "empty"),
        "horizon_chart": "scored_horizons" if market_horizons else ("backtest_horizons" if backtest_horizons else ("live_horizon_counts" if live_horizon_counts else "empty")),
        "score_source": selected_source,
    }

    bundle = {
        "generated_at": now_utc(),
        "strict_source_policy": True,
        "source_policy": "public GitHub Pages JSON/CSV artifacts only; no dummy rows; no page scraping; no zero-fill coupling; live predictions are not used for hit/PnL",
        "source_health": health,
        "summary": {
            "cycle_rows": len(cycle_rows),
            "current_wave_cycle_rows": len(active_cycle_rows),
            "archive_cycle_rows": len(archive_cycle_rows),
            "topic_source_mode": "current_wave" if active_cycle_rows else "archive_or_all",
            "topics": len(topic_summaries),
            "score_rows": len(scorecard_dedup),
            "backtest_rows": len(comparison_dedup),
            "market_horizons": len(market_horizons),
            "backtest_horizons": len(backtest_horizons),
            "live_prediction_rows": len(live_predictions),
            "coupling_rows": len(coupling),
            "pnl_audit_rows": len(pnl_audit),
            "theater_batches": len(theater_batches),
            "theater_rows": sum(len(b.get("rows", [])) for b in theater_batches),
            "theater_trade_signal_topics": len(theater_trade_signals),
            "narrative_radar_rows": len(narrative_radar),
            "candidate_coupling_rows": candidate_count,
            "score_source": selected_source,
            "beta_floor_watch": beta_floor,
            "beta_floor_share": beta_floor_share,
            "legacy_beta_floor_watch": legacy_beta_floor,
            "legacy_beta_floor_share": legacy_beta_floor_share,
            "archive_legacy_beta_floor_share": archive_legacy_share,
            "beta_035_share": legacy_beta_floor_share,
            "beta_below_035_share": below_legacy_share,
            "expanded_beta_min": expanded_beta_min,
            "expanded_beta_min_share": expanded_beta_min_share,
            "new_grid_floor_share": expanded_beta_min_share,
            "expanded_beta_grid": expanded_beta_grid,
            "beta_grid_status": beta_grid_status,
            "beta_mode": mode(betas),
            "beta_median": median(betas),
            "dust_rows": len(dust_values),
            "dust_nonzero_share": len(dust_nonzero_values) / len(dust_values) if dust_values else None,
            "verdict": verdict,
            "primary_horizons_loaded": [h.get("horizon") for h in primary_nonh1],
            "chart_status": chart_status,
        },
        "chart_status": chart_status,
        "live_horizon_counts": live_horizon_counts,
        "topic_summaries": topic_summaries,
        "archive_topic_summaries": archive_topic_summaries,
        "market_horizons": market_horizons,
        "backtest_horizons": backtest_horizons,
        "coupling_rows": coupling,
        "pnl_audit": pnl_audit,
        "theater_batches": theater_batches,
        "theater_trade_signals": theater_trade_signals,
        "narrative_radar": narrative_radar,
        "live_predictions": live_predictions,
        "raw_cycle_rows_preview": primary_cycle_rows[:60],
        "raw_archive_cycle_rows_preview": archive_cycle_rows[:60],
        "raw_score_rows_preview": selected_scores[:60],
    }
    BUNDLE_PATH.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    HEALTH_PATH.write_text(json.dumps({"generated_at": bundle["generated_at"], "source_health": health}, indent=2), encoding="utf-8")
    print(f"[OK] wrote {BUNDLE_PATH}")
    print(f"[INFO] cycle_rows={len(cycle_rows)} score_rows={len(scorecard_dedup)} backtest_rows={len(comparison_dedup)} live_predictions={len(live_predictions)} coupling_rows={len(coupling)}")
    print(f"[INFO] score_source={selected_source} verdict={verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
