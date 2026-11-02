#!/bin/sh
# rk-deploy board installer (generic, app-agnostic, POSIX sh).
# Usage: install.sh <package.tar.gz> <install_root>
# The package carries everything: this script only reads package.env
# (flat KEY=VALUE rendered at pack time) -- never YAML, never JSON.
# Exit codes: 0 ok, 4 verification/preflight failed (live version
# untouched), 6 deploy failed but auto-rollback succeeded, 7 deploy
# failed and rollback failed / nothing to roll back to.
set -u
PKG="$1"; ROOT="$2"
SYSTEMCTL="${RKDEPLOY_SYSTEMCTL:-systemctl}"
SYSTEMD_DIR="${RKDEPLOY_SYSTEMD_DIR:-/etc/systemd/system}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT INT TERM

say() { echo "[rkdeploy] $*"; }
fail4() { say "ABORT: $*"; echo "RKDEPLOY_RESULT=aborted reason=$1"; exit 4; }

tar -xzf "$PKG" -C "$WORK" || fail4 extract
cd "$WORK" || fail4 cd
[ -f package.env ] || fail4 no-package-env
# shellcheck disable=SC1091
. ./package.env
UNIT="rkdeploy-${APP_NAME}.service"

# --- 1. integrity gate: verify every payload file before touching anything live
sha256sum -c SHA256SUMS >/dev/null 2>&1 || fail4 sha256
say "integrity ok: $APP_NAME $APP_VERSION"

# --- 2. preflight
mkdir -p "$ROOT/releases" "$ROOT/shared/config" "$ROOT/shared/data"
FREE_PCT=$(df -P "$ROOT" | awk 'NR==2 {gsub(/%/,"",$5); print 100-$5}')
PKG_KB=$(du -k "$PKG" | awk '{print $1}')
FREE_KB=$(df -Pk "$ROOT" | awk 'NR==2 {print $4}')
if [ "$FREE_PCT" -lt 5 ] || [ "$FREE_KB" -lt $((PKG_KB * 3)) ]; then
  fail4 disk
fi
id "$SERVICE_USER" >/dev/null 2>&1 || fail4 user
for b in $DEPS_BINARIES; do command -v "$b" >/dev/null 2>&1 || fail4 "dep-binary-$b"; done
for lib in $DEPS_LIBS; do
  if command -v ldconfig >/dev/null 2>&1 && ldconfig -p 2>/dev/null | grep -q "$lib"; then :;
  elif [ -e "/usr/lib/aarch64-linux-gnu/$lib" ] || [ -e "/usr/lib/$lib" ] || [ -e "/lib/aarch64-linux-gnu/$lib" ]; then :;
  else fail4 "dep-lib-$lib"; fi
done
for p in $DEPS_PYTHON; do
  python3 -c "import $p" >/dev/null 2>&1 || say "warn: python module not importable: $p"
done
for a in $DEPS_APT; do
  command -v dpkg >/dev/null 2>&1 && dpkg -s "$a" >/dev/null 2>&1 || say "warn: apt package not installed: $a"
done
for dev in $DEPS_DEVICES; do [ -e "$dev" ] || say "warn: device missing: $dev"; done
say "preflight ok (disk free ${FREE_PCT}%)"

# --- helpers -------------------------------------------------------------
resolve() { echo "$1" | sed "s|@INSTALL_ROOT@|$ROOT|g"; }

write_install_env() { # $1 = release dir
  cat > "$1/install.env" <<EOF
APP_NAME=$APP_NAME
APP_VERSION=$APP_VERSION
SERVICE_USER=$SERVICE_USER
INSTALL_ROOT=$ROOT
UNIT=$UNIT
HEALTH_TYPE=$HEALTH_TYPE
HEALTH_TIMEOUT=$HEALTH_TIMEOUT
HEALTH_PATH=$(resolve "$HEALTH_PATH")
HEALTH_COMMAND=$(resolve "$HEALTH_COMMAND")
EOF
}

render_unit() { # $1 = release dir whose template to use
  sed -e "s|@INSTALL_ROOT@|$ROOT|g" -e "s|@APP_NAME@|$APP_NAME|g" \
      -e "s|@APP_VERSION@|$APP_VERSION|g" -e "s|@RUN_USER@|$SERVICE_USER|g" \
      "$1/service.template" > "$SYSTEMD_DIR/$UNIT"
  $SYSTEMCTL daemon-reload
}

switch_current() { # $1 = version
  ln -s "releases/$1" "$ROOT/current.tmp.$$"
  mv -Tf "$ROOT/current.tmp.$$" "$ROOT/current"
}

START_EPOCH=0
health_once() { # $1 = release dir (uses its install.env)
  _t="$HEALTH_TYPE"; _p="$HEALTH_PATH"; _c="$HEALTH_COMMAND"
  [ -f "$1/install.env" ] && { _t=$(grep '^HEALTH_TYPE=' "$1/install.env" | cut -d= -f2-);
    _p=$(grep '^HEALTH_PATH=' "$1/install.env" | cut -d= -f2-);
    _c=$(grep '^HEALTH_COMMAND=' "$1/install.env" | cut -d= -f2-); }
  case "$_t" in
    file)
      # existence is not enough: a stale file refreshed by the previous
      # version's still-running process must not pass. Require the file
      # to have been written at/after this deploy's restart moment.
      [ -f "$_p" ] || return 1
      _m=$(stat -c %Y "$_p" 2>/dev/null || echo 0)
      [ "$_m" -ge "$START_EPOCH" ] ;;
    command) sh -c "$_c" >/dev/null 2>&1 ;;
    systemd-only) [ "$($SYSTEMCTL is-active "$UNIT" 2>/dev/null)" = "active" ] ;;
    *) return 1 ;;
  esac
}

