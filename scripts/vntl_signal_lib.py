#!/usr/bin/env python3

from __future__ import annotations

import csv
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

API = "https://api.hyperliquid.xyz/info"
DEFAULT_DEX = "vntl"
DEFAULT_COIN = "vntl:ANTHROPIC"
DEFAULT_MARKETS = [
    {"coin": "vntl:ANTHROPIC", "title": "Anthropic"},
    {"coin": "vntl:OPENAI", "title": "OpenAI"},
    {"coin": "vntl:SPACEX", "title": "SpaceX"},
]
SNAPSHOT_CSV_COLUMNS = [
    "run_time_iso",
    "dex",
    "coin",
    "title",
    "status",
    "action",
    "current_estimated_funding_pct",
    "latest_realized_funding_pct",
    "latest_realized_time_iso",
    "last_extreme_funding_time_iso",
    "hours_since_last_extreme_funding",
    "mark_px",
    "oracle_px",
    "mark_vs_oracle_pct",
    "oracle_proxy_px",
    "oracle_proxy_change_1h_pct",
    "oracle_proxy_change_3h_pct",
    "oracle_proxy_change_6h_pct",
    "oracle_shock_down",
    "oracle_shock_up",
    "last_close",
    "recent_low",
    "above_recent_low",
    "rebound_confirmed",
]


@dataclass(frozen=True)
class SignalConfig:
    short_trigger: Decimal
    short_exit: Decimal
    long_funding_max: Decimal
    long_cooldown_hours: int
    recent_hours: int
    rebound_hours: int
    low_window_hours: int
    oracle_shock_threshold: Decimal


@dataclass(frozen=True)
class MarketSpec:
    coin: str
    title: str
    dex: str = DEFAULT_DEX


def now_ms() -> int:
    return int(time.time() * 1000)


def to_iso8601(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).isoformat()


def decimal_value(value: Any) -> Decimal:
    return Decimal(str(value))


def hour_bucket(timestamp_ms: int) -> int:
    return timestamp_ms - (timestamp_ms % 3_600_000)


def pct_change(current: Decimal, previous: Decimal | None) -> Decimal | None:
    if previous is None or previous == 0:
        return None
    return (current / previous - Decimal("1")) * Decimal("100")


def post_info(payload: dict[str, Any]) -> Any:
    request = urllib.request.Request(
        API,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def append_jsonl(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def append_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SNAPSHOT_CSV_COLUMNS)
        if write_header:
            writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column) for column in SNAPSHOT_CSV_COLUMNS})


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_market_specs(path: Path | None) -> list[MarketSpec]:
    if path is None:
        return [MarketSpec(**row) for row in DEFAULT_MARKETS]
    payload = load_json(path, default={"markets": DEFAULT_MARKETS})
    rows = payload.get("markets", DEFAULT_MARKETS) if isinstance(payload, dict) else DEFAULT_MARKETS
    return [MarketSpec(dex=row.get("dex", DEFAULT_DEX), coin=row["coin"], title=row.get("title", row["coin"])) for row in rows]


def fetch_market_contexts(dex: str) -> dict[str, dict[str, Any]]:
    meta, ctxs = post_info({"type": "metaAndAssetCtxs", "dex": dex})
    names = [row["name"] for row in meta["universe"]]
    return {coin: ctxs[index] for index, coin in enumerate(names)}


