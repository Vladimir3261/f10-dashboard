"""
The issue #15 candidate mappings: the OFFLINE half of a validation.

Nothing here claims a channel is right. What is pinned is everything a
candidate must get right BEFORE a drive can say anything about it:

  * it loads with `production: false` / `status: candidate` / version 1,
  * every frame it would put on the wire is observational,
  * it resolves ONLY against an ECU that has proved the capability the
    file requires, and is dropped with the right reason otherwise,
  * every declared scale decodes bit-identically to the table's formula
    (scale, then add, then round - the same separated steps the verified
    files rely on), and the declared range/quality semantics fire,
  * declared setpoint/actual pairs land in one rotation slot,
  * and none of it reaches the car by default: run_car.sh does not load
    these, and the production mapping is untouched.

No car, no network, no BMW data: the fake ECU answers the F303 define
with the raw words the tests choose.
"""

import os
import unittest

from tests import support  # noqa: F401
from tests.support import hexb
from tests.test_polling_pairs import (
    CAR_FILES, gap_and_coverage, owner_of, simulate,
)
from tests.test_variant_runtime import FakeDde, PROFILE, probe

from bmwdiag.mapping import MappingRegistry, load_file
from bmwdiag.mapping.decoder import CLIPPED, OK, read_response
from bmwdiag.mapping.modes import load_modes
from bmwdiag.mapping.polling import PollingPlan, resolve_classes
from bmwdiag.mapping.registry import AllCapabilities
from bmwdiag.obd import ObdCapabilitySet
from bmwdiag.protocol.safety import assert_observational
from bmwdiag.variant import CombinedCapabilitySet, profile_nominations

N47 = os.path.join(support.MAPPINGS, "candidates", "bmw", "dde", "n47")
EGS = os.path.join(support.MAPPINGS, "candidates", "bmw", "egs")
OBD = os.path.join(support.MAPPINGS, "candidates", "obd")

INJECTORS = os.path.join(N47, "d72n47a0_injectors.yaml")
EGR = os.path.join(N47, "d72n47a0_egr.yaml")
AIRPATH = os.path.join(N47, "d72n47a0_airpath.yaml")
IBS = os.path.join(N47, "d72n47a0_ibs.yaml")
TANK = os.path.join(N47, "d72n47a0_tank.yaml")
EGS_SPEEDS = os.path.join(EGS, "f10_transmission_speeds.yaml")
SAE_EXTRA = os.path.join(OBD, "engine_sae_extra.yaml")

DDE_FILES = (INJECTORS, EGR, AIRPATH, IBS, TANK)
ALL_FILES = DDE_FILES + (EGS_SPEEDS, SAE_EXTRA)

#: The F303 source ids each DDE candidate file requests, so the fake ECU
#: can answer them (any word - the resolution tests only need "answers").
DDE_IDS = {
    INJECTORS: ["5591", "5592", "5593", "5594", "45d5",
                "442a", "442b", "442c", "442d"],
    EGR: ["4c93", "4c97", "487e"],
    AIRPATH: ["4cc9", "4cc4", "42cd", "48d9", "48d7", "4bfb", "4bf8"],
    IBS: ["4286", "428d", "428c", "42a2", "4285", "4290"],
    TANK: ["4458"],
}


class AnsweringDde(FakeDde):
    """A FakeDde that also answers every candidate source id."""

    RAW = dict(FakeDde.RAW)
    for _ids in DDE_IDS.values():
        for _id in _ids:
            RAW.setdefault(_id, "12 34")


def registry_for(*paths):
    return MappingRegistry([load_file(p) for p in paths])


def engine_caps(ecu, *paths, pids=(0x0C,)):
    return CombinedCapabilitySet(ObdCapabilitySet(set(pids)), probe(ecu, *paths))


def f303(raw_hex):
    return hexb("62 F3 03 " + raw_hex)


# ------------------------------------------------------------- the shape


