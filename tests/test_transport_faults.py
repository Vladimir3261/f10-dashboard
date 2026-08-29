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

from bmwdiag.mapping.execute import (
    MappingExecutor,
    TRANSPORT_FAULT_BUDGET,
    _is_request_fault,
)
from bmwdiag.mapping.loader import load_text
from bmwdiag.mapping.registry import AllCapabilities, MappingRegistry


# Stand-ins for the transport's exceptions. bmwdiag never imports live.py, so
# the classifier matches structurally - these mimic the shapes it must handle.
class HsfzError(Exception):
    pass


class HsfzNack(HsfzError):
    pass


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
    response: {service: 0x62, identifier: 0xF300}
    signals:
      alpha:
        label: Alpha
        unit: C
        decode: {type: uint8}
  second:
    protocol: uds
    service: 0x22
    did: 0xF301
    response: {service: 0x62, identifier: 0xF301}
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


if __name__ == "__main__":
    unittest.main()
