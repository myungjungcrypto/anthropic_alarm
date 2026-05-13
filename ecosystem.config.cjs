module.exports = {
  apps: [
    {
      name: "vntl-signal-monitor",
      script: "./scripts/vntl_signal_daemon.sh",
      interpreter: "/bin/bash",
      cwd: "/home/ec2-user/anthropic_alarm",
      autorestart: true,
      watch: false,
      max_restarts: 20,
      restart_delay: 5000,
      env: {
        REPO_DIR: "/home/ec2-user/anthropic_alarm",
        PYTHON_BIN: "/usr/bin/python3",
        GIT_BRANCH: "main",
        GIT_PULL_BEFORE_RUN: "1",
        RUN_IMMEDIATELY: "1",
        RUN_MINUTE: "5",
        MONITOR_ARGS: "--stdout-json",
      },
    },
    {
      name: "vntl-telegram-status-bot",
      script: "./scripts/telegram_status_bot.py",
      interpreter: "/usr/bin/python3",
      cwd: "/home/ec2-user/anthropic_alarm",
      autorestart: true,
      watch: false,
      max_restarts: 20,
      restart_delay: 5000,
      env: {
        REPO_DIR: "/home/ec2-user/anthropic_alarm",
        PYTHON_BIN: "/usr/bin/python3",
        ENV_FILE: "/home/ec2-user/anthropic_alarm/.env",
      },
    },
  ],
};
