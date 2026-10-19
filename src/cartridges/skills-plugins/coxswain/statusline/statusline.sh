#!/bin/bash
set -u

json="$(</dev/stdin)"

model=""
if [[ "$json" =~ \"display_name\"[[:space:]]*:[[:space:]]*\"([^\"]*)\" ]]; then
    model="${BASH_REMATCH[1]}"
fi

profile="${AGENT_TOOLS_PROFILE:-${HOME:-}/.config/agent-tools/profile.yaml}"
slots=3
plugin_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v cox >/dev/null 2>&1 || [ ! -f "$profile" ] || ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "$model"
    exit 0
fi

status_json="$(cox route status --json 2>/dev/null)"
if [ -z "$status_json" ]; then
    printf '%s\n' "$model"
    exit 0
fi

summary="$(python3 "$plugin_dir/summarize.py" "$status_json" "$slots" 2>/dev/null)"
if [ -z "$summary" ]; then
    printf '%s\n' "$model"
    exit 0
fi

readarray -t summary_lines <<<"$summary"
running_count="${summary_lines[0]:-0}"
slot_count="${summary_lines[1]:-$slots}"
spend_amount="${summary_lines[2]:-}"

if [ -n "$spend_amount" ]; then
    printf '%s · cox: %s in flight/%s slots · $%s block\n' "$model" "$running_count" "$slot_count" "$spend_amount"
else
    printf '%s · cox: %s in flight/%s slots\n' "$model" "$running_count" "$slot_count"
fi
