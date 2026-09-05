"""
The observational safety gate, enforced in the production runtime.

The property under test is the one the whole repository promises:
nothing state-changing can reach the vehicle. Until this gate existed in
the runtime, that promise was only enforced in the supervised validation
tool - live.py sent mapping-defined payloads (including `setup:` frames
and variant probes) unchecked, so an accidentally edited
--extra-mappings file could have put a write service on the wire.

These tests prove the policy, prove it is applied where frames actually
leave the process, and prove there is exactly ONE implementation of it.
"""

import unittest

from tests import support  # noqa: F401

import live
from bmwdiag.mapping import MappingExecutor, MappingRegistry, load_text, load_tree
from bmwdiag.mapping.registry import AllCapabilities
from bmwdiag.protocol import build_request
from bmwdiag.protocol.safety import (
    DDD_SUBFUNCTIONS,
    OBSERVATIONAL_SERVICES,
    WRITE_SERVICES,
    ObservationalTransport,
    UnsafePayload,
    assert_observational,
)


class ThePolicy(unittest.TestCase):
    def test_every_observational_service_passes(self):
        for service in sorted(OBSERVATIONAL_SERVICES):
            with self.subTest(service=hex(service)):
                assert_observational(bytes([service, 0x00]))

    def test_every_write_service_is_rejected_by_name(self):
        for service, name in WRITE_SERVICES.items():
            with self.subTest(service=hex(service)):
                with self.assertRaises(UnsafePayload) as caught:
                    assert_observational(bytes([service, 0x01, 0x02]))

                self.assertIn(name, str(caught.exception))

    def test_an_unknown_service_fails_closed(self):
        """Not on any list is rejected, not tolerated."""
        for service in (0x00, 0x23, 0x2A, 0x83, 0xBA, 0xFF):
            with self.subTest(service=hex(service)):
                with self.assertRaises(UnsafePayload):
                    assert_observational(bytes([service]))

    def test_the_permitted_2c_subfunctions_pass(self):
        for sub in sorted(DDD_SUBFUNCTIONS):
            with self.subTest(sub=hex(sub)):
                assert_observational(bytes([0x2C, sub, 0xF3, 0x03]))

    def test_other_2c_subfunctions_are_rejected(self):
        #: 0x2C with the wrong subfunction is still a define WRITE shape.
        for sub in (0x00, 0x04, 0x11, 0x80, 0xFF):
            with self.subTest(sub=hex(sub)):
                with self.assertRaises(UnsafePayload):
                    assert_observational(bytes([0x2C, sub]))

    def test_a_bare_2c_is_rejected(self):
        with self.assertRaises(UnsafePayload):
            assert_observational(bytes([0x2C]))

    def test_an_empty_payload_is_rejected(self):
        with self.assertRaises(UnsafePayload):
            assert_observational(b"")


class EveryShippedFramePasses(unittest.TestCase):
    """
    The gate must not break anything the project actually sends.

    Walks EVERY mapping in the repository - production and candidates -
    and asserts every request payload and every setup frame passes. This
    is the test that fails if a future policy edit strands a real
    channel, or a future mapping smuggles in a service the policy would
    refuse only at runtime, in the car.
    """

    def test_all_mapping_payloads_and_setup_frames(self):
        targets = {"discovered_engine": 0x12}
        checked = 0
        rejected = []

        for mapping in load_tree(support.MAPPINGS, production_only=False):
            #
            # The synthetic fixture under mappings/examples/ deliberately
            # carries a RoutineControl-shaped raw job to document the
            # `raw` protocol shape. It is never loaded by the runtime
            # (family "example", excluded everywhere), and the gate MUST
            # reject it - asserted below, because that rejection is the
            # proof that a raw BMW-job payload cannot slip past.
            #
            is_example = mapping.ecu.family == "example"

            for request in mapping.requests:
                bound = build_request(request, targets)
                frames = [bound.payload] + [
                    bytes(f) for f in (request.setup or ())
                ]

                for frame in frames:
                    with self.subTest(request=request.id):
                        try:
                            assert_observational(frame)
                            checked += 1
                        except UnsafePayload:
                            if not is_example:
                                raise

                            rejected.append(request.id)

        #: Every real mapped request plus every F303 arm/clear frame.
        self.assertGreater(checked, 70)
        #: And the synthetic raw job was refused, not tolerated.
        self.assertIn("example.raw.job", rejected)


UNSAFE_MAPPING = """
schema_version: 1
mapping: {id: unsafe-fixture, version: 1, production: false}
ecu: {target: 0x12}
requests:
  attack:
    protocol: raw
    payload: "2E F1 90 41 41"
    polling: {class: fast}
    response: {data_length: 2}
    signals:
      x: {label: X, unit: "", decode: {type: uint8}}
"""

UNSAFE_SETUP = """
schema_version: 1
mapping: {id: unsafe-setup-fixture, version: 1, production: false}
ecu: {target: 0x12}
requests:
  probe:
    protocol: uds
    service: 0x22
    did: 0xF303
    setup: ["31 01 02 03"]
    polling: {class: fast}
    response: {data_length: 2}
    signals:
      x: {label: X, unit: "", decode: {type: uint16_be}}
"""


