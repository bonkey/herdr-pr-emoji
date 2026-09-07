#!/usr/bin/env bash
# One long-lived loop: every cycle, resolve each workspace's branch locally,
# ask GitHub once per repository for the newest pull request of each branch,
# and publish one emoji as the `pr_emoji` token on the workspace (Space rows)
# and on each of its panes (Agent rows).
#
#   bash daemon.sh                      loop; started by herdr as a [[startup]] hook
#   bash daemon.sh --once               one cycle, then exit (manual refresh, development)
#   bash daemon.sh --query owner/name   branches on stdin, "branch<TAB>emoji" on stdout
#
# Never calls the herdr API method `worktree.list`: herdr answers it by
# enumerating git worktrees on its main thread. `workspace list` and
# `pane list` are served from memory.
set -u

SOURCE="bonkey.pr-emoji"
TOKEN="pr_emoji"
HERDR="${HERDR_BIN_PATH:-herdr}"
STATE="${HERDR_PLUGIN_STATE_DIR:-${TMPDIR:-/tmp}/pr-emoji}"
CONFIG="${HERDR_PLUGIN_CONFIG_DIR:-$STATE}/config.toml"
LOG="$STATE/daemon.log"
PIDFILE="$STATE/daemon.pid"

INTERVAL=120
UNSTABLE="pass"
HERDR_TIMEOUT=10
GIT_TIMEOUT=5
GH_TIMEOUT=30

log() { printf '%s pr-emoji: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; }

# run_timeout SECONDS COMMAND... : like coreutils timeout, which macOS lacks.
# The watchdog gets no inherited fds: an orphaned `sleep` holding the stdout
# pipe would otherwise keep a `$(run_timeout ...)` open for the full timeout.
run_timeout() {
  local secs=$1 pid watchdog rc
  shift
  "$@" &
  pid=$!
  (
    sleep "$secs"
    kill "$pid" 2>/dev/null
  ) >/dev/null 2>&1 </dev/null &
  watchdog=$!
  wait "$pid"
  rc=$?
  kill "$watchdog" 2>/dev/null
  wait "$watchdog" 2>/dev/null
  return "$rc"
}

read_config() {
  local v
  [ -f "$CONFIG" ] || return 0
  v=$(sed -n 's/^[[:space:]]*refreshIntervalSeconds[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$CONFIG" | head -n1)
  if [ -n "$v" ]; then
    if [ "$v" -lt 60 ]; then
      log "refreshIntervalSeconds=$v raised to the 60 s floor"
      v=60
    fi
    INTERVAL=$v
  fi
  v=$(sed -n 's/^[[:space:]]*unstable[[:space:]]*=[[:space:]]*"\([a-z]*\)".*/\1/p' "$CONFIG" | head -n1)
  case $v in pass | warn) UNSTABLE=$v ;; esac
}

# owner/name for a github.com remote, empty for anything else.
github_slug() {
  local url
  url=$(run_timeout "$GIT_TIMEOUT" git -C "$1" remote get-url origin 2>/dev/null) || return 0
  case $url in
    git@github.com:*) url=${url#git@github.com:} ;;
    ssh://git@github.com/*) url=${url#ssh://git@github.com/} ;;
    https://github.com/*) url=${url#https://github.com/} ;;
    *) return 0 ;;
  esac
  url=${url%.git}
  url=${url%/}
  case $url in */*/*) return 0 ;; */*) printf '%s\n' "$url" ;; esac
}

