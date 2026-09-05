"""
One bad exchange must not tear down the whole link.

Drive 7 recorded as four runs instead of one: a single
`HsfzNack: gateway will not route to 0x18` propagated out of the executor,
the poll loop treated it as a dead link, and the reconnect started a new run.
The cost was 1.35% of wall time but all analytical continuity - no drive
could be summarised without stitching runs together.

These pin the distinction the fix rests on: a fault in ONE exchange is
skipped, a fault in the LINK still reaches the reconnect logic.
"""

import unittest

from tests import support  # noqa: F401

from bmwdiag.mapping import execute as execute_module
from bmwdiag.mapping.execute import (
    MappingExecutor,
    REQUEST_FAULT_LIMIT,
    REQUEST_REST_MAX_SECONDS,
    REQUEST_REST_SECONDS,
    TRANSPORT_FAULT_BUDGET,
    _is_request_fault,
)
from bmwdiag.mapping.loader import load_text
from bmwdiag.mapping.registry import AllCapabilities, MappingRegistry


from bmwdiag.protocol.errors import RoutingNack


# The transport's real categories. bmwdiag owns them since issue #11, so the
# executor classifies by TYPE - a class merely named `...Nack` no longer
# counts (see tests/test_diagnostic_errors.py). This keeps the call sites
# below reading as they did.
def HsfzNack(message="gateway will not route to 0x18"):
    return RoutingNack(0x18, message=message)


class HsfzError(Exception):
    """An exception the executor has never heard of."""


TWO_REQUESTS = """
schema_version: 1

mapping:
  id: fault-fixture
  version: 1
  production: false

ecu:
  family: test
  target: 0x12

requests:
  first:
    protocol: uds
    service: 0x22
    did: 0xF300
    response: {data_length: 1}
    signals:
      alpha:
        label: Alpha
        unit: C
        decode: {type: uint8}
  second:
    protocol: uds
    service: 0x22
    did: 0xF301
    response: {data_length: 1}
    signals:
      beta:
        label: Beta
        unit: C
        decode: {type: uint8}
"""


class ScriptedTransport:
    """Raises whatever the script says, per call, else answers plausibly."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def request(self, payload, dst=None, timeout=None):
        item = self.script[self.calls] if self.calls < len(self.script) else None
        self.calls += 1

        if isinstance(item, Exception):
            raise item

        # Echo a positive response for whichever DID was asked for.
        return bytes([payload[0] + 0x40, payload[1], payload[2], 0x2A])


def executor(script):
    mapping = load_text(TWO_REQUESTS, "test")
    registry = MappingRegistry([mapping])
    profile = registry.resolve(AllCapabilities(), config={})

    return MappingExecutor(profile, transport=ScriptedTransport(script)), profile


def run(ex, profile):
    """Poll every request once, as the live loop does."""
    return ex.execute_detailed(profile.requests)


class Classification(unittest.TestCase):
    def test_nack_is_one_exchange(self):
        """The gateway is alive; it just will not route to that ECU."""
        self.assertTrue(_is_request_fault(HsfzNack("will not route to 0x18")))

    def test_timeout_is_one_exchange(self):
        self.assertTrue(_is_request_fault(TimeoutError("HSFZ read timeout")))

    def test_closed_socket_is_a_link_fault(self):
        for exc in (ConnectionResetError("reset"), BrokenPipeError("pipe")):
            self.assertFalse(_is_request_fault(exc), exc)

    def test_unknown_errors_are_treated_as_link_faults(self):
        """
        Conservative direction. A needless reconnect costs seconds; mistaking
        a dead link for a slow ECU means polling a closed socket forever.
        """
        self.assertFalse(_is_request_fault(HsfzError("gateway closed the connection")))
        self.assertFalse(_is_request_fault(ValueError("something else")))


class OneBadExchange(unittest.TestCase):
    def test_a_nack_does_not_stop_the_other_requests(self):
        """The EGS refusing to route must not cost the DDE's channels."""
        ex, profile = executor([HsfzNack("will not route to 0x18"), None])
        out = run(ex, profile)

        self.assertEqual([r.request_id for r in out], ["second"])

    def test_a_timeout_does_not_stop_the_other_requests(self):
        ex, profile = executor([TimeoutError("HSFZ read timeout"), None])
        out = run(ex, profile)

        self.assertEqual([r.request_id for r in out], ["second"])

    def test_the_skipped_request_is_reported_not_silently_dropped(self):
        noted = []
        mapping = load_text(TWO_REQUESTS, "test")
        profile = MappingRegistry([mapping]).resolve(AllCapabilities(), config={})
        ex = MappingExecutor(
            profile,
            transport=ScriptedTransport([HsfzNack("nope"), None]),
            on_error=lambda rid, exc: noted.append(rid),
        )
        ex.execute_detailed(profile.requests)

        self.assertIn("first", noted)


