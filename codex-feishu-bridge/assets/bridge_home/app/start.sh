#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_WORKSPACE="${CODEX_FEISHU_DEFAULT_WORKSPACE:-${HOME}/.codex-feishu/workspace}"

PYTHON_BIN="${CODEX_FEISHU_PYTHON:-${HOME}/.codex-feishu/runtime/venv/bin/python}"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi

CODEX_BIN="${CODEX_FEISHU_CODEX_BIN:-/Applications/Codex.app/Contents/Resources/codex}"
if [[ ! -x "${CODEX_BIN}" ]]; then
  CODEX_BIN="${CODEX_FEISHU_CODEX_BIN:-codex}"
fi

export CODEX_FEISHU_RUNTIME_SRC="${CODEX_FEISHU_RUNTIME_SRC:-${HOME}/.codex-feishu/runtime/src}"
export PYTHONPATH="${CODEX_FEISHU_RUNTIME_SRC}${PYTHONPATH:+:${PYTHONPATH}}"

exec "${PYTHON_BIN}" "${APP_DIR}/codex_feishu_app.py" \
  --workspace "${CODEX_FEISHU_WORKSPACE:-${DEFAULT_WORKSPACE}}" \
  --codex-bin "${CODEX_BIN}" \
  --codex-home "${CODEX_FEISHU_CODEX_HOME:-${HOME}/.codex-feishu/codex-home}" \
  --env-file "${CODEX_FEISHU_ENV_FILE:-${HOME}/.codex-feishu/.env}" \
  "$@"
