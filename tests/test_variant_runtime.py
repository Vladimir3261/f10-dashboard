"""
Compatibility versus identity in the runtime.

The verified F-series dynamic channels reach the poll loop only when the
ECU has PROVED, by answering a read a mapping nominates, that it speaks
the profile those channels need. That is compatibility. Which exact
SGBD revision the ECU is is a different claim with a different kind of
evidence, and a successful probe never makes it - so `exact_sgbd` stays
`unknown` until something that IS identity evidence says otherwise.
Every outcome carries its reason. All against a fake transport - no car.

Issue #10 (2026-09-05): before this, one `sgbd_variant` capability
carried both claims and one answered read "confirmed" it.
"""

import unittest

from tests import support  # noqa: F401
from tests.support import hexb

from bmwdiag.mapping import MappingExecutor, MappingRegistry, load_file, load_text
from bmwdiag.mapping.errors import InvalidFieldError
from bmwdiag.mapping.model import Capability
from bmwdiag.obd import ObdCapabilitySet
from bmwdiag.protocol import NegativeResponse
from bmwdiag.variant import (
    AMBIGUOUS,
    COMPATIBLE,
    CONFIRMED,
    UNKNOWN,
    UNSUPPORTED,
    CombinedCapabilitySet,
    EcuIdentity,
    IdentityFact,
    IdentityResolution,
    ProfileProbe,
    ProfileResolution,
    profile_nominations,
)

N47 = support.os.path.join(
    support.ROOT, "mappings", "candidates", "bmw", "dde", "n47",
)
DYNAMIC = support.os.path.join(N47, "d72n47a0_dynamic.yaml")
FLOW = support.os.path.join(N47, "d72n47a0_flow.yaml")
DPF = support.os.path.join(N47, "d72n47a0_dpf_egr.yaml")
GEARBOX = support.os.path.join(N47, "d72n47a0_gearbox.yaml")
KWP = support.os.path.join(N47, "dde7_kwp_local_id.yaml")

PROFILE = "fseries-f303-d72-compatible"
PROFILE_CAP = Capability("diagnostic_profile", PROFILE)
SGBD_CAP = Capability("exact_sgbd", "d72n47a0")


class FakeDde:
    """Answers the F303 define/clear and returns per-source raw words."""

    RAW = {
        "4517": "39 08", "44be": "03 f7", "44c1": "03 f6", "4bc3": "0e 2f",
        "461b": "46 a6", "4841": "2c 33", "42c8": "2d 7b",
    }

    def __init__(self, refuse=()):
        self.sent = []
        self.last = None
        #: source ids the ECU refuses to define - NRC 0x31 to the setup
        self.refuse = {r.lower() for r in refuse}

    def request(self, payload, *, dst, timeout=None):
        h = bytes(payload).hex()
        self.sent.append(h)

        if h.startswith("2c01f303"):
            self.last = h[8:12]

            if self.last in self.refuse:
                raise NegativeResponse(0x2C, 0x31)

        if h == "22f303":
            return hexb("62 f3 03 " + self.RAW.get(self.last, "00 00"))

        return hexb("6c 03 f3 03")


