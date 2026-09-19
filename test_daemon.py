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
# p3 carries one, and one of its required contexts has yet to report. p4 is a
# pull request whose base branch requires conversation resolution: every
# required check has reported and passed, and one of its nine threads is open.
#
# lookup.json carries no `reviewDecision`, so it also covers a base branch that
# asks for no reviews. The 👀 cases are unit tests below. Its `defaultBranchRef`
# is the one field added by hand, so the recorded response covers the rule that
# keeps a door off the trunk. It also carries neither `isInMergeQueue` nor
# `timelineItems`, which is the shape of a repository with no merge queue.
#
# lookup_queue.json is the exception: it is built by hand, in the shape the
# recorded queries answer with. A pull request the merge queue has thrown out
# is a short-lived state, and none was open in any repository searched when
# this was written, so the ejected aliases could not be recorded from one.

import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

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


# The branches behind lookup_queue.json, in the order its aliases use.
QUEUE_PAIRS = [
    (APP, "feature/queued"),  # r0 b0: #110 OPEN, in the merge queue
    (APP, "feature/ejected"),  # r0 b1: #111 OPEN CLEAN, ejected, failed_checks
    (APP, "feature/ejected-conflict"),  # r0 b2: #112 OPEN DIRTY, merge_conflict
    (APP, "feature/merged-by-the-queue"),  # r0 b3: #113 MERGED, reason merged
    (APP, "feature/pushed-after"),  # r0 b4: #114 OPEN CLEAN, pushed since
]


# Most tests below name the emoji, so they ask for that set by name. The
# daemon's own default is the Nerd Font one, whose glyphs are private-use
# codepoints that no assertion could be read from.
EMOJI = daemon.EMOJI


def emoji_for(pr, icons=EMOJI):
    return daemon.emoji_for(pr, icons)


def decide(prs, marks, icons=EMOJI, signoffs=None):
    return daemon.decide(prs, marks, icons, signoffs)


