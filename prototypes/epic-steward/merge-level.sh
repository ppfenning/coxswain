#!/usr/bin/env bash
# Merge approved coxswain level branches into a ticket branch, run build/vet/unit tests, push.
# No model calls. Dry run by default; --apply merges and pushes.
#
# usage: merge-level.sh REPO INITIATIVE TICKET_BRANCH LEVEL [LEVEL...] [--apply]
#   e.g. merge-level.sh ~/work/my-repo my-initiative feature/123-my-ticket schema readers --apply
# Uses a temporary worktree, so the repo checkout is never touched. Stops at the first failure and
# pushes only if every merge and check passed. Integration tests are NOT run here (shared test
# database); the chair runs them once at the ticket gate.
set -euo pipefail
apply=0; args=()
for a in "$@"; do [ "$a" = "--apply" ] && apply=1 || args+=("$a"); done
[ ${#args[@]} -ge 4 ] || { sed -n 2,11p "$0"; exit 2; }
repo="${args[0]/#\~/$HOME}"; init="${args[1]}"; ticket="${args[2]}"; levels=("${args[@]:3}")
git -C "$repo" fetch -q origin
wt="$(mktemp -d)/wt"; trap 'git -C "$repo" worktree remove --force "$wt" 2>/dev/null || true' EXIT
git -C "$repo" worktree add -q --detach "$wt" "origin/$ticket"
cd "$wt"
for l in "${levels[@]}"; do
  b="epic/$init/$l"
  git rev-parse -q --verify "$b" >/dev/null || { echo "missing branch $b"; exit 1; }
  echo "== merge $b: ahead $(git rev-list --count HEAD.."$b")"
  git merge --no-ff -q "$b" -m "Merge level $l into $ticket" || { echo "CONFLICT in $l:"; git diff --name-only --diff-filter=U; exit 1; }
done
echo "== build";  go build ./...
echo "== vet";    go vet ./...
echo "== unit";   go test -count=1 ./... 2>&1 | grep -v 'no test files'
if [ "$apply" = 1 ]; then git push -q origin "HEAD:refs/heads/$ticket" && echo "pushed $(git rev-parse --short HEAD) to $ticket"; else echo "dry run: all checks passed; pass --apply to push $(git rev-parse --short HEAD)"; fi