class DeadLink(unittest.TestCase):
    def test_a_closed_socket_propagates_immediately(self):
        """Skipping would poll a dead socket forever instead of reconnecting."""
        ex, profile = executor([ConnectionResetError("reset"), None])

        with self.assertRaises(ConnectionResetError):
            run(ex, profile)

    def test_enough_consecutive_faults_are_treated_as_a_dead_link(self):
        """
        A link that dies mid-poll looks like a per-request timeout on every
        request. The budget is what stops that being absorbed forever.
        """
        ex, profile = executor([TimeoutError("t")] * (TRANSPORT_FAULT_BUDGET * 3))

        with self.assertRaises(TimeoutError):
            for _ in range(TRANSPORT_FAULT_BUDGET + 2):
                run(ex, profile)

    def test_a_success_resets_the_budget(self):
        """
        An intermittent ECU must never accumulate towards a reconnect: one
        good exchange means the link is fine, whatever that ECU is doing.
        """
        ex, profile = executor([])
        for _ in range(TRANSPORT_FAULT_BUDGET * 4):
            ex.transport.script = [HsfzNack("nope"), None]
            ex.transport.calls = 0
            run(ex, profile)

        self.assertLess(ex._transport_faults, TRANSPORT_FAULT_BUDGET)


# ----------------------------------------------------------------------
# Resting a request that keeps failing, and not counting a nack against
# the link. Both build on the skip-one-exchange behaviour above.
# ----------------------------------------------------------------------


class FakeClock:
    """
    A controllable stand-in for the `time` module.

    Keeps the two clocks separate on purpose. Rests are deadlined on
    `monotonic`; `last_ok` / `last_error_at` are stamped from `time`. The
    tests have to be able to move one without the other, because the whole
    point is that a wall-clock step must not touch a rest.
    """

    def __init__(self, start=1_000.0):
        self.mono = start
        self.wall = 1_756_000_000.0

    def monotonic(self):
        return self.mono

    def time(self):
        return self.wall

    def advance(self, seconds):
        """Time passes: both clocks move together, as they normally do."""
        self.mono += seconds
        self.wall += seconds

    def step_wall(self, seconds):
        """
        The wall clock jumps without time passing - an NTP correction.
        Monotonic deliberately does not move.
        """
        self.wall += seconds


class PerRequestTransport:
    """
    Answers by DID rather than by call order.

    Once requests start being rested the call sequence stops being a fixed
    interleave, so a script indexed by call number would silently test the
    wrong thing.
    """

    def __init__(self, behaviour):
        # {did: exception (or factory), or None for a good answer}
        self.behaviour = behaviour
        self.sent = []

    def request(self, payload, dst=None, timeout=None):
        did = (payload[1] << 8) | payload[2]
        self.sent.append(did)
        item = self.behaviour.get(did)

        if item is not None:
            raise item() if callable(item) else item

        return bytes([payload[0] + 0x40, payload[1], payload[2], 0x2A])


FIRST, SECOND = 0xF300, 0xF301
FIRST_ID, SECOND_ID = "first", "second"


class RestingCase(unittest.TestCase):
    """Base: an executor whose clock the test controls."""

    def build(self, behaviour):
        mapping = load_text(TWO_REQUESTS, "test")
        registry = MappingRegistry([mapping])
        profile = registry.resolve(AllCapabilities(), config={})
        transport = PerRequestTransport(behaviour)

        self.clock = FakeClock()
        real_time = execute_module.time
        execute_module.time = self.clock
        self.addCleanup(setattr, execute_module, "time", real_time)

        self.ex = MappingExecutor(profile, transport=transport)
        self.profile = profile
        self.transport = transport

    def turns(self, count):
        for _ in range(count):
            self.ex.execute_detailed(self.profile.requests)