def build_inferred_oracle_series(
    funding_rows: list[dict[str, Any]],
    candle_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candles_by_hour: dict[int, Decimal] = {}
    for row in candle_rows:
        candles_by_hour[hour_bucket(int(row["t"]))] = decimal_value(row["c"])

    series_by_hour: dict[int, dict[str, Any]] = {}
    for row in funding_rows:
        row_time = int(row["time"])
        hour_time = hour_bucket(row_time)
        close = candles_by_hour.get(hour_time)
        if close is None:
            continue
        funding_rate = decimal_value(row["fundingRate"])
        denominator = Decimal("1") + funding_rate
        if denominator <= 0:
            continue
        series_by_hour[hour_time] = {
            "time": row_time,
            "time_iso": to_iso8601(row_time),
            "funding_rate": funding_rate,
            "close": close,
            "oracle_proxy": close / denominator,
        }

    return [series_by_hour[key] for key in sorted(series_by_hour)]


def classify_signal(
    market: MarketSpec,
    config: SignalConfig,
    market_contexts: dict[str, dict[str, Any]] | None = None,
    current_time: int | None = None,
) -> dict[str, Any]:
    current_time = current_time or now_ms()
    market_contexts = market_contexts or fetch_market_contexts(market.dex)
    if market.coin not in market_contexts:
        raise ValueError(f"{market.coin} not found in {market.dex} universe.")
    ctx = market_contexts[market.coin]

    recent_start = current_time - config.recent_hours * 3_600_000
    funding_rows = sorted(
        post_info({"type": "fundingHistory", "coin": market.coin, "startTime": recent_start}),
        key=lambda row: int(row["time"]),
    )
    if not funding_rows:
        raise ValueError(f"No recent funding rows returned for {market.coin}.")

    candle_rows = sorted(
        post_info(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": market.coin,
                    "interval": "1h",
                    "startTime": current_time - max(config.recent_hours, config.low_window_hours + 2) * 3_600_000,
                    "endTime": current_time,
                },
            }
        ),
        key=lambda row: int(row["t"]),
    )
    if len(candle_rows) < max(config.rebound_hours, config.low_window_hours):
        raise ValueError(f"Not enough candle data returned for {market.coin}.")

    oracle_proxy_series = build_inferred_oracle_series(funding_rows, candle_rows)
    if len(oracle_proxy_series) < 2:
        raise ValueError(f"Not enough inferred oracle history returned for {market.coin}.")

    current_estimated_funding = decimal_value(ctx["funding"]) * Decimal("100")
    mark_px = decimal_value(ctx["markPx"])
    oracle_px = decimal_value(ctx["oraclePx"])
    latest_realized_funding = decimal_value(funding_rows[-1]["fundingRate"]) * Decimal("100")
    latest_realized_time = int(funding_rows[-1]["time"])

    last_extreme_funding_time = None
    for row in reversed(funding_rows):
        if decimal_value(row["fundingRate"]) * Decimal("100") >= config.short_trigger:
            last_extreme_funding_time = int(row["time"])
            break

    hours_since_last_extreme = None
    if last_extreme_funding_time is not None:
        hours_since_last_extreme = (current_time - last_extreme_funding_time) / 3_600_000

    rebound_closes = [decimal_value(row["c"]) for row in candle_rows[-config.rebound_hours :]]
    rebound_confirmed = all(
        rebound_closes[index] > rebound_closes[index - 1] for index in range(1, len(rebound_closes))
    )
    last_close = decimal_value(candle_rows[-1]["c"])
    recent_low = min(decimal_value(row["l"]) for row in candle_rows[-config.low_window_hours :])
    above_recent_low = last_close > recent_low
    mark_vs_oracle_pct = (mark_px / oracle_px - Decimal("1")) * Decimal("100")
    latest_oracle_proxy = oracle_proxy_series[-1]["oracle_proxy"]
    oracle_proxy_change_1h_pct = pct_change(
        latest_oracle_proxy,
        oracle_proxy_series[-2]["oracle_proxy"] if len(oracle_proxy_series) >= 2 else None,
    )
    oracle_proxy_change_3h_pct = pct_change(
        latest_oracle_proxy,
        oracle_proxy_series[-4]["oracle_proxy"] if len(oracle_proxy_series) >= 4 else None,
    )
    oracle_proxy_change_6h_pct = pct_change(
        latest_oracle_proxy,
        oracle_proxy_series[-7]["oracle_proxy"] if len(oracle_proxy_series) >= 7 else None,
    )
    oracle_shock_down = (
        oracle_proxy_change_1h_pct is not None and oracle_proxy_change_1h_pct <= -config.oracle_shock_threshold
    )
    oracle_shock_up = (
        oracle_proxy_change_1h_pct is not None and oracle_proxy_change_1h_pct >= config.oracle_shock_threshold
    )

    has_extreme_history = last_extreme_funding_time is not None

    if current_estimated_funding >= config.short_trigger or latest_realized_funding >= config.short_trigger:
        status = "SHORT_ENTRY"
        action = "Funding has spiked hard enough to treat this as a short-entry regime."
    elif (
        has_extreme_history
        and current_estimated_funding <= config.long_funding_max
        and above_recent_low
        and rebound_confirmed
        and hours_since_last_extreme is not None
        and hours_since_last_extreme >= config.long_cooldown_hours
    ):
        status = "LONG_ENTRY"
        action = "Funding has cooled and price is stabilizing. This is the long re-entry regime."
    elif has_extreme_history and current_estimated_funding < config.short_exit:
        status = "SHORT_EXIT"
        action = "Funding has normalized enough to take the short off and wait for long confirmation."
    else:
        status = "WATCH"
        action = "No fresh entry signal yet. Keep watching funding compression and price stabilization."

    notes: list[str] = []
    if oracle_shock_down:
        notes.append(
            "Inferred oracle proxy fell sharply in the last hour. I am logging it as context, not as a stand-alone short trigger."
        )
    elif oracle_shock_up:
        notes.append(
            "Inferred oracle proxy jumped sharply in the last hour. That can invalidate a stale short quickly."
        )

    signal = {
        "coin": market.coin,
        "title": market.title,
        "dex": market.dex,
        "as_of_time": current_time,
        "as_of_time_iso": to_iso8601(current_time),
        "status": status,
        "action": action,
        "current_estimated_funding_pct": str(current_estimated_funding),
        "latest_realized_funding_pct": str(latest_realized_funding),
        "latest_realized_time_iso": to_iso8601(latest_realized_time),
        "last_extreme_funding_time_iso": to_iso8601(last_extreme_funding_time) if last_extreme_funding_time else None,
        "hours_since_last_extreme_funding": hours_since_last_extreme,
        "mark_px": str(mark_px),
        "oracle_px": str(oracle_px),
        "mark_vs_oracle_pct": str(mark_vs_oracle_pct),
        "oracle_proxy_px": str(latest_oracle_proxy),
        "oracle_proxy_change_1h_pct": str(oracle_proxy_change_1h_pct) if oracle_proxy_change_1h_pct is not None else None,
        "oracle_proxy_change_3h_pct": str(oracle_proxy_change_3h_pct) if oracle_proxy_change_3h_pct is not None else None,
        "oracle_proxy_change_6h_pct": str(oracle_proxy_change_6h_pct) if oracle_proxy_change_6h_pct is not None else None,
        "oracle_shock_down": oracle_shock_down,
        "oracle_shock_up": oracle_shock_up,
        "last_close": str(last_close),
        "recent_low_window_hours": config.low_window_hours,
        "recent_low": str(recent_low),
        "above_recent_low": above_recent_low,
        "rebound_hours": config.rebound_hours,
        "rebound_confirmed": rebound_confirmed,
        "notes": notes,
        "rule_params": {
            "short_trigger_pct": str(config.short_trigger),
            "short_exit_pct": str(config.short_exit),
            "long_funding_max_pct": str(config.long_funding_max),
            "long_cooldown_hours": config.long_cooldown_hours,
            "oracle_shock_threshold_pct": str(config.oracle_shock_threshold),
        },
    }
    return signal