class RecordingTransport:
    """Fails the test if anything reaches it."""

    def __init__(self):
        self.sent = []

    def request(self, payload, *, dst, timeout=None, expect=None):
        self.sent.append(bytes(payload))

        return b"\x62\xf3\x03\x00\x00"


class TheRuntimeIsGated(unittest.TestCase):
    def _executor(self, text):
        mapping = load_text(text, "unsafe")
        profile = MappingRegistry([mapping]).resolve(AllCapabilities())
        inner = RecordingTransport()
        executor = MappingExecutor(
            profile, transport=ObservationalTransport(inner)
        )

        return executor, profile, inner

    def test_a_write_payload_in_a_mapping_never_reaches_the_transport(self):
        """
        The attack the issue describes: a mapping file carrying an
        explicit raw payload with a write service. Zero bytes may be
        sent - the refusal must precede any transport call.
        """
        executor, profile, inner = self._executor(UNSAFE_MAPPING)

        with self.assertRaises(UnsafePayload):
            executor.execute(profile.requests)

        self.assertEqual(inner.sent, [], "the write payload was transmitted")

    def test_an_unsafe_setup_frame_never_reaches_the_transport(self):
        """setup: frames go out before the poll - they are gated too."""
        executor, profile, inner = self._executor(UNSAFE_SETUP)

        with self.assertRaises(UnsafePayload):
            executor.execute(profile.requests)

        self.assertEqual(inner.sent, [], "the RoutineControl frame was sent")

    def test_the_hsfz_client_gates_before_any_io(self):
        """
        HsfzClient.request carries almost all traffic - OBD batches,
        the ECU scan, ident probes and the variant probes that bypass
        HsfzTransport; collect() is the one other send path, gated in
        the test below. Proven by ordering: on a client
        that has never connected, an unsafe payload raises UnsafePayload
        while a safe one raises "not connected" - so the check precedes
        ALL I/O, socket errors included.
        """
        client = live.HsfzClient("169.254.0.1")

        with self.assertRaises(UnsafePayload):
            client.request(bytes([0x2E, 0xF1, 0x90]))

        with self.assertRaises(live.HsfzError):
            client.request(bytes([0x22, 0xF1, 0x90]))

    def test_collect_gates_before_any_io(self):
        """
        collect() is the SECOND diagnostic send path - the functional
        broadcast ECU discovery uses it, and it transmits via _send()
        without going through request(). Review of the first cut of
        this gate found it unprotected. Same never-connected ordering
        proof: unsafe raises UnsafePayload, safe raises "not connected",
        so the gate precedes all I/O here too.
        """
        client = live.HsfzClient("169.254.0.1")

        with self.assertRaises(UnsafePayload):
            client.collect(bytes([0x2E, 0xF1, 0x90]), 0xDF, window=0.1)

        with self.assertRaises(live.HsfzError):
            client.collect(bytes([0x01, 0x00]), 0xDF, window=0.1)

    def test_session_control_is_rejected_by_default(self):
        """The production runtime never sets the opt-in."""
        client = live.HsfzClient("169.254.0.1")

        self.assertFalse(client.permit_session_control)

        with self.assertRaises(UnsafePayload):
            client.request(bytes([0x10, 0x03]))

    def test_the_session_control_opt_in_is_exactly_one_service(self):
        """
        tools/egs.py --session builds its client with
        permit_session_control=True. That grant covers service 0x10 and
        NOTHING else - every other write/control service stays rejected
        on an opted-in client.
        """
        client = live.HsfzClient(
            "169.254.0.1", permit_session_control=True
        )

        #: 0x10 passes the gate - proven by reaching "not connected".
        with self.assertRaises(live.HsfzError):
            client.request(bytes([0x10, 0x03]))

        for service in sorted(set(WRITE_SERVICES) - {0x10}):
            with self.subTest(service=hex(service)):
                with self.assertRaises(UnsafePayload):
                    client.request(bytes([service, 0x01]))

    def test_variant_probes_cannot_bypass_the_gate(self):
        """
        The variant probe path calls client.request directly, exactly as
        poll_loop wires it. Same never-connected ordering proof.
        """
        client = live.HsfzClient("169.254.0.1")
        send = lambda p, dst, timeout=None: client.request(p, timeout, dst)

        with self.assertRaises(UnsafePayload):
            send(bytes([0x31, 0x01, 0x02, 0x03]), 0x12)


class OnePolicyOnly(unittest.TestCase):
    """The validation tool must share the implementation, not mirror it."""

    def test_the_validation_tool_uses_the_shared_objects(self):
        import importlib.util
        import os

        spec = importlib.util.spec_from_file_location(
            "validate_candidate",
            os.path.join(support.ROOT, "tools", "validate_candidate.py"),
        )
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)

        #: Identity, not equality: a drifted copy could still be equal
        #: today and wrong tomorrow.
        self.assertIs(tool.assert_read_only, assert_observational)
        self.assertIs(tool.UnsafePayload, UnsafePayload)
        self.assertIs(tool.READ_ONLY_SERVICES, OBSERVATIONAL_SERVICES)
        self.assertIs(tool.DDD_READ_SUBFUNCTIONS, DDD_SUBFUNCTIONS)
        self.assertIs(tool.WRITE_SERVICES, WRITE_SERVICES)


if __name__ == "__main__":
    unittest.main()
