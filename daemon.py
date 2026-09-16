#!/usr/bin/env python3
# One long-lived loop: every cycle, resolve each workspace's branch locally,
# ask GitHub in one request for the newest pull request of every branch (plus
# one for the required checks and open conversations of the PRs that fail or
# are blocked), and publish one emoji as the `pr_emoji` token on the workspace
# (Space rows) and on each of its panes (Agent rows). Where a `signoffCommand`
# is configured, one more glyph rides behind it with the state of a review that
# happens outside GitHub.
#
#   python3 daemon.py                      loop; started by herdr as a [[startup]] hook
#   python3 daemon.py --once               one cycle, then exit (manual refresh, development)
#   python3 daemon.py --query owner/name   branches on stdin, "branch<TAB>emoji" on stdout
#   python3 daemon.py --resolve            "owner/name<TAB>branch" on stdin, "…<TAB>emoji" out
#   python3 daemon.py --sort               order the worktrees of every group by state, then name
#   python3 daemon.py --sort-name          order the worktrees of every group by name
#
# Never calls the herdr API method `worktree.list`: herdr answers it by
# enumerating git worktrees on its main thread. `workspace list` and
# `pane list` are served from memory.

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time

SOURCE = "bonkey.pr-emoji"
TOKEN = "pr_emoji"
HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"


def plugin_dir(passed, home, fallback, *parts):
    """The plugin directory herdr passes in the environment, or herdr's own.

    The server exports HERDR_PLUGIN_STATE_DIR and HERDR_PLUGIN_CONFIG_DIR for the
    daemon it starts. A daemon launched by hand resolves the same paths from the
    XDG base directories, so both copies share one pid file and one config file.
    """
    return os.environ.get(passed) or os.path.join(
        os.environ.get(home) or os.path.expanduser(fallback),
        "herdr",
        "plugins",
        *parts,
        SOURCE,
    )


STATE = plugin_dir("HERDR_PLUGIN_STATE_DIR", "XDG_STATE_HOME", "~/.local/state")
CONFIG = os.path.join(
    plugin_dir("HERDR_PLUGIN_CONFIG_DIR", "XDG_CONFIG_HOME", "~/.config", "config"),
    "config.toml",
)
LOG = os.path.join(STATE, "daemon.log")
PIDFILE = os.path.join(STATE, "daemon.pid")
STATES = os.path.join(STATE, "states.json")
LOG_LIMIT = 1000000

DEFAULT_INTERVAL = 120
MIN_INTERVAL = 60
DEFAULT_UNSTABLE = "ok"
HERDR_TIMEOUT = 10
GIT_TIMEOUT = 5
GH_TIMEOUT = 30
DEFAULT_SIGNOFF_TIMEOUT = 20

