# herdr-pr-emoji

[Herdr](https://herdr.dev) plugin: the pull request state of the workspace's branch, as one
emoji per sidebar row. Built to replace `mergr` for that purpose, with one specific
behaviour: **a PR whose only failing checks are non-required reads as mergeable**, the way
GitHub itself treats it.

    ✅ bonkey/purchase-to-unlock     🟡 bonkey/ios-27-siri-ai-intents     👀💬 bonkey/apple-ads-interface

## Install

    herdr plugin install bonkey/herdr-pr-emoji

Requires herdr ≥ 0.8.2, Python 3.9 or newer (standard library only), git and an
authenticated `gh`. No build step.

Then render the token in `config.toml` — the plugin only reports a value:

    [ui.sidebar.agents]
    rows = [
      ["state_icon", "workspace", "tab"],
      ["agent", { token = "$pr_emoji" }],
    ]

    [ui.sidebar.spaces]
    rows = [
      ["state_icon", "workspace"],
      ["branch", "git_status", { token = "$pr_emoji" }],
    ]

and `herdr server reload-config`. The daemon starts with the herdr server (`[[startup]]`),
so after installing either restart herdr or run it once by hand:

    herdr plugin action invoke bonkey.pr-emoji.refresh

## State mapping

The decisive fields are GitHub's `mergeStateStatus` and `reviewDecision`, plus whether a
*required* check is among the failing ones; first match wins:

| Condition | Emoji |
|---|---|
| no PR for the branch | ❔ |
| `state == MERGED` | 🟣 |
| `state == CLOSED` (closed unmerged), unless the branch is the repository's default | 🚪 |
| `isDraft` | 📝 |
| `isInMergeQueue` (the merge queue is holding it) | 🚂 |
| `mergeStateStatus == DIRTY` (merge conflict) | ⚠️ |
| the newest attempt of a **required** check failed (`isRequired` on a failing check run or status) **while other required checks still run** | 🟠 |
| the same failure with every required check settled | ❌ |
| checks still running (`statusCheckRollup.state == PENDING`, or a required check queued, in progress, or yet to report at all) | 🟡 |
| `reviewDecision == REVIEW_REQUIRED` (waiting for a reviewer) | 👀 |
| the merge queue let go of it, and not by merging it | 🪃 |
| `mergeStateStatus == BLOCKED` (not mergeable for some other reason) | 🛑 |
| `mergeStateStatus == UNSTABLE` (**only non-required checks failing**) | 🆗, or ✅ with `unstable = "pass"`, ⚠️ with `unstable = "warn"` |
| `mergeStateStatus` in `CLEAN`, `BEHIND`, `HAS_HOOKS` | ✅ |
| anything else (`UNKNOWN`, GitHub still computing) | keeps its last emoji, next poll |

An empty answer means a row with nothing to say: no branch, a remote that is not GitHub, or a
closed pull request on the default branch. A branch that simply has no pull request yet reads
❔.

An open pull request no row above matched keeps the emoji it already shows instead of losing
it. `UNKNOWN` is GitHub asking to be asked again: it invalidates mergeability whenever the
base branch moves and recomputes it only when something requests it, so the very query that
reports `UNKNOWN` is what makes the next one exact. A status this plugin does not recognise
is treated the same way. The TTL of three intervals still expires whatever nobody refreshes.

🚪 is suppressed on the default branch. `pullRequests(headRefName:, last: 1)` keeps finding
whatever pull request last carried the trunk's name — a release sync closed months ago, say —
so a door there would never go away. Only the door is suppressed: a pull request open *from*
the trunk is real work and reads like any other.

Running checks are reported before 👀 and `BLOCKED`: while required checks run GitHub already
says `BLOCKED`, and a 🛑 during every CI run is exactly the noise this plugin removes. Chasing
a reviewer is also pointless while the checks can still turn red.

👀 is decided by `reviewDecision` alone, without consulting `mergeStateStatus`. A missing
review is a fact of its own, and GitHub reports it whether the merge state says `BLOCKED`,
`BEHIND` or `UNSTABLE` — the last two fall through to ✅ further down, so gating 👀 on
`BLOCKED` would call an unreviewed pull request mergeable. What reaches 🛑 is therefore a
block that is neither a failed check, nor running CI, nor a missing review, nor an open
conversation: a stale required context, an unsatisfied deploy gate, or a base branch that
asks for no reviews at all.

🟠 splits ❌ in two. Both mean a required check has failed, and the difference is whether
the list of what to fix is complete: with ❌ every required check has settled, with 🟠
others are still running and a fix now invites a second pass. GitHub reports both facts
independently, so the split costs nothing — `isRequired` already answers it.

🚂 is read early because the queue owns the pull request while it holds it. A queued pull
request reports nothing useful about itself — `UNKNOWN` while GitHub recomputes
mergeability, `BLOCKED` where the queue is the only thing allowed to merge it — so without
`isInMergeQueue` the row shows either 🛑 or whatever it showed before, and neither is true.
The facts it does still report are the ones the queue acts on itself, by throwing it out,
which the next emoji reports. A queued pull request is also skipped by the second request:
its verdict is already settled, and that is what keeps 💬 off it.

🪃 is read late for the opposite reason. An ejection is a refusal, not a fix, and every
emoji above it names something that would make the pull request acceptable again: you
cannot queue an unapproved pull request, you should not queue one over a failing required
check, and a conflicted one cannot be queued at all. Being outranked by ⚠️, ❌, 🟠, 🟡 or
👀 costs nothing, because those are the next step. Being outranked by 🛑 or ✅ would cost
everything: the merge group that broke belonged to other pull requests, so this one's own
checks are entirely green, and without 🪃 it reads ✅ — the one place this plugin would say
*mergeable* about a pull request nothing is going to merge.

The pair works as a sequence. While the queue holds a pull request that conflicts, the row
reads 🚂; the moment the queue throws it out for the conflict, the row reads ⚠️, because 🪃
sits below it. The emoji names the errand as soon as there is one.

🪃 clears itself without this plugin remembering anything.
`timelineItems(last: 1, itemTypes: [ADDED_TO_MERGE_QUEUE_EVENT, REMOVED_FROM_MERGE_QUEUE_EVENT, PULL_REQUEST_COMMIT, HEAD_REF_FORCE_PUSHED_EVENT])`
asks which of four things happened to the pull request most recently: the queue took it,
the queue let it go, somebody pushed, somebody force-pushed. 🪃 is one of those four
answers and a push is another, so a fix erases it and so does queueing it again. The
timeline is the state file, and GitHub keeps it — across restarts, reinstalls and a second
hand-launched copy, which a local cache would not. One node, in the request the branch
lookup already makes.

`reason` tells a merge from a rejection. Every exit from the queue emits the same removal
event, and only `merged` means it worked; `manual` means a person took it out on purpose,
and that person knows. Every other reason counts, including one GitHub has yet to invent,
because an unfamiliar reason that fell through would read ✅ again — the opposite of the
caution the rest of this plugin uses, and for the opposite reason: here silence is the bug.
A removal that records no reason at all is no evidence, and says nothing.

## The 💬 modifier

💬 is the one emoji that joins another instead of replacing it, so the token is never wider
than two:

| Reads | Means |
|---|---|
| 👀💬 | nobody has reviewed it **and** a conversation is open |
| ❌💬, 🟠💬, 🟡💬, ⚠️💬, 🪃💬 | the blocker on the left, and a conversation is open |
| 💬 | conversations are the only thing left |

A missing review and an unresolved conversation are two errands for two people. The
reviewer who has not looked yet is rarely the person who answers the threads an earlier
reviewer or a review bot left behind, and the author usually answers those. One emoji
cannot ask for both, so this one rides along.

Where 💬 stands alone it replaces 🛑 rather than joining it: 🛑 means *blocked, and this
plugin cannot say why*, and open conversations say why. A draft swallows 💬 the way it
swallows every other blocker, and an empty verdict keeps its emptiness — `UNKNOWN` goes on
falling through to whatever the row already shows. A queued pull request carries no 💬
either, because it never reaches the request that reads the threads: the queue is merging
it, and a conversation is not an errand that can stop it.

An open thread only counts where the base branch asks for it. `requiresConversationResolution`
sits in the same `branchProtectionRule` the plugin already reads for
`requiredStatusCheckContexts`, and the threads in the same pull request, so 💬 costs no
extra request. Where the token cannot read the protection rule — no permission, or a
repository governed by rulesets rather than branch protection — the rule is null and the
pull request reads 🛑, the same fallback the required contexts get. Only the first hundred
threads are asked for; past that the answer is 🛑 again, never a 💬 the pull request has
not been shown to have earned. GitHub counts an outdated thread as unresolved, so
`isOutdated` decides nothing.

A re-run check keeps its earlier attempts in the rollup, so only the newest attempt of each
check name decides ❌, the way branch protection decides it. A missing review also arrives as
a required check run with the `ACTION_REQUIRED` conclusion, which is not breakage either.

`statusCheckRollup.state` alone cannot report running checks: GitHub turns the whole rollup
to `FAILURE` as soon as one context fails, however many are still queued, and one failing
optional check is enough. So 🟡 also comes from the individual required checks, which the
same request already carries.

The rollup cannot report a required check that has posted **nothing yet** either — an
unreported context is absent from it, not pending. GitHub shows that one as *Expected —
waiting for status to be reported* and blocks the merge on it, so the second request also
asks the base branch for `requiredStatusCheckContexts`: a required context no check has
reported counts as running, and reads 🟡 rather than 🛑. A name counts as reported however
GitHub marked it, so a check whose `isRequired` says false while branch protection asks for
the same name cannot pin the emoji to 🟡. Where the token cannot read the branch protection
rule, the verdict rests on the checks that did report.

## How it polls

One long-lived loop, started with the server. Each cycle:

1. `herdr workspace list` and `herdr pane list` — both served from herdr's memory. The
   herdr API method `worktree.list` is never called: herdr answers it by enumerating git
   worktrees synchronously on its main thread, which is what made `mergr` stall the server.
2. For each workspace, `git branch --show-current` and the `origin` URL, locally.
3. **One GraphQL request for every repository and branch at once**
   (`pullRequests(headRefName:, last: 1)` aliased per branch), so the answer is exact even
   for merged branches. The same request asks whether the merge queue holds the pull
   request and, from its timeline, which of four things happened to it most recently, so
   🚂 and 🪃 cost no round trip of their own. A second request, only for open PRs that
   report a failure or are `BLOCKED` and that the queue is not holding, asks each check
   `isRequired(pullRequestNumber:)`, the base branch for
   `requiredStatusCheckContexts` and `requiresConversationResolution`, and the pull request
   for its review threads, to tell 🟠 from ❌ from 🟡 from 💬 from 🛑. Two calls per cycle,
   however many repositories are open.
4. `report-metadata` on the workspace and each of its panes, with a TTL of three
   intervals: if the daemon dies, the emoji disappears instead of going stale.

Every subprocess (`herdr`, `git`, `gh`) has a timeout.

Failures are contained per branch. `gh api graphql` exits 1 whenever the response carries an
`errors` array, even when it also carries usable data, so the exit code alone decides
nothing: every repository and branch the answer does cover is used, and the error text lands
in the log with its path. A branch GitHub did not answer for keeps the emoji it already
shows — the token carries a TTL of three intervals, so an emoji nobody refreshes disappears
on its own, and one failed request no longer blanks every workspace. A pull request whose
second request went unanswered loses only its 🟠, ❌, 🟡 and 💬 verdicts and stays on 🛑. A
repository the token cannot see (a SAML-protected organisation the token is not authorised
for, a private repository) shows nothing.

Three consecutive herdr failures mean the server is gone and the loop exits. If the daemon
finds another instance in its pid file it stops it first, so a hand-launched copy never
polls in parallel with the one the server starts.

## Configuration

`$(herdr plugin config-dir bonkey.pr-emoji)/config.toml`, i.e. `~/.config/herdr/plugins/config/bonkey.pr-emoji/config.toml` (outside the plugin tree, survives reinstalls). See `config.example.toml`.

    refreshIntervalSeconds = 120   # floor: 60
    unstable = "ok"                # UNSTABLE: "ok" 🆗, "pass" ✅, "warn" ⚠️

Logs and the pid file: `~/.local/state/herdr/plugins/bonkey.pr-emoji/`, so the log is `~/.local/state/herdr/plugins/bonkey.pr-emoji/daemon.log`.

The server passes both directories in `HERDR_PLUGIN_CONFIG_DIR` and `HERDR_PLUGIN_STATE_DIR`, and a daemon started by hand resolves the same two paths itself, under `$XDG_CONFIG_HOME` and `$XDG_STATE_HOME` where those are set. So a hand-launched copy reads the same `config.toml` and finds the running daemon in the same pid file.

## Development

    git clone https://github.com/bonkey/herdr-pr-emoji
    herdr plugin link "$PWD/herdr-pr-emoji"

`herdr server reload-config` and plugin disable/enable do not run startup hooks, so during
development launch the loop by hand and check what it publishes with `herdr api snapshot`:

    python3 daemon.py --once                                # one cycle against the running herdr
    printf 'main\nfeature/x\n' | python3 daemon.py --query owner/name   # branch<TAB>emoji, no herdr needed
    printf 'o/r\tmain\no2/r2\tfix\n' | python3 daemon.py --resolve    # several repositories at once

The emoji decisions, the two GraphQL queries and the publishing plan are pure functions with
tests over GraphQL responses recorded from real pull requests, under `fixtures/`:

    python3 -m unittest

## Non-goals

Titles, review counts, several rows per space, opening PRs. One emoji per row, and 💬 after
it when conversations are open too — no third slot. A queued pull request's position in the
queue and its estimated time to merge are the queue's business, not a row's.

## License

MIT
