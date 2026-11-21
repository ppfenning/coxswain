#!/usr/bin/env bash
# Render the cast into a Claude Code agents directory.
#
# Symlinks, not copies: editing a seat in this repository changes what every
# session loads, and `--check` can always say where a definition came from. A
# copy drifts silently, which is the failure this whole repository is arranged
# to avoid.
#
# Only the function-named seats are rendered. The old person names (sarah,
# lewis, ...) are aliases the cartridge loader resolves at lookup for one
# release, never agent files; a link an old name left here points at a seat
# file that no longer exists, and install and uninstall remove it.
#
#   ./install.sh                 symlink every seat into ~/.claude/agents
#   ./install.sh --check         report what is installed, and from where
#   ./install.sh --prefix DIR    install into DIR instead
#   ./install.sh --uninstall     remove only the links that point here
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEATS="$REPO/seats"
PREFIX="${HOME}/.claude/agents"
MODE=install

while [ $# -gt 0 ]; do
  case "$1" in
    --check)     MODE=check ;;
    --uninstall) MODE=uninstall ;;
    --prefix)    PREFIX="${2:?--prefix needs a directory}"; shift ;;
    -h|--help)   sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)           echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

seat_files() { find "$SEATS" -maxdepth 1 -name '*.md' ! -name 'SEATS.md' | sort; }

# Links in PREFIX that point into this repository's seats/ at a file that is
# gone: what a renamed or retired seat leaves behind.
stale_links() {
  [ -d "$PREFIX" ] || return 0
  find "$PREFIX" -maxdepth 1 -name '*.md' -type l | sort | while read -r link; do
    src="$(readlink "$link")"
    case "$src" in "$SEATS"/*) [ -e "$src" ] || echo "$link" ;; esac
  done
}

case "$MODE" in
  check)
    printf '%-14s %-10s %s\n' SEAT STATE SOURCE
    for seat in $(seat_files); do
      name="$(basename "$seat" .md)"
      target="$PREFIX/$name.md"
      if [ -L "$target" ]; then
        src="$(readlink "$target")"
        [ "$src" = "$seat" ] && printf '%-14s %-10s %s\n' "$name" linked "$src" \
                             || printf '%-14s %-10s %s\n' "$name" foreign "$src"
      elif [ -e "$target" ]; then
        printf '%-14s %-10s %s\n' "$name" "not-a-link" "$target"
      else
        printf '%-14s %-10s %s\n' "$name" absent -
      fi
    done
    for link in $(stale_links); do
      printf '%-14s %-10s %s\n' "$(basename "$link" .md)" stale "$(readlink "$link")"
    done
    ;;
  uninstall)
    for seat in $(seat_files); do
      target="$PREFIX/$(basename "$seat")"
      # Only ever remove a link that points into THIS repository. Anything
      # else belongs to someone else, whatever its name happens to be.
      if [ -L "$target" ] && [ "$(readlink "$target")" = "$seat" ]; then
        rm "$target"; echo "removed $target"
      fi
    done
    for link in $(stale_links); do rm "$link"; echo "removed $link (stale)"; done
    ;;
  install)
    mkdir -p "$PREFIX"
    for link in $(stale_links); do rm "$link"; echo "removed $link (stale)"; done
    for seat in $(seat_files); do
      name="$(basename "$seat" .md)"
      target="$PREFIX/$name.md"
      if [ -e "$target" ] && [ ! -L "$target" ]; then
        echo "refusing to overwrite $target (not a symlink)" >&2
        continue
      fi
      ln -sfn "$seat" "$target"
      echo "$name -> $target"
    done
    echo
    echo "Installed into $PREFIX. Bind seats to your team's skills with a"
    echo "crew: block in your cartridge — see the README."
    ;;
esac
