#!/usr/bin/env python3
# Offline tests for the decisions the daemon makes: python3 -m unittest
#
# The payloads under fixtures/ are GitHub GraphQL responses recorded from real
# pull requests, with owners, repositories, branches, pull request numbers and
# check names replaced by neutral ones and most of the passing optional checks
# dropped. Their shape, states and timestamps are untouched.
#
# Aliases p0, p1 and p2 of required_checks.json carry no `baseRef`, which is
# also the shape a token that cannot read the base branch's protection gets.
# p3 carries one, and one of its required contexts has yet to report.
#
# lookup.json carries no `reviewDecision`, so it also covers a base branch that
# asks for no reviews. The 👀 cases are unit tests below.

import json
import os
import tempfile
import unittest

import daemon

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

APP = "octo-org/app"
SERVICE = "octo-org/service"

# The branches behind the recorded aliases, in the sorted order the query uses.
PAIRS = [
    (APP, "feature/a-clean"),  # r0 b0: #101 OPEN CLEAN, checks passed
    (APP, "feature/b-clean"),  # r0 b1: #102 OPEN CLEAN, checks passed
    (APP, "feature/c-blocked"),  # r0 b2: #104 OPEN BLOCKED, rollup FAILURE
    (APP, "feature/d-unstable"),  # r0 b3: #103 OPEN UNSTABLE, rollup FAILURE
    (APP, "main"),  # r0 b4: #105 CLOSED
    (SERVICE, "feature/e-clean"),  # r1 b0: #201 OPEN CLEAN
    (SERVICE, "feature/f-clean"),  # r1 b1: #202 OPEN CLEAN
    (SERVICE, "feature/g-clean"),  # r1 b2: #203 OPEN CLEAN
    (SERVICE, "feature/h-blocked"),  # r1 b3: #204 OPEN BLOCKED, rollup FAILURE
    (SERVICE, "main"),  # r1 b4: #205 CLOSED
    (SERVICE, "no-pr"),  # r1 b5: no pull request
]


