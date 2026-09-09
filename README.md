# herdr-pr-emoji

[Herdr](https://herdr.dev) plugin: one emoji per sidebar row with the pull request state of
the workspace's branch. Built to replace `mergr` for that purpose, with one specific
behaviour: **a PR whose only failing checks are non-required reads as mergeable**, the way
GitHub itself treats it.

    ✅ bonkey/purchase-to-unlock     🟡 bonkey/ios-27-siri-ai-intents     👀 bonkey/apple-ads-interface

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
| `mergeStateStatus == DIRTY` (merge conflict) | ⚠️ |
| the newest attempt of a **required** check failed (`isRequired` on a failing check run or status) | ❌ |
| checks still running (`statusCheckRollup.state == PENDING`, or a required check queued, in progress, or yet to report at all) | 🟡 |
| `reviewDecision == REVIEW_REQUIRED` (waiting for a reviewer) | 👀 |
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
block that is neither a failed check, nor running CI, nor a missing review: a stale required
context, an unsatisfied deploy gate, or a base branch that asks for no reviews at all.

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
   for merged branches. A second request, only for open PRs that report a failure or are
   `BLOCKED`, asks each check `isRequired(pullRequestNumber:)` and the base branch for
   `requiredStatusCheckContexts`, to tell ❌ from 🟡 from 🛑. Two calls per cycle, however
   many repositories are open.
4. `report-metadata` on the workspace and each of its panes, with a TTL of three
   intervals: if the daemon dies, the emoji disappears instead of going stale.

Every subprocess (`herdr`, `git`, `gh`) has a timeout.

Failures are contained per branch. `gh api graphql` exits 1 whenever the response carries an
`errors` array, even when it also carries usable data, so the exit code alone decides
nothing: every repository and branch the answer does cover is used, and the error text lands
in the log with its path. A branch GitHub did not answer for keeps the emoji it already
shows — the token carries a TTL of three intervals, so an emoji nobody refreshes disappears
on its own, and one failed request no longer blanks every workspace. A pull request whose
required checks went unanswered loses only its ❌ and 🟡 verdicts and stays on 🛑. A
repository the token cannot see (a SAML-protected organisation the token is not authorised
for, a private repository) shows nothing.

Three consecutive herdr failures mean the server is gone and the loop exits. If the daemon
finds another instance in its pid file it stops it first, so a hand-launched copy never
polls in parallel with the one the server starts.

## Configuration

`$(herdr plugin config-dir bonkey.pr-emoji)/config.toml` (outside the plugin tree, survives
reinstalls). See `config.example.toml`.

    refreshIntervalSeconds = 120   # floor: 60
    unstable = "ok"                # UNSTABLE: "ok" 🆗, "pass" ✅, "warn" ⚠️

Logs: `daemon.log` under `$(herdr plugin state-dir)`, i.e.
`~/.local/state/herdr/plugins/bonkey.pr-emoji/daemon.log`.

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

Titles, review counts, several rows per space, opening PRs. One emoji per row.

## License

MIT