class EveryCandidateIsHonestAboutItsState(unittest.TestCase):
    def test_not_production_not_verified_version_one(self):
        for path in ALL_FILES:
            with self.subTest(file=os.path.basename(path)):
                mapping = load_file(path)
                self.assertFalse(mapping.production)
                self.assertEqual(mapping.verification.status, "candidate")
                self.assertEqual(mapping.version, 1)
                #: the plan is written down, not "TBD"
                self.assertIn("validate_candidate.py", mapping.verification.method)
                self.assertIn("fail", mapping.verification.method)

    def test_every_frame_a_candidate_would_send_is_observational(self):
        """
        Setup frames and the main request, through the ONE gate the
        runtime uses. A candidate that needed a write could not be
        validated read-only, so it could not exist.
        """
        from bmwdiag.protocol.request import build_payload

        for path in ALL_FILES:
            for request in load_file(path).requests:
                with self.subTest(file=os.path.basename(path), request=request.id):
                    for frame in request.setup:
                        assert_observational(bytes(frame))

                    payload = build_payload(request)
                    assert_observational(payload)
                    self.assertIn(payload[0], (0x01, 0x22))

    def test_the_dde_files_keep_the_variant_they_came_from(self):
        """
        Every row was quoted from d72n47a0 and the file says so; the
        profile it requires is the d72-compatible one, never a d71/d73
        identifier borrowed across variants.
        """
        for path in DDE_FILES:
            with self.subTest(file=os.path.basename(path)):
                mapping = load_file(path)
                self.assertEqual(mapping.ecu.sgbd, "d72n47a0")
                self.assertEqual(mapping.provenance.type, "ediabas")
                self.assertIn("b644de8fbfbb4b207f57794e3c7894dc1dc58627",
                              mapping.provenance.notes)
                [nomination] = profile_nominations([mapping])
                self.assertEqual(nomination.profile, PROFILE)
                #: nominated from the file's OWN requests
                own = {r.id for r in mapping.requests}
                self.assertTrue({r.id for r in nomination.requests} <= own)

    def test_the_egs_and_sae_files_name_their_sources(self):
        egs = load_file(EGS_SPEEDS)
        self.assertEqual(egs.provenance.type, "manual")
        self.assertIn("d634524724470765a16c2867eea515e462336f0e", egs.provenance.notes)
        self.assertIn("Field order", egs.provenance.notes)

        sae = load_file(SAE_EXTRA)
        self.assertEqual(sae.provenance.type, "obd_standard")
        self.assertIn("validation-runs/20260825T191658Z-identify", sae.provenance.notes)

    def test_nothing_here_is_loaded_by_the_car_launcher(self):
        """
        Candidates reach the car only through an explicit --extra-mappings
        on a validation run. run_car.sh (mirrored by CAR_FILES) must not
        know them, and the production set is a different file entirely.
        """
        with open(os.path.join(support.ROOT, "run_car.sh"), encoding="utf-8") as fh:
            launcher = fh.read()

        for path in ALL_FILES:
            name = os.path.basename(path)
            with self.subTest(file=name):
                self.assertNotIn(name, launcher)
                self.assertFalse(any(f.endswith(name) for f in CAR_FILES))


# -------------------------------------------------------------- resolve


