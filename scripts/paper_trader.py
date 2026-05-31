#!/usr/bin/env python3
"""S2 paper trading board.

Reads data/derived/signal_cycle_bundle.json, maintains a small local paper ledger,
and optionally sends orders to Alpaca paper trading when explicitly enabled.

Strict safety defaults:
- no real trading endpoint
- no Alpaca submission unless ALPACA_PAPER_ENABLED=true and keys are present
- local $1000 ledger is the source of experiment accounting
- h1 is never tradable
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import uuid
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_PATH = ROOT / "data" / "derived" / "signal_cycle_bundle.json"
PAPER_PATH = ROOT / "data" / "derived" / "paper_trading.json"
STATE_DIR = ROOT / "data" / "state"
LEDGER_PATH = STATE_DIR / "paper_ledger.json"

SAFE_SYMBOL_RE = re.compile(r"^[A-Z]{1,5}$")
DIAGNOSTIC_HORIZONS = {"h1", "1", "1d"}
DEFAULT_TRADABLE_HORIZONS = {"h5", "h10", "h20"}


def norm_horizon(value: Any) -> str:
    text = str(value or "").strip().lower().replace(" ", "")
    if not text:
        return ""
    text = text.replace("horizon", "")
    if text.startswith("h") and text[1:].isdigit():
        return text
    if text.endswith("d") and text[:-1].isdigit():
        return "h" + text[:-1]
    if text.isdigit():
        return "h" + text
    m = re.search(r"(?:^|[_-])h?(\d+)(?:d)?$", text)
    return "h" + m.group(1) if m else text


def is_diagnostic_horizon(value: Any) -> bool:
    return norm_horizon(value) == "h1"


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def log(msg: str) -> None:
    print(msg, flush=True)


def num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        x = float(str(v).replace("%", "").strip())
    except Exception:
        return None
    if not math.isfinite(x):
        return None
    return x / 100.0 if "%" in str(v) and abs(x) > 1 else x


def env_bool(name: str, default: bool = False) -> bool:
    text = os.getenv(name)
    if text is None:
        return default
    return text.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except Exception:
        return default


def read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=False), encoding="utf-8")


def parse_horizon_days(h: Any) -> int:
    text = norm_horizon(h).replace("h", "")
    try:
        return max(1, int(float(text)))
    except Exception:
        return 5


def parse_date(text: Any) -> dt.date | None:
    s = str(text or "").strip()
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s[:10])
    except Exception:
        return None


def add_days(date_text: Any, days: int) -> str:
    d = parse_date(date_text) or dt.datetime.now(dt.timezone.utc).date()
    return (d + dt.timedelta(days=days)).isoformat()


def direction_from_signal(pred: Any, exp_ret: Any) -> str:
    text = str(pred or "").strip().lower()
    if any(x in text for x in ["buy", "long", "up"]):
        return "BUY"
    if any(x in text for x in ["sell", "short", "down"]):
        return "SELL"
    er = num(exp_ret)
    if er is not None:
        if er > 0:
            return "BUY"
        if er < 0:
            return "SELL"
    return "HOLD"


def load_or_init_ledger(starting_cash: float) -> dict[str, Any]:
    ledger = read_json(LEDGER_PATH, {})
    if not isinstance(ledger, dict) or not ledger:
        ledger = {
            "version": 1,
            "created_at": now_utc(),
            "starting_cash": starting_cash,
            "cash": starting_cash,
            "realized_pnl": 0.0,
            "positions": {},
            "closed_trades": [],
            "order_history": [],
        }
    ledger.setdefault("starting_cash", starting_cash)
    ledger.setdefault("cash", starting_cash)
    ledger.setdefault("realized_pnl", 0.0)
    ledger.setdefault("positions", {})
    ledger.setdefault("closed_trades", [])
    ledger.setdefault("order_history", [])
    return ledger


def latest_prices(live_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return both ticker-only prices and ticker+horizon live prediction state.

    h1 rows may be the freshest or most extreme live rows, but they must never
    open or close paper positions. We therefore use ticker-only rows only for
    mark-to-market price, and horizon-matched non-h1 rows for exit direction.
    """
    by_ticker: dict[str, dict[str, Any]] = {}
    by_ticker_horizon: dict[tuple[str, str], dict[str, Any]] = {}
    for r in live_rows:
        ticker = str(r.get("ticker") or "").upper().strip()
        price = num(r.get("asof_close"))
        if not ticker or price is None or price <= 0:
            continue
        horizon = norm_horizon(r.get("horizon"))
        item = {
            "ticker": ticker,
            "price": price,
            "asof_date": r.get("asof_date"),
            "horizon": horizon,
            "prediction": r.get("prediction"),
            "expected_return": num(r.get("expected_return")),
            "probability": num(r.get("probability")),
        }
        prev = by_ticker.get(ticker)
        if not prev or str(r.get("asof_date") or "") >= str(prev.get("asof_date") or ""):
            by_ticker[ticker] = item
        if horizon:
            prev_h = by_ticker_horizon.get((ticker, horizon))
            if not prev_h or str(r.get("asof_date") or "") >= str(prev_h.get("asof_date") or ""):
                by_ticker_horizon[(ticker, horizon)] = item
    return {"by_ticker": by_ticker, "by_ticker_horizon": by_ticker_horizon}