def text_of(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def probe(ecu, *mappings, dst=0x12):
    """Resolve every profile the mappings require against a fake ECU."""
    registry = MappingRegistry([load_file(m) for m in mappings])
    nominations = profile_nominations(registry.mappings)

    return EcuIdentity(ProfileProbe(
        lambda p, dst, timeout=None: ecu.request(p, dst=dst)
    ).resolve(nominations, dst))


class Nominations(unittest.TestCase):
    def test_a_file_nominates_its_probes_explicitly(self):
        """
        Not "the first request in the file": a reorder must not change
        what is sent to the car to prove the profile.
        """
        registry = MappingRegistry([load_file(DYNAMIC)])
        [nomination] = profile_nominations(registry.mappings)

        self.assertEqual(nomination.profile, PROFILE)
        self.assertEqual(
            [r.id for r in nomination.requests],
            ["n47.d72.dyn.4517", "n47.d72.dyn.4BC3"],
        )
        self.assertEqual(nomination.derived_from, ("d72n47a0",))

    def test_files_sharing_a_profile_pool_their_probes(self):
        """One profile, one probe pass at connect - not one per file."""
        registry = MappingRegistry([
            load_file(m) for m in (DYNAMIC, FLOW, DPF, GEARBOX)
        ])
        nominations = profile_nominations(registry.mappings)

        self.assertEqual([n.profile for n in nominations], [PROFILE])
        self.assertEqual(
            [r.id for r in nominations[0].requests],
            ["n47.d72.dyn.4517", "n47.d72.dyn.4BC3", "n47.d72.dyn.461B",
             "n47.d72.dyn.4841", "n47.d72.dyn.44B8", "n47.d72.dyn.467E",
             "n47.d72.dyn.46F0", "n47.d72.dyn.46ED"],
        )

    def test_a_nomination_must_name_a_request_in_the_file(self):
        text = text_of(DYNAMIC).replace(
            "probe: [n47.d72.dyn.4517, n47.d72.dyn.4BC3]",
            "probe: [n47.d72.dyn.4517, n47.d72.dyn.9999]",
        )

        with self.assertRaises(InvalidFieldError) as caught:
            load_text(text, "dynamic")

        self.assertEqual(caught.exception.path, "ecu.match.probe[1]")
        self.assertIn("9999", str(caught.exception))

    def test_a_probe_needs_a_profile_to_prove(self):
        text = text_of(DYNAMIC).replace(
            f"      diagnostic_profile: {PROFILE}\n", ""
        )

        with self.assertRaises(InvalidFieldError) as caught:
            load_text(text, "dynamic")

        self.assertEqual(caught.exception.path, "ecu.match.probe")

    def test_the_conflated_capability_is_refused_by_name(self):
        """
        `sgbd_variant` claimed identity on the strength of a probe. A
        file still spelling it must not load and quietly prove nothing.
        """
        text = text_of(DYNAMIC).replace(
            f"diagnostic_profile: {PROFILE}", "sgbd_variant: d72n47a0"
        ).replace("    probe: [n47.d72.dyn.4517, n47.d72.dyn.4BC3]\n", "")

        with self.assertRaises(InvalidFieldError) as caught:
            load_text(text, "dynamic")

        self.assertEqual(
            caught.exception.path, "ecu.match.capability.sgbd_variant"
        )
        self.assertIn("diagnostic_profile", str(caught.exception))
        self.assertIn("exact_sgbd", str(caught.exception))


class Probing(unittest.TestCase):
    def test_a_successful_probe_makes_the_profile_compatible(self):
        fake = FakeDde()
        identity = probe(fake, DYNAMIC)

        self.assertEqual(identity.compatible, {PROFILE})
        self.assertEqual(identity.outcome(PROFILE_CAP), COMPATIBLE)

        [resolution] = identity.profiles.values()
        self.assertEqual(len(resolution.probes), 1)
        self.assertEqual(resolution.probes[0].request_id, "n47.d72.dyn.4517")
        self.assertEqual(resolution.probes[0].reason, "answered")
        self.assertEqual(resolution.probes[0].detail, "62 f3 03 39 08")
        #: the alternate was not sent - one shape-correct answer is what
        #: compatibility means, and connect traffic stays what it was
        self.assertNotIn("2c01f3034bc3" + "0102", fake.sent)

    def test_compatibility_does_not_make_the_exact_sgbd_known(self):
        """The whole point: a probe proves behaviour, never identity."""
        identity = probe(FakeDde(), DYNAMIC)

        self.assertTrue(identity.satisfies(PROFILE_CAP))
        self.assertFalse(identity.satisfies(SGBD_CAP))
        self.assertEqual(identity.outcome(SGBD_CAP), UNKNOWN)
        self.assertEqual(identity.identity.sgbd, None)
        self.assertIn("no identity evidence", identity.explain(SGBD_CAP))

    def test_the_alternate_probe_rescues_a_refused_first_one(self):
        fake = FakeDde(refuse=["4517"])
        identity = probe(fake, DYNAMIC)

        self.assertEqual(identity.outcome(PROFILE_CAP), COMPATIBLE)

        [resolution] = identity.profiles.values()
        self.assertEqual(
            [(p.request_id, p.answered, p.reason) for p in resolution.probes],
            [("n47.d72.dyn.4517", False, "negative_response"),
             ("n47.d72.dyn.4BC3", True, "answered")],
        )
        #: the refusal is recorded with the frame it refused and the code
        self.assertIn("NRC 0x31", resolution.probes[0].detail)
        self.assertIn("setup 2c 01 f3 03 45 17", resolution.probes[0].detail)
        self.assertEqual(resolution.describe(),
                         "compatible: n47.d72.dyn.4BC3 answered")

    def test_every_probe_refused_is_unsupported_with_each_reason(self):
        class Refuses:
            def request(self, payload, *, dst, timeout=None):
                raise NegativeResponse(payload[0], 0x31)

        identity = probe(Refuses(), DYNAMIC)

        self.assertEqual(identity.outcome(PROFILE_CAP), UNSUPPORTED)
        self.assertFalse(identity.satisfies(PROFILE_CAP))
        self.assertFalse(identity.known)

        why = identity.explain(PROFILE_CAP)
        self.assertTrue(why.startswith("unsupported: "), why)
        self.assertIn("n47.d72.dyn.4517: negative_response", why)
        self.assertIn("n47.d72.dyn.4BC3: negative_response", why)
        self.assertIn("NRC 0x31", why)

    def test_a_wrong_shape_is_not_compatible_and_says_which_way(self):
        cases = {
            #: answers, but to the wrong DID
            "wrong_prefix": lambda: hexb("62 f3 04 39 08"),
            #: right prefix, one data byte where the mapping declares two
            "short_response": lambda: hexb("62 f3 03 39"),
            #: nothing at all
            "short_response ": lambda: b"",
        }

        for expected, reply in cases.items():
            with self.subTest(expected=expected):
                class OddEcu:
                    def request(self, payload, *, dst, timeout=None):
                        if bytes(payload).hex() == "22f303":
                            return reply()
                        return hexb("6c 03 f3 03")

                identity = probe(OddEcu(), DYNAMIC)

                self.assertEqual(identity.outcome(PROFILE_CAP), UNSUPPORTED)
                [resolution] = identity.profiles.values()
                self.assertEqual(
                    {p.reason for p in resolution.probes},
                    {expected.strip()},
                )

    def test_a_timeout_and_a_nack_are_named_as_such(self):
        class Silent:
            def request(self, payload, *, dst, timeout=None):
                raise TimeoutError("HSFZ read timeout")

        class Unrouted:
            def request(self, payload, *, dst, timeout=None):
                import live
                raise live.HsfzNack("gateway will not route to 0x12")

        for ecu, reason in ((Silent(), "transport_timeout"),
                            (Unrouted(), "transport_nack")):
            with self.subTest(reason=reason):
                identity = probe(ecu, DYNAMIC)
                [resolution] = identity.profiles.values()

                self.assertEqual(resolution.outcome, UNSUPPORTED)
                self.assertEqual(
                    {p.reason for p in resolution.probes}, {reason}
                )

    def test_a_gate_refusal_is_a_mapping_bug_not_an_ecu_refusal(self):
        """
        A probe our own safety gate refuses to send never reached the car.
        It must not land in the "ECU refused" bucket: `unsafe_payload` is
        its own reason, so a broken nomination is visible as such.
        """
        from bmwdiag.protocol import UnsafePayload

        class Gate:
            def request(self, payload, *, dst, timeout=None):
                raise UnsafePayload("service 0x2E is not observational")

        identity = probe(Gate(), DYNAMIC)
        [resolution] = identity.profiles.values()

        self.assertEqual(resolution.outcome, UNSUPPORTED)
        self.assertEqual(
            {p.reason for p in resolution.probes}, {"unsafe_payload"}
        )
        self.assertIn("unsafe_payload", resolution.describe())

    def test_a_hand_built_compatible_resolution_still_describes_itself(self):
        """`describe()` feeds `/api/diagnostics`; it must never raise."""
        bare = ProfileResolution("some-profile", COMPATIBLE)

        self.assertEqual(bare.describe(), "compatible: no probe recorded")
        self.assertEqual(bare.as_dict()["summary"], bare.describe())

    def test_a_profile_nobody_nominated_a_probe_for_is_unknown_not_false(self):
        text = text_of(DYNAMIC).replace(
            "    probe: [n47.d72.dyn.4517, n47.d72.dyn.4BC3]\n", ""
        )
        registry = MappingRegistry([load_text(text, "dynamic")])
        fake = FakeDde()
        identity = EcuIdentity(ProfileProbe(
            lambda p, dst, timeout=None: fake.request(p, dst=dst)
        ).resolve(profile_nominations(registry.mappings), 0x12))

        self.assertEqual(fake.sent, [])
        self.assertEqual(identity.outcome(PROFILE_CAP), UNKNOWN)
        self.assertFalse(identity.satisfies(PROFILE_CAP))
        self.assertIn("nominates a probe", identity.explain(PROFILE_CAP))

    def test_a_profile_no_loaded_mapping_mentions_is_unknown(self):
        identity = probe(FakeDde(), DYNAMIC)
        other = Capability("diagnostic_profile", "dde7-kwp-local-id")

        self.assertEqual(identity.outcome(other), UNKNOWN)
        self.assertIn("no loaded mapping", identity.explain(other))

    def test_several_profiles_resolve_each_on_their_own_evidence(self):
        """
        Two profiles, one ECU: each is judged by its own nominated read,
        and both outcomes are reported - an ECU compatible with the F303
        sequence and not with KWP local ids is exactly that, not "the
        d72 one".
        """
        identity = probe(FakeDde(), DYNAMIC, KWP)

        self.assertEqual(identity.outcome(PROFILE_CAP), COMPATIBLE)
        kwp = Capability("diagnostic_profile", "dde7-kwp-local-id")
        self.assertEqual(identity.outcome(kwp), UNSUPPORTED)
        #: 6C 03 F3 03 came back - the fake's catch-all - not the 6C 10
        #: prefix the KWP read declares
        self.assertEqual(
            identity.profiles["dde7-kwp-local-id"].probes[0].reason,
            "wrong_prefix",
        )
        self.assertEqual(identity.compatible, {PROFILE})
        self.assertEqual(identity.outcome(SGBD_CAP), UNKNOWN)


class Identity(unittest.TestCase):
    """Identity evidence is separate and additive - never a probe."""

    def test_no_evidence_is_unknown(self):
        self.assertEqual(IdentityResolution.from_facts([]).outcome, UNKNOWN)
        self.assertEqual(EcuIdentity().outcome(SGBD_CAP), UNKNOWN)

    def test_agreeing_evidence_confirms_exactly_that_sgbd(self):
        identity = EcuIdentity(
            identity=IdentityResolution.from_facts([
                IdentityFact("d72n47a0", "uds 22 F19E"),
                IdentityFact("d72n47a0", "ISTA ident page"),
            ]),
        )

        self.assertEqual(identity.outcome(SGBD_CAP), CONFIRMED)
        self.assertTrue(identity.satisfies(SGBD_CAP))
        self.assertTrue(identity.known)
        #: exactly that one - a sibling revision is refused, with the
        #: evidence named
        other = Capability("exact_sgbd", "d73n47a0")
        self.assertEqual(identity.outcome(other), UNSUPPORTED)
        self.assertIn("d72n47a0", identity.explain(other))

    def test_disagreeing_evidence_is_ambiguous_and_satisfies_nothing(self):
        identity = EcuIdentity(
            identity=IdentityResolution.from_facts([
                IdentityFact("d72n47a0", "uds 22 F19E"),
                IdentityFact("d73n47a0", "operator note"),
            ]),
        )

        self.assertEqual(identity.outcome(SGBD_CAP), AMBIGUOUS)
        self.assertFalse(identity.satisfies(SGBD_CAP))
        self.assertFalse(identity.satisfies(Capability("exact_sgbd", "d73n47a0")))
        self.assertIn("disagrees", identity.explain(SGBD_CAP))

    def test_identity_never_stands_in_for_compatibility(self):
        """
        Knowing WHAT the ECU is does not mean it answered anything: a
        mapping gated on a profile still needs the probe.
        """
        identity = EcuIdentity(
            identity=IdentityResolution.from_facts(
                [IdentityFact("d72n47a0", "uds 22 F19E")]
            ),
        )

        self.assertFalse(identity.satisfies(PROFILE_CAP))
        self.assertEqual(identity.outcome(PROFILE_CAP), UNKNOWN)

    def test_a_mapping_can_require_identity_and_stay_dormant(self):
        text = text_of(DYNAMIC).replace(
            f"      diagnostic_profile: {PROFILE}\n",
            f"      diagnostic_profile: {PROFILE}\n"
            "      exact_sgbd: d72n47a0\n",
        )
        registry = MappingRegistry([load_text(text, "dynamic")])
        fake = FakeDde()
        compatible = EcuIdentity(ProfileProbe(
            lambda p, dst, timeout=None: fake.request(p, dst=dst)
        ).resolve(profile_nominations(registry.mappings), 0x12))

        dormant = registry.resolve(compatible, targets={"discovered_engine": 0x12})
        self.assertEqual(dormant.requests, [])
        [dropped] = dormant.report.by_reason("ecu_mismatch")
        self.assertIn("exact_sgbd='d72n47a0'", dropped.detail)
        self.assertIn("unknown: no identity evidence", dropped.detail)
        #: and the profile is NOT in the complaint - it was met
        self.assertNotIn("diagnostic_profile", dropped.detail)

        with_evidence = EcuIdentity(
            compatible.profiles.values(),
            IdentityResolution.from_facts([IdentityFact("d72n47a0", "test")]),
        )
        active = registry.resolve(with_evidence, targets={"discovered_engine": 0x12})
        self.assertEqual(len(active.requests), 4)


class Resolution(unittest.TestCase):
    def test_combined_caps_answer_each_kind_from_its_provider(self):
        caps = CombinedCapabilitySet(
            ObdCapabilitySet({0x0C, 0x05}),
            probe(FakeDde(), DYNAMIC),
        )
        self.assertTrue(caps.satisfies(Capability("obd_mode01_pid", 0x0C)))
        self.assertTrue(caps.satisfies(PROFILE_CAP))
        self.assertFalse(caps.satisfies(SGBD_CAP))
        self.assertIn("no identity evidence", caps.explain(SGBD_CAP))
        self.assertIsNone(caps.explain(Capability("obd_mode01_pid", 0x0D)))

    def test_resolution_gates_the_channels_on_demonstrated_compatibility(self):
        registry = MappingRegistry([load_file(DYNAMIC)])

        #: no provider for the profile -> the file's ecu.match fails
        without = registry.resolve(
            ObdCapabilitySet({0x0C}), targets={"discovered_engine": 0x12}
        )
        self.assertEqual(without.requests, [])

        #: profile proven -> the channels resolve, with no identity claim
        identity = probe(FakeDde(), DYNAMIC)
        self.assertEqual(identity.identity.outcome, UNKNOWN)
        with_profile = registry.resolve(
            CombinedCapabilitySet(ObdCapabilitySet({0x0C}), identity),
            targets={"discovered_engine": 0x12},
        )
        self.assertEqual(len(with_profile.requests), 4)

    def test_the_report_carries_the_reason_not_just_false(self):
        registry = MappingRegistry([load_file(DYNAMIC)])
        silent = type("Silent", (), {
            "request": lambda self, p, *, dst, timeout=None:
                (_ for _ in ()).throw(TimeoutError("HSFZ read timeout")),
        })()
        identity = probe(silent, DYNAMIC)
        profile = registry.resolve(identity, targets={"discovered_engine": 0x12})

        [dropped] = profile.report.by_reason("ecu_mismatch")
        self.assertIn("unsupported", dropped.detail)
        self.assertIn("transport_timeout", dropped.detail)

    def test_the_diagnostics_shape_keeps_the_two_claims_apart(self):
        identity = probe(FakeDde(refuse=["4517"]), DYNAMIC)
        d = identity.as_dict()

        self.assertEqual(
            [(p["profile"], p["outcome"]) for p in d["profiles"]],
            [(PROFILE, COMPATIBLE)],
        )
        self.assertEqual(
            [(q["request"], q["answered"], q["reason"])
             for q in d["profiles"][0]["probes"]],
            [("n47.d72.dyn.4517", False, "negative_response"),
             ("n47.d72.dyn.4BC3", True, "answered")],
        )
        self.assertEqual(d["profiles"][0]["derived_from"], ["d72n47a0"])
        self.assertEqual(d["exact_sgbd"]["outcome"], UNKNOWN)
        self.assertEqual(d["exact_sgbd"]["sgbd"], None)
        self.assertEqual(d["exact_sgbd"]["facts"], [])


class F303Multiplexing(unittest.TestCase):
    def profile(self):
        registry = MappingRegistry([load_file(FLOW)])

        return registry.resolve(
            probe(FakeDde(), FLOW), targets={"discovered_engine": 0x12},
        )

    def test_shared_dynamic_did_channels_decode_independently(self):
        profile = self.profile()
        fake = FakeDde()
        executor = MappingExecutor(profile, transport=fake)

        requests = [profile.request(i) for i in (
            "n47.d72.dyn.461B", "n47.d72.dyn.4841", "n47.d72.dyn.42C8"
        )]
        values = executor.execute(requests)

        # each channel decoded ITS OWN source, no bleed across the shared DID
        self.assertEqual(values["n47d_coolant"], 80.86)
        self.assertEqual(round(values["n47d_boost_act"], 1), 1035.9)
        self.assertEqual(round(values["n47d_boost_set"], 1), 1066.0)

        # a define was re-armed before each different poll, in order
        defines = [f[8:12] for f in fake.sent if f.startswith("2c01")]
        self.assertEqual(defines, ["461b", "4841", "42c8"])

    def test_a_single_channel_arms_its_define_once(self):
        profile = self.profile()
        fake = FakeDde()
        executor = MappingExecutor(profile, transport=fake)
        req = profile.request("n47.d72.dyn.461B")

        executor.execute([req])
        executor.execute([req])          # armed already -> reuse

        defines = [f for f in fake.sent if f.startswith("2c01")]
        self.assertEqual(len(defines), 1)


class RuntimeLoad(unittest.TestCase):
    def test_extra_mappings_load_but_base_stays_obd_only(self):
        import live

        base = live.load_registry(support.MAPPINGS)
        self.assertEqual({m.id for m in base.mappings}, {"sae-obd-engine"})

        live.load_extra(base, [N47])
        ids = {m.id for m in base.mappings}
        self.assertIn("sae-obd-engine", ids)
        self.assertIn("candidate-n47-d72-dynamic", ids)
        self.assertIn("candidate-n47-d72-flow", ids)

    def test_the_negative_response_is_data_on_the_live_transport(self):
        """
        live.py's NRC error carries the code as an attribute, so the
        probe and the fault recorder classify it without parsing prose.
        """
        import live
        from bmwdiag.mapping import fault_kind

        exc = live.HsfzNegativeResponse(0x22, 0x31)

        self.assertIsInstance(exc, live.HsfzError)
        self.assertIsInstance(exc, NegativeResponse)
        self.assertEqual((exc.service, exc.nrc), (0x22, 0x31))
        self.assertEqual(fault_kind(exc), "negative_response")
        self.assertEqual(str(exc), "negative response to 0x22: NRC 0x31")


if __name__ == "__main__":
    unittest.main()