class TheDdeCandidatesResolveByProof(unittest.TestCase):
    def _resolve(self, ecu, path, **kw):
        registry = registry_for(path)
        return registry.resolve(
            engine_caps(ecu, path, **kw), targets={"discovered_engine": 0x12},
        )

    def test_each_file_resolves_when_its_own_probe_answers(self):
        for path in DDE_FILES:
            with self.subTest(file=os.path.basename(path)):
                ecu = AnsweringDde()
                profile = self._resolve(ecu, path)

                self.assertEqual(len(profile.requests), len(DDE_IDS[path]))
                self.assertEqual(profile.report.dropped, ())
                #: and the proof was one of the file's own nominated ids
                nominated = [r.id[-4:].lower() for r in
                             profile_nominations(registry_for(path).mappings)[0].requests]
                self.assertIn(ecu.last, nominated)

    def test_each_file_is_dropped_as_ecu_mismatch_when_every_probe_is_refused(self):
        """
        An ECU that refuses to define BOTH nominated sources has not
        proved the profile - the file is dropped, with the reason, and
        no member of it is ever sent as telemetry.
        """
        for path in DDE_FILES:
            with self.subTest(file=os.path.basename(path)):
                nominated = [r.id[-4:] for r in
                             profile_nominations(registry_for(path).mappings)[0].requests]
                ecu = AnsweringDde(refuse=nominated)
                profile = self._resolve(ecu, path)

                self.assertEqual(profile.requests, [])
                [dropped] = profile.report.by_reason("ecu_mismatch")
                self.assertEqual(dropped.kind, "mapping")
                self.assertIn("diagnostic_profile", dropped.detail)
                #: only the probe frames went out - never a 22 F3 03 read
                self.assertNotIn("22f303", ecu.sent)

    def test_each_file_is_dropped_when_nothing_proves_the_profile(self):
        """OBD capability alone is not a profile proof."""
        for path in DDE_FILES:
            with self.subTest(file=os.path.basename(path)):
                profile = registry_for(path).resolve(
                    ObdCapabilitySet({0x0C}), targets={"discovered_engine": 0x12},
                )
                self.assertEqual(profile.requests, [])
                self.assertEqual(len(profile.report.by_reason("ecu_mismatch")), 1)

    def test_a_refused_member_that_is_not_the_probe_does_not_drop_the_file(self):
        """
        Per #10: the profile is proved once; an individual source the DDE
        then refuses is a request with `sent` and no `ok` in the
        diagnostics, not a dropped file. Resolution still includes it.
        """
        ecu = AnsweringDde(refuse=["487e"])
        profile = self._resolve(ecu, EGR)

        self.assertEqual(len(profile.requests), 3)
        self.assertEqual(profile.report.dropped, ())

    def test_beside_the_verified_set_the_profile_is_one_nomination(self):
        """
        Loading all five candidates with the four verified DDE files pools
        the probes into ONE nomination for the one profile: the first
        answer proves it for every file, no per-file probe traffic.
        """
        verified = [os.path.join(N47, "d72n47a0_%s.yaml" % n)
                    for n in ("dynamic", "flow", "dpf_egr", "gearbox")]
        registry = registry_for(*verified, *DDE_FILES)
        nominations = profile_nominations(registry.mappings)

        self.assertEqual([n.profile for n in nominations], [PROFILE])
        #: the verified files' probes come first - the proof still comes
        #: from a channel that has answered on this car before
        self.assertEqual(nominations[0].requests[0].id, "n47.d72.dyn.4517")

        ecu = AnsweringDde()
        profile = registry.resolve(
            engine_caps(ecu, *verified, *DDE_FILES),
            targets={"discovered_engine": 0x12},
        )
        self.assertEqual(profile.report.dropped, ())
        #: exactly one define was needed to prove the profile
        self.assertEqual(
            sum(1 for h in ecu.sent if h.startswith("2c01f303")), 1,
        )


class TheEgsCandidateResolvesByFamily(unittest.TestCase):
    def test_resolves_for_the_transmission_at_its_fixed_address(self):
        profile = registry_for(EGS_SPEEDS).resolve(AllCapabilities())

        self.assertEqual([r.id for r in profile.requests],
                         ["egs.speeds.DA2A", "egs.atf.DA12"])
        self.assertTrue(all(r.target.address == 0x18 for r in profile.requests))
        self.assertTrue(all(r.timeout == 0.4 for r in profile.requests))

    def test_it_is_not_an_engine_file(self):
        """
        Resolving the ENGINE set against an EGS file: the family does not
        match, so the file is skipped with the reason - no DID reaches
        the DDE that was meant for the gearbox.
        """
        profile = registry_for(EGS_SPEEDS).resolve(
            AllCapabilities(), family="engine",
        )
        self.assertEqual(profile.requests, [])
        [dropped] = profile.report.by_reason("family")
        self.assertIn("transmission", dropped.detail)


class TheSaeExtraResolvesByAdvertisement(unittest.TestCase):
    def test_requests_drop_individually_when_the_pid_is_not_advertised(self):
        profile = registry_for(SAE_EXTRA).resolve(
            ObdCapabilitySet({0x0C, 0x11}), targets={"discovered_engine": 0x12},
        )
        self.assertEqual(profile.requests, [])
        dropped = {d.id: d for d in profile.report.by_reason("capability")}
        self.assertEqual(set(dropped), {"obd.mode01.01", "obd.mode01.4C"})
        self.assertTrue(all(d.kind == "request" for d in dropped.values()))

    def test_they_resolve_when_advertised(self):
        profile = registry_for(SAE_EXTRA).resolve(
            ObdCapabilitySet({0x0C, 0x01, 0x4C}), targets={"discovered_engine": 0x12},
        )
        self.assertEqual({r.id for r in profile.requests},
                         {"obd.mode01.01", "obd.mode01.4C"})
        self.assertEqual(profile.report.dropped, ())

    def test_the_file_itself_needs_pid_0c_like_the_production_set(self):
        profile = registry_for(SAE_EXTRA).resolve(
            ObdCapabilitySet({0x01, 0x4C}), targets={"discovered_engine": 0x12},
        )
        self.assertEqual(profile.requests, [])
        self.assertEqual(len(profile.report.by_reason("ecu_mismatch")), 1)


# --------------------------------------------------------------- decode


