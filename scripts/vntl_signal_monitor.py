#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import urllib.error
from decimal import Decimal
from pathlib import Path

from vntl_signal_lib import (
    SignalConfig,
    build_monitor_state,
    classify_markets,
    deliver_notifications,
    load_market_specs,
    load_monitor_state,
    persist_monitor_outputs,
    save_monitor_state,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Monitor Ventuals pre-IPO markets, persist hourly signal snapshots, and notify on status changes."
        )
    )
    parser.add_argument(
        "--markets-config",
        type=Path,
        default=Path("config/vntl_signal_markets.json"),
        help="JSON config listing markets to monitor.",
    )
    parser.add_argument(
        "--coin",
        action="append",
        default=[],
        help="Restrict monitoring to specific coins. Repeatable.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/signals/vntl_monitor"),
        help="Directory where latest snapshots, history, and state are stored.",
    )
    parser.add_argument("--short-trigger-pct", type=Decimal, default=Decimal("0.9"))
    parser.add_argument("--short-exit-pct", type=Decimal, default=Decimal("0.2"))
    parser.add_argument("--long-funding-max-pct", type=Decimal, default=Decimal("0.2"))
    parser.add_argument("--long-cooldown-hours", type=int, default=12)
    parser.add_argument("--recent-hours", type=int, default=72)
    parser.add_argument("--rebound-hours", type=int, default=3)
    parser.add_argument("--low-window-hours", type=int, default=12)
    parser.add_argument("--oracle-shock-threshold-pct", type=Decimal, default=Decimal("5"))
    parser.add_argument("--stdout-json", action="store_true", help="Print the run payload as JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.long_cooldown_hours < 0 or args.recent_hours <= 0 or args.rebound_hours <= 1 or args.low_window_hours <= 0:
        print("Argument error: check hours arguments.")
        return 1

    markets = load_market_specs(args.markets_config if args.markets_config.exists() else None)
    if args.coin:
        allowed = set(args.coin)
        markets = [market for market in markets if market.coin in allowed]
    if not markets:
        print("No markets selected.")
        return 1

    config = SignalConfig(
        short_trigger=args.short_trigger_pct,
        short_exit=args.short_exit_pct,
        long_funding_max=args.long_funding_max_pct,
        long_cooldown_hours=args.long_cooldown_hours,
        recent_hours=args.recent_hours,
        rebound_hours=args.rebound_hours,
        low_window_hours=args.low_window_hours,
        oracle_shock_threshold=args.oracle_shock_threshold_pct,
    )

    try:
        signals = classify_markets(markets=markets, config=config)
        persist_monitor_outputs(args.output_dir, signals)
        previous_state = load_monitor_state(args.output_dir / "state.json")
        next_state, notifications = build_monitor_state(previous_state, signals)
        delivered = deliver_notifications(notifications)
        save_monitor_state(args.output_dir / "state.json", next_state)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP error {exc.code}: {body}")
        return 1
    except urllib.error.URLError as exc:
        print(f"Network error: {exc.reason}")
        return 1
    except ValueError as exc:
        print(f"Data error: {exc}")
        return 1

    payload = {
        "signals": signals,
        "notifications": delivered,
        "output_dir": str(args.output_dir.resolve()),
    }

    if args.stdout_json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"markets: {len(signals)}")
        print(f"notifications_sent: {len(delivered)}")
        print(f"output_dir: {args.output_dir.resolve()}")
        for signal in signals:
            print(
                f"{signal['title']} {signal['status']} funding={signal.get('current_estimated_funding_pct')} "
                f"mark_oracle={signal.get('mark_vs_oracle_pct')} "
                f"oracle_1h={signal.get('oracle_proxy_change_1h_pct')}"
            )

    return 0 if all(signal["status"] != "ERROR" for signal in signals) else 1


if __name__ == "__main__":
    raise SystemExit(main())
