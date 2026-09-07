# herdr-pr-emoji

[Herdr](https://herdr.dev) plugin: one emoji per sidebar row with the pull request state of
the workspace's branch. Built to replace `mergr` for that purpose, with one specific
behaviour: **a PR whose only failing checks are non-required reads as mergeable**, the way
GitHub itself treats it.

    ✅ bonkey/purchase-to-unlock     🟡 bonkey/ios-27-siri-ai-intents     ❌ bonkey/apple-ads-interface

## Install

    herdr plugin install bonkey/herdr-pr-emoji

Requires herdr ≥ 0.8.2, bash, git, `jq` and an authenticated `gh`. No build step.

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

The decisive field is GitHub's `mergeStateStatus`; first match wins:

| Condition | Emoji |
|---|---|
| no PR for the branch, or a closed unmerged one | (empty) |
| `state == MERGED` | 🟣 |
| `isDraft` | 📝 |
| `mergeStateStatus == DIRTY` (merge conflict) | ⚠️ |
| checks still running (`statusCheckRollup.state == PENDING`) | 🟡 |
| `mergeStateStatus == BLOCKED` (required check failing, or review required) | ❌ |
| `mergeStateStatus == UNSTABLE` (**only non-required checks failing**) | ✅, or ⚠️ with `unstable = "warn"` |
| `mergeStateStatus` in `CLEAN`, `BEHIND`, `HAS_HOOKS` | ✅ |
| anything else (`UNKNOWN`, GitHub still computing) | (empty), next poll |

Running checks are reported before `BLOCKED`: while required checks run GitHub already
says `BLOCKED`, and a red ❌ during every CI run is exactly the noise this plugin removes.

## How it polls

One long-lived loop, started with the server. Each cycle:

1. `herdr workspace list` and `herdr pane list` — both served from herdr's memory. The
   herdr API method `worktree.list` is never called: herdr answers it by enumerating git
   worktrees synchronously on its main thread, which is what made `mergr` stall the server.
2. For each workspace, `git branch --show-current` and the `origin` URL, locally.
3. **One GraphQL request per GitHub repository**, aliasing every branch of that repository
   (`pullRequests(headRefName:, last: 1)`), so the answer is exact even for merged branches.
4. `report-metadata` on the workspace and each of its panes, with a TTL of three
   intervals: if the daemon dies, the emoji disappears instead of going stale.

Every subprocess (`herdr`, `git`, `gh`) has a timeout. A failed GitHub query clears the
tokens of that repository's workspaces rather than showing a misleading emoji. Three
consecutive herdr failures mean the server is gone and the loop exits. If the daemon finds
another instance in its pid file it stops it first, so a hand-launched copy never polls in
parallel with the one the server starts.

## Configuration

`$(herdr plugin config-dir bonkey.pr-emoji)/config.toml` (outside the plugin tree, survives
reinstalls). See `config.example.toml`.

    refreshIntervalSeconds = 120   # floor: 60
    unstable = "pass"              # "warn" shows ⚠️ for UNSTABLE

Logs: `daemon.log` under `$(herdr plugin state-dir)`, i.e.
`~/.local/state/herdr/plugins/bonkey.pr-emoji/daemon.log`.

## Development

    git clone https://github.com/bonkey/herdr-pr-emoji
    herdr plugin link "$PWD/herdr-pr-emoji"

`herdr server reload-config` and plugin disable/enable do not run startup hooks, so during
development launch the loop by hand and check what it publishes with `herdr api snapshot`:

    bash daemon.sh --once                                   # one cycle against the running herdr
    printf 'main\nfeature/x\n' | bash daemon.sh --query owner/name   # branch<TAB>emoji, no herdr needed

## Non-goals

Titles, review counts, several rows per space, opening PRs. One emoji per row.

## License

MIT