# A required check whose newest attempt ended this way is real breakage.
# ACTION_REQUIRED is missing, not broken: `mergeStateStatus` reports it as BLOCKED.
FAILING_RESULTS = frozenset(
    ["FAILURE", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE", "ERROR"]
)
RUNNING_STATUS = frozenset(["QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"])
MERGEABLE_STATUS = frozenset(["CLEAN", "BEHIND", "HAS_HOOKS"])
# A required check whose newest attempt ended this way asks for nothing more.
# GitHub merges over a skipped or neutral required check.
SATISFIED_RESULTS = frozenset(["SUCCESS", "NEUTRAL", "SKIPPED"])

EMOJI = {
    "no_pr": "❔",
    "merged": "🟣",
    "closed": "🚪",
    "draft": "📝",
    "queued": "🚂",
    "conflict": "⚠️",
    "failing_growing": "🟠",
    "failing": "❌",
    "running": "🟡",
    "review": "👀",
    "ejected": "🪃",
    "blocked": "🛑",
    "unstable": "🆗",
    "mergeable": "✅",
    "conversation": "💬",
    # The sign-off a `signoffCommand` reports, behind the blocker and its 💬.
    # These four name no blocker and take no sort rank: they say what a review
    # outside GitHub asks for, which the pull request itself cannot report.
    "signoff_not_required": "➖",
    "signoff_missing": "📭",
    "signoff_open": "🎫",
    "signoff_done": "🏁",
}
# Octicons, GitHub's own icon language, as a Nerd Font carries them. One of
# them is drawn for the merge queue. Codepoints from the Nerd Fonts glyph
# table rather than from memory; every one is a single cell, where an emoji is
# two, so a row of these is half the width.
NERD = {
    "no_pr": "\uF420",  # oct-question
    "merged": "\uF419",  # oct-git_merge
    "closed": "\uF4DC",  # oct-git_pull_request_closed
    "draft": "\uF4DD",  # oct-git_pull_request_draft
    "queued": "\uF4DB",  # oct-git_merge_queue
    "conflict": "\uF421",  # oct-alert
    "failing_growing": "\uF52F",  # oct-x_circle
    "failing": "\uF530",  # oct-x_circle_fill
    "running": "\uF46A",  # oct-sync
    "review": "\uF441",  # oct-eye
    "ejected": "\uF426",  # oct-sign_out
    "blocked": "\uF4F4",  # oct-no_entry
    "unstable": "\uF42E",  # oct-check
    "mergeable": "\uF4A4",  # oct-check_circle_fill
    "conversation": "\uF442",  # oct-comment_discussion
    # One shield family for the three sign-off states that ask for something,
    # so they tell each other apart by shape: the cell takes the blocker's
    # colour, and a glyph behind it is drawn in that same colour.
    "signoff_not_required": "\uF48B",  # oct-dash
    "signoff_missing": "\uF49C",  # oct-shield
    "signoff_open": "\uF513",  # oct-shield_x
    "signoff_done": "\uF510",  # oct-shield_check
}
ICON_SETS = {"emoji": EMOJI, "nerd": NERD}
DEFAULT_ICON_SET = "nerd"
SIGNOFF_PREFIX = "signoff_"
# What a `signoffCommand` may answer: the glyph table is the vocabulary, so a
# state nothing can draw is a state the hook cannot report.
SIGNOFF_STATES = frozenset(
    name[len(SIGNOFF_PREFIX) :] for name in EMOJI if name.startswith(SIGNOFF_PREFIX)
)
# The name of every blocker state, as a glyph table: `blocker_for` reads its
# verdict out of whatever table it is handed, so this one makes it answer with
# the name rather than the glyph. A sign-off is not a blocker and is left out.
STATE_NAMES = {name: name for name in EMOJI if not name.startswith(SIGNOFF_PREFIX)}

# The order `--sort` puts a worktree group in: what a hand of yours is needed
# for first, then what waits on somebody else, then what has no pull request
# to speak of, then what is finished. A state the file does not record — a
# row nobody has answered for yet, or one with nothing to say — goes last.
SORT_ORDER = [
    "mergeable",  # ✅ press the button
    "unstable",  # 🆗 the same, once you decide the optional failures do not matter
    "conflict",  # ⚠️ rebase it
    "failing",  # ❌ fix it
    "failing_growing",  # 🟠 fix it, and more may be coming
    "ejected",  # 🪃 queue it again, once you know why it came back
    "conversation",  # 💬 answer the threads
    "blocked",  # 🛑 find out why
    "review",  # 👀 somebody else's turn
    "running",  # 🟡 CI's turn
    "queued",  # 🚂 the queue's turn
    "draft",  # 📝 not up for merging yet
    "no_pr",  # ❔ not up for anything yet
    "merged",  # 🟣 done
    "closed",  # 🚪 done with
]
SORT_RANK = {name: rank for rank, name in enumerate(SORT_ORDER)}


def icon_set(name, unstable):
    """The glyph for every state, with the `unstable` setting resolved into it.

    Carrying the resolved table instead of the setting keeps the choice in one
    place: nothing downstream has to know that UNSTABLE is configurable.
    """
    icons = dict(ICON_SETS.get(name) or ICON_SETS[DEFAULT_ICON_SET])
    if unstable == "warn":
        icons["unstable"] = icons["conflict"]
    elif unstable == "pass":
        icons["unstable"] = icons["mergeable"]
    return icons


DEFAULT_ICONS = icon_set(DEFAULT_ICON_SET, DEFAULT_UNSTABLE)


def log(message):
    sys.stderr.write(
        "%s pr-emoji: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    )
    sys.stderr.flush()


def run(argv, timeout, text=None):
    """(returncode, stdout, stderr). A timed-out command returns 124, like coreutils.

    `text` is written to the command's stdin, which otherwise reads nothing.
    `subprocess.run` refuses `stdin` and `input` together, so only one of them
    is ever passed.
    """
    feed = (
        {"input": text.encode("utf-8")}
        if text is not None
        else {"stdin": subprocess.DEVNULL}
    )
    try:
        done = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            **feed
        )
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %ss" % timeout
    except OSError as exc:
        return 127, "", str(exc)
    return (
        done.returncode,
        done.stdout.decode("utf-8", "replace"),
        done.stderr.decode("utf-8", "replace"),
    )


def read_config(path):
    """(interval, icons) from config.toml, defaults for anything it does not set."""
    interval, unstable, icons = DEFAULT_INTERVAL, DEFAULT_UNSTABLE, DEFAULT_ICON_SET
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return interval, icon_set(icons, unstable)
    found = re.search(
        r"^[ \t]*refreshIntervalSeconds[ \t]*=[ \t]*(\d+)", text, re.MULTILINE
    )
    if found:
        interval = int(found.group(1))
        if interval < MIN_INTERVAL:
            log(
                "refreshIntervalSeconds=%d raised to the %d s floor"
                % (interval, MIN_INTERVAL)
            )
            interval = MIN_INTERVAL
    found = re.search(r'^[ \t]*unstable[ \t]*=[ \t]*"([a-z]*)"', text, re.MULTILINE)
    if found and found.group(1) in ("ok", "pass", "warn"):
        unstable = found.group(1)
    found = re.search(r'^[ \t]*icons[ \t]*=[ \t]*"([a-z]*)"', text, re.MULTILINE)
    if found and found.group(1) in ICON_SETS:
        icons = found.group(1)
    return interval, icon_set(icons, unstable)


def read_signoff(path):
    """(command, timeout) for the sign-off hook; an empty command means none.

    Read apart from the rest of the configuration because the hook is optional
    in a way the other settings are not: without a command nothing is run, no
    row grows a third glyph, and the cycle costs exactly what it did before.
    """
    command, timeout = "", DEFAULT_SIGNOFF_TIMEOUT
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return command, timeout
    found = re.search(
        r'^[ \t]*signoffCommand[ \t]*=[ \t]*"([^"]*)"', text, re.MULTILINE
    )
    if found and found.group(1).strip():
        command = os.path.expanduser(found.group(1).strip())
    found = re.search(
        r"^[ \t]*signoffTimeoutSeconds[ \t]*=[ \t]*(\d+)", text, re.MULTILINE
    )
    if found and int(found.group(1)) > 0:
        timeout = int(found.group(1))
    return command, timeout


# ---------------------------------------------------------------- pure decisions


def required_state(contexts, expected=()):
    """(failing, running, unsatisfied) over the newest attempt of every
    required check.

    A re-run keeps every earlier attempt in the rollup, so only the newest
    attempt of a check name counts, the way branch protection counts it. An
    attempt still in flight is the newest whatever its timestamp says: a queued
    run can report a `startedAt` days old.

    `expected` is the base branch's required contexts. One that has posted
    nothing at all is absent from the rollup rather than passing, and GitHub
    blocks the merge on it as "Expected - waiting for status to be reported",
    so it counts as running. A name counts as reported however GitHub marked
    it: a context whose `isRequired` says false while branch protection asks
    for the same name would otherwise stay pending for good.

    `unsatisfied` is [(name, result)] for every required check whose newest
    attempt did not succeed, sorted by name, with `EXPECTED` as the result of
    one that has reported nothing. It names what the row cannot: 🛑 says a pull
    request is blocked, and this says which check does the blocking, which is
    what a `signoffCommand` reads to tell a gate it cares about from one it
    does not. SKIPPED and NEUTRAL count as satisfied, the way GitHub counts
    them — a skipped required check does not block a merge.
    """
    latest = {}
    reported = set()
    for context in contexts:
        name = context.get("name") or context.get("context") or ""
        reported.add(name)
        if context.get("isRequired") is not True:
            continue
        at = (
            context.get("completedAt")
            or context.get("startedAt")
            or context.get("createdAt")
            or ""
        )
        result = context.get("conclusion") or context.get("state") or ""
        running = (
            context.get("status") in RUNNING_STATUS
            or (context.get("state") or "") == "PENDING"
        )
        rank = (running, at)
        if name not in latest or rank >= latest[name][0]:
            latest[name] = (rank, result, running)
    failing = any(result in FAILING_RESULTS for _, result, _ in latest.values())
    running = any(running for _, _, running in latest.values()) or any(
        name not in reported for name in expected
    )
    unsatisfied = {
        name: result
        for name, (_, result, _) in latest.items()
        if result not in SATISFIED_RESULTS
    }
    unsatisfied.update(
        (name, "EXPECTED") for name in expected if name not in reported
    )
    return failing, running, sorted(unsatisfied.items())


def blocker_for(pr, icons=DEFAULT_ICONS):
    """The one thing most worth doing about a pull request. First match wins.

    A branch with no pull request reads ❔. An empty answer is reserved for a
    row with nothing to say — no branch, or a remote that is not GitHub, both
    of which `plan_publications` blanks — and for `UNKNOWN`, where GitHub has
    yet to compute mergeability and the next poll decides.

    `reviewDecision` is asked without `mergeStateStatus`: a missing review is a
    fact of its own, and GitHub reports it whether the merge state says
    BLOCKED, BEHIND or UNSTABLE. Gating it on BLOCKED would read ✅ for a pull
    request nobody has reviewed.

    🟠 separates a failure that is still growing from a settled one: while
    other required checks run, the list of what to fix is incomplete, and
    fixing it now invites a second pass.

    🚂 is read early because the queue owns the pull request while it holds
    it. The facts a queued pull request still reports are the ones the queue
    acts on itself, by throwing it out, which 🪃 then reports.

    🪃 is read late for the opposite reason: an ejection is a refusal rather
    than a fix, and every rung above it names something that would make the
    pull request acceptable again. What it replaces is the ✅ below it, where
    the merge group broke on another pull request and this one's own checks
    are green.
    """
    if pr.get("number") is None:
        return icons["no_pr"]
    if pr.get("state") == "MERGED":
        return icons["merged"]
    if pr.get("state") == "CLOSED":
        # The trunk keeps whatever pull request last carried its name, and
        # `last: 1` goes on finding it for as long as the branch exists, so a
        # door on the default branch is history nobody acts on.
        return "" if pr.get("on_default_branch") else icons["closed"]
    if pr.get("isDraft"):
        return icons["draft"]
    if pr.get("in_merge_queue"):
        return icons["queued"]
    status = pr.get("mergeStateStatus")
    if status == "DIRTY":
        return icons["conflict"]
    if pr.get("required_failing"):
        return (
            icons["failing_growing"] if pr.get("required_running") else icons["failing"]
        )
    if pr.get("rollup") == "PENDING" or pr.get("required_running"):
        return icons["running"]
    if pr.get("reviewDecision") == "REVIEW_REQUIRED":
        return icons["review"]
    if pr.get("ejected"):
        return icons["ejected"]
    if status == "BLOCKED":
        return icons["blocked"]
    if status == "UNSTABLE":
        return icons["unstable"]
    if status in MERGEABLE_STATUS:
        return icons["mergeable"]
    return ""


def conversation_block(pull):
    """True when the base branch asks for resolved conversations and a thread
    is still open.

    An unresolved thread is ordinary on a pull request whose base branch never
    asks about it, so the protection rule decides whether it means anything. A
    rule the token cannot read is a null rule, which answers False and leaves
    the pull request on 🛑 — the fallback `requiredStatusCheckContexts`
    already gets, and the one a repository governed by rulesets rather than
    branch protection gets too.

    Only the first hundred threads are asked for. Past that the answer is 🛑
    again, never a 💬 the pull request has not been shown to have earned.
    GitHub counts an outdated thread as unresolved, so `isOutdated` decides
    nothing here.
    """
    rule = (pull.get("baseRef") or {}).get("branchProtectionRule") or {}
    if not rule.get("requiresConversationResolution"):
        return False
    threads = (pull.get("reviewThreads") or {}).get("nodes") or []
    return any(thread.get("isResolved") is False for thread in threads)


def queue_ejection(node):
    """True when the last thing that happened to this pull request was the
    merge queue letting go of it, and not by merging it.

    The timeline is the state file, and GitHub keeps it. `timelineItems` asks
    which of four things happened most recently — the queue took it, the queue
    let it go, somebody pushed, somebody force-pushed — so a fix clears this
    and so does queueing it again, with nothing remembered between cycles.

    Every exit from the queue emits the same removal event, and `reason` tells
    them apart: `merged` means it worked, `manual` means a person took it out
    and knows. Every other reason counts, including one GitHub has yet to
    invent, because an unfamiliar reason that fell through would read ✅ about
    a pull request nothing is going to merge. A removal with no reason
    recorded is no evidence, and says nothing.
    """
    items = (node.get("timelineItems") or {}).get("nodes") or []
    newest = (items[-1] if items else None) or {}
    if newest.get("__typename") != "RemovedFromMergeQueueEvent":
        return False
    reason = newest.get("reason") or ""
    return bool(reason) and reason not in ("merged", "manual")


def verdict_for(pr):
    """(state, conversation): the blocker by name, and whether 💬 rides along.

    A missing review and an unresolved conversation are two errands for two
    people: the reviewer who has not looked yet, and whoever answers the
    threads an earlier reviewer or a review bot left behind. One emoji cannot
    ask for both, so 💬 is the one modifier that rides along, and the token
    is never wider than two.

    🛑 is replaced rather than suffixed: it means "blocked, and this plugin
    cannot say why", and open conversations say why. An empty verdict keeps
    its emptiness — UNKNOWN has to go on falling through to whatever the row
    already shows — and a draft swallows 💬 the way it swallows every other
    blocker.
    """
    state = blocker_for(pr, STATE_NAMES)
    if not state or pr.get("isDraft") or not pr.get("conversation_block"):
        return state, False
    if state == "blocked":
        return "conversation", False
    return state, True


def emoji_for(pr, icons=DEFAULT_ICONS):
    """The blocker's glyph, 💬 after it when conversations are open too, and
    the sign-off glyph behind both.

    A row with nothing to say keeps its emptiness: a sign-off glyph on its own
    would report a review of a pull request whose own state went unsaid. A
    draft swallows the sign-off the way it swallows 💬 — the hook is not even
    asked about a draft — and a sign-off nobody answered for draws nothing.
    """
    state, conversation = verdict_for(pr)
    if not state:
        return ""
    glyph = icons[state] + (icons["conversation"] if conversation else "")
    if pr.get("isDraft"):
        return glyph
    return glyph + (icons.get(SIGNOFF_PREFIX + (pr.get("signoff") or "")) or "")


def signoff_input(prs):
    """The hook's stdin, one line per open, undrafted pull request:
    `slug<TAB>branch<TAB>number<TAB>review<TAB>merge state<TAB>unsatisfied`.

    Only the rows whose answer a glyph could show are sent. A merged, closed or
    drafted pull request, and a branch with none at all, are left out, so the
    hook is never asked a question the row would swallow.

    The columns are append-only: a hook that reads the first three goes on
    working whatever is added behind them. The verdict is not among them — it
    now carries the sign-off glyph itself, so passing it in would ask the hook
    to answer with what it was given.

    `unsatisfied` names the required checks that have yet to pass, as
    `name=RESULT` joined by `;`. `reviewDecision` alone cannot report a review
    that a required check asks for and a bot has already approved: GitHub
    answers APPROVED while the check stands at ACTION_REQUIRED. The check names
    say which gate is open, and which of them means a human review is the
    hook's business, not this plugin's.

    It is empty where nothing is unsatisfied and where the plugin did not ask:
    the second query covers the pull requests that report a failure or are
    BLOCKED, which is every one a gate is holding, but not one the merge queue
    is holding.
    """
    lines = []
    for pr in prs:
        if pr.get("number") is None or pr.get("state") != "OPEN" or pr.get("isDraft"):
            continue
        lines.append(
            "%s\t%s\t%d\t%s\t%s\t%s\n"
            % (
                pr["slug"],
                pr["branch"],
                pr["number"],
                pr.get("reviewDecision") or "",
                pr.get("mergeStateStatus") or "",
                unsatisfied_column(pr.get("required_unsatisfied") or ()),
            )
        )
    return "".join(lines)


def unsatisfied_column(unsatisfied):
    """[(name, result)] as `name=RESULT;name=RESULT`.

    A check name is free text, so `;`, `=` and the column separators are
    replaced by a space in it rather than allowed to shift a field. A hook
    matching a name by substring reads the same either way.
    """
    return ";".join(
        "%s=%s" % (column_safe(name), column_safe(result))
        for name, result in unsatisfied
    )


def column_safe(text):
    return re.sub(r"[;=\t\r\n]", " ", str(text)).strip()


def parse_signoff(text):
    """({(slug, branch): state}, [unknown state, ...]) from the hook's stdout.

    One `slug<TAB>branch<TAB>state` line per answer. A line of another shape is
    dropped, and so is a state no glyph answers for — the row then keeps the
    emoji it has until its TTL runs out, which is what an unanswered row gets
    everywhere else. The unknown names are returned rather than logged, so the
    caller does the talking and this stays a decision.
    """
    states, unknown = {}, []
    for line in text.splitlines():
        parts = [part.strip() for part in line.split("\t")]
        if len(parts) != 3 or not parts[0] or not parts[1]:
            continue
        slug, branch, state = parts
        if state not in SIGNOFF_STATES:
            unknown.append(state)
            continue
        states[(slug, branch)] = state
    return states, unknown


def describe_errors(errors):
    """The `errors` array of a GraphQL response as one log line."""
    parts = []
    for error in errors or []:
        message = str(error.get("message", "")).strip() or json.dumps(error)
        path = error.get("path")
        if path:
            message += " [at %s]" % "/".join(str(step) for step in path)
        parts.append(message)
    return "; ".join(parts) or "no error detail"


def lookup_query(pairs):
    """The branch lookup for every repository at once, one alias per branch."""
    body = []
    for repo_index, (slug, branches) in enumerate(group_branches(pairs)):
        owner, name = slug.split("/", 1)
        fields = []
        for branch_index, branch in enumerate(branches):
            fields.append(
                "b%d: pullRequests(headRefName: %s, last: 1, orderBy: {field: CREATED_AT, direction: ASC}) "
                "{ nodes { number isDraft state mergeStateStatus reviewDecision isInMergeQueue "
                "timelineItems(last: 1, itemTypes: [ADDED_TO_MERGE_QUEUE_EVENT, "
                "REMOVED_FROM_MERGE_QUEUE_EVENT, PULL_REQUEST_COMMIT, HEAD_REF_FORCE_PUSHED_EVENT]) "
                "{ nodes { __typename ... on RemovedFromMergeQueueEvent { reason } } } "
                "commits(last: 1) "
                "{ nodes { commit { statusCheckRollup { state } } } } } }"
                % (branch_index, json.dumps(branch))
            )
        body.append(
            "r%d: repository(owner: %s, name: %s) { defaultBranchRef { name } %s }"
            % (repo_index, json.dumps(owner), json.dumps(name), " ".join(fields))
        )
    return "query { %s }" % " ".join(body)


def group_branches(pairs):
    """[(slug, [branch, ...])] in the order the aliases use."""
    grouped = []
    for slug, branch in pairs:
        if grouped and grouped[-1][0] == slug:
            grouped[-1][1].append(branch)
        else:
            grouped.append((slug, [branch]))
    return grouped


def parse_lookup(data, pairs):
    """(prs, unanswered) from the branch lookup's `data`.

    A branch whose alias came back null was not answered — GitHub reported an
    error for it — which is not the same as having no pull request. Its caller
    must leave that branch alone rather than publish an empty emoji.
    """
    prs, unanswered = [], []
    data = data or {}
    for repo_index, (slug, branches) in enumerate(group_branches(pairs)):
        repo = data.get("r%d" % repo_index)
        default_branch = ((repo or {}).get("defaultBranchRef") or {}).get("name") or ""
        for branch_index, branch in enumerate(branches):
            field = repo.get("b%d" % branch_index) if repo is not None else None
            if field is None:
                unanswered.append((slug, branch))
                continue
            nodes = field.get("nodes") or []
            pr = {
                "slug": slug,
                "branch": branch,
                "number": None,
                "on_default_branch": bool(default_branch) and branch == default_branch,
            }
            if nodes:
                node = nodes[0]
                commits = (node.get("commits") or {}).get("nodes") or []
                commit = (commits[0] or {}).get("commit") if commits else None
                rollup = ((commit or {}).get("statusCheckRollup") or {}).get("state")
                pr.update(
                    number=node.get("number"),
                    isDraft=node.get("isDraft"),
                    state=node.get("state"),
                    mergeStateStatus=node.get("mergeStateStatus"),
                    reviewDecision=node.get("reviewDecision") or "",
                    rollup=rollup or "",
                    in_merge_queue=node.get("isInMergeQueue") is True,
                    ejected=queue_ejection(node),
                )
            prs.append(pr)
    return prs, unanswered


def required_targets(prs):
    """The open pull requests whose checks need a closer look, in alias order.

    `statusCheckRollup.state` cannot answer the ❌/🟡/🛑 question on its own:
    GitHub reports FAILURE for the whole rollup as soon as one context fails,
    however many are still queued, and one failing optional check is enough.

    A BLOCKED pull request needs the same look: a required context that has
    posted nothing leaves no trace in the rollup, so a clean-looking BLOCKED
    pull request can still be waiting for a required check.

    A pull request the merge queue holds is skipped: whatever merge state it
    reports, 🚂 is already its whole verdict, so the closer look would buy
    nothing. That also keeps 💬 off it, since conversations are read by the
    same request.
    """
    targets = set()
    for pr in prs:
        if pr.get("state") != "OPEN":
            continue
        if pr.get("in_merge_queue"):
            continue
        if (
            pr.get("rollup") in ("FAILURE", "ERROR")
            or pr.get("mergeStateStatus") == "BLOCKED"
        ):
            targets.add((pr["slug"], pr["number"]))
    return sorted(targets)


def required_query(targets):
    """isRequired(pullRequestNumber:) on every check of every target, plus what
    the base branch requires, one request.

    `requiredStatusCheckContexts` names the checks that have to pass, whether
    or not they have reported: a required context missing from the rollup is
    the difference between 🟡 and 🛑. `requiresConversationResolution` rides
    along in the same rule, and the threads in the same pull request, so 💬
    costs no request of its own.
    """
    body = []
    for index, (slug, number) in enumerate(targets):
        owner, name = slug.split("/", 1)
        body.append(
            "p%d: repository(owner: %s, name: %s) "
            "{ pullRequest(number: %d) "
            "{ baseRef { branchProtectionRule { requiredStatusCheckContexts "
            "requiresConversationResolution } } "
            "reviewThreads(first: 100) { nodes { isResolved } } "
            "commits(last: 1) { nodes { commit { statusCheckRollup "
            "{ contexts(first: 100) { nodes { __typename "
            "... on CheckRun { name status conclusion startedAt completedAt isRequired(pullRequestNumber: %d) } "
            "... on StatusContext { context state createdAt isRequired(pullRequestNumber: %d) } } } } } } } } }"
            % (index, json.dumps(owner), json.dumps(name), number, number, number)
        )
    return "query { %s }" % " ".join(body)


def parse_required(data, targets):
    """{(slug, number): (failing, running)} for the targets the answer covers.

    A token that cannot read the base branch's protection gets a null rule,
    which leaves the expected contexts empty: the verdict then rests on the
    checks that did report.
    """
    marks = {}
    data = data or {}
    for index, target in enumerate(targets):
        repo = data.get("p%d" % index)
        if repo is None:
            continue
        pull = repo.get("pullRequest")
        if pull is None:
            continue
        commits = (pull.get("commits") or {}).get("nodes") or []
        commit = (commits[0] or {}).get("commit") if commits else None
        rollup = (commit or {}).get("statusCheckRollup") or {}
        contexts = (rollup.get("contexts") or {}).get("nodes") or []
        rule = (pull.get("baseRef") or {}).get("branchProtectionRule") or {}
        expected = rule.get("requiredStatusCheckContexts") or []
        marks[target] = required_state(contexts, expected) + (
            conversation_block(pull),
        )
    return marks


def decide(prs, marks, icons=DEFAULT_ICONS, signoffs=None):
    """{(slug, branch): emoji} from the pull requests, their required-check
    marks and the sign-off states the hook answered with.

    A pull request the required-check query did not answer for keeps the
    defaults, so it loses only its ❌, 🟠, 🟡 and 💬 verdicts and stays on 🛑.

    An open pull request no row of `blocker_for` matched gets no verdict at all,
    which leaves its emoji alone rather than erasing it. `UNKNOWN` is GitHub
    asking to be asked again — it invalidates mergeability whenever the base
    branch moves, and computes it only when something requests it, so the very
    query that reports `UNKNOWN` is what makes the next one exact. Publishing
    an empty token for that clears the row, and a status this plugin does not
    recognise is no better a reason to erase a good emoji. The TTL still
    expires whatever nobody refreshes.
    """
    verdicts = {}
    for pr in prs:
        failing, running, unsatisfied, conversations = marks.get(
            (pr["slug"], pr.get("number")), (False, False, (), False)
        )
        pr["required_failing"], pr["required_running"] = failing, running
        pr["required_unsatisfied"] = unsatisfied
        pr["conversation_block"] = conversations
        pr["signoff"] = (signoffs or {}).get((pr["slug"], pr["branch"]), "")
        emoji = emoji_for(pr, icons)
        if not emoji and pr.get("number") is not None and pr.get("state") == "OPEN":
            continue
        verdicts[(pr["slug"], pr["branch"])] = emoji
    return verdicts


def states_of(prs, verdicts):
    """{(slug, branch): state name} for the pull requests `decide` gave a verdict.

    The name is what `--sort` ranks by, where the glyph is what the row shows:
    the same state carries a different glyph in each icon set, and 🆗 borrows
    ⚠️ or ✅ under the `unstable` setting, so the glyph alone cannot be read
    back into a rank. An empty verdict records an empty name, which sorts last.
    """
    return {
        (pr["slug"], pr["branch"]): verdict_for(pr)[0]
        for pr in prs
        if (pr["slug"], pr["branch"]) in verdicts
    }


def plan_states(rows, states, previous):
    """{workspace_id: state name} to write after a cycle.

    A branch GitHub did not answer for keeps the state it had, the way its
    emoji keeps showing; a workspace that is gone is dropped, so the file never
    outgrows the sidebar; a row with nothing to say records an empty name.
    """
    plan = {}
    for workspace_id, slug, branch in rows:
        if not slug or not branch:
            plan[workspace_id] = ""
        elif (slug, branch) in states:
            plan[workspace_id] = states[(slug, branch)]
        elif workspace_id in previous:
            plan[workspace_id] = previous[workspace_id]
    return plan


def plan_publications(rows, verdicts):
    """[(workspace_id, emoji or None)] — None means publish nothing for that row.

    A branch GitHub did not answer for keeps whatever it already shows: the
    token was published with a TTL of three intervals, so a value nobody
    refreshes disappears on its own. Clearing it here instead would blank every
    workspace over one failed request.
    """
    plan = []
    for workspace_id, slug, branch in rows:
        if not slug or not branch:
            plan.append((workspace_id, ""))
        elif (slug, branch) in verdicts:
            plan.append((workspace_id, verdicts[(slug, branch)]))
        else:
            plan.append((workspace_id, None))
    return plan


# --------------------------------------------------------------------- the world


def gh_graphql(query):
    """(data, errors).

    `gh api graphql` exits 1 whenever the response carries an `errors` array,
    even when it also carries usable data, so the exit code alone cannot say
    whether the answer is worth reading. Partial data is worth reading.
    """
    rc, out, err = run(["gh", "api", "graphql", "-f", "query=" + query], GH_TIMEOUT)
    try:
        body = json.loads(out)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        detail = err.strip() or "gh api graphql exited %d with no output" % rc
        return None, [{"message": detail}]
    return body.get("data"), body.get("errors") or []


def run_signoff(command, timeout, text):
    """{(slug, branch): state} from the sign-off hook; empty whenever it cannot
    answer.

    The command runs as one argv with no shell, so nothing in the configured
    string is interpreted, and every row goes to its stdin at once: one
    subprocess a cycle, however many worktrees are open. A hook that times out,
    exits non-zero or cannot be run costs its own glyph and nothing else — the
    pull request state is already decided without it.
    """
    rc, out, err = run([command], timeout, text)
    if rc != 0:
        log(
            "signoffCommand exited %d: %s"
            % (rc, err.strip().splitlines()[-1] if err.strip() else "no output")
        )
        return {}
    states, unknown = parse_signoff(out)
    if unknown:
        log(
            "signoffCommand answered with %s, which no glyph draws"
            % ", ".join(sorted(set(unknown)))
        )
    return states


def resolve(pairs, icons=DEFAULT_ICONS, signoff=None):
    """({(slug, branch): emoji}, {(slug, branch): state}) for the branches
    GitHub answered for.

    `signoff` is the (command, timeout) pair `read_signoff` returns. Without a
    command no hook runs at all.

    Every failure is contained: an unanswered branch is simply absent, and an
    unanswered required-check query only loses the ❌ and 🟡 verdicts of that
    pull request, leaving it on 🛑.
    """
    data, errors = gh_graphql(lookup_query(pairs))
    if errors:
        log("branch lookup reported: %s" % describe_errors(errors))
    prs, unanswered = parse_lookup(data, pairs)
    if unanswered:
        log(
            "no answer for %d branch(es): %s"
            % (len(unanswered), ", ".join("%s %s" % pair for pair in unanswered))
        )
    targets = required_targets(prs)
    marks = {}
    if targets:
        data, errors = gh_graphql(required_query(targets))
        if errors:
            log("required-check query reported: %s" % describe_errors(errors))
        marks = parse_required(data, targets)
        missing = [target for target in targets if target not in marks]
        if missing:
            log(
                "no required checks for %s, ❌ and 🟡 fall back to 🛑"
                % ", ".join("%s#%d" % target for target in missing)
            )
    signoffs = {}
    command, timeout = signoff or ("", DEFAULT_SIGNOFF_TIMEOUT)
    if command:
        text = signoff_input(prs)
        if text:
            signoffs = run_signoff(command, timeout, text)
    verdicts = decide(prs, marks, icons, signoffs)
    return verdicts, states_of(prs, verdicts)


def git_branch(path):
    rc, out, _ = run(["git", "-C", path, "branch", "--show-current"], GIT_TIMEOUT)
    return out.strip() if rc == 0 else ""


def github_slug(path):
    """owner/name for a github.com origin, empty for anything else."""
    rc, out, _ = run(["git", "-C", path, "remote", "get-url", "origin"], GIT_TIMEOUT)
    return slug_from_url(out.strip()) if rc == 0 else ""


def slug_from_url(url):
    """owner/name for a github.com remote URL, empty for anything else."""
    for prefix in ("git@github.com:", "ssh://git@github.com/", "https://github.com/"):
        if url.startswith(prefix):
            url = url[len(prefix):]
            break
    else:
        return ""
    if url.endswith(".git"):
        url = url[: -len(".git")]
    parts = url.rstrip("/").split("/")
    return "/".join(parts) if len(parts) == 2 and all(parts) else ""


def herdr_json(*args):
    rc, out, _ = run([HERDR] + list(args), HERDR_TIMEOUT)
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def publish(kind, target_id, emoji, ttl_ms):
    argv = [HERDR, kind, "report-metadata", target_id, "--source", SOURCE]
    if emoji:
        argv += ["--token", "%s=%s" % (TOKEN, emoji), "--ttl-ms", str(ttl_ms)]
    else:
        argv += ["--clear-token", TOKEN]
    return run(argv, HERDR_TIMEOUT)[0] == 0


def workspace_rows(workspaces, panes):
    """[(workspace_id, checkout_path)] — the worktree checkout, else a pane's cwd."""
    rows = []
    for workspace in (workspaces.get("result") or {}).get("workspaces") or []:
        workspace_id = workspace.get("workspace_id")
        if not workspace_id:
            continue
        path = ((workspace.get("worktree") or {}).get("checkout_path")) or ""
        if not path:
            for pane in (panes.get("result") or {}).get("panes") or []:
                if pane.get("workspace_id") == workspace_id and pane.get("cwd"):
                    path = pane["cwd"]
                    break
        rows.append((workspace_id, path))
    return rows


def panes_of(panes, workspace_id):
    return [
        pane["pane_id"]
        for pane in (panes.get("result") or {}).get("panes") or []
        if pane.get("workspace_id") == workspace_id and pane.get("pane_id")
    ]


def cycle(interval, icons, signoff=None):
    """One cycle. Returns False only when herdr itself is unreachable."""
    workspaces = herdr_json("workspace", "list")
    panes = herdr_json("pane", "list")
    if workspaces is None or panes is None:
        return False

    rows = []
    for workspace_id, path in workspace_rows(workspaces, panes):
        slug, branch = "", ""
        if path and os.path.isdir(path):
            branch = git_branch(path)
            if branch:
                slug = github_slug(path)
        rows.append((workspace_id, slug, branch))

    pairs = sorted({(slug, branch) for _, slug, branch in rows if slug and branch})
    verdicts, states = resolve(pairs, icons, signoff) if pairs else ({}, {})
    save_states(plan_states(rows, states, load_states()))

    ttl_ms = interval * 3 * 1000
    skipped = 0
    for workspace_id, emoji in plan_publications(rows, verdicts):
        if emoji is None:
            skipped += 1
            continue
        if not publish("workspace", workspace_id, emoji, ttl_ms):
            log("workspace %s: report-metadata failed" % workspace_id)
        for pane_id in panes_of(panes, workspace_id):
            if not publish("pane", pane_id, emoji, ttl_ms):
                log("pane %s: report-metadata failed" % pane_id)
    if skipped:
        log("%d workspace(s) keep their last emoji until its TTL runs out" % skipped)
    return True


def guarded_cycle(interval, icons, signoff=None):
    """One cycle, with an unexpected answer contained.

    A payload no parser expected must cost one cycle, not the daemon: without a
    daemon nothing refreshes the tokens and every emoji disappears.
    """
    try:
        return cycle(interval, icons, signoff)
    except Exception as exc:
        log("cycle failed: %r" % exc)
        return True


def read_pairs(stream, slug=None):
    """Sorted (slug, branch) pairs from "owner/name<TAB>branch" lines on a
    stream, or from bare branch names when one slug covers all of them."""
    pairs = set()
    for line in stream:
        line = line.strip()
        if not line:
            continue
        if slug:
            pairs.add((slug, line))
        elif "\t" in line:
            left, right = line.split("\t", 1)
            if left.strip() and right.strip():
                pairs.add((left.strip(), right.strip()))
    return sorted(pairs)


def print_verdicts(pairs, icons, with_slug, signoff=None):
    verdicts, _ = resolve(pairs, icons, signoff)
    for slug, branch in pairs:
        emoji = verdicts.get((slug, branch), "")
        if with_slug:
            print("%s\t%s\t%s" % (slug, branch, emoji))
        else:
            print("%s\t%s" % (branch, emoji))


# ------------------------------------------------------------------- the sort


def load_states():
    """{workspace_id: state name} as the last cycle wrote it; empty without one."""
    try:
        with open(STATES, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return {}
    states = document.get("states") if isinstance(document, dict) else None
    if not isinstance(states, dict):
        return {}
    return {
        key: value
        for key, value in states.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def save_states(states):
    """Write the states file whole, through a rename, so a sort that reads it
    mid-cycle sees the last complete one."""
    temporary = STATES + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "states": states}, handle, indent=2, sort_keys=True)
        os.replace(temporary, STATES)
    except OSError as exc:
        log("states file: %s" % exc)
        try:
            os.remove(temporary)
        except OSError:
            pass


def workspace_records(workspaces):
    """Every workspace as `workspace list` reports it, in sidebar order."""
    return [
        workspace
        for workspace in (workspaces.get("result") or {}).get("workspaces") or []
        if workspace.get("workspace_id")
    ]


def repo_key(workspace):
    """What holds a worktree parent and its children together in the sidebar.
    Empty for a workspace without a worktree, which belongs to no group."""
    worktree = workspace.get("worktree")
    if not isinstance(worktree, dict):
        return ""
    value = worktree.get("repo_key")
    return value if isinstance(value, str) else ""


def is_child(workspace):
    """True for a linked worktree. Only children are sorted; the parent of a
    group keeps the top of its group."""
    worktree = workspace.get("worktree")
    if not isinstance(worktree, dict):
        return False
    return worktree.get("is_linked_worktree") is True


def label_key(workspace):
    """What a workspace sorts by when the name decides: its label, case folded."""
    label = workspace.get("label")
    return label.casefold() if isinstance(label, str) else ""


def state_rank(states, workspace):
    """What a workspace sorts by when the state decides: its rank in SORT_ORDER,
    and one past the end for a state the file does not know."""
    return SORT_RANK.get(states.get(workspace.get("workspace_id", ""), ""), len(SORT_ORDER))


def by_state(states):
    """The key for `--sort`: state first, name inside a state."""
    return lambda workspace: (state_rank(states, workspace), label_key(workspace))


def by_name():
    """The key for `--sort-name`."""
    return label_key


def group_order(members, key):
    """One worktree group in order: the parent first, then the children by `key`.
    The sort is stable, so children the key cannot tell apart keep their order."""
    parents = [member for member in members if not is_child(member)]
    children = [member for member in members if is_child(member)]
    children.sort(key=key)
    return parents + children


def apply_move(order, request):
    """`order` with one workspace.move_block request applied: what it names
    comes out and goes back in front of before_workspace_id, or at the end when
    that is null."""
    moved = list(request["workspace_ids"])
    rest = [found for found in order if found not in set(moved)]
    before = request.get("before_workspace_id")
    at = rest.index(before) if before in rest else len(rest)
    return rest[:at] + moved + rest[at:]


def move_requests(records, key):
    """The workspace.move_block requests that put every worktree group in the
    order `key` asks for, in the order they have to be sent.

    Each request moves one workspace, because herdr does not promise to keep
    the order of a list of several. A group lands as one block where its first
    member is now, so a workspace of another group that sits between two
    members ends up after the block.
    """
    order = [record.get("workspace_id", "") for record in records]
    requests = []
    for group in dict.fromkeys(repo_key(record) for record in records):
        if not group:
            continue
        members = [record for record in records if repo_key(record) == group]
        wanted = [member.get("workspace_id", "") for member in group_order(members, key)]
        held = set(wanted)
        places = [at for at, found in enumerate(order) if found in held]
        packed = places[-1] - places[0] == len(places) - 1
        if packed and [order[at] for at in places] == wanted:
            continue
        after = [found for found in order[places[0] :] if found not in held]
        plan = [
            {
                "workspace_ids": [wanted[-1]],
                "before_workspace_id": after[0] if after else None,
            }
        ]
        for at in range(len(wanted) - 2, -1, -1):
            plan.append(
                {"workspace_ids": [wanted[at]], "before_workspace_id": wanted[at + 1]}
            )
        for request in plan:
            order = apply_move(order, request)
        requests += plan
    return requests


def read_reply(client):
    """The first line herdr answers with. The reply ends at the newline, not at
    end of stream."""
    buffer = b""
    while b"\n" not in buffer:
        chunk = client.recv(65536)
        if not chunk:
            break
        buffer += chunk
    return buffer.decode("utf-8", "replace").split("\n", 1)[0]


def socket_request(request_id, method, params):
    """(ok, reply) for one request to the herdr API socket. The socket carries
    methods the command line does not, which is how a workspace is moved.
    Newline-delimited JSON, one line each way."""
    path = os.environ.get("HERDR_SOCKET_PATH")
    if not path:
        log("HERDR_SOCKET_PATH is not set; run this under herdr")
        return False, {}
    request = json.dumps({"id": request_id, "method": method, "params": params})
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(HERDR_TIMEOUT)
            client.connect(path)
            client.sendall((request + "\n").encode("utf-8"))
            reply = read_reply(client)
    except OSError as error:
        log("%s: %s" % (path, error))
        return False, {}
    try:
        answer = json.loads(reply)
    except ValueError:
        answer = None
    if not isinstance(answer, dict) or "result" not in answer:
        detail = (answer or {}).get("error") if isinstance(answer, dict) else None
        log("%s: %s" % (method, detail or reply.strip() or "no reply"))
        return False, {}
    return True, answer


def sort_workspaces(what):
    """Order the worktrees of every group by state (`what` = "state") or by
    name. No token changes; only the sidebar order moves."""
    workspaces = herdr_json("workspace", "list")
    records = workspace_records(workspaces) if workspaces else []
    if not records:
        log("workspace list is empty; sorted nothing")
        return False
    key = by_state(load_states()) if what == "state" else by_name()
    requests = move_requests(records, key)
    if not requests:
        log("sort by %s: already in order" % what)
        return True
    for number, request in enumerate(requests, start=1):
        ok, _ = socket_request(
            "pr-emoji:sort:%d" % number, "workspace.move_block", request
        )
        if not ok:
            log("sort by %s: stopped after %d of %d moves" % (what, number - 1, len(requests)))
            return False
    log("sort by %s: %d moves" % (what, len(requests)))
    return True


def take_over_pidfile():
    """Single instance: a daemon launched by hand survives a server restart, so
    replace whatever holds the pidfile, waiting for it to actually go away."""
    try:
        with open(PIDFILE, "r", encoding="utf-8") as handle:
            old = int(handle.read().strip())
    except (OSError, ValueError):
        old = 0
    if old and old != os.getpid() and alive(old):
        log("stopping previous daemon %d" % old)
        kill(old, signal.SIGTERM)
        for _ in range(10):
            if not alive(old):
                break
            time.sleep(0.5)
        if alive(old):
            log("previous daemon %d ignored TERM, killing it" % old)
            kill(old, signal.SIGKILL)
            time.sleep(0.5)
    with open(PIDFILE, "w", encoding="utf-8") as handle:
        handle.write("%d\n" % os.getpid())


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def kill(pid, sig):
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def drop_pidfile():
    """Remove the pidfile only while it is still ours: a successor may own it."""
    try:
        with open(PIDFILE, "r", encoding="utf-8") as handle:
            if int(handle.read().strip()) != os.getpid():
                return
    except (OSError, ValueError):
        return
    try:
        os.remove(PIDFILE)
    except OSError:
        pass


def redirect_to_log():
    """herdr captures the startup hook's output until it exits; log to a file."""
    try:
        if os.path.getsize(LOG) > LOG_LIMIT:
            os.truncate(LOG, 0)
    except OSError:
        pass
    handle = open(LOG, "a", buffering=1, encoding="utf-8")
    os.dup2(handle.fileno(), sys.stdout.fileno())
    os.dup2(handle.fileno(), sys.stderr.fileno())


def main(argv):
    try:
        os.makedirs(STATE, exist_ok=True)
    except OSError:
        pass
    interval, icons = read_config(CONFIG)
    signoff = read_signoff(CONFIG)

    command = argv[0] if argv else ""
    if command in ("--sort", "--sort-name"):
        # The sort reads the last cycle's states file and herdr's own list;
        # it asks GitHub nothing, so it needs no `gh`.
        return 0 if sort_workspaces("state" if command == "--sort" else "name") else 1

    if not shutil.which("gh"):
        log("gh is required; not starting")
        return 0

    if command == "--query":
        if len(argv) < 2 or "/" not in argv[1]:
            log("usage: daemon.py --query owner/name")
            return 2
        print_verdicts(
            read_pairs(sys.stdin, argv[1]), icons, with_slug=False, signoff=signoff
        )
        return 0
    if command == "--resolve":
        print_verdicts(read_pairs(sys.stdin), icons, with_slug=True, signoff=signoff)
        return 0
    if command == "--once":
        if not guarded_cycle(interval, icons, signoff):
            log("herdr unreachable")
        return 0

    redirect_to_log()
    take_over_pidfile()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: sys.exit(0))
    log(
        "started pid %d, interval %ds, unstable=%s, signoff=%s"
        % (os.getpid(), interval, icons["unstable"], signoff[0] or "none")
    )

    failures = 0
    try:
        while True:
            if guarded_cycle(interval, icons, signoff):
                failures = 0
            else:
                failures += 1
                log("herdr unreachable (%d/3)" % failures)
                if failures >= 3:
                    log("herdr is gone, exiting")
                    return 0
            time.sleep(interval)
    finally:
        drop_pidfile()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
