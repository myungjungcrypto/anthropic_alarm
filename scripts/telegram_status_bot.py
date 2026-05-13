#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any

from vntl_signal_lib import (
    SignalConfig,
    classify_markets,
    load_market_specs,
    load_json,
    to_iso8601,
    now_ms,
)

DEFAULT_OUTPUT_DIR = Path("data/signals/vntl_monitor")
DEFAULT_STATE_PATH = Path("data/telegram_status_bot/state.json")


def load_env_file() -> None:
    env_path = os.environ.get("ENV_FILE")
    if env_path:
        path = Path(env_path)
    else:
        repo_dir = Path(os.environ.get("REPO_DIR", "."))
        path = repo_dir / ".env"

    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def telegram_api(method: str, payload: dict[str, Any]) -> Any:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required.")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=70) as response:
        return json.loads(response.read().decode("utf-8"))


def send_message(chat_id: str, text: str) -> None:
    telegram_api("sendMessage", {"chat_id": chat_id, "text": text})


def load_state(path: Path) -> dict[str, Any]:
    return load_json(path, default={"last_update_id": 0})


def save_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def get_updates(offset: int) -> list[dict[str, Any]]:
    response = telegram_api(
        "getUpdates",
        {
            "offset": offset,
            "timeout": 60,
            "allowed_updates": ["message"],
        },
    )
    if not response.get("ok"):
        return []
    return response.get("result", [])


def build_config() -> SignalConfig:
    return SignalConfig(
        short_trigger=Decimal("0.9"),
        short_exit=Decimal("0.2"),
        long_funding_max=Decimal("0.2"),
        long_cooldown_hours=12,
        recent_hours=72,
        rebound_hours=3,
        low_window_hours=12,
        oracle_shock_threshold=Decimal("5"),
    )


def format_status(signals: list[dict[str, Any]], config: SignalConfig) -> str:
    lines = [
        "current monitor status",
        f"short_entry >= {config.short_trigger}%/h",
        f"short_exit < {config.short_exit}%/h",
        (
            "long_entry <= "
            f"{config.long_funding_max}%/h, cooldown >= {config.long_cooldown_hours}h, "
            f"rebound_hours = {config.rebound_hours}, low_window_hours = {config.low_window_hours}"
        ),
        "",
    ]

    for signal in signals:
        lines.extend(
            [
                f"{signal['title']}: {signal['status']}",
                f"estimated funding: {signal.get('current_estimated_funding_pct')}%/h",
                f"realized funding: {signal.get('latest_realized_funding_pct')}%/h",
                f"mark/oracle: {signal.get('mark_vs_oracle_pct')}%",
                f"oracle 1h: {signal.get('oracle_proxy_change_1h_pct')}%",
                f"last extreme: {signal.get('last_extreme_funding_time_iso')}",
                f"hours since extreme: {signal.get('hours_since_last_extreme_funding')}",
                "",
            ]
        )

    lines.append(f"time: {to_iso8601(now_ms())}")
    return "\n".join(lines)


def build_help_text() -> str:
    return "\n".join(
        [
            "available commands",
            "/status - fetch current market signals and thresholds",
            "/help - show this help message",
        ]
    )


def handle_message(message: dict[str, Any], config: SignalConfig) -> None:
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    allowed_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if allowed_chat_id and chat_id != allowed_chat_id:
        return

    text = (message.get("text") or "").strip()
    if not text:
        return

    if text.startswith("/status"):
        markets = load_market_specs(Path("config/vntl_signal_markets.json"))
        signals = classify_markets(markets=markets, config=config)
        send_message(chat_id, format_status(signals, config))
        return

    if text.startswith("/help") or text.startswith("/start"):
        send_message(chat_id, build_help_text())


def main() -> int:
    load_env_file()
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        print("TELEGRAM_BOT_TOKEN is required.")
        return 1

    state_path = DEFAULT_STATE_PATH
    state = load_state(state_path)
    offset = int(state.get("last_update_id", 0)) + 1
    config = build_config()

    while True:
        try:
            updates = get_updates(offset)
            for update in updates:
                update_id = int(update["update_id"])
                message = update.get("message")
                if message:
                    handle_message(message, config)
                offset = update_id + 1
                save_state(state_path, {"last_update_id": update_id})
        except urllib.error.URLError as exc:
            print(f"Network error: {exc.reason}")
            time.sleep(5)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            print(f"HTTP error {exc.code}: {body}")
            time.sleep(5)
        except Exception as exc:  # pragma: no cover
            print(f"Unexpected error: {exc}")
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
