#!/bin/sh
# rk-deploy fleet status probe (generic, POSIX sh, read-only).
# Usage: probe.sh <install_root> <app_name>
# Prints KEY=VALUE lines; the controller turns them into the report.
set -u
ROOT="$1"; APP_NAME="$2"
SYSTEMCTL="${RKDEPLOY_SYSTEMCTL:-systemctl}"
UNIT="rkdeploy-${APP_NAME}.service"
echo "PROBE=1"
if [ ! -d "$ROOT" ]; then echo "INSTALLED=0"; exit 0; fi
echo "INSTALLED=1"
VERSION=""; PREVIOUS=""
if [ -L "$ROOT/current" ]; then VERSION=$(basename "$(readlink "$ROOT/current")"); fi
if [ -f "$ROOT/state.json" ]; then
  PREVIOUS=$(sed -n 's/.*"previous": "\([^"]*\)".*/\1/p' "$ROOT/state.json" | head -1)
fi
echo "VERSION=$VERSION"
echo "PREVIOUS=$PREVIOUS"
echo "SERVICE_ACTIVE=$($SYSTEMCTL is-active "$UNIT" 2>/dev/null || echo unknown)"
echo "SERVICE_ENABLED=$($SYSTEMCTL is-enabled "$UNIT" 2>/dev/null || echo unknown)"
PID=$($SYSTEMCTL show -p MainPID "$UNIT" 2>/dev/null | sed -n 's/^MainPID=//p')
echo "MAIN_PID=${PID:-0}"
SINCE=$($SYSTEMCTL show -p ExecMainStartTimestamp "$UNIT" 2>/dev/null | sed -n 's/^ExecMainStartTimestamp=//p')
echo "SERVICE_SINCE=$SINCE"
REL=$(ls "$ROOT/releases" 2>/dev/null | sort | tr '\n' ',' | sed 's/,$//')
echo "RELEASES=$REL"
echo "RELEASE_COUNT=$(ls "$ROOT/releases" 2>/dev/null | wc -l | tr -d ' ')"
echo "DISK_FREE_PCT=$(df -P "$ROOT" | awk 'NR==2 {gsub(/%/,"",$5); print 100-$5}')"
echo "DISK_FREE_KB=$(df -Pk "$ROOT" | awk 'NR==2 {print $4}')"
SOC=""; MAX=-1
for z in /sys/class/thermal/thermal_zone*; do
  [ -f "$z/type" ] && [ -f "$z/temp" ] || continue
  T=$(cat "$z/type" 2>/dev/null); V=$(cat "$z/temp" 2>/dev/null) || continue
  case "$V" in ''|*[!0-9-]*) continue ;; esac
  C=$((V / 1000))
  [ "$C" -gt "$MAX" ] && MAX=$C
  case "$T" in *soc*) SOC=$C ;; esac
done
echo "TEMP_SOC_C=$SOC"
echo "TEMP_MAX_C=$MAX"
# live health check, once, from the current release's install.env
HEALTH=skip
DIR="$ROOT/current"
if [ -f "$DIR/install.env" ]; then
  HT=$(grep '^HEALTH_TYPE=' "$DIR/install.env" | cut -d= -f2-)
  HP=$(grep '^HEALTH_PATH=' "$DIR/install.env" | cut -d= -f2-)
  HC=$(grep '^HEALTH_COMMAND=' "$DIR/install.env" | cut -d= -f2-)
  case "$HT" in
    file) [ -f "$HP" ] && HEALTH=ok || HEALTH=fail ;;
    command) sh -c "$HC" >/dev/null 2>&1 && HEALTH=ok || HEALTH=fail ;;
    systemd-only) [ "$($SYSTEMCTL is-active "$UNIT" 2>/dev/null)" = "active" ] && HEALTH=ok || HEALTH=fail ;;
  esac
fi
echo "HEALTH=$HEALTH"
ERRS=-1
if command -v journalctl >/dev/null 2>&1; then
  ERRS=$(journalctl -u "$UNIT" --since "-1h" -p err --no-pager 2>/dev/null | grep -c . || echo -1)
fi
echo "JOURNAL_ERRORS_1H=$ERRS"