def refresh_positions(ledger: dict[str, Any], price_state: dict[str, Any], allow_local_fills: bool = True) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    positions = ledger.get("positions", {}) or {}
    by_ticker = price_state.get("by_ticker", {}) if isinstance(price_state, dict) else {}
    by_ticker_horizon = price_state.get("by_ticker_horizon", {}) if isinstance(price_state, dict) else {}
    today = dt.datetime.now(dt.timezone.utc).date()
    for ticker, pos in list(positions.items()):
        horizon = norm_horizon(pos.get("horizon"))
        pm = by_ticker.get(ticker)
        hm = by_ticker_horizon.get((ticker, horizon)) if horizon else None
        if pm:
            pos["last_price"] = pm["price"]
            pos["last_price_date"] = pm.get("asof_date")
        qty = num(pos.get("qty")) or 0.0
        avg = num(pos.get("avg_price")) or 0.0
        last = num(pos.get("last_price")) or avg
        pos["horizon"] = horizon or pos.get("horizon")
        pos["market_value"] = qty * last
        pos["unrealized_pnl"] = (last - avg) * qty
        exit_date = parse_date(pos.get("planned_exit_date"))
        should_exit = bool(exit_date and today >= exit_date)
        exit_reason = "horizon elapsed"
        if is_diagnostic_horizon(horizon):
            should_exit = True
            exit_reason = "h1 guard purge; diagnostic horizon is not tradable"
        else:
            # Only same-horizon live signals can close a same-horizon paper position.
            # h1 live rows are deliberately ignored for exits.
            live_dir = direction_from_signal(hm.get("prediction") if hm else None, hm.get("expected_return") if hm else None) if hm else "HOLD"
            if live_dir == "SELL":
                should_exit = True
                exit_reason = "same-horizon live SELL/avoid signal"
        if should_exit and allow_local_fills and qty > 0 and last > 0:
            pnl = (last - avg) * qty
            ledger["cash"] = float(ledger.get("cash", 0.0)) + qty * last
            ledger["realized_pnl"] = float(ledger.get("realized_pnl", 0.0)) + pnl
            trade = {
                "id": f"close-{uuid.uuid4().hex[:10]}",
                "closed_at": now_utc(),
                "ticker": ticker,
                "side": "SELL",
                "qty": qty,
                "entry_price": avg,
                "exit_price": last,
                "realized_pnl": pnl,
                "entry_date": pos.get("entry_date"),
                "exit_reason": exit_reason,
                "source_topic": pos.get("source_topic"),
                "horizon": horizon,
            }
            ledger.setdefault("closed_trades", []).append(trade)
            events.append(trade)
            del positions[ticker]
    ledger["positions"] = positions
    return events


def usable_symbol(symbol: str) -> bool:
    # Conservative: avoids indices, crypto pairs, warrants, odd symbols and most fractional syntax issues.
    return bool(SAFE_SYMBOL_RE.match(symbol))