def u16(raw):
    return "%02X %02X" % (raw >> 8, raw & 0xFF)


class FrozenScales(unittest.TestCase):
    """
    Every declared transform, computed the way the table states it and
    the decoder is contracted to apply it: raw * scale, + add, round(n).
    `expected` is written as that expression, not as a literal, so the
    test pins the STEP ORDER - a merged `(raw * s + a)` in one float op
    would not be bit-identical for every raw.
    """

    def setUp(self):
        self.registry = registry_for(*ALL_FILES)

    def read(self, request_id, response):
        return read_response(self.registry.find_request(request_id), response)

    def _check(self, request_id, key, raws, formula, digits):
        for raw in raws:
            with self.subTest(request=request_id, raw=hex(raw)):
                [reading] = [v for k, v in self.read(request_id, f303(u16(raw))).items()
                             if k == key]
                self.assertEqual(reading.value, round(formula(raw), digits))

    def test_offset_100_scale_0_003052(self):
        #: injector corrections, quantity setpoint, EGR position setpoint,
        #: throttle setpoint. raw 32767 ~ 0.0 mg/hub / 0 %.
        f = lambda r: r * 0.003052 + -100.0  # noqa: E731
        raws = (0, 1, 32767, 32768, 33000, 0xFFFE, 0xFFFF)

        self._check("n47.d72.dyn.5591", "n47d_inj_corr_cyl1", raws, f, 3)
        self._check("n47.d72.dyn.45D5", "n47d_inj_qty_set", raws, f, 3)
        self._check("n47.d72.dyn.4C93", "n47d_egr_pos_set", raws, f, 2)
        self._check("n47.d72.dyn.4BFB", "n47d_throttle_set", raws, f, 2)

    def test_unsigned_scale_0_001526(self):
        f = lambda r: r * 0.001526  # noqa: E731
        raws = (0, 1, 655, 32768, 65535)

        self._check("n47.d72.dyn.4C97", "n47d_egr_pos_act", raws, f, 2)
        self._check("n47.d72.dyn.4CC4", "n47d_vnt_act", raws, f, 2)
        self._check("n47.d72.dyn.48D9", "n47d_swirl_set", raws, f, 2)
        self._check("n47.d72.dyn.42A2", "n47d_battery_soc", raws, f, 2)

    def test_unsigned_scale_0_01_and_0_012207(self):
        raws = (0, 1, 5000, 10000, 65535)
        for request_id, key in (
            ("n47.d72.dyn.487E", "n47d_egr_rate_set"),
            ("n47.d72.dyn.4CC9", "n47d_vnt_set"),
            ("n47.d72.dyn.4BF8", "n47d_throttle_act"),
        ):
            self._check(request_id, key, raws, lambda r: r * 0.01, 2)

        self._check("n47.d72.dyn.48D7", "n47d_swirl_act", raws, lambda r: r * 0.012207, 2)

    def test_boost_governor_deviation_is_centred_on_32767(self):
        f = lambda r: r * 0.999985 + -32767.0  # noqa: E731
        self._check("n47.d72.dyn.42CD", "n47d_boost_gov_dev",
                    (0, 32767, 32768, 33767, 65535), f, 2)
        #: and the sign convention survives: raw above centre is positive
        [dev] = self.read("n47.d72.dyn.42CD", f303(u16(33767))).values()
        self.assertGreater(dev.value, 999.0)

    def test_ibs_current_voltage_temperature_and_start_margin(self):
        self._check("n47.d72.dyn.4286", "n47d_ibs_current",
                    (0, 2500, 2501, 3000, 12000), lambda r: r * 0.08 + -200.0, 2)
        self._check("n47.d72.dyn.428D", "n47d_ibs_voltage",
                    (0, 25600, 26000, 65535), lambda r: r * 0.00025 + 6.0, 4)
        self._check("n47.d72.dyn.428C", "n47d_battery_temp",
                    (0, 5413, 7500, 65535), lambda r: r * 0.009237 + -50.0, 2)
        self._check("n47.d72.dyn.4285", "n47d_battery_start_margin",
                    (0, 16384, 32767), lambda r: r * 0.003052, 2)

    def test_alternator_current_is_a_single_byte(self):
        request = self.registry.find_request("n47.d72.dyn.4290")
        #: the define asks for ONE byte, and the response is one byte
        self.assertEqual(bytes(request.setup[1]).hex(" "), "2c 01 f3 03 42 90 01 01")
        [reading] = read_response(request, hexb("62 F3 03 7B")).values()
        self.assertEqual(reading, (123.0, OK))

    def test_tank_content_litres(self):
        self._check("n47.d72.dyn.4458", "n47d_tank_content",
                    (0, 1, 26220, 36700, 39328), lambda r: r * 0.001907, 3)

    def test_misfire_counters_are_plain_counts(self):
        for idx, request_id in enumerate(("n47.d72.dyn.442A", "n47.d72.dyn.442B",
                                          "n47.d72.dyn.442C", "n47.d72.dyn.442D")):
            [reading] = self.read(request_id, f303(u16(idx + 7))).values()
            self.assertEqual(reading, (float(idx + 7), OK))


