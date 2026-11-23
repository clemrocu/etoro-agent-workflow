#!/usr/bin/env bash
# Run `uv` with this app's reproducible index selection. When sourced, this wrapper prepares the
# same environment for all package-manager commands in the workspace. (Vendored from the Mistral
# cookbook scaffold, path mistral/agents/slack_triage_workflow/slack-triage-workflow.)
_uv_sh_executed=0
if [ -n "${BASH_SOURCE[0]:-}" ] && [ "${BASH_SOURCE[0]}" = "$0" ]; then
  _uv_sh_executed=1
  set -euo pipefail
fi

PUBLIC_INDEX="https://pypi.org/simple"
export UV_DEFAULT_INDEX="$PUBLIC_INDEX"
export UV_INDEX_URL="$PUBLIC_INDEX"
export PIP_INDEX_URL="$PUBLIC_INDEX"
export UV_EXTRA_INDEX_URL=""
unset AGENT

if [ "$_uv_sh_executed" = 1 ]; then
  unset _uv_sh_executed
  case "${1:-}" in
    add | remove)
      unset UV_DEFAULT_INDEX UV_INDEX_URL
      ;;
  esac
  exec uv "$@"
fi
unset _uv_sh_executed
