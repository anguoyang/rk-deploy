#!/bin/sh
# rk-deploy board rollback (generic, POSIX sh).
# Usage: rollback.sh <install_root> <app_name> [target_version]
# Flips the current symlink back, re-renders the target release's
# systemd unit, restarts and health-checks. No re-upload involved.
# Exit codes: 0 ok, 4 nothing to roll back to / target missing,
# 7 rollback health check failed (original version restored).
set -u
ROOT="$1"; APP_NAME="$2"; TARGET="${3:-}"
SYSTEMCTL="${RKDEPLOY_SYSTEMCTL:-systemctl}"
SYSTEMD_DIR="${RKDEPLOY_SYSTEMD_DIR:-/etc/systemd/system}"
UNIT="rkdeploy-${APP_NAME}.service"
say() { echo "[rkdeploy] $*"; }

[ -f "$ROOT/state.json" ] || { say "no state.json at $ROOT"; echo "RKDEPLOY_RESULT=aborted reason=no-state"; exit 4; }
CUR=$(sed -n 's/.*"current": "\([^"]*\)".*/\1/p' "$ROOT/state.json" | head -1)
PREV=$(sed -n 's/.*"previous": "\([^"]*\)".*/\1/p' "$ROOT/state.json" | head -1)
[ -z "$TARGET" ] && TARGET="$PREV"
if [ -z "$TARGET" ] || [ ! -d "$ROOT/releases/$TARGET" ]; then
  say "no retained release to roll back to (current=$CUR)"
  echo "RKDEPLOY_RESULT=aborted reason=no-target current=$CUR"
  exit 4
fi
[ "$TARGET" = "$CUR" ] && { say "already on $TARGET"; echo "RKDEPLOY_RESULT=ok version=$TARGET"; exit 0; }
DIR="$ROOT/releases/$TARGET"
[ -f "$DIR/install.env" ] || { say "release $TARGET has no install.env"; echo "RKDEPLOY_RESULT=aborted reason=corrupt-release"; exit 4; }

# facts of the target release
APP_VERSION=$(grep '^APP_VERSION=' "$DIR/install.env" | cut -d= -f2-)
SERVICE_USER=$(grep '^SERVICE_USER=' "$DIR/install.env" | cut -d= -f2-)
HEALTH_TYPE=$(grep '^HEALTH_TYPE=' "$DIR/install.env" | cut -d= -f2-)
HEALTH_PATH=$(grep '^HEALTH_PATH=' "$DIR/install.env" | cut -d= -f2-)
HEALTH_COMMAND=$(grep '^HEALTH_COMMAND=' "$DIR/install.env" | cut -d= -f2-)
HEALTH_TIMEOUT=$(grep '^HEALTH_TIMEOUT=' "$DIR/install.env" | cut -d= -f2-)

START_EPOCH=$(date +%s)
health_once() {
  case "$HEALTH_TYPE" in
    file) [ -f "$HEALTH_PATH" ] || return 1
      _m=$(stat -c %Y "$HEALTH_PATH" 2>/dev/null || echo 0)
      [ "$_m" -ge "$START_EPOCH" ] ;;
    command) sh -c "$HEALTH_COMMAND" >/dev/null 2>&1 ;;
    systemd-only) [ "$($SYSTEMCTL is-active "$UNIT" 2>/dev/null)" = "active" ] ;;
    *) return 1 ;;
  esac
}
wait_health() { _n=0; while [ "$_n" -lt "$HEALTH_TIMEOUT" ]; do
  if health_once; then return 0; fi; _n=$((_n + 1)); sleep 1; done; health_once; }

render_and_switch() { # $1 = version, $2 = its release dir
  sed -e "s|@INSTALL_ROOT@|$ROOT|g" -e "s|@APP_NAME@|$APP_NAME|g" \
      -e "s|@APP_VERSION@|$1|g" -e "s|@RUN_USER@|$SERVICE_USER|g" \
      "$2/service.template" > "$SYSTEMD_DIR/$UNIT"
  $SYSTEMCTL daemon-reload
  ln -s "releases/$1" "$ROOT/current.tmp.$$"
  mv -Tf "$ROOT/current.tmp.$$" "$ROOT/current"
  $SYSTEMCTL restart "$UNIT" || true
}

write_state() { # $1 = current, $2 = previous, $3 = version acted on, $4 = result
  _now=$(date +%Y-%m-%dT%H:%M:%S%z)
  echo "{\"at\": \"$_now\", \"action\": \"rollback\", \"version\": \"$3\", \"result\": \"$4\"}" >> "$ROOT/history.jsonl"
  _hist=$(sed 's/^/,/' "$ROOT/history.jsonl" | tr -d '\n' | sed 's/^,//')
  _prev="null"; [ -n "$2" ] && _prev="\"$2\""
  printf '{\n  "name": "%s",\n  "current": "%s",\n  "previous": %s,\n  "updated_at": "%s",\n  "history": [%s]\n}\n' \
    "$APP_NAME" "$1" "$_prev" "$_now" "$_hist" > "$ROOT/state.json.tmp"
  mv "$ROOT/state.json.tmp" "$ROOT/state.json"
}

render_and_switch "$TARGET" "$DIR"
if wait_health; then
  write_state "$TARGET" "$CUR" "$TARGET" ok
  say "rolled back $APP_NAME: $CUR -> $TARGET"
  echo "RKDEPLOY_RESULT=ok version=$TARGET previous=$CUR"
  exit 0
fi
say "rollback health check failed; restoring $CUR"
CUR_DIR="$ROOT/releases/$CUR"
if [ -d "$CUR_DIR" ]; then
  SERVICE_USER=$(grep '^SERVICE_USER=' "$CUR_DIR/install.env" | cut -d= -f2-)
  render_and_switch "$CUR" "$CUR_DIR"
fi
write_state "$CUR" "$TARGET" "$TARGET" rollback-failed
echo "RKDEPLOY_RESULT=rollback-failed version=$CUR failed-target=$TARGET"
exit 7
