# anthropic_alarm

`trade.xyz` / Ventuals pre-IPO markets에서 급격한 funding spike와 가격 왜곡을 감지해 `SHORT_ENTRY`, `SHORT_EXIT`, `LONG_ENTRY` 알람을 보내는 프로젝트입니다.

현재 기본 감시 대상:

- `vntl:ANTHROPIC`
- `vntl:OPENAI`
- `vntl:SPACEX`

다른 pre-IPO 기업은 [config/vntl_signal_markets.json](/Users/myunggeunjung/trade.xyz/config/vntl_signal_markets.json)에 추가하면 됩니다.

## Signal logic

- `SHORT_ENTRY`
  - funding이 극단적으로 치솟은 상태
  - 가격 왜곡이 커서 숏과 funding 수취를 노리는 구간
- `SHORT_EXIT`
  - funding이 `0.2%/h` 아래로 식은 상태
  - 숏을 정리하고 롱 전환을 기다리는 구간
- `LONG_ENTRY`
  - funding이 계속 낮고
  - 최근 저점 위에서
  - 3시간 연속 반등이 확인된 상태
  - 정상화 방향 롱을 노리는 구간
- `WATCH`
  - 아직 새 진입 신호 없음

historical oracle feed가 직접 제공되지 않아서, 스크립트는 `close / (1 + realized funding)` 기반의 `inferred oracle proxy`를 같이 기록합니다. 이 값은 설명용 컨텍스트이지, 메인 진입 트리거는 아닙니다.

## Files that matter

- [config/vntl_signal_markets.json](/Users/myunggeunjung/trade.xyz/config/vntl_signal_markets.json)
- [ecosystem.config.cjs](/Users/myunggeunjung/trade.xyz/ecosystem.config.cjs)
- [scripts/vntl_signal_lib.py](/Users/myunggeunjung/trade.xyz/scripts/vntl_signal_lib.py)
- [scripts/vntl_signal_monitor.py](/Users/myunggeunjung/trade.xyz/scripts/vntl_signal_monitor.py)
- [scripts/vntl_signal_daemon.sh](/Users/myunggeunjung/trade.xyz/scripts/vntl_signal_daemon.sh)
- [scripts/telegram_status_bot.py](/Users/myunggeunjung/trade.xyz/scripts/telegram_status_bot.py)

## Environment

Telegram:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Optional Slack:

- `SLACK_WEBHOOK_URL`

Runtime data and logs stay on EC2 and are not committed:

- `data/`
- `logs/`

## EC2 `.env`

운영용 텔레그램/슬랙 값은 GitHub에 올리지 말고 EC2 로컬 `.env`에만 둡니다.

```bash
cd /home/ec2-user/anthropic_alarm
cp .env.example .env
chmod 600 .env
```

`.env` 예시:

```bash
TELEGRAM_BOT_TOKEN=your_real_bot_token
TELEGRAM_CHAT_ID=your_real_chat_id
# Optional
# SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

데몬은 매 실행 전에 `/home/ec2-user/anthropic_alarm/.env`를 자동으로 읽습니다.
그리고 PM2 프로세스가 시작될 때 1회, 텔레그램으로 `모니터링 시작 + 현재 임계값` 알림을 보냅니다.

## Telegram commands

- `/status`
  - 현재 마켓별 상태
  - estimated funding
  - realized funding
  - mark/oracle 괴리
  - 최근 extreme 이후 경과시간
  - 현재 임계값
- `/help`
  - 사용 가능한 명령 표시

## EC2 + PM2

```bash
cd /home/ec2-user/anthropic_alarm
chmod +x scripts/vntl_signal_daemon.sh
pm2 start ecosystem.config.cjs
pm2 save
```

Useful commands:

```bash
pm2 status
pm2 logs vntl-signal-monitor
pm2 logs vntl-telegram-status-bot
pm2 restart vntl-signal-monitor --update-env
pm2 restart vntl-telegram-status-bot --update-env
```

The daemon pulls `origin/main` before each hourly run and then executes the monitor.
Only the first run after daemon start sends the startup threshold message. The later hourly runs only send signal-change alerts.
The second PM2 process listens for Telegram commands and replies to `/status`.