class RestingARepeatedlyFailingRequest(RestingCase):
    """
    An ECU that is simply absent must stop costing a timeout every turn.

    Skipping the exchange keeps the drive in one run, but the request is
    still attempted every time it comes due. At the gear channel's 2 Hz
    that is a permanent tax on the whole loop.
    """

    def test_a_failing_request_stops_being_sent(self):
        self.build({FIRST: lambda: HsfzNack("no route")})

        self.turns(REQUEST_FAULT_LIMIT)
        sent_by_now = self.transport.sent.count(FIRST)

        self.turns(5)

        self.assertEqual(
            self.transport.sent.count(FIRST), sent_by_now,
            "a request past its fault limit should be resting, not retried",
        )

    def test_the_healthy_request_keeps_flowing_throughout(self):
        self.build({FIRST: lambda: HsfzNack("no route")})

        self.turns(8)

        self.assertEqual(self.transport.sent.count(SECOND), 8)

    def test_a_rested_request_is_retried_once_the_rest_expires(self):
        self.build({FIRST: lambda: HsfzNack("no route")})

        self.turns(REQUEST_FAULT_LIMIT)
        rested_at = self.transport.sent.count(FIRST)

        self.clock.advance(REQUEST_REST_SECONDS + 0.1)
        self.turns(1)

        self.assertEqual(
            self.transport.sent.count(FIRST), rested_at + 1,
            "a rest must expire - a briefly-asleep ECU has to come back",
        )

    def test_the_rest_is_measured_in_seconds_not_turns(self):
        """
        The bug this replaces: rest counted in due-turns meant one constant
        spanned 3s on `motion` and 32 minutes on `rare`. Spinning the loop
        without advancing the clock must not shorten a rest.
        """
        self.build({FIRST: lambda: HsfzNack("no route")})

        self.turns(REQUEST_FAULT_LIMIT)
        before = self.ex.stats()[FIRST_ID]["resting_for"]

        self.turns(50)                      # lots of turns, no time passing

        self.assertAlmostEqual(
            self.ex.stats()[FIRST_ID]["resting_for"], before, places=6
        )

    def test_a_backward_clock_step_does_not_extend_a_rest(self):
        """
        This host has no RTC. Its clock is corrected at boot and can step
        backwards on an NTP overshoot; yesterday it stepped 76.5 minutes
        FORWARD mid-drive and corrupted a session timeline.

        Against wall time a 30-minute backward step turned a 5-second rest
        into a 30-minute one, stranding the channel for most of a drive.
        A rest is a duration, so it is deadlined on the monotonic clock.
        """
        self.build({FIRST: lambda: HsfzNack("no route")})

        self.turns(REQUEST_FAULT_LIMIT)
        before = self.ex.stats()[FIRST_ID]["resting_for"]

        self.clock.step_wall(-1800)

        self.assertAlmostEqual(
            self.ex.stats()[FIRST_ID]["resting_for"], before, places=6,
            msg="a wall-clock step must not change a rest",
        )

    def test_a_forward_clock_step_does_not_shorten_a_rest_either(self):
        self.build({FIRST: lambda: HsfzNack("no route")})

        self.turns(REQUEST_FAULT_LIMIT)
        before = self.ex.stats()[FIRST_ID]["resting_for"]

        self.clock.step_wall(4600)          # the 76.5 min correction

        self.assertAlmostEqual(
            self.ex.stats()[FIRST_ID]["resting_for"], before, places=6
        )

    def test_the_rest_grows_each_time_it_keeps_failing(self):
        self.build({FIRST: lambda: HsfzNack("no route")})

        self.turns(REQUEST_FAULT_LIMIT)
        first_rest = self.ex.stats()[FIRST_ID]["resting_for"]

        self.clock.advance(first_rest + 0.1)
        self.turns(REQUEST_FAULT_LIMIT)
        second_rest = self.ex.stats()[FIRST_ID]["resting_for"]

        self.assertGreater(second_rest, first_rest)

    def test_the_rest_is_capped(self):
        self.build({FIRST: lambda: HsfzNack("no route")})

        for _ in range(12):
            self.turns(REQUEST_FAULT_LIMIT)
            self.clock.advance(self.ex.stats()[FIRST_ID]["resting_for"] + 0.1)

        self.turns(REQUEST_FAULT_LIMIT)

        self.assertLessEqual(
            self.ex.stats()[FIRST_ID]["resting_for"], REQUEST_REST_MAX_SECONDS
        )

    def test_recovery_clears_the_history(self):
        behaviour = {FIRST: lambda: HsfzNack("no route")}
        self.build(behaviour)

        self.turns(REQUEST_FAULT_LIMIT - 1)
        self.assertEqual(
            self.ex.stats()[FIRST_ID]["consecutive_faults"],
            REQUEST_FAULT_LIMIT - 1,
        )

        behaviour[FIRST] = None                 # the ECU answers again
        self.turns(1)

        self.assertEqual(self.ex.stats()[FIRST_ID]["consecutive_faults"], 0)
        self.assertEqual(self.ex.stats()[FIRST_ID]["resting_for"], 0.0)


