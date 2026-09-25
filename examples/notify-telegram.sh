#!/bin/sh
# Example ATMOTUBE_NOTIFY_CMD: sends the alert to a Telegram chat.
#
# Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment - put them
# in /etc/atmotube.env (mode 600). The token goes to curl on stdin as a config
# line, NOT as an argument: arguments of running processes are visible to every
# local user via ps.
set -eu
: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN missing}"
: "${TELEGRAM_CHAT_ID:?TELEGRAM_CHAT_ID missing}"
printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TELEGRAM_BOT_TOKEN" |
  curl -sS --fail --max-time 20 -K - -o /dev/null \
       --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
       --data-urlencode "text=$1
$2"
