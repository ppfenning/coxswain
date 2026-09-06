#!/bin/bash
set -u

profile="${AGENT_TOOLS_PROFILE:-${HOME:-}/.config/agent-tools/profile.yaml}"

if [ ! -f "$profile" ]; then
    exit 0
fi

cox route context 2>/dev/null || agent-tools route context 2>/dev/null || true
cox route leader status 2>/dev/null || agent-tools route leader status 2>/dev/null || true
