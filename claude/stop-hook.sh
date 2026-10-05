#!/bin/sh
# Entry of `turnstile hook claude-stop`: runs the Stop hook in the project's own
# toolchain. Outside a dev shell, a flake project's environment comes from a
# `nix print-dev-env` profile cached under the git dir and keyed on flake.nix and
# flake.lock, because `nix develop` re-evaluates a dirty tree on every call.

home=${TURNSTILE_HOME:-$(cd "$(dirname "$0")/.." && pwd -P)}
py=$(command -v python3) || { echo "python3 not on PATH - the claude-stop hook cannot run" >&2; exit 1; }
hook="$home/claude/stop-hook.py"

run_bare() { exec "$py" "$hook"; }

root=$(git rev-parse --show-toplevel 2>/dev/null) || run_bare
[ -f "$root/.turnstile" ] || run_bare
[ -f "$root/flake.nix" ] && [ -z "${IN_NIX_SHELL:-}${DIRENV_DIR:-}" ] || run_bare

nix=
for candidate in nix /nix/var/nix/profiles/default/bin/nix; do
  command -v "$candidate" >/dev/null 2>&1 && { nix=$candidate; break; }
done
[ -n "$nix" ] || run_bare

cache=$(cd "$root" && cd "$(git rev-parse --git-common-dir)" && pwd -P)/turnstile
key=$(cat "$root/flake.nix" "$root/flake.lock" 2>/dev/null | git hash-object --stdin)
profile="$cache/devenv-$key.sh"

if [ ! -f "$profile" ]; then
  mkdir -p "$cache"
  building="$profile.$$"
  if (cd "$root" && "$nix" print-dev-env --no-write-lock-file >"$building" 2>"$building.err" </dev/null); then
    rm -f "$cache"/devenv-*.sh
    mv "$building" "$profile"
  else
    reason=$(grep . "$building.err" | tail -n 1)
    TURNSTILE_NOTICE="turnstile: could not build the dev environment ($reason), so the checks ran without it"
    profile=
  fi
  rm -f "$building" "$building.err"
  export TURNSTILE_NOTICE
fi

[ -n "$profile" ] || run_bare

# The profile is bash 4+ syntax: macOS's /bin/bash 3.2 cannot source it, so use
# the bash the environment itself names.
shell=$(sed -n "s/^BASH='\\(.*\\)'\$/\\1/p" "$profile" | head -n 1)
[ -x "$shell" ] || shell=bash

"$shell" -c '
  . "$1"
  shift
  "$@"
  status=$?
  case ${NIX_BUILD_TOP:-} in */nix-shell.*) rm -rf "$NIX_BUILD_TOP" ;; esac
  exit $status
' turnstile-devenv "$profile" "$py" "$hook"
