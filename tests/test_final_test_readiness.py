"""
Dress rehearsal for the issue #15 final on-car test - offline.

The validation day loads, on top of the production set and the five
verified files, up to seven candidate files (`./run_car.sh --candidate
...`). Nothing here proves a channel right; it proves the LOAD is sane
before anyone sits in the car with it: every file loads together, no
two files claim a channel id or put the same frame on the wire twice,
every polling class the files declare is one the drive-mode table
(`config/modes.yaml` v3) knows how to treat, and the rotation cost the
docs quote is the one the plan actually has.

The capability set advertises everything, so what is measured is the
plan at its largest - the validation load, not a normal drive. No car,
no network, no BMW data.
"""

import os
import unittest

from tests import support
from tests.test_run_car import CANDIDATES, VERIFIED

from bmwdiag.mapping import MappingRegistry, load_file
from bmwdiag.mapping.modes import load_modes
from bmwdiag.mapping.polling import PollingPlan, resolve_classes
from bmwdiag.mapping.registry import AllCapabilities
from bmwdiag.protocol.request import build_request

PRODUCTION = ["mappings/obd/engine.yaml"]
CANDIDATE_FILES = list(CANDIDATES.values())
EVERYTHING = PRODUCTION + VERIFIED + CANDIDATE_FILES

#: What docs/TELEMETRY_CANDIDATES.md and n47-next-session.md § 3b quote
#: for the validation load. Measured here; the docs were updated to
#: these numbers on 2026-09-10.
QUOTED = {"dde_dyn": 34, "dde_slow": 15, "egs": 2, "egs_slow": 1}


def load_all(files=EVERYTHING):
    return [load_file(os.path.join(support.ROOT, f)) for f in files]


def rehearsal(mode=None, files=EVERYTHING):
    """Registry, profile and plan for the whole validation load."""
    registry = MappingRegistry(load_all(files))
    profile = registry.resolve(
        AllCapabilities(), config={"tank": 70.0},
        targets={"discovered_engine": 0x12},
    )
    plan = PollingPlan(
        profile.requests, resolve_classes(registry.polling_classes()), mode=mode,
    )

    return registry, profile, plan


def wire_identity(request, targets):
    """
    What actually goes on the wire, so two requests that would send the
    same frame to the same ECU are seen as one - regardless of their ids.

    The frame comes from the canonical builder (the one the executor
    uses), not from a re-derivation of its fields here: the pair (dst,
    built payload) plus the setup frames that precede it.
    """
    built = build_request(request, targets)
    return (built.dst, built.payload, request.setup)


