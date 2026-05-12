#!/usr/bin/env bash

set -euo pipefail

REPO_DIR="${REPO_DIR:-/home/ec2-user/anthropic_alarm}"
ENV_FILE="${ENV_FILE:-$REPO_DIR/.env}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
RUN_MINUTE="${RUN_MINUTE:-5}"
RUN_IMMEDIATELY="${RUN_IMMEDIATELY:-1}"
MONITOR_ARGS="${MONITOR_ARGS:---stdout-json}"
GIT_PULL_BEFORE_RUN="${GIT_PULL_BEFORE_RUN:-1}"
GIT_BRANCH="${GIT_BRANCH:-main}"
LOG_PREFIX="${LOG_PREFIX:-[vntl-signal-daemon]}"

load_env_file() {
  if [ -f "$ENV_FILE" ]; then
    echo "$LOG_PREFIX $(date -u +%FT%TZ) loading env from $ENV_FILE"
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
  fi
}

run_once() {
  cd "$REPO_DIR"
  if [ "$GIT_PULL_BEFORE_RUN" = "1" ]; then
    echo "$LOG_PREFIX $(date -u +%FT%TZ) git pull --ff-only origin $GIT_BRANCH"
    git pull --ff-only origin "$GIT_BRANCH"
  fi

  echo "$LOG_PREFIX $(date -u +%FT%TZ) python3 scripts/vntl_signal_monitor.py $MONITOR_ARGS"
  # shellcheck disable=SC2086
  "$PYTHON_BIN" scripts/vntl_signal_monitor.py $MONITOR_ARGS
}

sleep_until_next_window() {
  now_epoch="$(date +%s)"
  current_hour_start=$(( now_epoch - (now_epoch % 3600) ))
  target_this_hour=$(( current_hour_start + RUN_MINUTE * 60 ))

  if [ "$now_epoch" -lt "$target_this_hour" ]; then
    next_run="$target_this_hour"
  else
    next_run=$(( current_hour_start + 3600 + RUN_MINUTE * 60 ))
  fi

  sleep_seconds=$(( next_run - now_epoch ))
  echo "$LOG_PREFIX $(date -u +%FT%TZ) sleeping ${sleep_seconds}s until next run window"
  sleep "$sleep_seconds"
}

if [ "$RUN_IMMEDIATELY" = "1" ]; then
  load_env_file
  run_once
fi

while true; do
  sleep_until_next_window
  load_env_file
  run_once
done