def select_signal_orders(bundle: dict[str, Any], ledger: dict[str, Any], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    live = bundle.get("live_predictions") or []
    tradable_horizons = {norm_horizon(h) for h in (cfg.get("tradable_horizons") or DEFAULT_TRADABLE_HORIZONS)}
    couplings = []
    for r in (bundle.get("coupling_rows") or []):
        h = norm_horizon(r.get("horizon"))
        if r.get("status") == "candidate coupling" and h in tradable_horizons and not is_diagnostic_horizon(h):
            rr = dict(r)
            rr["horizon"] = h
            couplings.append(rr)
    if not couplings:
        return []
    # Choose strongest candidate horizon/topic combinations.
    couplings = sorted(couplings, key=lambda r: (num(r.get("coupling_score")) or 0.0), reverse=True)[:5]
    horizons = {str(c.get("horizon") or "") for c in couplings}
    best_by_h = {str(c.get("horizon") or ""): c for c in couplings}
    min_conf = cfg["min_confidence"]
    min_ret = cfg["min_expected_return"]
    max_new = cfg["max_new_orders"]
    max_position = cfg["max_position_notional"]
    max_exposure = cfg["max_total_exposure"]
    min_price = cfg["min_price"]
    max_price = cfg["max_price"]
    positions = ledger.get("positions", {}) or {}
    open_exposure = sum(num(p.get("market_value")) or num(p.get("notional")) or 0.0 for p in positions.values())
    cash = float(ledger.get("cash", 0.0))
    proposals: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in live:
        ticker = str(row.get("ticker") or "").upper().strip()
        horizon = norm_horizon(row.get("horizon"))
        if is_diagnostic_horizon(horizon) or horizon not in horizons or ticker in seen or ticker in positions:
            continue
        if not usable_symbol(ticker):
            continue
        price = num(row.get("asof_close"))
        prob = num(row.get("probability"))
        exp_ret = num(row.get("expected_return"))
        side = direction_from_signal(row.get("prediction"), exp_ret)
        if side != "BUY":
            continue
        if price is None or price < min_price or price > max_price:
            continue
        if prob is None or prob < min_conf:
            continue
        if exp_ret is None or exp_ret < min_ret:
            continue
        if open_exposure >= max_exposure or cash <= 0:
            break
        coupling = best_by_h[horizon]
        notional = min(max_position, max_exposure - open_exposure, cash)
        if notional < cfg["min_order_notional"]:
            continue
        qty = notional / price
        rank = (num(coupling.get("coupling_score")) or 0.0) * (prob or 0.0) * max(0.0, exp_ret or 0.0)
        proposals.append({
            "id": f"proposal-{uuid.uuid4().hex[:10]}",
            "created_at": now_utc(),
            "ticker": ticker,
            "side": side,
            "qty": round(qty, 6),
            "notional": round(notional, 2),
            "paper_fill_price": price,
            "horizon": horizon,
            "planned_exit_date": add_days(row.get("asof_date"), parse_horizon_days(horizon)),
            "signal_asof": row.get("asof_date"),
            "confidence": prob,
            "expected_return": exp_ret,
            "source_topic": coupling.get("topic"),
            "coupling_score": coupling.get("coupling_score"),
            "delta_hit": coupling.get("delta_hit"),
            "delta_pnl": coupling.get("delta_pnl"),
            "reason": "non-h1 candidate coupling + live BUY vector + confidence/return/liquidity gates",
            "rank_score": rank,
        })
        open_exposure += notional
        cash -= notional
        seen.add(ticker)
    proposals.sort(key=lambda r: r.get("rank_score") or 0.0, reverse=True)
    return proposals[:max_new]


def apply_local_fills(ledger: dict[str, Any], proposals: list[dict[str, Any]], enabled: bool) -> list[dict[str, Any]]:
    if not enabled:
        return []
    fills: list[dict[str, Any]] = []
    positions = ledger.setdefault("positions", {})
    for p in proposals:
        ticker = p["ticker"]
        if ticker in positions:
            continue
        notional = num(p.get("notional")) or 0.0
        price = num(p.get("paper_fill_price")) or 0.0
        qty = num(p.get("qty")) or 0.0
        if notional <= 0 or price <= 0 or qty <= 0:
            continue
        if float(ledger.get("cash", 0.0)) < notional:
            p["status"] = "rejected_cash"
            continue
        ledger["cash"] = float(ledger.get("cash", 0.0)) - notional
        positions[ticker] = {
            "ticker": ticker,
            "side": "LONG",
            "qty": qty,
            "avg_price": price,
            "last_price": price,
            "notional": notional,
            "market_value": notional,
            "unrealized_pnl": 0.0,
            "entry_date": p.get("signal_asof"),
            "created_at": now_utc(),
            "planned_exit_date": p.get("planned_exit_date"),
            "source_topic": p.get("source_topic"),
            "horizon": norm_horizon(p.get("horizon")),
            "source_proposal_id": p.get("id"),
        }
        p["status"] = "local_filled"
        fills.append(dict(p))
    ledger["positions"] = positions
    ledger.setdefault("order_history", []).extend(fills)
    return fills


def alpaca_headers(key: str, secret: str) -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "Content-Type": "application/json",
    }


