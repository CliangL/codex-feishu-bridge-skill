#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${CODEX_FEISHU_ENV_FILE:-${HOME}/.codex-feishu/.env}"
ENV_DIR="$(dirname "${ENV_FILE}")"

mkdir -p "${ENV_DIR}"
chmod 700 "${ENV_DIR}"

printf "Codex Feishu app_id: "
IFS= read -r APP_ID
printf "Codex Feishu app_secret: "
stty -echo
IFS= read -r APP_SECRET
stty echo
printf "\n"

if [[ -z "${APP_ID}" || -z "${APP_SECRET}" ]]; then
  echo "app_id/app_secret 不能为空" >&2
  exit 1
fi

cat > "${ENV_FILE}" <<EOF
# Codex dedicated Feishu/Lark bot credentials.
# Keep this file local. Do not commit or paste it into chats.
CODEX_FEISHU_APP_ID=${APP_ID}
CODEX_FEISHU_APP_SECRET=${APP_SECRET}
CODEX_FEISHU_DOMAIN=feishu
CODEX_FEISHU_CONNECTION_MODE=websocket
CODEX_FEISHU_GROUP_POLICY=allowlist
CODEX_FEISHU_ALLOWED_USERS=
CODEX_FEISHU_REACTIONS=true
EOF

chmod 600 "${ENV_FILE}"
echo "已写入 ${ENV_FILE}"
echo "下一步运行：${HOME}/.codex-feishu/app/start.sh --check"
