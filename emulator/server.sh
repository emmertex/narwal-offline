#!/usr/bin/env bash
# server.sh — the one-command server side of the offline stack.
#
# Runs on your emulator box (the always-on LAN machine). It provides:
#   :8081  HTTP — serves the command file the robot polls every 15 s,
#          plus files you want the robot to fetch (e.g. your CA)
#   :4550  collector — receives command output from the robot
#   :4545  listener — receives the robot's reverse-shell beacon
#
# Usage:
#   ./server.sh start                      # start everything (idempotent)
#   ./server.sh cmd 'ls /etc/ssl/certs'    # run a shell command on the robot,
#                                          # print its output when it arrives
#   ./server.sh put <localfile>            # stage a file for robot wget
#   ./server.sh stop
#
# The robot polls only while its rc.local payload is installed
# (tools/owner_setup.sh does that) and the robot is awake/networked.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$HERE/cmdroot"
LOG="$ROOT/server.log"
PID="$ROOT/server.pids"
mkdir -p "$ROOT"
echo "# no-op" > "$ROOT/cmd"

alive() { pgrep -f "$1" >/dev/null 2>&1; }

start() {
  # http server on 8081
  if ! alive "http.server 8081"; then
    ( cd "$ROOT" && setsid nohup python3 -m http.server 8081 \
        </dev/null >>"$LOG" 2>&1 & )
    echo "http :8081 up (serving $ROOT)"
  fi
  # output collector on 4550 (one file per robot poll)
  if ! alive "nc -l 4550"; then
    ( i=0
      while true; do
        setsid nc -l 4550 >"$ROOT/out_$i.txt" 2>/dev/null
        i=$((i+1)); sleep 1
      done </dev/null >>"$LOG" 2>&1 & )
    echo "collector :4550 up"
  fi
  # reverse shell listener on 4545 (logged; interactive use is optional)
  if ! alive "nc -l 4545"; then
    ( while true; do setsid nc -l 4545 >>"$ROOT/shell.log" 2>/dev/null; sleep 1; done \
        </dev/null >>"$LOG" 2>&1 & )
    echo "shell listener :4545 up"
  fi
}

cmd() {
  local before after
  before=$(ls -1 "$ROOT"/out_*.txt 2>/dev/null | wc -l)
  printf '%s\n' "$1" > "$ROOT/cmd"
  echo "(sent; waiting up to 60s for the robot's next poll…)"
  for _ in $(seq 1 60); do
    sleep 1
    after=$(ls -1 "$ROOT"/out_*.txt 2>/dev/null | wc -l)
    if [ "$after" -gt "$before" ]; then
      sleep 2                      # let the output finish landing
      cat "$(ls -t "$ROOT"/out_*.txt | head -1)"
      echo "# no-op" > "$ROOT/cmd" # neutralize
      return 0
    fi
  done
  echo "no poll arrived — is the robot awake? retry or check wake."
  echo "# no-op" > "$ROOT/cmd"
  return 1
}

put() {
  cp "$1" "$ROOT/"
  echo "staged: http://<this-host>:8081/$(basename "$1")"
}

stop() {
  pkill -f "http.server 8081" 2>/dev/null
  pkill -f "nc -l 4550" 2>/dev/null
  pkill -f "nc -l 4545" 2>/dev/null
  pkill -f "server.sh" 2>/dev/null
  echo "stopped"
}

case "${1:-}" in
  start) start ;;
  cmd)   shift; cmd "$*" ;;
  put)   put "${2:?file}" ;;
  stop)  stop ;;
  *) echo "usage: $0 start|cmd '<shell cmd>'|put <file>|stop"; exit 1 ;;
esac