class DeclaredQuality(unittest.TestCase):
    def setUp(self):
        self.registry = registry_for(*ALL_FILES)

    def read(self, request_id, response):
        return read_response(self.registry.find_request(request_id), response)

    def test_a_stuck_ffff_injector_correction_is_clipped_not_believed(self):
        """
        No sentinel is sourced for these rows, so none is declared; the
        declared +/-20 mg/hub range is what catches a stuck word. The
        number is kept (bit-exact +100.0), the label says it is not a
        measurement.
        """
        [reading] = self.read("n47.d72.dyn.5593", f303("FF FF")).values()
        self.assertEqual(reading.value, round(0xFFFF * 0.003052 + -100.0, 3))
        self.assertEqual(reading.quality, CLIPPED)
        self.assertFalse(reading.usable)

        #: and a plausible correction is ok
        [reading] = self.read("n47.d72.dyn.5593", f303(u16(32900))).values()
        self.assertEqual(reading.quality, OK)

    def test_ibs_current_above_1000_a_is_clipped(self):
        #: raw 0xFFFF -> 5042.8 A: no 12 V lead-acid does that. Raw 15000
        #: (= 1000.0 A, the cranking peak's order of magnitude) is the edge.
        [reading] = self.read("n47.d72.dyn.4286", f303("FF FF")).values()
        self.assertEqual(reading.quality, CLIPPED)
        [reading] = self.read("n47.d72.dyn.4286", f303(u16(15000))).values()
        self.assertEqual(reading, (1000.0, OK))

    def test_tank_content_above_75_l_is_clipped(self):
        #: raw 39328 -> 75.0 L (the 70 L tank + margin); one more is clipped
        [reading] = self.read("n47.d72.dyn.4458", f303(u16(39329))).values()
        self.assertEqual(reading.quality, CLIPPED)
        [reading] = self.read("n47.d72.dyn.4458", f303(u16(39328))).values()
        self.assertEqual(reading.quality, OK)


class TheEgsWordsAndTheSaeBits(unittest.TestCase):
    def setUp(self):
        self.registry = registry_for(*ALL_FILES)

    def test_da2a_is_two_signed_words_named_by_offset(self):
        request = self.registry.find_request("egs.speeds.DA2A")
        readings = read_response(request, hexb("62 DA 2A 07 D0 FF 38"))

        self.assertEqual(readings["egs_da2a_w0"], (2000.0, OK))
        self.assertEqual(readings["egs_da2a_w1"], (-200.0, OK))

    def test_da12_is_kept_raw(self):
        request = self.registry.find_request("egs.atf.DA12")
        [reading] = read_response(request, hexb("62 DA 12 5A")).values()
        self.assertEqual(reading, (90.0, OK))
        self.assertEqual(request.signals[0].unit, "")

    def test_pid_01_mil_bit_and_dtc_count(self):
        request = self.registry.find_request("obd.mode01.01")

        lit = read_response(request, hexb("41 01 83 07 65 04"))
        self.assertEqual(lit["mil"].value, 1.0)
        self.assertEqual(lit["dtc_count"].value, 3.0)

        clear = read_response(request, hexb("41 01 00 07 65 04"))
        self.assertEqual(clear["mil"].value, 0.0)
        self.assertEqual(clear["dtc_count"].value, 0.0)

        #: 0x7F: every count bit, MIL off - the mask must not leak bit 7
        full = read_response(request, hexb("41 01 7F 00 00 00"))
        self.assertEqual((full["mil"].value, full["dtc_count"].value), (0.0, 127.0))

    def test_pid_4c_uses_the_same_formula_as_pid_11(self):
        """
        0x4C is the commanded value 0x11 is the actual of; it is declared
        with the same separated steps (scale 100, divide 255, the loader's
        default rounding) so the two are bit-identical for every raw byte.
        """
        request = self.registry.find_request("obd.mode01.4C")
        production = registry_for(support.OBD_MAPPING).find_request("obd.mode01.11")
        [actual] = [s for s in production.signals if s.key == "throttle"]

        self.assertEqual(
            (actual.decode.scale, actual.decode.divide, actual.decode.round),
            (request.signals[0].decode.scale, request.signals[0].decode.divide,
             request.signals[0].decode.round),
        )

        for raw in range(256):
            with self.subTest(raw=raw):
                [commanded] = read_response(request, bytes([0x41, 0x4C, raw])).values()
                expected = read_response(production, bytes([0x41, 0x11, raw]))["throttle"]
                self.assertEqual(commanded.value, expected.value)
                self.assertEqual(commanded.value, round(raw * 100.0 / 255.0, actual.decode.round))