# One GraphQL request: the newest pull request of every branch of one repository.
# stdin: branch names, one per line. stdout: TSV "branch<TAB>emoji". Fails on any error.
query_repo() {
  local slug=$1 owner=${1%%/*} name=${1#*/} branches query out
  branches=$(jq -R . | jq -s .)
  query=$(jq -rn --arg owner "$owner" --arg name "$name" --argjson b "$branches" '
    "query { repository(owner: \($owner|tojson), name: \($name|tojson)) { "
    + ([$b | to_entries[] |
        "b\(.key): pullRequests(headRefName: \(.value|tojson), last: 1, orderBy: {field: CREATED_AT, direction: ASC}) "
        + "{ nodes { isDraft state mergeStateStatus commits(last: 1) { nodes { commit { statusCheckRollup { state } } } } } }"
      ] | join(" "))
    + " } }"')
  out=$(run_timeout "$GH_TIMEOUT" gh api graphql -f query="$query" 2>/dev/null) || return 1
  printf '%s' "$out" | jq -r --argjson b "$branches" --arg unstable "$UNSTABLE" '
    def emoji:
      if . == null then ""
      elif .state == "MERGED" then "🟣"
      elif .state == "CLOSED" then ""
      elif .isDraft then "📝"
      elif .mergeStateStatus == "DIRTY" then "⚠️"
      elif (.commits.nodes[0].commit.statusCheckRollup.state // "") == "PENDING" then "🟡"
      elif .mergeStateStatus == "BLOCKED" then "❌"
      elif .mergeStateStatus == "UNSTABLE" then (if $unstable == "warn" then "⚠️" else "✅" end)
      elif .mergeStateStatus == "CLEAN" or .mergeStateStatus == "BEHIND" or .mergeStateStatus == "HAS_HOOKS" then "✅"
      else "" end;
    if .data.repository == null then error("repository not found") else . end
    | .data.repository as $r
    | $b | to_entries[] | "\(.value)\t\($r["b\(.key)"].nodes[0] | emoji)"'
}

# publish KIND ID EMOJI
publish() {
  local ttl=$((INTERVAL * 3 * 1000))
  if [ -n "$3" ]; then
    run_timeout "$HERDR_TIMEOUT" "$HERDR" "$1" report-metadata "$2" --source "$SOURCE" --token "$TOKEN=$3" --ttl-ms "$ttl" >/dev/null 2>&1
  else
    run_timeout "$HERDR_TIMEOUT" "$HERDR" "$1" report-metadata "$2" --source "$SOURCE" --clear-token "$TOKEN" >/dev/null 2>&1
  fi
}

# One cycle. Returns 1 only when herdr itself is unreachable.
cycle() {
  local workspaces panes rows ws dir branch slug repos results line emoji pane
  workspaces=$(run_timeout "$HERDR_TIMEOUT" "$HERDR" workspace list 2>/dev/null) || return 1
  panes=$(run_timeout "$HERDR_TIMEOUT" "$HERDR" pane list 2>/dev/null) || return 1

  # ws<TAB>dir : the worktree checkout, else the first pane's cwd.
  rows=$(jq -r --argjson panes "$panes" '
    .result.workspaces[] | .workspace_id as $id
    | [$id, (.worktree.checkout_path // ([$panes.result.panes[] | select(.workspace_id == $id) | .cwd][0] // ""))]
    | @tsv' <<<"$workspaces")

  # ws<TAB>slug<TAB>branch, resolved locally.
  results=""
  repos=""
  while IFS=$'\t' read -r ws dir; do
    [ -n "$ws" ] || continue
    slug=""
    branch=""
    if [ -d "$dir" ]; then
      branch=$(run_timeout "$GIT_TIMEOUT" git -C "$dir" branch --show-current 2>/dev/null) || branch=""
      [ -n "$branch" ] && slug=$(github_slug "$dir")
    fi
    results="$results$ws	$slug	$branch
"
    [ -n "$slug" ] && repos="$repos$slug
"
  done <<<"$rows"

  # One GitHub call per repository; a failure clears its workspaces.
  local lookups=""
  for slug in $(printf '%s' "$repos" | sort -u); do
    line=$(printf '%s' "$results" | awk -F'\t' -v s="$slug" '$2 == s && $3 != "" { print $3 }' | sort -u | query_repo "$slug") || {
      log "$slug: GitHub query failed, clearing its workspaces"
      continue
    }
    lookups="$lookups$(printf '%s\n' "$line" | sed "s#^#$slug	#")
"
  done

  while IFS=$'\t' read -r ws slug branch; do
    [ -n "$ws" ] || continue
    emoji=""
    if [ -n "$slug" ]; then
      emoji=$(printf '%s' "$lookups" | awk -F'\t' -v s="$slug" -v b="$branch" '$1 == s && $2 == b { print $3; exit }')
    fi
    publish workspace "$ws" "$emoji" || log "workspace $ws: report-metadata failed"
    for pane in $(jq -r --arg id "$ws" '.result.panes[] | select(.workspace_id == $id) | .pane_id' <<<"$panes"); do
      publish pane "$pane" "$emoji" || log "pane $pane: report-metadata failed"
    done
  done <<<"$results"
  return 0
}

main() {
  local once=0 failures=0 old
  [ "${1:-}" = "--once" ] && once=1
  mkdir -p "$STATE"
  read_config

  if ! command -v gh >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
    log "gh and jq are required; not starting"
    return 0
  fi

  if [ "${1:-}" = "--query" ]; then
    query_repo "${2:?usage: daemon.sh --query owner/name}"
    return $?
  fi
  if [ "$once" = 1 ]; then
    cycle || log "herdr unreachable"
    return 0
  fi

  # Single instance: a daemon launched by hand survives a server restart, so
  # replace whatever holds the pidfile.
  if [ -f "$PIDFILE" ]; then
    old=$(cat "$PIDFILE" 2>/dev/null)
    if [ -n "$old" ] && [ "$old" != "$$" ] && kill -0 "$old" 2>/dev/null; then
      log "stopping previous daemon $old"
      kill "$old" 2>/dev/null
    fi
  fi
  printf '%s\n' "$$" >"$PIDFILE"
  trap 'rm -f "$PIDFILE"; exit 0' TERM INT

  # herdr captures the startup hook's output until it exits; log to a file instead.
  [ -f "$LOG" ] && [ "$(wc -c <"$LOG")" -gt 1000000 ] && : >"$LOG"
  exec >>"$LOG" 2>&1
  log "started pid $$, interval ${INTERVAL}s, unstable=$UNSTABLE"

  while :; do
    if cycle; then
      failures=0
    else
      failures=$((failures + 1))
      log "herdr unreachable ($failures/3)"
      if [ "$failures" -ge 3 ]; then
        log "herdr is gone, exiting"
        rm -f "$PIDFILE"
        return 0
      fi
    fi
    sleep "$INTERVAL" &
    wait $!
  done
}

main "$@"