class RestingIsNotFailing(RestingCase):
    """
    A rested request must not be counted as asked-and-unanswered.

    The diagnostics view reads `sent` with no `ok` as "the car is not
    answering this". If resting incremented `sent`, a channel that is
    deliberately quiet would look like a collapsing success rate instead.
    """

    def test_a_rested_request_is_not_counted_as_sent(self):
        self.build({FIRST: lambda: HsfzNack("no route")})

        self.turns(REQUEST_FAULT_LIMIT)
        sent_at_rest = self.ex.stats()[FIRST_ID]["sent"]

        self.turns(20)

        self.assertEqual(self.ex.stats()[FIRST_ID]["sent"], sent_at_rest)

    def test_stats_says_why_the_channel_is_quiet(self):
        self.build({FIRST: lambda: HsfzNack("no route")})

        self.turns(REQUEST_FAULT_LIMIT)
        first = self.ex.stats()[FIRST_ID]

        self.assertGreater(first["resting_for"], 0)
        self.assertEqual(self.ex.stats()[SECOND_ID]["resting_for"], 0.0)

    def test_a_resting_request_still_reports_what_the_rest_was_for(self):
        """
        "resting, 40s left after 3 timeouts" needs both halves. The live
        fault count is zeroed when the rest starts, so the count that
        caused it is kept separately.
        """
        self.build({FIRST: lambda: HsfzNack("no route")})

        self.turns(REQUEST_FAULT_LIMIT)
        first = self.ex.stats()[FIRST_ID]

        self.assertGreater(first["resting_for"], 0)
        self.assertEqual(first["consecutive_faults"], REQUEST_FAULT_LIMIT)
        self.assertEqual(first["kinds"], {"transport_nack": REQUEST_FAULT_LIMIT})


class ANackIsProofTheLinkIsAlive(RestingCase):
    """
    The gateway answering in order to refuse is evidence FOR the link, so
    it must never accumulate towards concluding the link is dead. Only
    silence can do that.
    """

    def test_nacks_alone_never_trigger_a_reconnect(self):
        self.build({
            FIRST: lambda: HsfzNack("no route"),
            SECOND: lambda: HsfzNack("no route"),
        })

        # Far past the budget, with no successful exchange to reset it, and
        # stepping the clock so rests keep expiring and the requests keep
        # being retried rather than quietly sitting out.
        for _ in range(TRANSPORT_FAULT_BUDGET * 4):
            self.turns(REQUEST_FAULT_LIMIT)
            self.clock.advance(REQUEST_REST_MAX_SECONDS + 1)

        self.assertEqual(self.ex._transport_faults, 0)

    def test_silence_still_reaches_the_reconnect_logic(self):
        self.build({
            FIRST: lambda: TimeoutError("timed out"),
            SECOND: lambda: TimeoutError("timed out"),
        })

        with self.assertRaises(TimeoutError):
            self.turns(TRANSPORT_FAULT_BUDGET)

    def test_a_nack_does_not_mask_a_dying_link(self):
        """A nack in the mix must not reset the timeout budget either."""
        self.build({
            FIRST: lambda: HsfzNack("no route"),
            SECOND: lambda: TimeoutError("timed out"),
        })

        with self.assertRaises(TimeoutError):
            for _ in range(TRANSPORT_FAULT_BUDGET * 3):
                self.turns(1)
                self.clock.advance(REQUEST_REST_MAX_SECONDS + 1)


if __name__ == "__main__":
    unittest.main()