def classify_markets(markets: list[MarketSpec], config: SignalConfig) -> list[dict[str, Any]]:
    grouped_by_dex: dict[str, list[MarketSpec]] = {}
    for market in markets:
        grouped_by_dex.setdefault(market.dex, []).append(market)

    signals: list[dict[str, Any]] = []
    current_time = now_ms()
    for dex, dex_markets in grouped_by_dex.items():
        contexts = fetch_market_contexts(dex)
        for market in dex_markets:
            try:
                signals.append(
                    classify_signal(
                        market=market,
                        config=config,
                        market_contexts=contexts,
                        current_time=current_time,
                    )
                )
            except (urllib.error.HTTPError, urllib.error.URLError, ValueError) as exc:
                signals.append(
                    {
                        "coin": market.coin,
                        "title": market.title,
                        "dex": market.dex,
                        "as_of_time": current_time,
                        "as_of_time_iso": to_iso8601(current_time),
                        "status": "ERROR",
                        "action": f"Signal evaluation failed: {exc}",
                        "error": str(exc),
                    }
                )
    signals.sort(key=lambda row: row["coin"])
    return signals


def format_signal_message(signal: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"{signal['title']} ({signal['coin']})",
            f"status: {signal['status']}",
            f"action: {signal['action']}",
            f"estimated funding: {signal.get('current_estimated_funding_pct')}",
            f"realized funding: {signal.get('latest_realized_funding_pct')}",
            f"mark/oracle: {signal.get('mark_vs_oracle_pct')}",
            f"oracle proxy 1h: {signal.get('oracle_proxy_change_1h_pct')}",
            f"last extreme: {signal.get('last_extreme_funding_time_iso')}",
            f"hours since extreme: {signal.get('hours_since_last_extreme_funding')}",
            f"time: {signal['as_of_time_iso']}",
        ]
    )


def format_startup_message(config: SignalConfig, markets: list[MarketSpec]) -> str:
    market_list = ", ".join(market.title for market in markets)
    return "\n".join(
        [
            "vntl-signal-monitor started",
            f"markets: {market_list}",
            f"short_entry: funding >= {config.short_trigger}%/h",
            f"short_exit: funding < {config.short_exit}%/h",
            (
                "long_entry: funding <= "
                f"{config.long_funding_max}%/h, cooldown >= {config.long_cooldown_hours}h, "
                f"rebound_hours = {config.rebound_hours}, low_window_hours = {config.low_window_hours}"
            ),
            f"oracle_context_alert: inferred oracle 1h move >= {config.oracle_shock_threshold}%",
            f"time: {to_iso8601(now_ms())}",
        ]
    )


def load_monitor_state(path: Path) -> dict[str, Any]:
    return load_json(path, default={"markets": {}})