def fixture(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


def check(**fields):
    """One CheckRun context, shaped like the recorded ones."""
    context = {
        "__typename": "CheckRun",
        "name": "build",
        "status": "COMPLETED",
        "conclusion": None,
        "startedAt": None,
        "completedAt": None,
        "isRequired": True,
    }
    context.update(fields)
    return context


class EmojiPrecedence(unittest.TestCase):
    """First match wins, in the order the commits fought for."""

    def test_no_pull_request_asks(self):
        # Not empty: an empty answer means a row with nothing to say at all.
        self.assertEqual(daemon.emoji_for({"number": None}), "❔")

    def test_merged_beats_everything(self):
        self.assertEqual(
            daemon.emoji_for(
                {
                    "number": 1,
                    "state": "MERGED",
                    "isDraft": True,
                    "mergeStateStatus": "DIRTY",
                    "required_failing": True,
                }
            ),
            "🟣",
        )

    def test_closed_unmerged_shuts(self):
        self.assertEqual(
            daemon.emoji_for(
                {"number": 1, "state": "CLOSED", "mergeStateStatus": "DIRTY"}
            ),
            "🚪",
        )

    def test_draft_beats_conflict(self):
        self.assertEqual(
            daemon.emoji_for(
                {
                    "number": 1,
                    "state": "OPEN",
                    "isDraft": True,
                    "mergeStateStatus": "DIRTY",
                }
            ),
            "📝",
        )

    def test_conflict_beats_failing_check(self):
        self.assertEqual(
            daemon.emoji_for(
                {
                    "number": 1,
                    "state": "OPEN",
                    "mergeStateStatus": "DIRTY",
                    "required_failing": True,
                }
            ),
            "⚠️",
        )

    def test_failing_required_check_beats_running_and_blocked(self):
        self.assertEqual(
            daemon.emoji_for(
                {
                    "number": 1,
                    "state": "OPEN",
                    "mergeStateStatus": "BLOCKED",
                    "rollup": "PENDING",
                    "required_failing": True,
                    "required_running": True,
                }
            ),
            "❌",
        )

    def test_running_required_check_beats_blocked(self):
        self.assertEqual(
            daemon.emoji_for(
                {
                    "number": 1,
                    "state": "OPEN",
                    "mergeStateStatus": "BLOCKED",
                    "rollup": "FAILURE",
                    "required_running": True,
                }
            ),
            "🟡",
        )

    def test_pending_rollup_runs(self):
        self.assertEqual(
            daemon.emoji_for(
                {
                    "number": 1,
                    "state": "OPEN",
                    "mergeStateStatus": "CLEAN",
                    "rollup": "PENDING",
                }
            ),
            "🟡",
        )

    def test_review_required_beats_blocked(self):
        self.assertEqual(
            daemon.emoji_for(
                {
                    "number": 1,
                    "state": "OPEN",
                    "mergeStateStatus": "BLOCKED",
                    "reviewDecision": "REVIEW_REQUIRED",
                }
            ),
            "👀",
        )

    def test_review_required_is_not_gated_on_the_merge_state(self):
        # GitHub reports a missing review whatever `mergeStateStatus` says.
        # BEHIND and UNSTABLE both fall through to ✅ further down, so gating
        # 👀 on BLOCKED would call an unreviewed pull request mergeable.
        for status in ("BEHIND", "UNSTABLE", "UNKNOWN", "HAS_HOOKS"):
            self.assertEqual(
                daemon.emoji_for(
                    {
                        "number": 1,
                        "state": "OPEN",
                        "mergeStateStatus": status,
                        "reviewDecision": "REVIEW_REQUIRED",
                    }
                ),
                "👀",
                status,
            )

    def test_running_checks_beat_review_required(self):
        # Chasing a reviewer is pointless while the checks can still turn red.
        self.assertEqual(
            daemon.emoji_for(
                {
                    "number": 1,
                    "state": "OPEN",
                    "mergeStateStatus": "BLOCKED",
                    "reviewDecision": "REVIEW_REQUIRED",
                    "required_running": True,
                }
            ),
            "🟡",
        )

    def test_approved_but_still_blocked_stops(self):
        self.assertEqual(
            daemon.emoji_for(
                {
                    "number": 1,
                    "state": "OPEN",
                    "mergeStateStatus": "BLOCKED",
                    "reviewDecision": "APPROVED",
                }
            ),
            "🛑",
        )

    def test_blocked(self):
        self.assertEqual(
            daemon.emoji_for(
                {"number": 1, "state": "OPEN", "mergeStateStatus": "BLOCKED"}
            ),
            "🛑",
        )

    def test_unstable_reads_ok_by_default_and_follows_the_setting(self):
        pr = {"number": 1, "state": "OPEN", "mergeStateStatus": "UNSTABLE"}
        self.assertEqual(daemon.emoji_for(pr), "🆗")
        self.assertEqual(daemon.emoji_for(pr, "pass"), "✅")
        self.assertEqual(daemon.emoji_for(pr, "warn"), "⚠️")

    def test_mergeable(self):
        for status in ("CLEAN", "BEHIND", "HAS_HOOKS"):
            self.assertEqual(
                daemon.emoji_for(
                    {"number": 1, "state": "OPEN", "mergeStateStatus": status}
                ),
                "✅",
                status,
            )

    def test_unknown_status_is_empty(self):
        self.assertEqual(
            daemon.emoji_for(
                {"number": 1, "state": "OPEN", "mergeStateStatus": "UNKNOWN"}
            ),
            "",
        )


class RequiredChecks(unittest.TestCase):
    def contexts(self, alias):
        data = fixture("required_checks.json")["data"]
        rollup = data[alias]["pullRequest"]["commits"]["nodes"][0]["commit"][
            "statusCheckRollup"
        ]
        return rollup["contexts"]["nodes"]

    def expected(self, alias):
        pull = fixture("required_checks.json")["data"][alias]["pullRequest"]
        rule = (pull.get("baseRef") or {}).get("branchProtectionRule") or {}
        return rule.get("requiredStatusCheckContexts") or []

    def test_recorded_optional_failure_is_not_a_failure(self):
        # p0: three required checks passed, an optional one was CANCELLED.
        self.assertEqual(daemon.required_state(self.contexts("p0")), (False, False))

    def test_recorded_action_required_is_not_a_failure(self):
        # p1: the missing-review gate arrives as a required check whose
        # conclusion is ACTION_REQUIRED. mergeStateStatus reports it as BLOCKED.
        results = [
            c.get("conclusion") for c in self.contexts("p1") if c.get("isRequired")
        ]
        self.assertIn("ACTION_REQUIRED", results)
        self.assertEqual(daemon.required_state(self.contexts("p1")), (False, False))

    def test_recorded_rerun_is_judged_by_its_newest_attempt(self):
        # p2: four attempts of one required check; the newest passed, an earlier
        # one was CANCELLED.
        attempts = [
            c
            for c in self.contexts("p2")
            if c.get("isRequired") and c.get("name") == "build / build"
        ]
        self.assertEqual(len(attempts), 4)
        self.assertIn("CANCELLED", [c["conclusion"] for c in attempts])
        self.assertEqual(daemon.required_state(self.contexts("p2")), (False, False))

    def test_newest_attempt_failing_is_a_failure(self):
        contexts = [
            check(conclusion="SUCCESS", completedAt="2026-09-01T10:00:00Z"),
            check(conclusion="FAILURE", completedAt="2026-09-01T11:00:00Z"),
        ]
        self.assertEqual(daemon.required_state(contexts), (True, False))

    def test_attempt_in_flight_outranks_a_newer_completed_failure(self):
        # A queued run can report a startedAt days old, so time alone would let
        # the completed failure win and show ❌ in the middle of a CI run.
        contexts = [
            check(conclusion="FAILURE", completedAt="2026-09-08T11:00:00Z"),
            check(status="QUEUED", startedAt="2026-09-01T09:00:00Z"),
        ]
        self.assertEqual(daemon.required_state(contexts), (False, True))

    def test_pending_status_context_runs(self):
        contexts = [
            {
                "__typename": "StatusContext",
                "context": "ci/setup",
                "state": "PENDING",
                "createdAt": "2026-09-08T11:00:00Z",
                "isRequired": True,
            }
        ]
        self.assertEqual(daemon.required_state(contexts), (False, True))

    def test_recorded_expected_context_that_never_reported_runs(self):
        # p3: four required contexts, one of which ("ci/checkpoint") had posted
        # nothing, so it is absent from the rollup rather than passing. GitHub
        # calls it expected and blocks the merge on it; 🛑 would hide a CI run.
        contexts = self.contexts("p3")
        names = [c.get("name") or c.get("context") for c in contexts]
        self.assertNotIn("ci/checkpoint", names)
        self.assertEqual(
            daemon.required_state(contexts, self.expected("p3")), (False, True)
        )

    def test_recorded_pull_request_without_branch_protection_keeps_its_verdict(self):
        # With no expected contexts, the checks that did report decide alone.
        self.assertEqual(self.expected("p0"), [])
        self.assertEqual(daemon.required_state(self.contexts("p0"), []), (False, False))

    def test_expected_context_that_reported_does_not_run(self):
        contexts = [check(name="lint", conclusion="SUCCESS")]
        self.assertEqual(daemon.required_state(contexts, ["lint"]), (False, False))

    def test_expected_context_reported_as_optional_does_not_run(self):
        # Branch protection asks for the name, GitHub marks the run optional.
        # Counting it as unreported would pin the emoji to 🟡 for good.
        contexts = [check(name="lint", conclusion="SUCCESS", isRequired=False)]
        self.assertEqual(daemon.required_state(contexts, ["lint"]), (False, False))

    def test_expected_context_that_never_reported_beats_a_passing_sibling(self):
        contexts = [check(name="lint", conclusion="SUCCESS")]
        self.assertEqual(
            daemon.required_state(contexts, ["lint", "unit-tests"]), (False, True)
        )

    def test_optional_checks_are_ignored(self):
        contexts = [
            check(conclusion="FAILURE", completedAt="2026-09-08T11:00:00Z", isRequired=False),
            check(status="IN_PROGRESS", name="other", isRequired=False),
        ]
        self.assertEqual(daemon.required_state(contexts), (False, False))


class Lookup(unittest.TestCase):
    def test_recorded_response_maps_every_branch(self):
        prs, unanswered = daemon.parse_lookup(fixture("lookup.json")["data"], PAIRS)
        self.assertEqual(unanswered, [])
        targets = daemon.required_targets(prs)
        self.assertEqual(targets, [(APP, 103), (APP, 104), (SERVICE, 204)])
        marks = daemon.parse_required(fixture("required_checks.json")["data"], targets)
        verdicts = daemon.decide(prs, marks)
        self.assertEqual(
            verdicts,
            {
                (APP, "feature/a-clean"): "✅",
                (APP, "feature/b-clean"): "✅",
                (APP, "feature/c-blocked"): "🛑",
                (APP, "feature/d-unstable"): "🆗",
                (APP, "main"): "🚪",
                (SERVICE, "feature/e-clean"): "✅",
                (SERVICE, "feature/f-clean"): "✅",
                (SERVICE, "feature/g-clean"): "✅",
                (SERVICE, "feature/h-blocked"): "🛑",
                (SERVICE, "main"): "🚪",
                (SERVICE, "no-pr"): "❔",
            },
        )

    def test_partial_data_keeps_the_repository_that_answered(self):
        # Recorded from a response that carried `data` and `errors` together:
        # `gh api graphql` exits 1 for it, and the data is still good.
        payload = fixture("lookup_partial.json")
        prs, unanswered = daemon.parse_lookup(payload["data"], PAIRS)
        self.assertEqual([pr["branch"] for pr in prs], [pair[1] for pair in PAIRS[:5]])
        self.assertEqual(unanswered, PAIRS[5:])
        self.assertIn("Could not resolve", daemon.describe_errors(payload["errors"]))
        self.assertIn("[at r1]", daemon.describe_errors(payload["errors"]))

    def test_no_data_at_all_answers_for_nothing(self):
        prs, unanswered = daemon.parse_lookup(None, PAIRS)
        self.assertEqual(prs, [])
        self.assertEqual(unanswered, PAIRS)

    def test_required_targets_are_open_pull_requests_that_fail_or_are_blocked(self):
        prs = [
            {"slug": APP, "number": 1, "state": "OPEN", "rollup": "SUCCESS"},
            {"slug": APP, "number": 2, "state": "OPEN", "rollup": "FAILURE"},
            {"slug": APP, "number": 3, "state": "OPEN", "rollup": "ERROR"},
            {"slug": APP, "number": 4, "state": "MERGED", "rollup": "FAILURE"},
            {"slug": APP, "number": None},
            # Nothing failed and nothing is queued, and the merge is still
            # blocked: only the required checks can say whether one of them
            # has yet to report.
            {
                "slug": APP,
                "number": 5,
                "state": "OPEN",
                "rollup": "SUCCESS",
                "mergeStateStatus": "BLOCKED",
            },
        ]
        self.assertEqual(daemon.required_targets(prs), [(APP, 2), (APP, 3), (APP, 5)])

    def test_expected_required_check_reads_as_running_not_blocked(self):
        # The whole path for the recorded pull request of p3: blocked, rollup
        # FAILURE from one optional check, every reported required check green,
        # one required context still unreported.
        prs = [
            {
                "slug": APP,
                "branch": "feature/c-blocked",
                "number": 104,
                "state": "OPEN",
                "mergeStateStatus": "BLOCKED",
                "rollup": "FAILURE",
            }
        ]
        data = {"p0": fixture("required_checks.json")["data"]["p3"]}
        marks = daemon.parse_required(data, [(APP, 104)])
        self.assertEqual(marks, {(APP, 104): (False, True)})
        self.assertEqual(daemon.decide(prs, marks)[(APP, "feature/c-blocked")], "🟡")

    def test_unanswered_pull_request_falls_back_to_blocked(self):
        # The recorded shape of a pull request GitHub reported an error for.
        data = {"p0": {"pullRequest": None}}
        targets = [(APP, 104)]
        marks = daemon.parse_required(data, targets)
        self.assertEqual(marks, {})
        prs = [
            {
                "slug": APP,
                "branch": "feature/c-blocked",
                "number": 104,
                "state": "OPEN",
                "mergeStateStatus": "BLOCKED",
                "rollup": "FAILURE",
            }
        ]
        self.assertEqual(daemon.decide(prs, marks)[(APP, "feature/c-blocked")], "🛑")


class Publishing(unittest.TestCase):
    def test_an_unanswered_branch_is_left_alone(self):
        rows = [
            ("w1", APP, "feature/a-clean"),
            ("w2", APP, "feature/unanswered"),
            ("w3", "", ""),
        ]
        plan = daemon.plan_publications(rows, {(APP, "feature/a-clean"): "✅"})
        self.assertEqual(plan, [("w1", "✅"), ("w2", None), ("w3", "")])

    def test_a_failed_cycle_leaves_every_emoji_in_place(self):
        rows = [("w1", APP, "feature/a-clean"), ("w2", SERVICE, "main")]
        self.assertEqual(
            daemon.plan_publications(rows, {}), [("w1", None), ("w2", None)]
        )

    def test_an_empty_verdict_clears_the_token(self):
        rows = [("w1", SERVICE, "no-pr")]
        self.assertEqual(
            daemon.plan_publications(rows, {(SERVICE, "no-pr"): ""}), [("w1", "")]
        )


class GuardedCycle(unittest.TestCase):
    def test_an_unexpected_answer_costs_one_cycle_not_the_daemon(self):
        def raising(*_):
            raise AttributeError("'list' object has no attribute 'get'")

        original = daemon.cycle
        daemon.cycle = raising
        try:
            self.assertTrue(daemon.guarded_cycle(120, "pass"))
        finally:
            daemon.cycle = original


class Queries(unittest.TestCase):
    def test_lookup_query_asks_for_the_review_decision(self):
        # It rides along in the branch lookup, so 👀 costs no extra request.
        self.assertIn("reviewDecision", daemon.lookup_query(PAIRS))

    def test_lookup_query_aliases_every_branch_of_every_repository(self):
        query = daemon.lookup_query(PAIRS)
        self.assertIn('r0: repository(owner: "octo-org", name: "app")', query)
        self.assertIn('r1: repository(owner: "octo-org", name: "service")', query)
        self.assertIn('b4: pullRequests(headRefName: "main"', query)
        self.assertIn('b5: pullRequests(headRefName: "no-pr"', query)

    def test_required_query_asks_is_required_per_pull_request(self):
        query = daemon.required_query([(APP, 104)])
        self.assertIn("p0: repository", query)
        self.assertIn("pullRequest(number: 104)", query)
        self.assertIn("isRequired(pullRequestNumber: 104)", query)

    def test_required_query_asks_what_the_base_branch_requires(self):
        query = daemon.required_query([(APP, 104)])
        self.assertIn(
            "baseRef { branchProtectionRule { requiredStatusCheckContexts } }", query
        )
        # One selection set on `pullRequest`, not two.
        self.assertEqual(query.count("pullRequest(number: 104)"), 1)

    def test_both_queries_are_balanced(self):
        # One brace short and GitHub rejects the whole request with
        # "Expected NAME, actual: (none)", which cost the required-check query
        # its ❌ and 🟡 verdicts once already.
        for query in (
            daemon.lookup_query(PAIRS),
            daemon.required_query([(APP, 104), (SERVICE, 204)]),
        ):
            self.assertEqual(query.count("{"), query.count("}"), query)


class Remotes(unittest.TestCase):
    def test_github_remotes(self):
        for url in (
            "git@github.com:octo-org/app.git",
            "ssh://git@github.com/octo-org/app",
            "https://github.com/octo-org/app.git",
            "https://github.com/octo-org/app/",
        ):
            self.assertEqual(daemon.slug_from_url(url), "octo-org/app", url)

    def test_other_remotes(self):
        for url in (
            "git@gitlab.com:octo-org/app.git",
            "https://github.com/octo-org/app/tree/main",
            "https://example.com/app.git",
            "",
        ):
            self.assertEqual(daemon.slug_from_url(url), "", url)


class Config(unittest.TestCase):
    def read(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as handle:
            handle.write(text)
            path = handle.name
        try:
            return daemon.read_config(path)
        finally:
            os.remove(path)

    def test_defaults_without_a_file(self):
        self.assertEqual(
            daemon.read_config(os.path.join(FIXTURES, "no-such-config.toml")),
            (daemon.DEFAULT_INTERVAL, "ok"),
        )

    def test_interval_and_unstable(self):
        self.assertEqual(
            self.read('refreshIntervalSeconds = 300\nunstable = "warn"\n'), (300, "warn")
        )

    def test_interval_floor(self):
        self.assertEqual(self.read("refreshIntervalSeconds = 5\n")[0], 60)

    def test_unreadable_values_keep_the_defaults(self):
        self.assertEqual(
            self.read('refreshIntervalSeconds = soon\nunstable = "maybe"\n'),
            (daemon.DEFAULT_INTERVAL, "ok"),
        )

    def test_every_unstable_value(self):
        for value in ("ok", "pass", "warn"):
            self.assertEqual(self.read('unstable = "%s"\n' % value)[1], value)


class Errors(unittest.TestCase):
    def test_message_and_path(self):
        self.assertEqual(
            daemon.describe_errors(
                [
                    {"message": "Could not resolve to a Repository", "path": ["r1"]},
                    {"message": "timeout", "path": ["r0", "b2"]},
                ]
            ),
            "Could not resolve to a Repository [at r1]; timeout [at r0/b2]",
        )

    def test_error_without_a_message(self):
        self.assertIn("SERVER_ERROR", daemon.describe_errors([{"type": "SERVER_ERROR"}]))


if __name__ == "__main__":
    unittest.main()
