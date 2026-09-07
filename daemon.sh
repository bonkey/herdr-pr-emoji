#!/usr/bin/env bash
# One long-lived loop: every cycle, resolve each workspace's branch locally,
# ask GitHub in one request for the newest pull request of every branch (plus
# one for the required checks of PRs with failures), and publish one emoji as the `pr_emoji` token on the workspace (Space rows)
# and on each of its panes (Agent rows).
#
#   bash daemon.sh                      loop; started by herdr as a [[startup]] hook
#   bash daemon.sh --once               one cycle, then exit (manual refresh, development)
#   bash daemon.sh --query owner/name   branches on stdin, "branch<TAB>emoji" on stdout
#   bash daemon.sh --resolve            "owner/name<TAB>branch" on stdin, "…<TAB>emoji" on stdout
#   bash daemon.sh --map                lookup JSON on stdin, "slug<TAB>branch<TAB>emoji" out
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

# `gh api graphql` exits 1 when the response carries `errors`, even with partial
# data (a repository the token cannot see). Keep whatever came back; the
# callers treat missing aliases as "no pull request".
gh_graphql() {
  local out
  out=$(run_timeout "$GH_TIMEOUT" gh api graphql -f query="$1" 2>/dev/null)
  printf '%s' "$out" | jq -e '.data != null' >/dev/null 2>&1 || return 1
  printf '%s' "$out"
}