def maybe_submit_alpaca(proposals: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    if not cfg["alpaca_enabled"]:
        return []
    key = os.getenv("ALPACA_PAPER_KEY_ID") or os.getenv("APCA_API_KEY_ID") or ""
    secret = os.getenv("ALPACA_PAPER_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY") or ""
    base_url = (os.getenv("ALPACA_PAPER_BASE_URL") or "https://paper-api.alpaca.markets").rstrip("/")
    if base_url.endswith("/v2"):
        base_url = base_url[:-3]
    if not key or not secret:
        return [{"status": "not_submitted", "reason": "missing Alpaca paper API key/secret"}]
    submitted: list[dict[str, Any]] = []
    headers = alpaca_headers(key, secret)
    for p in proposals:
        if p.get("side") != "BUY":
            continue
        payload = {
            "symbol": p["ticker"],
            "notional": str(round(float(p["notional"]), 2)),
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
            "client_order_id": f"s2-{uuid.uuid4().hex[:20]}",
        }
        try:
            resp = requests.post(f"{base_url}/v2/orders", headers=headers, json=payload, timeout=20)
            item = {"ticker": p["ticker"], "request": payload, "http_status": resp.status_code}
            try:
                body = resp.json()
            except Exception:
                body = {"text": resp.text[:500]}
            item["response"] = body
            item["status"] = "submitted" if 200 <= resp.status_code < 300 else "rejected"
            submitted.append(item)
        except Exception as exc:
            submitted.append({"ticker": p.get("ticker"), "status": "error", "error": str(exc)})
    return submitted


def build_report(bundle: dict[str, Any], ledger: dict[str, Any], proposals: list[dict[str, Any]], local_fills: list[dict[str, Any]], alpaca_events: list[dict[str, Any]], closed_events: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    positions = list((ledger.get("positions", {}) or {}).values())
    cash = float(ledger.get("cash", 0.0))
    unrealized = sum(float(num(p.get("unrealized_pnl")) or 0.0) for p in positions)
    market_value = sum(float(num(p.get("market_value")) or num(p.get("notional")) or 0.0) for p in positions)
    realized = float(ledger.get("realized_pnl", 0.0))
    equity = cash + market_value
    closed = ledger.get("closed_trades", []) or []
    wins = [t for t in closed if (num(t.get("realized_pnl")) or 0.0) > 0]
    losses = [t for t in closed if (num(t.get("realized_pnl")) or 0.0) < 0]
    if proposals:
        verdict = "proposed S2 paper orders"
    elif positions:
        verdict = "tracking open S2 paper positions"
    else:
        verdict = "no strong paper vector now"
    return {
        "generated_at": now_utc(),
        "mode": "alpaca_paper_enabled" if cfg["alpaca_enabled"] else "local_paper_ledger_only",
        "strict_safety": "paper only; h1 disabled; no real-money endpoint; no orders submitted unless Alpaca paper secrets and ALPACA_PAPER_ENABLED=true",
        "verdict": verdict,
        "account": {
            "starting_cash": float(ledger.get("starting_cash", cfg["starting_cash"])),
            "cash": cash,
            "market_value": market_value,
            "equity": equity,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "total_pnl": equity - float(ledger.get("starting_cash", cfg["starting_cash"])),
            "open_positions": len(positions),
            "closed_trades": len(closed),
            "win_rate": (len(wins) / len(closed)) if closed else None,
            "avg_win": (sum(num(t.get("realized_pnl")) or 0.0 for t in wins) / len(wins)) if wins else None,
            "avg_loss": (sum(num(t.get("realized_pnl")) or 0.0 for t in losses) / len(losses)) if losses else None,
            "budget": cfg["starting_cash"],
            "max_position_notional": cfg["max_position_notional"],
            "max_total_exposure": cfg["max_total_exposure"],
        },
        "gates": {
            "min_confidence": cfg["min_confidence"],
            "min_expected_return": cfg["min_expected_return"],
            "min_order_notional": cfg["min_order_notional"],
            "non_h1_only": True,
            "long_only": True,
        },
        "proposed_orders": proposals,
        "local_fills": local_fills,
        "alpaca_events": alpaca_events,
        "closed_events": closed_events,
        "open_positions": positions,
        "closed_trades": closed[-100:],
        "order_history": (ledger.get("order_history", []) or [])[-200:],
        "narrative": [
            "This board is a paper experiment. It records what the S2-adjusted process would do under a fixed $1,000 ledger.",
            "Only non-h1 candidate coupling rows can create a proposal. h1 remains a dust diagnostic.",
            "Live prediction rows can propose orders, but they do not create scored performance until later runs update prices and close/mark positions.",
            "Alpaca paper submission is opt-in. Without secrets, the local ledger still tracks proposed and locally filled paper orders.",
        ],
    }


def main() -> int:
    if not BUNDLE_PATH.exists():
        log(f"[PAPER] missing bundle at {BUNDLE_PATH}; skipping")
        return 0
    cfg = {
        "starting_cash": env_float("PAPER_STARTING_CASH", 1000.0),
        "max_position_notional": env_float("PAPER_MAX_POSITION_NOTIONAL", 125.0),
        "max_total_exposure": env_float("PAPER_MAX_TOTAL_EXPOSURE", 400.0),
        "min_confidence": env_float("PAPER_MIN_CONFIDENCE", 0.53),
        "min_expected_return": env_float("PAPER_MIN_EXPECTED_RETURN", 0.001),
        "min_order_notional": env_float("PAPER_MIN_ORDER_NOTIONAL", 25.0),
        "max_new_orders": env_int("PAPER_MAX_NEW_ORDERS", 3),
        "min_price": env_float("PAPER_MIN_PRICE", 1.0),
        "max_price": env_float("PAPER_MAX_PRICE", 500.0),
        "local_fill_enabled": env_bool("PAPER_LOCAL_FILL_ENABLED", True),
        "alpaca_enabled": env_bool("ALPACA_PAPER_ENABLED", False),
        "tradable_horizons": [h.strip() for h in os.getenv("PAPER_TRADABLE_HORIZONS", "h5,h10,h20").split(",") if h.strip()],
    }
    bundle = read_json(BUNDLE_PATH, {})
    ledger = load_or_init_ledger(cfg["starting_cash"])
    price_state = latest_prices(bundle.get("live_predictions") or [])
    closed_events = refresh_positions(ledger, price_state, allow_local_fills=cfg["local_fill_enabled"])
    proposals = select_signal_orders(bundle, ledger, cfg)
    local_fills = apply_local_fills(ledger, proposals, cfg["local_fill_enabled"])
    alpaca_events = maybe_submit_alpaca(proposals, cfg)
    report = build_report(bundle, ledger, proposals, local_fills, alpaca_events, closed_events, cfg)
    write_json(LEDGER_PATH, ledger)
    write_json(PAPER_PATH, report)
    # Inject report into main bundle so the static app needs only one fetch.
    bundle["paper_trading"] = report
    BUNDLE_PATH.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
    log(f"[PAPER] wrote {PAPER_PATH} positions={len(report['open_positions'])} proposals={len(proposals)} mode={report['mode']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