def build_snapshot_rows(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for signal in signals:
        rows.append(
            {
                "run_time_iso": signal["as_of_time_iso"],
                "dex": signal["dex"],
                "coin": signal["coin"],
                "title": signal["title"],
                "status": signal["status"],
                "action": signal["action"],
                "current_estimated_funding_pct": signal.get("current_estimated_funding_pct"),
                "latest_realized_funding_pct": signal.get("latest_realized_funding_pct"),
                "latest_realized_time_iso": signal.get("latest_realized_time_iso"),
                "last_extreme_funding_time_iso": signal.get("last_extreme_funding_time_iso"),
                "hours_since_last_extreme_funding": signal.get("hours_since_last_extreme_funding"),
                "mark_px": signal.get("mark_px"),
                "oracle_px": signal.get("oracle_px"),
                "mark_vs_oracle_pct": signal.get("mark_vs_oracle_pct"),
                "oracle_proxy_px": signal.get("oracle_proxy_px"),
                "oracle_proxy_change_1h_pct": signal.get("oracle_proxy_change_1h_pct"),
                "oracle_proxy_change_3h_pct": signal.get("oracle_proxy_change_3h_pct"),
                "oracle_proxy_change_6h_pct": signal.get("oracle_proxy_change_6h_pct"),
                "oracle_shock_down": signal.get("oracle_shock_down"),
                "oracle_shock_up": signal.get("oracle_shock_up"),
                "last_close": signal.get("last_close"),
                "recent_low": signal.get("recent_low"),
                "above_recent_low": signal.get("above_recent_low"),
                "rebound_confirmed": signal.get("rebound_confirmed"),
            }
        )
    return rows


def persist_monitor_outputs(output_dir: Path, signals: list[dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    latest_path = output_dir / "latest.json"
    history_jsonl_path = output_dir / "history.jsonl"
    history_csv_path = output_dir / "history.csv"

    payload = {
        "run_time": now_ms(),
        "run_time_iso": to_iso8601(now_ms()),
        "signals": signals,
    }
    atomic_write_json(latest_path, payload)
    append_jsonl(history_jsonl_path, payload)
    append_csv(history_csv_path, build_snapshot_rows(signals))


def build_monitor_state(previous: dict[str, Any], signals: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    previous_markets = previous.get("markets", {}) if isinstance(previous, dict) else {}

    actionable_statuses = {"SHORT_ENTRY", "SHORT_EXIT", "LONG_ENTRY", "ERROR"}
    notifications: list[dict[str, Any]] = []
    next_state = {"updated_at": now_ms(), "updated_at_iso": to_iso8601(now_ms()), "markets": {}}

    for signal in signals:
        previous_row = previous_markets.get(signal["coin"], {})
        previous_status = previous_row.get("status")
        current_status = signal["status"]
        next_state["markets"][signal["coin"]] = {
            "status": current_status,
            "title": signal["title"],
            "last_signal": signal,
        }
        if previous_status is None:
            should_notify = current_status in actionable_statuses
        else:
            should_notify = previous_status != current_status and (
                current_status in actionable_statuses or previous_status in actionable_statuses
            )
        if should_notify:
            notifications.append(
                {
                    "coin": signal["coin"],
                    "title": signal["title"],
                    "previous_status": previous_status,
                    "current_status": current_status,
                    "signal": signal,
                }
            )

    return next_state, notifications


def save_monitor_state(path: Path, state: dict[str, Any]) -> None:
    atomic_write_json(path, state)


def send_slack(webhook_url: str, text: str) -> None:
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps({"text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20):
        return


def send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20):
        return


def send_startup_notification(config: SignalConfig, markets: list[MarketSpec]) -> list[str]:
    slack_webhook = os.environ.get("SLACK_WEBHOOK_URL")
    telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    text = format_startup_message(config=config, markets=markets)

    channels: list[str] = []
    if slack_webhook:
        send_slack(slack_webhook, text)
        channels.append("slack")
    if telegram_bot_token and telegram_chat_id:
        send_telegram(telegram_bot_token, telegram_chat_id, text)
        channels.append("telegram")
    return channels


def deliver_notifications(notifications: list[dict[str, Any]]) -> list[dict[str, Any]]:
    slack_webhook = os.environ.get("SLACK_WEBHOOK_URL")
    telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    delivered: list[dict[str, Any]] = []
    for item in notifications:
        signal = item["signal"]
        prefix = f"[{item.get('previous_status') or 'NONE'} -> {item['current_status']}]"
        text = prefix + "\n" + format_signal_message(signal)
        channels: list[str] = []
        if slack_webhook:
            send_slack(slack_webhook, text)
            channels.append("slack")
        if telegram_bot_token and telegram_chat_id:
            send_telegram(telegram_bot_token, telegram_chat_id, text)
            channels.append("telegram")
        delivered.append(
            {
                "coin": signal["coin"],
                "title": signal["title"],
                "previous_status": item.get("previous_status"),
                "current_status": item["current_status"],
                "channels": channels,
            }
        )
    return delivered