# Newest pull request of every branch, all repositories in one GraphQL request.
# stdin: "owner/name<TAB>branch" lines. stdout: JSON array of
# {slug, branch, number, isDraft, state, mergeStateStatus, rollup}. Fails on any error.
lookup_prs() {
  local pairs query out
  pairs=$(jq -Rn '[inputs | split("\t") | {slug: .[0], branch: .[1]}] | unique')
  query=$(jq -rn --argjson p "$pairs" '
    ($p | group_by(.slug) | to_entries | map(
      .key as $ri | .value[0].slug as $slug |
      "r\($ri): repository(owner: \($slug | split("/")[0] | tojson), name: \($slug | split("/")[1] | tojson)) { "
      + ([.value | to_entries[] |
          "b\(.key): pullRequests(headRefName: \(.value.branch | tojson), last: 1, orderBy: {field: CREATED_AT, direction: ASC}) "
          + "{ nodes { number isDraft state mergeStateStatus commits(last: 1) { nodes { commit { statusCheckRollup { state } } } } } }"
        ] | join(" "))
      + " }") | join(" ")) as $body
    | "query { \($body) }"')
  out=$(gh_graphql "$query") || return 1
  printf '%s' "$out" | jq -c --argjson p "$pairs" '
    if .data == null then error("no data") else . end
    | .data as $d
    | [$p | group_by(.slug) | to_entries[] | .key as $ri | .value | to_entries[]
       | .value as $pair | ($d["r\($ri)"] // {})["b\(.key)"].nodes[0] as $pr
       | {slug: $pair.slug, branch: $pair.branch}
         + (if $pr == null then {} else
             {number: $pr.number, isDraft: $pr.isDraft, state: $pr.state, mergeStateStatus: $pr.mergeStateStatus,
              rollup: ($pr.commits.nodes[0].commit.statusCheckRollup.state // "")} end)]'
}

# For the open PRs whose rollup reports a failure: of their *required* checks, is any
# failing, and is any still running? One GraphQL request for all of them. A re-run check
# keeps every earlier attempt in the rollup, so only the newest attempt of each check
# name counts, the way branch protection counts it, and an attempt still in flight is
# the newest whatever its timestamp says: a queued run can report a `startedAt` days
# old. ACTION_REQUIRED is the
# missing-review gate rather than a broken check, and `mergeStateStatus` already
# reports that as BLOCKED. `statusCheckRollup.state` cannot answer the running
# question here: GitHub reports FAILURE for the whole rollup as soon as one context
# fails, however many are still queued. stdin: the lookup JSON. stdout: the same JSON
# with `required_failing` and `required_running` set. Fails on any error.
mark_required_state() {
  local prs targets query out
  prs=$(cat)
  targets=$(printf '%s' "$prs" | jq -c '[.[] | select(.state == "OPEN" and (.rollup == "FAILURE" or .rollup == "ERROR")) | {slug, number}] | unique')
  if [ "$targets" = "[]" ]; then
    printf '%s' "$prs" | jq -c 'map(. + {required_failing: false, required_running: false})'
    return 0
  fi
  query=$(jq -rn --argjson t "$targets" '
    "query { " + ([$t | to_entries[] |
      "p\(.key): repository(owner: \(.value.slug | split("/")[0] | tojson), name: \(.value.slug | split("/")[1] | tojson)) "
      + "{ pullRequest(number: \(.value.number)) { commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) { nodes { __typename "
      + "... on CheckRun { name status conclusion startedAt completedAt isRequired(pullRequestNumber: \(.value.number)) } "
      + "... on StatusContext { context state createdAt isRequired(pullRequestNumber: \(.value.number)) } } } } } } } } }"
    ] | join(" ")) + " }"')
  out=$(gh_graphql "$query") || return 1
  printf '%s' "$out" | jq -c --argjson t "$targets" --argjson prs "$prs" '
    if .data == null then error("no data") else . end
    | .data as $d
    | [$t | to_entries[]
       | ([($d["p\(.key)"].pullRequest.commits.nodes[0].commit.statusCheckRollup.contexts.nodes // [])[]
           | select(.isRequired == true)
           | {name: (.name // .context // ""),
              at: (.completedAt // .startedAt // .createdAt // ""),
              result: (.conclusion // .state // ""),
              running: ((.status | IN("QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED")) or (.state // "") == "PENDING")}]
          | group_by(.name) | map(max_by([.running, .at]))) as $latest
       | .value + {
           failing: ([$latest[] | select(.result | IN("FAILURE", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE", "ERROR"))] | length > 0),
           running: ([$latest[] | select(.running)] | length > 0)}] as $marks
    | $prs | map(. as $pr
        | ([$marks[] | select(.slug == $pr.slug and .number == $pr.number)][0]) as $m
        | . + {required_failing: ($m.failing // false), required_running: ($m.running // false)})'
}

# Lookup JSON on stdin, "slug<TAB>branch<TAB>emoji" on stdout. First match wins.
map_emoji() {
  jq -r --arg unstable "$UNSTABLE" '
    def emoji:
      if .number == null then ""
      elif .state == "MERGED" then "🟣"
      elif .state == "CLOSED" then ""
      elif .isDraft then "📝"
      elif .mergeStateStatus == "DIRTY" then "⚠️"
      elif .required_failing then "❌"
      elif .rollup == "PENDING" or .required_running then "🟡"
      elif .mergeStateStatus == "BLOCKED" then "🛑"
      elif .mergeStateStatus == "UNSTABLE" then (if $unstable == "warn" then "⚠️" else "✅" end)
      elif .mergeStateStatus == "CLEAN" or .mergeStateStatus == "BEHIND" or .mergeStateStatus == "HAS_HOOKS" then "✅"
      else "" end;
    .[] | "\(.slug)\t\(.branch)\t\(emoji)"'
}

# "slug<TAB>branch" lines in, "slug<TAB>branch<TAB>emoji" lines out.
# A failed lookup fails the whole resolve; a failed required-check query only
# loses the ❌ and 🟡 verdicts for this cycle, leaving those PRs on 🛑.
resolve() {
  local prs marked
  prs=$(lookup_prs) || return 1
  if marked=$(printf '%s' "$prs" | mark_required_state); then
    prs=$marked
  else
    log "required-check query failed, ❌ and 🟡 fall back to 🛑 this cycle"
    prs=$(printf '%s' "$prs" | jq -c 'map(. + {required_failing: false, required_running: false})')
  fi
  printf '%s' "$prs" | map_emoji
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
  local workspaces panes rows ws dir branch slug results emoji pane
  workspaces=$(run_timeout "$HERDR_TIMEOUT" "$HERDR" workspace list 2>/dev/null) || return 1
  panes=$(run_timeout "$HERDR_TIMEOUT" "$HERDR" pane list 2>/dev/null) || return 1

  # ws<TAB>dir : the worktree checkout, else the first pane's cwd.
  rows=$(jq -r --argjson panes "$panes" '
    .result.workspaces[] | .workspace_id as $id
    | [$id, (.worktree.checkout_path // ([$panes.result.panes[] | select(.workspace_id == $id) | .cwd][0] // ""))]
    | @tsv' <<<"$workspaces")

  # ws<TAB>slug<TAB>branch, resolved locally.
  results=""
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
  done <<<"$rows"

  # Two GitHub calls for everything; a failure clears every workspace.
  local lookups
  lookups=$(printf '%s' "$results" | awk -F'\t' '$2 != "" && $3 != "" { print $2 "\t" $3 }' | sort -u)
  if [ -n "$lookups" ]; then
    lookups=$(printf '%s\n' "$lookups" | resolve) || {
      log "GitHub lookup failed, clearing every workspace"
      lookups=""
    }
  fi

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
    sed "s#^#${2:?usage: daemon.sh --query owner/name}	#" | resolve | cut -f2-
    return $?
  fi
  if [ "${1:-}" = "--resolve" ]; then
    resolve
    return $?
  fi
  if [ "${1:-}" = "--map" ]; then
    map_emoji
    return $?
  fi
  if [ "$once" = 1 ]; then
    cycle || log "herdr unreachable"
    return 0
  fi

  # herdr captures the startup hook's output until it exits; log to a file instead.
  [ -f "$LOG" ] && [ "$(wc -c <"$LOG")" -gt 1000000 ] && : >"$LOG"
  exec >>"$LOG" 2>&1

  # Single instance: a daemon launched by hand survives a server restart, so
  # replace whatever holds the pidfile, waiting for it to actually go away.
  if [ -f "$PIDFILE" ]; then
    old=$(cat "$PIDFILE" 2>/dev/null)
    if [ -n "$old" ] && [ "$old" != "$$" ] && kill -0 "$old" 2>/dev/null; then
      log "stopping previous daemon $old"
      kill "$old" 2>/dev/null
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$old" 2>/dev/null || break
        sleep 0.5
      done
      if kill -0 "$old" 2>/dev/null; then
        log "previous daemon $old ignored TERM, killing it"
        kill -9 "$old" 2>/dev/null
        sleep 0.5
      fi
    fi
  fi
  printf '%s\n' "$$" >"$PIDFILE"
  # Remove the pidfile only while it is still ours: a successor may already own it.
  trap '[ "$(cat "$PIDFILE" 2>/dev/null)" = "$$" ] && rm -f "$PIDFILE"; exit 0' TERM INT
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