class TheWholeLoadFits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry, cls.profile, cls.plan = rehearsal()
        cls.table = load_modes()

    def test_every_file_is_there_and_nothing_was_dropped(self):
        """With every capability advertised, every request resolves."""
        self.assertEqual(len(self.registry.mappings), len(EVERYTHING))
        self.assertEqual(self.profile.report.dropped, ())
        self.assertEqual(
            {m.id for m in self.profile.mappings},
            {m.id for m in self.registry.mappings},
        )

    def test_the_table_is_v3(self):
        self.assertEqual(self.table.fingerprint(), "drive-modes@3")

    def test_no_two_files_claim_a_channel(self):
        """The registry refuses this at add(); pin it for the full set."""
        keys = [s.key for m in self.registry.mappings for s in m.signals]
        keys += [d.key for m in self.registry.mappings for d in m.derived]

        dupes = sorted({k for k in keys if keys.count(k) > 1})
        self.assertEqual(dupes, [])
        self.assertEqual(len(self.profile.keys()), len(set(self.profile.keys())))

    def test_no_two_requests_share_an_id(self):
        ids = [r.id for r in self.profile.requests]

        self.assertEqual(sorted({i for i in ids if ids.count(i) > 1}), [])

    def test_no_two_requests_put_the_same_frame_on_the_wire(self):
        """
        Same ECU, same service, same PID/DID/payload, same setup: the
        second one is a wasted exchange on a rotation that is already
        the slowest it has been, and its two channels would disagree
        only by timing. The F303 dynamic reads are distinguished by
        their setup (define) frames, which is what makes them different.
        """
        targets = {"discovered_engine": 0x12}
        seen = {}

        for request in self.profile.requests:
            identity = wire_identity(request, targets)
            with self.subTest(request=request.id):
                self.assertIsNone(
                    seen.get(identity),
                    f"{request.id} sends the same frame as {seen.get(identity)}",
                )
            seen[identity] = request.id

    def test_a_class_declared_by_several_files_is_declared_identically(self):
        """
        `dde_slow` is declared in three files; resolve_classes() keeps the
        last. That is only harmless while they agree.
        """
        by_name = {}

        for mapping in self.registry.mappings:
            for cls in mapping.polling_classes:
                key = (cls.period, cls.priority, cls.stagger)
                by_name.setdefault(cls.name, {})[mapping.id] = key

        for name, decls in sorted(by_name.items()):
            with self.subTest(polling_class=name):
                self.assertEqual(len(set(decls.values())), 1, decls)

    def test_every_declared_class_is_one_the_modes_know(self):
        """
        Every polling class a loaded file names is named by the mode
        table - so no class is silently left at x1.0 by every mode, and
        `sampling` has decided whether it sleeps. And the other way
        round: with everything loaded, no mode names a class nobody
        declares (a dead multiplier).
        """
        declared = {c.name for c in self.registry.polling_classes()}
        declared |= {r.polling_class for r in self.profile.requests}
        known = set()

        for mode in self.table.modes.values():
            known |= set(mode.classes_used())

        self.assertEqual(sorted(declared - known), [])
        self.assertEqual(self.table.unknown_classes(declared), {})
        self.assertEqual(
            sorted(known),
            ["context", "control_ctx", "dde_dyn", "dde_slow", "egs",
             "egs_slow", "motion", "rare", "slow"],
        )

    def test_the_rotation_counts_the_docs_quote(self):
        counts = self.plan.counts()

        for name, expected in QUOTED.items():
            with self.subTest(polling_class=name):
                self.assertEqual(counts[name], expected, counts)

    def test_the_plan_builds_in_every_mode(self):
        for mode in self.table.modes.values():
            with self.subTest(mode=mode.name):
                rehearsal(mode)

    def test_the_candidate_classes_are_treated_like_slow(self):
        """
        What modes.yaml v3 changed, measured on the plan: the 10 s
        candidate classes are exempt from `sampling`'s sleep, unscaled
        in `long`, and x0.1 in `debug` - the same as `slow`.
        """
        for name in ("dde_slow", "egs_slow"):
            with self.subTest(polling_class=name):
                for mode_name in ("long", "debug", "sampling"):
                    _, _, plan = rehearsal(self.table.get(mode_name))
                    self.assertEqual(
                        plan.classes[name].period, plan.classes["slow"].period,
                        mode_name,
                    )

        _, _, plan = rehearsal(self.table.get("sampling"))
        plan.due(0, 1000.0)                    # sets the duty origin
        fired = set()
        now = 1000.0 + 200.0                   # 80 s into the 600 s sleep
        cycle = 2000

        while now < 1000.0 + 700.0:
            fired |= {r.polling_class for r in plan.due(cycle, now)}
            cycle += 1
            now += 0.1

        self.assertEqual(
            fired, {"slow", "rare", "dde_dyn", "dde_slow", "egs_slow"},
        )

    def test_each_candidate_loads_alone_on_the_verified_set_too(self):
        """
        The validation day loads ONE file per drive. Each must fit on
        the car set by itself, and the docs' per-file rotation costs
        follow from that.
        """
        base = rehearsal(files=PRODUCTION + VERIFIED)[2].counts()
        self.assertEqual(base.get("dde_dyn"), 23)
        self.assertNotIn("dde_slow", base)

        expected = {
            "injectors": {"dde_slow": 9},
            "egr": {"dde_dyn": 23 + 3},
            "airpath": {"dde_dyn": 23 + 7},
            "ibs": {"dde_dyn": 23 + 1, "dde_slow": 5},
            "tank": {"dde_slow": 1},
            "egs-speeds": {"egs": 2, "egs_slow": 1},
            # PID 0x01 is one request carrying mil + dtc_count; 0x4C one more.
            "sae-extra": {"rare": base["rare"] + 1, "context": base["context"] + 1},
        }

        for name, path in CANDIDATES.items():
            with self.subTest(candidate=name):
                counts = rehearsal(files=PRODUCTION + VERIFIED + [path])[2].counts()

                for cls, n in expected[name].items():
                    self.assertEqual(counts.get(cls), n, (name, counts))


if __name__ == "__main__":
    unittest.main()