def required_state(contexts, expected=()):
    """(failing, running) — the two flags most of these tests are about. The
    unsatisfied check names have tests of their own."""
    failing, running, _ = daemon.required_state(contexts, expected)
    return failing, running


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
        self.assertEqual(emoji_for({"number": None}), "❔")

    def test_merged_beats_everything(self):
        self.assertEqual(
            emoji_for(
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
            emoji_for(
                {"number": 1, "state": "CLOSED", "mergeStateStatus": "DIRTY"}
            ),
            "🚪",
        )

    def test_closed_on_the_default_branch_stays_quiet(self):
        # The trunk keeps whatever pull request last carried its name, so a
        # door there would never go away.
        self.assertEqual(
            emoji_for(
                {"number": 1, "state": "CLOSED", "on_default_branch": True}
            ),
            "",
        )

    def test_the_default_branch_still_reports_every_other_state(self):
        # Only the door is suppressed: a pull request open from the trunk is
        # real work and reads like any other.
        self.assertEqual(
            emoji_for(
                {
                    "number": 1,
                    "state": "OPEN",
                    "mergeStateStatus": "BLOCKED",
                    "reviewDecision": "REVIEW_REQUIRED",
                    "on_default_branch": True,
                }
            ),
            "👀",
        )

    def test_draft_beats_conflict(self):
        self.assertEqual(
            emoji_for(
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
            emoji_for(
                {
                    "number": 1,
                    "state": "OPEN",
                    "mergeStateStatus": "DIRTY",
                    "required_failing": True,
                }
            ),
            "⚠️",
        )

    def test_a_failure_with_checks_still_running_is_not_settled(self):
        # Something is already broken and the list of what to fix is still
        # growing, so fixing it now invites a second pass.
        self.assertEqual(
            emoji_for(
                {
                    "number": 1,
                    "state": "OPEN",
                    "mergeStateStatus": "BLOCKED",
                    "rollup": "PENDING",
                    "required_failing": True,
                    "required_running": True,
                }
            ),
            "🟠",
        )

    def test_a_settled_failure_beats_running_and_blocked(self):
        self.assertEqual(
            emoji_for(
                {
                    "number": 1,
                    "state": "OPEN",
                    "mergeStateStatus": "BLOCKED",
                    "rollup": "PENDING",
                    "required_failing": True,
                }
            ),
            "❌",
        )

    def test_running_required_check_beats_blocked(self):
        self.assertEqual(
            emoji_for(
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
            emoji_for(
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
            emoji_for(
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
                emoji_for(
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
            emoji_for(
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
            emoji_for(
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
            emoji_for(
                {"number": 1, "state": "OPEN", "mergeStateStatus": "BLOCKED"}
            ),
            "🛑",
        )

    def test_unstable_reads_ok_by_default_and_follows_the_setting(self):
        pr = {"number": 1, "state": "OPEN", "mergeStateStatus": "UNSTABLE"}
        self.assertEqual(emoji_for(pr), "🆗")
        self.assertEqual(emoji_for(pr, daemon.icon_set("emoji", "pass")), "✅")
        self.assertEqual(emoji_for(pr, daemon.icon_set("emoji", "warn")), "⚠️")

    def test_mergeable(self):
        for status in ("CLEAN", "BEHIND", "HAS_HOOKS"):
            self.assertEqual(
                emoji_for(
                    {"number": 1, "state": "OPEN", "mergeStateStatus": status}
                ),
                "✅",
                status,
            )

    # `decide` turns this empty answer into no verdict at all, so the row keeps
    # what it shows; see Lookup below.
    def test_unknown_status_is_empty(self):
        self.assertEqual(
            emoji_for(
                {"number": 1, "state": "OPEN", "mergeStateStatus": "UNKNOWN"}
            ),
            "",
        )


    def test_the_queue_owns_a_pull_request_while_it_holds_it(self):
        # Everything the pull request still reports is the queue's business:
        # if any of it matters, the queue throws it out and 🪃 says so.
        self.assertEqual(
            emoji_for(
                {
                    "number": 1,
                    "state": "OPEN",
                    "in_merge_queue": True,
                    "mergeStateStatus": "BLOCKED",
                    "rollup": "PENDING",
                    "required_running": True,
                }
            ),
            "🚂",
        )

    def test_a_draft_cannot_be_queued(self):
        self.assertEqual(
            emoji_for(
                {"number": 1, "state": "OPEN", "isDraft": True, "in_merge_queue": True}
            ),
            "📝",
        )

    def test_merged_beats_the_queue_that_merged_it(self):
        # Both facts arrive in one response, and the merge is the later one.
        self.assertEqual(
            emoji_for(
                {"number": 1, "state": "MERGED", "in_merge_queue": True}
            ),
            "🟣",
        )

    def test_an_ejected_pull_request_is_not_mergeable(self):
        # The merge group broke on somebody else's pull request, so this one's
        # own checks are green and every other rung is silent. Without 🪃 this
        # reads ✅ about a pull request nothing is going to merge.
        self.assertEqual(
            emoji_for(
                {
                    "number": 1,
                    "state": "OPEN",
                    "ejected": True,
                    "mergeStateStatus": "CLEAN",
                }
            ),
            "🪃",
        )

    def test_an_ejection_explains_a_block(self):
        self.assertEqual(
            emoji_for(
                {
                    "number": 1,
                    "state": "OPEN",
                    "ejected": True,
                    "mergeStateStatus": "BLOCKED",
                }
            ),
            "🪃",
        )

    def test_an_ejection_yields_to_whatever_would_fix_it(self):
        # An ejection is a refusal, not a fix, and every rung above it names a
        # prerequisite for queueing the pull request again.
        for fields, expected in (
            ({"mergeStateStatus": "DIRTY"}, "⚠️"),
            ({"required_failing": True}, "❌"),
            ({"required_failing": True, "required_running": True}, "🟠"),
            ({"required_running": True}, "🟡"),
            ({"reviewDecision": "REVIEW_REQUIRED"}, "👀"),
        ):
            pr = {"number": 1, "state": "OPEN", "ejected": True}
            pr.update(fields)
            self.assertEqual(emoji_for(pr), expected, fields)

    def test_the_queue_outranks_an_ejection_it_has_taken_back(self):
        # Queued again before the answer was read: the newer fact wins.
        self.assertEqual(
            emoji_for(
                {
                    "number": 1,
                    "state": "OPEN",
                    "in_merge_queue": True,
                    "ejected": True,
                    "mergeStateStatus": "CLEAN",
                }
            ),
            "🚂",
        )


class Conversations(unittest.TestCase):
    """💬 is the one modifier, and the token is never wider than two."""

    def blocked(self, **fields):
        pr = {
            "number": 1,
            "state": "OPEN",
            "mergeStateStatus": "BLOCKED",
            "conversation_block": True,
        }
        pr.update(fields)
        return pr

    def test_a_missing_review_and_an_open_thread_ask_for_both(self):
        # Two errands for two people: the reviewer who has not looked yet, and
        # whoever answers the threads an earlier reviewer or a bot left behind.
        self.assertEqual(
            emoji_for(self.blocked(reviewDecision="REVIEW_REQUIRED")), "👀💬"
        )

    def test_open_threads_alone_replace_the_unexplained_block(self):
        # 🛑 says "blocked, and this plugin cannot say why". The threads say why.
        self.assertEqual(emoji_for(self.blocked()), "💬")

    def test_the_modifier_rides_along_with_every_blocker(self):
        for fields, expected in (
            ({"mergeStateStatus": "DIRTY"}, "⚠️💬"),
            ({"required_failing": True}, "❌💬"),
            ({"required_failing": True, "required_running": True}, "🟠💬"),
            ({"required_running": True}, "🟡💬"),
            ({"ejected": True}, "🪃💬"),
        ):
            self.assertEqual(emoji_for(self.blocked(**fields)), expected, fields)

    def test_a_draft_swallows_it_like_every_other_blocker(self):
        self.assertEqual(emoji_for(self.blocked(isDraft=True)), "📝")

    def test_an_unknown_merge_state_is_still_empty(self):
        # An empty verdict keeps its emptiness: UNKNOWN has to go on falling
        # through to whatever the row already shows.
        self.assertEqual(emoji_for(self.blocked(mergeStateStatus="UNKNOWN")), "")

    def test_nothing_is_appended_without_open_conversations(self):
        self.assertEqual(emoji_for(self.blocked(conversation_block=False)), "🛑")


class MergeQueue(unittest.TestCase):
    """Which of four things happened to the pull request most recently."""

    def timeline(self, *items):
        return {"timelineItems": {"nodes": list(items)}}

    def removal(self, reason):
        return {"__typename": "RemovedFromMergeQueueEvent", "reason": reason}

    def test_a_failed_merge_group_ejects(self):
        self.assertTrue(
            daemon.queue_ejection(self.timeline(self.removal("failed_checks")))
        )

    def test_a_merge_group_conflict_ejects(self):
        self.assertTrue(
            daemon.queue_ejection(self.timeline(self.removal("merge_conflict")))
        )

    def test_the_queue_merging_it_is_not_an_ejection(self):
        # Every exit from the queue emits this event, the successful one too.
        self.assertFalse(daemon.queue_ejection(self.timeline(self.removal("merged"))))

    def test_a_person_taking_it_out_is_not_an_ejection(self):
        # They did it on purpose and already know.
        self.assertFalse(daemon.queue_ejection(self.timeline(self.removal("manual"))))

    def test_a_reason_github_has_yet_to_invent_still_ejects(self):
        # Falling through would read ✅ about a pull request nothing will merge.
        self.assertTrue(
            daemon.queue_ejection(self.timeline(self.removal("queue_cleared")))
        )

    def test_a_removal_with_no_reason_recorded_says_nothing(self):
        self.assertFalse(daemon.queue_ejection(self.timeline(self.removal(None))))

    def test_a_commit_after_the_removal_clears_it(self):
        self.assertFalse(
            daemon.queue_ejection(
                self.timeline(
                    self.removal("failed_checks"), {"__typename": "PullRequestCommit"}
                )
            )
        )

    def test_a_force_push_after_the_removal_clears_it(self):
        self.assertFalse(
            daemon.queue_ejection(
                self.timeline(
                    self.removal("failed_checks"),
                    {"__typename": "HeadRefForcePushedEvent"},
                )
            )
        )

    def test_being_queued_again_clears_it(self):
        self.assertFalse(
            daemon.queue_ejection(
                self.timeline(
                    self.removal("failed_checks"),
                    {"__typename": "AddedToMergeQueueEvent"},
                )
            )
        )

    def test_the_newest_item_is_the_last_one(self):
        # The timeline ascends, so the answer is at the end of it.
        self.assertTrue(
            daemon.queue_ejection(
                self.timeline(
                    {"__typename": "PullRequestCommit"},
                    self.removal("failed_checks"),
                )
            )
        )

    def test_a_pull_request_that_never_saw_the_queue(self):
        self.assertFalse(daemon.queue_ejection(self.timeline()))

    def test_a_timeline_the_answer_did_not_carry(self):
        # The shape of a repository with no merge queue, and of a field the
        # response left out.
        for node in ({}, {"timelineItems": None}, {"timelineItems": {"nodes": None}}):
            self.assertFalse(daemon.queue_ejection(node), node)


class RequiredChecks(unittest.TestCase):
    def contexts(self, alias):
        data = fixture("required_checks.json")["data"]
        rollup = data[alias]["pullRequest"]["commits"]["nodes"][0]["commit"][
            "statusCheckRollup"
        ]
        return rollup["contexts"]["nodes"]

    def expected(self, alias):
        rule = (self.pull(alias).get("baseRef") or {}).get("branchProtectionRule") or {}
        return rule.get("requiredStatusCheckContexts") or []

    def pull(self, alias):
        return fixture("required_checks.json")["data"][alias]["pullRequest"]

    def test_recorded_open_thread_blocks_when_the_branch_asks_for_resolution(self):
        self.assertIs(daemon.conversation_block(self.pull("p4")), True)

    def test_resolved_threads_do_not_block(self):
        pull = self.pull("p4")
        for thread in pull["reviewThreads"]["nodes"]:
            thread["isResolved"] = True
        self.assertIs(daemon.conversation_block(pull), False)

    def test_open_threads_do_not_block_a_branch_that_never_asks(self):
        # Unresolved threads are ordinary where nobody has to resolve them.
        pull = self.pull("p4")
        rule = pull["baseRef"]["branchProtectionRule"]
        rule["requiresConversationResolution"] = False
        self.assertIs(daemon.conversation_block(pull), False)

    def test_a_protection_rule_the_token_cannot_read_does_not_block(self):
        # The same null rule a repository governed by rulesets answers with.
        self.assertIs(daemon.conversation_block(self.pull("p0")), False)

    def test_recorded_pull_request_with_an_open_thread_has_no_check_to_blame(self):
        self.assertEqual(
            required_state(self.contexts("p4"), self.expected("p4")),
            (False, False),
        )

    def test_recorded_optional_failure_is_not_a_failure(self):
        # p0: three required checks passed, an optional one was CANCELLED.
        self.assertEqual(required_state(self.contexts("p0")), (False, False))

    def test_recorded_action_required_is_not_a_failure(self):
        # p1: the missing-review gate arrives as a required check whose
        # conclusion is ACTION_REQUIRED. mergeStateStatus reports it as BLOCKED.
        results = [
            c.get("conclusion") for c in self.contexts("p1") if c.get("isRequired")
        ]
        self.assertIn("ACTION_REQUIRED", results)
        self.assertEqual(required_state(self.contexts("p1")), (False, False))

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
        self.assertEqual(required_state(self.contexts("p2")), (False, False))

    def test_newest_attempt_failing_is_a_failure(self):
        contexts = [
            check(conclusion="SUCCESS", completedAt="2026-09-01T10:00:00Z"),
            check(conclusion="FAILURE", completedAt="2026-09-01T11:00:00Z"),
        ]
        self.assertEqual(required_state(contexts), (True, False))

    def test_attempt_in_flight_outranks_a_newer_completed_failure(self):
        # A queued run can report a startedAt days old, so time alone would let
        # the completed failure win and show ❌ in the middle of a CI run.
        contexts = [
            check(conclusion="FAILURE", completedAt="2026-09-08T11:00:00Z"),
            check(status="QUEUED", startedAt="2026-09-01T09:00:00Z"),
        ]
        self.assertEqual(required_state(contexts), (False, True))

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
        self.assertEqual(required_state(contexts), (False, True))

    def test_recorded_expected_context_that_never_reported_runs(self):
        # p3: four required contexts, one of which ("ci/checkpoint") had posted
        # nothing, so it is absent from the rollup rather than passing. GitHub
        # calls it expected and blocks the merge on it; 🛑 would hide a CI run.
        contexts = self.contexts("p3")
        names = [c.get("name") or c.get("context") for c in contexts]
        self.assertNotIn("ci/checkpoint", names)
        self.assertEqual(
            required_state(contexts, self.expected("p3")), (False, True)
        )

    def test_recorded_pull_request_without_branch_protection_keeps_its_verdict(self):
        # With no expected contexts, the checks that did report decide alone.
        self.assertEqual(self.expected("p0"), [])
        self.assertEqual(required_state(self.contexts("p0"), []), (False, False))

    def test_expected_context_that_reported_does_not_run(self):
        contexts = [check(name="lint", conclusion="SUCCESS")]
        self.assertEqual(required_state(contexts, ["lint"]), (False, False))

    def test_expected_context_reported_as_optional_does_not_run(self):
        # Branch protection asks for the name, GitHub marks the run optional.
        # Counting it as unreported would pin the emoji to 🟡 for good.
        contexts = [check(name="lint", conclusion="SUCCESS", isRequired=False)]
        self.assertEqual(required_state(contexts, ["lint"]), (False, False))

    def test_expected_context_that_never_reported_beats_a_passing_sibling(self):
        contexts = [check(name="lint", conclusion="SUCCESS")]
        self.assertEqual(
            required_state(contexts, ["lint", "unit-tests"]), (False, True)
        )

    def test_optional_checks_are_ignored(self):
        contexts = [
            check(conclusion="FAILURE", completedAt="2026-09-08T11:00:00Z", isRequired=False),
            check(status="IN_PROGRESS", name="other", isRequired=False),
        ]
        self.assertEqual(required_state(contexts), (False, False))


class Lookup(unittest.TestCase):
    def test_recorded_response_maps_every_branch(self):
        prs, unanswered = daemon.parse_lookup(fixture("lookup.json")["data"], PAIRS)
        self.assertEqual(unanswered, [])
        targets = daemon.required_targets(prs)
        self.assertEqual(targets, [(APP, 103), (APP, 104), (SERVICE, 204)])
        marks = daemon.parse_required(fixture("required_checks.json")["data"], targets)
        verdicts = decide(prs, marks)
        self.assertEqual(
            verdicts,
            {
                (APP, "feature/a-clean"): "✅",
                (APP, "feature/b-clean"): "✅",
                (APP, "feature/c-blocked"): "🛑",
                (APP, "feature/d-unstable"): "🆗",
                (APP, "main"): "",
                (SERVICE, "feature/e-clean"): "✅",
                (SERVICE, "feature/f-clean"): "✅",
                (SERVICE, "feature/g-clean"): "✅",
                (SERVICE, "feature/h-blocked"): "🛑",
                (SERVICE, "main"): "",
                (SERVICE, "no-pr"): "❔",
            },
        )

    def test_the_queue_fixture_maps_every_branch(self):
        prs, unanswered = daemon.parse_lookup(
            fixture("lookup_queue.json")["data"], QUEUE_PAIRS
        )
        self.assertEqual(unanswered, [])
        verdicts = decide(prs, {})
        self.assertEqual(
            verdicts,
            {
                (APP, "feature/queued"): "🚂",
                (APP, "feature/ejected"): "🪃",
                (APP, "feature/ejected-conflict"): "⚠️",
                (APP, "feature/merged-by-the-queue"): "🟣",
                (APP, "feature/pushed-after"): "✅",
            },
        )

    def test_a_queued_pull_request_needs_no_second_request(self):
        # Its verdict is already settled, so the required-check query would buy
        # nothing — and that is also what keeps 💬 off it.
        prs, _ = daemon.parse_lookup(
            fixture("lookup_queue.json")["data"], QUEUE_PAIRS
        )
        self.assertEqual(daemon.required_targets(prs), [])

    def test_a_repository_with_no_merge_queue_reads_as_it_always_did(self):
        # lookup.json carries neither new field, so the fields it never
        # answered for cannot invent a verdict.
        prs, _ = daemon.parse_lookup(fixture("lookup.json")["data"], PAIRS)
        self.assertFalse(any(pr.get("in_merge_queue") for pr in prs))
        self.assertFalse(any(pr.get("ejected") for pr in prs))

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
        self.assertEqual(marks, {(APP, 104): (False, True, [("ci/checkpoint", "EXPECTED")], False)})
        self.assertEqual(decide(prs, marks)[(APP, "feature/c-blocked")], "🟡")

    def test_recorded_missing_review_and_open_thread_read_together(self):
        # The whole path for the recorded pull request of p4, the shape GitHub
        # sums up as "All comments must be resolved" and "At least 1 approving
        # review is required": every required check reported and passed, so
        # neither errand hides the other.
        prs = [
            {
                "slug": APP,
                "branch": "feature/i-conversations",
                "number": 106,
                "state": "OPEN",
                "mergeStateStatus": "BLOCKED",
                "rollup": "FAILURE",
                "reviewDecision": "REVIEW_REQUIRED",
            }
        ]
        data = {"p0": fixture("required_checks.json")["data"]["p4"]}
        marks = daemon.parse_required(data, [(APP, 106)])
        self.assertEqual(marks, {(APP, 106): (False, False, [("required-review", "ACTION_REQUIRED")], True)})
        self.assertEqual(
            decide(prs, marks)[(APP, "feature/i-conversations")], "👀💬"
        )

    def test_an_unknown_merge_state_keeps_the_last_emoji(self):
        # GitHub invalidates mergeability whenever the base branch moves and
        # recomputes it only on request, so the query that reports UNKNOWN is
        # what makes the next one exact. Clearing the token blanks the row for
        # at least one interval, and for longer whenever the next cycle fails.
        prs = [
            {
                "slug": APP,
                "branch": "feature/a-clean",
                "number": 101,
                "state": "OPEN",
                "mergeStateStatus": "UNKNOWN",
            }
        ]
        self.assertEqual(decide(prs, {}), {})
        rows = [("w1", APP, "feature/a-clean")]
        self.assertEqual(daemon.plan_publications(rows, decide(prs, {})), [("w1", None)])

    def test_an_unrecognised_merge_state_keeps_the_last_emoji(self):
        # A status this plugin has never heard of is no better a reason to
        # erase a good emoji than UNKNOWN is.
        prs = [
            {
                "slug": APP,
                "branch": "feature/a-clean",
                "number": 101,
                "state": "OPEN",
                "mergeStateStatus": "SOMETHING_GITHUB_ADDED",
            }
        ]
        self.assertEqual(decide(prs, {}), {})

    def test_a_closed_pull_request_on_the_trunk_is_still_cleared(self):
        # Empty is the verdict there, not the absence of one.
        prs = [
            {
                "slug": APP,
                "branch": "main",
                "number": 105,
                "state": "CLOSED",
                "on_default_branch": True,
            }
        ]
        self.assertEqual(decide(prs, {}), {(APP, "main"): ""})

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
        self.assertEqual(decide(prs, marks)[(APP, "feature/c-blocked")], "🛑")


class Signoff(unittest.TestCase):
    """The third glyph: what the hook is asked, what it may answer, and what a
    hook that cannot answer costs."""

    def open_pr(self, branch, **fields):
        pr = {
            "slug": APP,
            "branch": branch,
            "number": 1,
            "state": "OPEN",
            "mergeStateStatus": "CLEAN",
            "reviewDecision": "APPROVED",
        }
        pr.update(fields)
        return pr

    def hook(self, body):
        """An executable stub on disk, the way herdr runs a configured one."""
        directory = tempfile.mkdtemp(prefix="pr-emoji-signoff-")
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, True))
        path = os.path.join(directory, "hook")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    # ------------------------------------------------------------ what it is asked

    def test_only_an_open_undrafted_pull_request_is_asked_about(self):
        prs = [
            self.open_pr("feature/open"),
            self.open_pr("feature/draft", isDraft=True),
            self.open_pr("feature/merged", state="MERGED"),
            self.open_pr("feature/closed", state="CLOSED"),
            {"slug": APP, "branch": "main", "number": None},
        ]
        self.assertEqual(
            daemon.signoff_input(prs),
            "%s\tfeature/open\t1\tAPPROVED\tCLEAN\t\n" % APP,
        )

    def test_the_unsatisfied_required_checks_ride_along(self):
        # The shape the recorded p4 has, and the one PAIR leaves behind: GitHub
        # answers APPROVED while a required check still asks for a review.
        prs = [
            self.open_pr(
                "feature/x",
                mergeStateStatus="BLOCKED",
                required_unsatisfied=[
                    ("Required review", "ACTION_REQUIRED"),
                    ("Test Results", "EXPECTED"),
                ],
            )
        ]
        self.assertEqual(
            daemon.signoff_input(prs),
            "%s\tfeature/x\t1\tAPPROVED\tBLOCKED"
            "\tRequired review=ACTION_REQUIRED;Test Results=EXPECTED\n" % APP,
        )

    def test_a_check_name_cannot_shift_a_column(self):
        # A check name is free text. Nothing in it may be read as a separator.
        self.assertEqual(
            daemon.unsatisfied_column([("a;b=c\td", "FAILURE")]), "a b c d=FAILURE"
        )

    def test_a_satisfied_required_check_is_not_named(self):
        contexts = [
            check(name="lint", conclusion="SUCCESS"),
            check(name="flaky", conclusion="SKIPPED"),
            check(name="advisory", conclusion="NEUTRAL"),
            check(name="build", conclusion="FAILURE"),
            check(name="review", conclusion="ACTION_REQUIRED"),
        ]
        self.assertEqual(
            daemon.required_state(contexts)[2],
            [("build", "FAILURE"), ("review", "ACTION_REQUIRED")],
        )

    def test_an_optional_check_is_not_named_however_it_ended(self):
        contexts = [check(name="optional", conclusion="FAILURE", isRequired=False)]
        self.assertEqual(daemon.required_state(contexts)[2], [])

    def test_a_required_context_that_reported_nothing_is_expected(self):
        contexts = [check(name="lint", conclusion="SUCCESS")]
        self.assertEqual(
            daemon.required_state(contexts, ["lint", "review"])[2],
            [("review", "EXPECTED")],
        )

    def test_a_pull_request_the_plugin_did_not_ask_about_names_nothing(self):
        prs = [self.open_pr("feature/x", mergeStateStatus="CLEAN")]
        self.assertTrue(daemon.signoff_input(prs).endswith("\tCLEAN\t\n"))

    def test_resolve_asks_the_hook_with_the_checks_already_marked(self):
        """The whole path: the hook is asked before a verdict is decided, so
        the marks must reach the pull requests before it, not with `decide`."""
        lookup = {
            "r0": {
                "defaultBranchRef": {"name": "main"},
                "b0": {
                    "nodes": [
                        {
                            "number": 104,
                            "isDraft": False,
                            "state": "OPEN",
                            "mergeStateStatus": "BLOCKED",
                            "reviewDecision": "APPROVED",
                            "isInMergeQueue": False,
                            "commits": {
                                "nodes": [
                                    {"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}
                                ]
                            },
                        }
                    ]
                },
            }
        }
        required = {"p0": fixture("required_checks.json")["data"]["p4"]}
        answers = iter([(lookup, []), (required, [])])
        asked = []

        def hook(command, timeout, text):
            asked.append(text)
            return {}

        with mock.patch.object(daemon, "gh_graphql", lambda query: next(answers)):
            with mock.patch.object(daemon, "run_signoff", hook):
                daemon.resolve([(APP, "feature/x")], EMOJI, ("/bin/true", 10))

        self.assertEqual(
            asked,
            [
                "%s\tfeature/x\t104\tAPPROVED\tBLOCKED"
                "\trequired-review=ACTION_REQUIRED\n" % APP
            ],
        )

    def test_nothing_to_ask_about_is_an_empty_input(self):
        self.assertEqual(daemon.signoff_input([{"number": None}]), "")

    def test_a_missing_field_is_an_empty_column(self):
        prs = [self.open_pr("feature/x", reviewDecision="", mergeStateStatus=None)]
        self.assertEqual(daemon.signoff_input(prs), "%s\tfeature/x\t1\t\t\t\n" % APP)

    # ------------------------------------------------------- what it may answer

    def test_every_answer_the_glyphs_draw(self):
        lines = "".join(
            "%s\tfeature/%s\t%s\n" % (APP, state, state) for state in daemon.SIGNOFF_STATES
        )
        states, unknown = daemon.parse_signoff(lines)
        self.assertEqual(unknown, [])
        self.assertEqual(len(states), len(daemon.SIGNOFF_STATES))
        for state in daemon.SIGNOFF_STATES:
            self.assertEqual(states[(APP, "feature/" + state)], state)

    def test_surrounding_space_is_not_part_of_an_answer(self):
        states, _ = daemon.parse_signoff(" %s \t feature/x \t done \n" % APP)
        self.assertEqual(states, {(APP, "feature/x"): "done"})

    def test_a_state_no_glyph_draws_is_reported_and_dropped(self):
        states, unknown = daemon.parse_signoff(
            "%s\tfeature/x\tapproved\n%s\tfeature/y\tdone\n" % (APP, APP)
        )
        self.assertEqual(states, {(APP, "feature/y"): "done"})
        self.assertEqual(unknown, ["approved"])

    def test_a_line_of_another_shape_is_dropped(self):
        states, unknown = daemon.parse_signoff(
            "\n%s\tfeature/x\n%s\tfeature/y\tdone\textra\n\tfeature/z\tdone\n"
            % (APP, APP)
        )
        self.assertEqual(states, {})
        self.assertEqual(unknown, [])

    # ------------------------------------------------------------- what it draws

    def test_the_glyph_rides_behind_the_blocker(self):
        pr = self.open_pr("feature/x", signoff="missing")
        self.assertEqual(
            emoji_for(pr), EMOJI["mergeable"] + EMOJI["signoff_missing"]
        )

    def test_the_glyph_rides_behind_a_conversation_too(self):
        pr = self.open_pr(
            "feature/x",
            mergeStateStatus="BLOCKED",
            reviewDecision="REVIEW_REQUIRED",
            conversation_block=True,
            signoff="open",
        )
        self.assertEqual(
            emoji_for(pr),
            EMOJI["review"] + EMOJI["conversation"] + EMOJI["signoff_open"],
        )

    def test_a_draft_swallows_the_glyph(self):
        pr = self.open_pr("feature/x", isDraft=True, signoff="missing")
        self.assertEqual(emoji_for(pr), EMOJI["draft"])

    def test_an_empty_verdict_keeps_its_emptiness(self):
        pr = self.open_pr("feature/x", mergeStateStatus="UNKNOWN", signoff="done")
        self.assertEqual(emoji_for(pr), "")

    def test_a_row_the_hook_left_out_grows_no_glyph(self):
        prs = [self.open_pr("feature/answered"), self.open_pr("feature/silent")]
        verdicts = decide(prs, {}, EMOJI, {(APP, "feature/answered"): "done"})
        self.assertEqual(
            verdicts[(APP, "feature/answered")],
            EMOJI["mergeable"] + EMOJI["signoff_done"],
        )
        self.assertEqual(verdicts[(APP, "feature/silent")], EMOJI["mergeable"])

    def test_no_hook_at_all_draws_what_it_always_drew(self):
        prs = [self.open_pr("feature/x")]
        self.assertEqual(decide(prs, {})[(APP, "feature/x")], EMOJI["mergeable"])

    # ----------------------------------------------------------- how it is read

    def test_no_command_configured(self):
        self.assertEqual(
            daemon.read_signoff(os.path.join(FIXTURES, "no-such-config.toml")),
            ("", daemon.DEFAULT_SIGNOFF_TIMEOUT),
        )

    def test_the_command_and_its_timeout(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as handle:
            handle.write(
                'signoffCommand = "~/bin/pr-signoff"\nsignoffTimeoutSeconds = 5\n'
            )
            path = handle.name
        self.addCleanup(lambda: os.remove(path))
        command, timeout = daemon.read_signoff(path)
        self.assertEqual(command, os.path.expanduser("~/bin/pr-signoff"))
        self.assertEqual(timeout, 5)

    def test_unreadable_values_keep_the_defaults(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as handle:
            handle.write('signoffCommand = "  "\nsignoffTimeoutSeconds = 0\n')
            path = handle.name
        self.addCleanup(lambda: os.remove(path))
        self.assertEqual(
            daemon.read_signoff(path), ("", daemon.DEFAULT_SIGNOFF_TIMEOUT)
        )

    # ---------------------------------------------------------- how it is run

    def test_the_rows_reach_the_command_on_its_stdin(self):
        hook = self.hook("#!/bin/sh\nsed 's/\t.*//;s|$|\tfeature/x\tdone|'\n")
        self.assertEqual(
            daemon.run_signoff(hook, 10, "%s\tfeature/x\t1\t\tCLEAN\n" % APP),
            {(APP, "feature/x"): "done"},
        )

    def test_a_command_that_answers_nothing_answers_nothing(self):
        self.assertEqual(
            daemon.run_signoff(self.hook("#!/bin/sh\nexit 0\n"), 10, "row\n"), {}
        )

    def test_a_command_that_fails_costs_its_own_glyph_and_no_more(self):
        self.assertEqual(
            daemon.run_signoff(self.hook("#!/bin/sh\nexit 3\n"), 10, "row\n"), {}
        )

    def test_a_command_nobody_can_run_answers_nothing(self):
        self.assertEqual(daemon.run_signoff("/no/such/hook", 10, "row\n"), {})

    def test_a_command_that_times_out_answers_nothing(self):
        with mock.patch.object(
            daemon, "run", return_value=(124, "", "timed out after 20s")
        ):
            self.assertEqual(daemon.run_signoff("/bin/cat", 20, "row\n"), {})


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


class Verdicts(unittest.TestCase):
    """The state by name, which the sort ranks by where the row shows a glyph."""

    def test_the_name_and_the_glyph_agree(self):
        for pr in (
            {"number": None},
            {"number": 1, "state": "MERGED"},
            {"number": 1, "state": "OPEN", "isDraft": True},
            {"number": 1, "state": "OPEN", "mergeStateStatus": "DIRTY"},
            {"number": 1, "state": "OPEN", "mergeStateStatus": "CLEAN"},
            {"number": 1, "state": "OPEN", "mergeStateStatus": "UNSTABLE"},
        ):
            state, conversation = daemon.verdict_for(pr)
            self.assertFalse(conversation)
            self.assertEqual(EMOJI[state], emoji_for(pr))

    def test_open_conversations_ride_along_by_name(self):
        pr = {
            "number": 1,
            "state": "OPEN",
            "mergeStateStatus": "BLOCKED",
            "reviewDecision": "REVIEW_REQUIRED",
            "conversation_block": True,
        }
        self.assertEqual(daemon.verdict_for(pr), ("review", True))

    def test_open_conversations_explain_a_block_by_name(self):
        pr = {
            "number": 1,
            "state": "OPEN",
            "mergeStateStatus": "BLOCKED",
            "conversation_block": True,
        }
        self.assertEqual(daemon.verdict_for(pr), ("conversation", False))

    def test_an_unknown_merge_state_has_no_name(self):
        pr = {"number": 1, "state": "OPEN", "mergeStateStatus": "UNKNOWN"}
        self.assertEqual(daemon.verdict_for(pr), ("", False))

    def test_every_named_state_has_a_rank_and_nothing_else_does(self):
        # STATE_NAMES is the glyph table without its sign-off states, which
        # name no blocker: a sort ranks what a pull request itself reports.
        self.assertEqual(sorted(daemon.SORT_ORDER), sorted(daemon.STATE_NAMES))
        signoffs = [daemon.SIGNOFF_PREFIX + name for name in daemon.SIGNOFF_STATES]
        self.assertEqual(sorted(EMOJI), sorted(list(daemon.STATE_NAMES) + signoffs))

    def test_states_follow_the_verdicts(self):
        prs, _ = daemon.parse_lookup(fixture("lookup.json")["data"], PAIRS)
        targets = daemon.required_targets(prs)
        marks = daemon.parse_required(fixture("required_checks.json")["data"], targets)
        verdicts = decide(prs, marks)
        states = daemon.states_of(prs, verdicts)
        self.assertEqual(sorted(states), sorted(verdicts))
        self.assertEqual(states[(APP, "feature/a-clean")], "mergeable")
        self.assertEqual(states[(APP, "feature/c-blocked")], "blocked")
        self.assertEqual(states[(APP, "feature/d-unstable")], "unstable")
        self.assertEqual(states[(APP, "main")], "")
        self.assertEqual(states[(SERVICE, "no-pr")], "no_pr")

    def test_a_pull_request_without_a_verdict_records_no_state(self):
        prs = [
            {
                "slug": APP,
                "branch": "feature/unknown",
                "number": 1,
                "state": "OPEN",
                "mergeStateStatus": "UNKNOWN",
            }
        ]
        verdicts = decide(prs, {})
        self.assertEqual(verdicts, {})
        self.assertEqual(daemon.states_of(prs, verdicts), {})

    def test_the_states_file_follows_the_publishing_plan(self):
        rows = [
            ("w1", APP, "feature/a-clean"),
            ("w2", APP, "feature/unanswered"),
            ("w3", APP, "feature/never-answered"),
            ("w4", "", ""),
        ]
        states = {(APP, "feature/a-clean"): "mergeable"}
        previous = {"w2": "running", "w9": "merged"}
        self.assertEqual(
            daemon.plan_states(rows, states, previous),
            {"w1": "mergeable", "w2": "running", "w4": ""},
        )


APP_GIT = "/repos/app/.git"
LIB_GIT = "/repos/lib/.git"


def worktree_records(workspaces):
    """Workspace records as `workspace list` reports them for worktree groups.
    Each entry is (id, label, repo key, linked); an empty repo key makes a
    workspace with no worktree at all."""
    records = []
    for workspace_id, label, repo, linked in workspaces:
        record = {"workspace_id": workspace_id, "label": label}
        if repo:
            record["worktree"] = {
                "checkout_path": "/checkouts/" + workspace_id,
                "repo_key": repo,
                "is_linked_worktree": linked,
            }
        records.append(record)
    return records


def worktree_list(workspaces):
    return {"result": {"type": "workspace_list", "workspaces": worktree_records(workspaces)}}


def order_after(records, key):
    """The order the plan for `records` leaves the sidebar in."""
    found = [record["workspace_id"] for record in records]
    for request in daemon.move_requests(records, key):
        found = daemon.apply_move(found, request)
    return found


class SortPlan(unittest.TestCase):
    """The moves a sort plans, without herdr and without a socket."""

    def group(self, children):
        """One group: its parent, then a child per (id, label) in `children`."""
        records = [("p", "app", APP_GIT, False)]
        records += [(found, label, APP_GIT, True) for found, label in children]
        return worktree_records(records)

    def test_children_sort_by_state_in_the_documented_order(self):
        records = self.group([("a", "a"), ("b", "b"), ("c", "c"), ("d", "d"), ("e", "e")])
        states = {"a": "running", "b": "merged", "c": "mergeable", "d": "failing", "e": "draft"}
        self.assertEqual(
            order_after(records, daemon.by_state(states)), ["p", "c", "d", "a", "e", "b"]
        )

    def test_the_whole_order_is_what_the_readme_says(self):
        children = [(name, name) for name in daemon.SORT_ORDER]
        records = self.group(list(reversed(children)))
        states = {name: name for name in daemon.SORT_ORDER}
        self.assertEqual(
            order_after(records, daemon.by_state(states)), ["p"] + daemon.SORT_ORDER
        )

    def test_a_state_the_file_does_not_know_sorts_last(self):
        records = self.group([("a", "a"), ("b", "b"), ("c", "c")])
        states = {"a": "", "c": "closed"}
        self.assertEqual(order_after(records, daemon.by_state(states)), ["p", "c", "a", "b"])

    def test_the_name_decides_inside_a_state(self):
        records = self.group([("a", "TASK-153"), ("b", "task-132"), ("c", "TASK-143")])
        states = {"a": "mergeable", "b": "mergeable", "c": "mergeable"}
        self.assertEqual(order_after(records, daemon.by_state(states)), ["p", "b", "c", "a"])

    def test_the_name_sort_ignores_the_state(self):
        records = self.group([("a", "zeta"), ("b", "Alpha"), ("c", "mid")])
        self.assertEqual(order_after(records, daemon.by_name()), ["p", "b", "c", "a"])

    def test_the_parent_keeps_the_top_of_its_group(self):
        records = self.group([("a", "a")])
        states = {"p": "merged", "a": "mergeable"}
        self.assertEqual(order_after(records, daemon.by_state(states)), ["p", "a"])

    def test_the_same_key_keeps_the_order_the_group_has(self):
        records = self.group([("a", "same"), ("b", "same"), ("c", "same")])
        states = {"a": "review", "b": "review", "c": "review"}
        self.assertEqual(daemon.move_requests(records, daemon.by_state(states)), [])

    def test_a_group_already_in_order_moves_nothing(self):
        records = self.group([("a", "a"), ("b", "b")])
        states = {"a": "mergeable", "b": "merged"}
        self.assertEqual(daemon.move_requests(records, daemon.by_state(states)), [])

    def test_each_request_moves_one_workspace(self):
        records = self.group([("a", "a"), ("b", "b")])
        requests = daemon.move_requests(records, daemon.by_state({"b": "mergeable"}))
        self.assertTrue(requests)
        for request in requests:
            self.assertEqual(len(request["workspace_ids"]), 1)

    def test_a_group_whose_parent_is_not_open_sorts_its_children(self):
        records = worktree_records([("a", "a", APP_GIT, True), ("b", "b", APP_GIT, True)])
        states = {"a": "merged", "b": "review"}
        self.assertEqual(order_after(records, daemon.by_state(states)), ["b", "a"])

    def test_a_workspace_without_a_worktree_stays_where_it_is(self):
        records = worktree_records(
            [
                ("x", "x", "", False),
                ("p", "app", APP_GIT, False),
                ("a", "a", APP_GIT, True),
                ("b", "b", APP_GIT, True),
            ]
        )
        states = {"x": "mergeable", "a": "merged", "b": "review"}
        self.assertEqual(order_after(records, daemon.by_state(states)), ["x", "p", "b", "a"])

    def test_every_group_sorts_on_its_own(self):
        records = worktree_records(
            [
                ("p", "app", APP_GIT, False),
                ("a", "a", APP_GIT, True),
                ("b", "b", APP_GIT, True),
                ("q", "lib", LIB_GIT, False),
                ("c", "c", LIB_GIT, True),
                ("d", "d", LIB_GIT, True),
            ]
        )
        states = {"a": "closed", "b": "running", "c": "draft", "d": "conflict"}
        self.assertEqual(
            order_after(records, daemon.by_state(states)), ["p", "b", "a", "q", "d", "c"]
        )

    def test_a_workspace_between_two_members_ends_up_after_the_block(self):
        records = worktree_records(
            [
                ("p", "app", APP_GIT, False),
                ("a", "a", APP_GIT, True),
                ("x", "x", "", False),
                ("b", "b", APP_GIT, True),
            ]
        )
        states = {"a": "merged", "b": "mergeable"}
        self.assertEqual(order_after(records, daemon.by_state(states)), ["p", "b", "a", "x"])

    def test_states_of_a_workspace_that_is_not_open_are_ignored(self):
        records = self.group([("a", "a"), ("b", "b")])
        states = {"w27": "mergeable", "a": "merged", "b": "review"}
        self.assertEqual(order_after(records, daemon.by_state(states)), ["p", "b", "a"])

    def test_a_group_of_one_moves_nothing(self):
        records = worktree_records([("p", "app", APP_GIT, False)])
        self.assertEqual(daemon.move_requests(records, daemon.by_state({"p": "merged"})), [])


class StatesFile(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="pr-emoji-states-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True))
        self.path = os.path.join(self.directory, "states.json")
        patcher = mock.patch.object(daemon, "STATES", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_what_is_saved_is_what_is_loaded(self):
        daemon.save_states({"w1": "mergeable", "w2": ""})
        self.assertEqual(daemon.load_states(), {"w1": "mergeable", "w2": ""})
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_no_file_is_no_states(self):
        self.assertEqual(daemon.load_states(), {})

    def test_a_file_that_is_not_ours_is_no_states(self):
        for text in ("", "not json", "[]", '{"states": []}', '{"states": {"w1": 3}}'):
            with open(self.path, "w", encoding="utf-8") as handle:
                handle.write(text)
            self.assertEqual(daemon.load_states(), {})


SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daemon.py")

# A stand-in for `herdr`: answers `workspace list` from a file and refuses
# everything else, so a sort that tried to publish would fail loudly. It names
# this interpreter outright, so it runs with no PATH at all.
STUB = """#!""" + sys.executable + """
import os
import sys

argv = sys.argv[1:]
if argv[:2] == ["workspace", "list"]:
    sys.stdout.write(open(os.environ["WORKSPACES"], encoding="utf-8").read())
elif argv[:2] == ["pane", "list"]:
    sys.stdout.write('{"result": {"type": "pane_list", "panes": []}}')
else:
    with open(os.environ["CALLS"], "a", encoding="utf-8") as handle:
        handle.write(" ".join(argv) + "\\n")
    sys.exit(1)
"""


class SocketStub(object):
    """A stand-in for the herdr API socket. Records every request it receives and
    answers each one with an empty result."""

    def __init__(self, case):
        self.path = os.path.join(socket_dir(case), "herdr.sock")
        self.requests = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(16)
        case.addCleanup(self.close)
        self.thread = threading.Thread(target=self._serve)
        self.thread.daemon = True
        self.thread.start()

    def close(self):
        self.server.close()

    def methods(self):
        return [request.get("method") for request in self.requests]

    def params(self):
        return [request.get("params") for request in self.requests]

    def _serve(self):
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            try:
                stream = connection.makefile("rwb")
                line = stream.readline()
                if line:
                    request = json.loads(line.decode("utf-8"))
                    self.requests.append(request)
                    answer = {"id": request.get("id", ""), "result": {}}
                    stream.write((json.dumps(answer) + "\n").encode("utf-8"))
                    stream.flush()
            except (OSError, ValueError):
                pass
            finally:
                connection.close()


def socket_dir(case):
    """A short folder to bind a unix socket in: the whole path has to fit in
    about a hundred bytes, which a deep TMPDIR does not always allow."""
    import shutil

    for base in ("/tmp", tempfile.gettempdir()):
        if not os.path.isdir(base):
            continue
        try:
            directory = tempfile.mkdtemp(prefix="pre-", dir=base)
        except OSError:
            continue
        case.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        if len(os.path.join(directory, "herdr.sock").encode("utf-8")) < 100:
            return directory
    raise RuntimeError("no folder short enough for a unix socket")


class SortEndToEnd(unittest.TestCase):
    """What `--sort` and `--sort-name` send over the socket, run as herdr runs
    them: a fresh process, a stub herdr on HERDR_BIN_PATH, the states file the
    daemon would have left, and a stub socket in HERDR_SOCKET_PATH."""

    def setUp(self):
        import shutil

        self.root = tempfile.mkdtemp(prefix="pr-emoji-sort-")
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.state = os.path.join(self.root, "state")
        os.makedirs(self.state)
        self.calls = os.path.join(self.root, "calls")
        self.workspaces = os.path.join(self.root, "workspaces.json")
        stub = os.path.join(self.root, "herdr")
        with open(stub, "w", encoding="utf-8") as handle:
            handle.write(STUB)
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IXUSR)
        self.socket = SocketStub(self)
        self.env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": self.root,
            "HERDR_BIN_PATH": stub,
            "HERDR_PLUGIN_CONFIG_DIR": os.path.join(self.root, "config"),
            "HERDR_PLUGIN_STATE_DIR": self.state,
            "HERDR_SOCKET_PATH": self.socket.path,
            "WORKSPACES": self.workspaces,
            "CALLS": self.calls,
        }
        self.given_worktrees(
            [
                ("w1", "app", APP_GIT, False),
                ("w2", "TASK-153", APP_GIT, True),
                ("w3", "TASK-101", APP_GIT, True),
                ("w4", "TASK-143", APP_GIT, True),
            ]
        )
        # By state: w4 w2 w3. By name: w3 w4 w2. The two orders share nothing.
        self.given_states({"w2": "running", "w3": "merged", "w4": "mergeable"})

    def given_worktrees(self, workspaces):
        with open(self.workspaces, "w", encoding="utf-8") as handle:
            json.dump(worktree_list(workspaces), handle)

    def given_states(self, states):
        with open(os.path.join(self.state, "states.json"), "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "states": states}, handle)

    def run_daemon(self, args):
        done = subprocess.run(
            [sys.executable, SCRIPT] + args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
            timeout=60,
        )
        return done.returncode, done.stderr.decode("utf-8", "replace")

    def ordered(self):
        found = ["w1", "w2", "w3", "w4"]
        for params in self.socket.params():
            found = daemon.apply_move(found, params)
        return found

    def test_a_sort_puts_the_mergeable_worktree_first_and_the_merged_one_last(self):
        code, _ = self.run_daemon(["--sort"])
        self.assertEqual(code, 0)
        self.assertEqual(set(self.socket.methods()), {"workspace.move_block"})
        self.assertEqual(self.ordered(), ["w1", "w4", "w2", "w3"])

    def test_a_sort_by_name_ignores_the_states(self):
        code, _ = self.run_daemon(["--sort-name"])
        self.assertEqual(code, 0)
        self.assertEqual(self.ordered(), ["w1", "w3", "w4", "w2"])

    def test_a_sort_publishes_no_token_and_needs_no_gh(self):
        self.env["PATH"] = ""
        code, _ = self.run_daemon(["--sort"])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.calls))

    def test_a_group_already_in_order_sends_nothing(self):
        self.given_states({"w2": "mergeable", "w3": "review", "w4": "merged"})
        code, _ = self.run_daemon(["--sort"])
        self.assertEqual(code, 0)
        self.assertEqual(self.socket.requests, [])

    def test_a_sort_without_states_still_orders_by_name(self):
        os.remove(os.path.join(self.state, "states.json"))
        code, _ = self.run_daemon(["--sort"])
        self.assertEqual(code, 0)
        self.assertEqual(self.ordered(), ["w1", "w3", "w4", "w2"])

    def test_a_sort_without_a_socket_fails(self):
        del self.env["HERDR_SOCKET_PATH"]
        code, err = self.run_daemon(["--sort"])
        self.assertEqual(code, 1)
        self.assertIn("HERDR_SOCKET_PATH", err)

    def test_a_sort_that_the_socket_refuses_fails(self):
        self.socket.close()
        code, _ = self.run_daemon(["--sort"])
        self.assertEqual(code, 1)

    def test_an_empty_workspace_list_fails(self):
        self.given_worktrees([])
        code, _ = self.run_daemon(["--sort"])
        self.assertEqual(code, 1)
        self.assertEqual(self.socket.requests, [])

    def test_a_cycle_writes_the_states_file_the_sort_reads(self):
        # A cycle over workspaces without a checkout has nothing to ask GitHub,
        # so it runs without `gh` being called and records an empty name each.
        self.given_worktrees([("w1", "app", "", False)])
        os.remove(os.path.join(self.state, "states.json"))
        self.env["PATH"] = os.path.join(self.root, "bin")
        os.makedirs(self.env["PATH"])
        gh = os.path.join(self.env["PATH"], "gh")
        with open(gh, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nexit 1\n")
        os.chmod(gh, os.stat(gh).st_mode | stat.S_IXUSR)
        code, _ = self.run_daemon(["--once"])
        self.assertEqual(code, 0)
        with open(os.path.join(self.state, "states.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"version": 1, "states": {"w1": ""}})


class Queries(unittest.TestCase):
    def test_lookup_query_asks_the_default_branch_once_per_repository(self):
        query = daemon.lookup_query(PAIRS)
        self.assertEqual(query.count("defaultBranchRef { name }"), 2)

    def test_lookup_query_asks_for_the_review_decision(self):
        # It rides along in the branch lookup, so 👀 costs no extra request.
        self.assertIn("reviewDecision", daemon.lookup_query(PAIRS))

    def test_lookup_query_asks_whether_the_pull_request_is_queued(self):
        self.assertIn("isInMergeQueue", daemon.lookup_query(PAIRS))

    def test_lookup_query_asks_the_timeline_for_the_last_queue_event(self):
        query = daemon.lookup_query(PAIRS)
        self.assertIn("REMOVED_FROM_MERGE_QUEUE_EVENT", query)
        self.assertIn("... on RemovedFromMergeQueueEvent { reason }", query)

    def test_the_timeline_costs_one_item_per_branch(self):
        # One node each, in the request the branch lookup already makes.
        query = daemon.lookup_query(PAIRS)
        self.assertEqual(query.count("timelineItems(last: 1,"), len(PAIRS))
        self.assertNotIn("mergeQueueEntry", query)

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
            "baseRef { branchProtectionRule { requiredStatusCheckContexts "
            "requiresConversationResolution } }",
            query,
        )
        # One selection set on `pullRequest`, not two.
        self.assertEqual(query.count("pullRequest(number: 104)"), 1)

    def test_required_query_asks_for_the_review_threads(self):
        # Same request and the same selection set as the required checks, so
        # 💬 costs no round trip of its own.
        query = daemon.required_query([(APP, 104)])
        self.assertIn("reviewThreads(first: 100) { nodes { isResolved } }", query)
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

    def test_ssh_host_alias_remotes(self):
        """Multi-account setups point origin at an ssh config alias, not github.com."""
        alias = lambda host: host == "github.com-work"
        for url in (
            "git@github.com-work:octo-org/app.git",
            "ssh://git@github.com-work/octo-org/app",
        ):
            self.assertEqual(daemon.slug_from_url(url, alias), "octo-org/app", url)

    def test_ssh_host_alias_for_other_forge(self):
        """An alias that does not resolve to github.com stays unmatched."""
        self.assertEqual(
            daemon.slug_from_url("git@gl-work:octo-org/app.git", lambda h: False), ""
        )

    def test_other_remotes(self):
        for url in (
            "git@gitlab.com:octo-org/app.git",
            "https://github.com/octo-org/app/tree/main",
            "https://example.com/app.git",
            "",
        ):
            self.assertEqual(daemon.slug_from_url(url), "", url)


class Directories(unittest.TestCase):
    """Where the pid file, the log and config.toml live."""

    def state(self):
        return daemon.plugin_dir(
            "HERDR_PLUGIN_STATE_DIR", "XDG_STATE_HOME", "~/.local/state"
        )

    def config(self):
        return daemon.plugin_dir(
            "HERDR_PLUGIN_CONFIG_DIR", "XDG_CONFIG_HOME", "~/.config", "config"
        )

    def test_the_environment_the_server_passes_wins(self):
        passed = {
            "HERDR_PLUGIN_STATE_DIR": "/run/herdr/state",
            "HERDR_PLUGIN_CONFIG_DIR": "/run/herdr/config",
            "XDG_STATE_HOME": "/elsewhere/state",
            "XDG_CONFIG_HOME": "/elsewhere/config",
        }
        with mock.patch.dict(os.environ, passed, clear=True):
            self.assertEqual(self.state(), "/run/herdr/state")
            self.assertEqual(self.config(), "/run/herdr/config")

    def test_a_hand_launched_daemon_finds_the_same_directories(self):
        with mock.patch.dict(
            os.environ, {"HOME": "/home/dev", "TMPDIR": "/tmp/somewhere"}, clear=True
        ):
            self.assertEqual(
                self.state(), "/home/dev/.local/state/herdr/plugins/bonkey.pr-emoji"
            )
            self.assertEqual(
                self.config(), "/home/dev/.config/herdr/plugins/config/bonkey.pr-emoji"
            )

    def test_the_xdg_base_directories_move_them(self):
        moved = {
            "HOME": "/home/dev",
            "XDG_STATE_HOME": "/var/state",
            "XDG_CONFIG_HOME": "/var/config",
        }
        with mock.patch.dict(os.environ, moved, clear=True):
            self.assertEqual(self.state(), "/var/state/herdr/plugins/bonkey.pr-emoji")
            self.assertEqual(
                self.config(), "/var/config/herdr/plugins/config/bonkey.pr-emoji"
            )

    def test_the_daemon_reads_and_writes_where_it_resolves(self):
        self.assertEqual(daemon.STATE, self.state())
        self.assertEqual(daemon.LOG, os.path.join(daemon.STATE, "daemon.log"))
        self.assertEqual(daemon.PIDFILE, os.path.join(daemon.STATE, "daemon.pid"))
        self.assertEqual(daemon.CONFIG, os.path.join(self.config(), "config.toml"))


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
            (daemon.DEFAULT_INTERVAL, daemon.DEFAULT_ICONS),
        )

    def test_the_icon_set(self):
        self.assertEqual(self.read('icons = "emoji"\n')[1], daemon.EMOJI)
        self.assertEqual(self.read('icons = "nerd"\n')[1], daemon.NERD)
        # A name nobody defined keeps the default rather than blanking a row.
        self.assertEqual(self.read('icons = "wingdings"\n')[1], daemon.DEFAULT_ICONS)

    def test_interval_and_unstable(self):
        interval, icons = self.read(
            'refreshIntervalSeconds = 300\nunstable = "warn"\n'
        )
        self.assertEqual(interval, 300)
        self.assertEqual(icons["unstable"], icons["conflict"])

    def test_interval_floor(self):
        self.assertEqual(self.read("refreshIntervalSeconds = 5\n")[0], 60)

    def test_unreadable_values_keep_the_defaults(self):
        self.assertEqual(
            self.read('refreshIntervalSeconds = soon\nunstable = "maybe"\n'),
            (daemon.DEFAULT_INTERVAL, daemon.DEFAULT_ICONS),
        )

    def test_every_unstable_value(self):
        # The setting resolves into the table, so nothing downstream has to
        # know that UNSTABLE is configurable.
        for value, key in (("ok", "unstable"), ("pass", "mergeable"), ("warn", "conflict")):
            icons = self.read('unstable = "%s"\n' % value)[1]
            self.assertEqual(icons["unstable"], icons[key], value)


class IconSets(unittest.TestCase):
    def test_both_sets_answer_for_every_state(self):
        self.assertEqual(set(daemon.NERD), set(daemon.EMOJI))

    def test_no_state_borrows_another_state_glyph(self):
        # 🆗 and ✅ may coincide once `unstable` has resolved, but the sets
        # themselves have to tell every state apart.
        for name, icons in daemon.ICON_SETS.items():
            self.assertEqual(len(set(icons.values())), len(icons), name)

    def test_the_nerd_font_set_is_the_default(self):
        self.assertEqual(daemon.DEFAULT_ICONS["queued"], daemon.NERD["queued"])

    def test_the_setting_chooses_the_set(self):
        self.assertEqual(
            daemon.icon_set("emoji", "ok")["mergeable"], daemon.EMOJI["mergeable"]
        )
        self.assertEqual(
            daemon.icon_set("nerd", "ok")["mergeable"], daemon.NERD["mergeable"]
        )

    def test_a_set_nobody_defined_falls_back_to_the_default(self):
        self.assertEqual(daemon.icon_set("wingdings", "ok"), daemon.DEFAULT_ICONS)

    def test_the_unstable_setting_resolves_inside_either_set(self):
        for name, table in daemon.ICON_SETS.items():
            self.assertEqual(
                daemon.icon_set(name, "warn")["unstable"], table["conflict"], name
            )
            self.assertEqual(
                daemon.icon_set(name, "pass")["unstable"], table["mergeable"], name
            )


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