# -------------------------------------------------------------- polling


def full_plan(mode=None):
    """The car set plus every #15 candidate - a validation-run load."""
    registry = MappingRegistry(
        [load_file(os.path.join(support.ROOT, f)) for f in CAR_FILES]
        + [load_file(p) for p in ALL_FILES]
    )
    profile = registry.resolve(
        AllCapabilities(), config={"tank": 70.0},
        targets={"discovered_engine": 0x12},
    )
    plan = PollingPlan(
        profile.requests, resolve_classes(registry.polling_classes()), mode=mode,
    )

    return profile, plan


class TheCandidatesScheduleBesideTheCarSet(unittest.TestCase):
    def setUp(self):
        self.profile, self.plan = full_plan()
        self.fired, _ = simulate(self.plan)
        self.owner = owner_of(self.profile)

    def test_the_combined_plan_builds_in_every_mode(self):
        for mode in load_modes().modes.values():
            with self.subTest(mode=mode.name):
                full_plan(mode)

    def test_declared_pairs_share_a_firing(self):
        for a, b in (
            ("n47d_egr_pos_set", "n47d_egr_pos_act"),
            ("n47d_vnt_set", "n47d_vnt_act"),
            ("n47d_swirl_set", "n47d_swirl_act"),
            ("n47d_throttle_set", "n47d_throttle_act"),
        ):
            with self.subTest(pair=(a, b)):
                med, cov = gap_and_coverage(
                    self.fired, self.owner[a], self.owner[b], 1.0,
                )
                self.assertEqual(med, 0.0)
                self.assertEqual(cov, 100.0)

    def test_the_verified_pairs_are_not_disturbed(self):
        """Adding rotation members must not split boost or rail."""
        for a, b in (("n47d_boost_act", "n47d_boost_set"),
                     ("n47d_rail_act", "n47d_rail_set")):
            with self.subTest(pair=(a, b)):
                med, cov = gap_and_coverage(
                    self.fired, self.owner[a], self.owner[b], 1.0,
                )
                self.assertEqual((med, cov), (0.0, 100.0))

    def test_slow_adaptation_values_are_not_on_the_fast_rotation(self):
        """
        The injector corrections, IBS state and tank content declare
        `dde_slow`; only the IBS current (a fast quantity) rides dde_dyn.
        """
        by_class = {r.id: r.polling_class for r in self.profile.requests}

        for rid in ("n47.d72.dyn.5591", "n47.d72.dyn.442A", "n47.d72.dyn.42A2",
                    "n47.d72.dyn.428D", "n47.d72.dyn.4290", "n47.d72.dyn.4458"):
            self.assertEqual(by_class[rid], "dde_slow", rid)

        self.assertEqual(by_class["n47.d72.dyn.4286"], "dde_dyn")
        self.assertEqual(by_class["egs.speeds.DA2A"], "egs")
        self.assertEqual(by_class["egs.atf.DA12"], "egs_slow")
        self.assertEqual(by_class["obd.mode01.01"], "rare")
        self.assertEqual(by_class["obd.mode01.4C"], "context")

    def test_the_rotation_cost_the_files_state(self):
        """
        The comments in the files quote the rotation growth; pin the
        numbers so a comment cannot drift from the plan it describes.
        """
        counts = self.plan.counts()
        self.assertEqual(counts["dde_dyn"], 23 + 3 + 7 + 1)   # egr, airpath, ibs current
        self.assertEqual(counts["dde_slow"], 9 + 5 + 1)       # injectors, ibs, tank
        self.assertEqual(counts["egs"], 2)
        self.assertEqual(counts["egs_slow"], 1)


if __name__ == "__main__":
    unittest.main()