wait_health() { # $1 = release dir, $2 = timeout seconds
  _n=0
  while [ "$_n" -lt "$2" ]; do
    if health_once "$1"; then return 0; fi
    _n=$((_n + 1)); sleep 1
  done
  health_once "$1"
}

write_state() { # $1 = current, $2 = previous, $3 = action, $4 = version, $5 = result
  _now=$(date +%Y-%m-%dT%H:%M:%S%z)
  echo "{\"at\": \"$_now\", \"action\": \"$3\", \"version\": \"$4\", \"result\": \"$5\"}" >> "$ROOT/history.jsonl"
  _hist=$(sed 's/^/,/' "$ROOT/history.jsonl" | tr -d '\n' | sed 's/^,//')
  _prev="null"; [ -n "$2" ] && _prev="\"$2\""
  printf '{\n  "name": "%s",\n  "current": "%s",\n  "previous": %s,\n  "updated_at": "%s",\n  "history": [%s]\n}\n' \
    "$APP_NAME" "$1" "$_prev" "$_now" "$_hist" > "$ROOT/state.json.tmp"
  mv "$ROOT/state.json.tmp" "$ROOT/state.json"
}

prune_releases() { # keep newest KEEP_RELEASES by mtime, never current/previous
  _cur="$1"; _prev="$2"; _n=0
  for d in $(ls -dt "$ROOT"/releases/*/ 2>/dev/null); do
    _v=$(basename "$d"); _n=$((_n + 1))
    if [ "$_n" -gt "$KEEP_RELEASES" ] && [ "$_v" != "$_cur" ] && [ "$_v" != "$_prev" ]; then
      rm -rf "$d"; say "pruned old release $_v"
    fi
  done
}

# --- 3. unpack into an immutable release directory
DEST="$ROOT/releases/$APP_VERSION"
[ -e "$DEST" ] && fail4 release-exists
mkdir "$DEST"
cp -a "$WORK"/. "$DEST"/
write_install_env "$DEST"
say "unpacked to releases/$APP_VERSION"

PREV=""
[ -L "$ROOT/current" ] && PREV=$(basename "$(readlink "$ROOT/current")")

# --- 4. render unit, switch, restart, health check (all on the board)
render_unit "$DEST"
$SYSTEMCTL enable "$UNIT" >/dev/null 2>&1 || true
switch_current "$APP_VERSION"
START_EPOCH=$(date +%s)
$SYSTEMCTL restart "$UNIT" || true
if wait_health "$DEST" "$HEALTH_TIMEOUT"; then
  write_state "$APP_VERSION" "$PREV" deploy "$APP_VERSION" ok
  prune_releases "$APP_VERSION" "$PREV"
  say "deployed $APP_NAME $APP_VERSION (previous: ${PREV:-none})"
  echo "RKDEPLOY_RESULT=ok version=$APP_VERSION previous=${PREV:-none}"
  exit 0
fi

# --- 5. health failed: auto-rollback unless disabled or impossible
say "health check failed for $APP_VERSION within ${HEALTH_TIMEOUT}s"
if [ -n "${RKDEPLOY_NO_AUTO_ROLLBACK:-}" ]; then
  write_state "$APP_VERSION" "$PREV" deploy "$APP_VERSION" failed-no-rollback
  echo "RKDEPLOY_RESULT=failed version=$APP_VERSION"
  exit 7
fi
if [ -z "$PREV" ] || [ ! -d "$ROOT/releases/$PREV" ]; then
  write_state "$APP_VERSION" "" deploy "$APP_VERSION" failed-no-previous
  echo "RKDEPLOY_RESULT=failed version=$APP_VERSION reason=no-previous"
  exit 7
fi
PREV_DIR="$ROOT/releases/$PREV"
APP_VERSION_SAVED="$APP_VERSION"
# render the previous release's unit (its own template/package facts)
APP_VERSION="$PREV"
. "$PREV_DIR/package.env"
render_unit "$PREV_DIR"
switch_current "$PREV"
START_EPOCH=$(date +%s)
$SYSTEMCTL restart "$UNIT" || true
if wait_health "$PREV_DIR" "$HEALTH_TIMEOUT"; then
  APP_VERSION="$APP_VERSION_SAVED"
  write_state "$PREV" "" deploy "$APP_VERSION" rolled-back
  say "auto-rollback succeeded: back to $PREV"
  echo "RKDEPLOY_RESULT=rolled-back version=$PREV failed-version=$APP_VERSION_SAVED"
  exit 6
fi
write_state "$PREV" "" deploy "$APP_VERSION_SAVED" rollback-failed
echo "RKDEPLOY_RESULT=rollback-failed version=$PREV failed-version=$APP_VERSION_SAVED"
exit 7
