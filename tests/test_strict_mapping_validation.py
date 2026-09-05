"""
Mapping files fail closed (issue #9).

A mapping decides which bytes reach the car and which channels reach the
production set, so a field the loader does not understand cannot be a
no-op. Before 2026-09-03 it was: the loader read the keys it knew and
ignored the rest, so `prodution: false` loaded a candidate into the
production set, `production: "false"` (a string) was truthy, and
`defaults.request.timeout: 0.4` in two tracked files never reached the
wire. Every case here is a file that used to load, and what it used to
mean.
"""

import math
import os
import unittest

from . import support
from bmwdiag.mapping import load_file, load_text, load_tree
from bmwdiag.mapping.errors import (
    InvalidFieldError,
    InvalidLengthError,
    MappingError,
    UnknownFieldError,
    UnsupportedSchemaVersion,
)
from bmwdiag.mapping.loader import (
    FIELDS_REQUEST,
    FIELDS_REQUEST_DEFAULTS,
    RETIRED_FIELDS,
)

BASE = """
schema_version: 1

mapping:
  id: strict-fixture
  version: 1
  production: false

source:
  type: synthetic
  notes: unit test

verification:
  status: discovered
  method: none

ecu:
  family: test
  target: 0x7E
  match:
    capability:
      example_feature: true

defaults:
  request:
    transport: diagnostic

polling_classes:
  fast: {seconds: 0.1, priority: 0}
  paced: {seconds: 1.0, priority: 1, stagger: true}

requests:
  test.one:
    protocol: obd
    service: 0x01
    pid: 0x0C
    polling: {class: fast}
    response: {data_length: 2}
    signals:
      alpha:
        label: Alpha
        unit: rpm
        display: {digits: 0, min: 0, max: 5000}
        decode: {type: uint16_be, divide: 4.0}

derived:
  gamma:
    label: Gamma
    unit: rpm
    operation: subtract_scale
    inputs:
      value: alpha
      reference: alpha
    scale: 1.0
"""


def edit(old: str, new: str, text: str = BASE) -> str:
    """`text` with exactly one occurrence of `old` replaced."""
    assert text.count(old) == 1, old

    return text.replace(old, new)


def fails(test, text, error=MappingError, path=None, contains=None):
    """Load `text`, expect `error`, and check the reported path."""
    with test.assertRaises(error) as caught:
        load_text(text, "test")

    exc = caught.exception

    if path is not None:
        test.assertEqual(exc.path, path, str(exc))

    if contains is not None:
        test.assertIn(contains, str(exc))

    return exc


class TestTheIssueExamples(unittest.TestCase):
    """The negative cases issue #9 lists, verbatim."""

    def test_production_as_a_string_is_not_a_boolean(self):
        """`bool("false")` is True: this file used to load as production."""
        fails(
            self,
            edit("production: false", 'production: "false"'),
            InvalidFieldError, "mapping.production", "true or false",
        )

    def test_a_misspelt_production_key_is_not_a_default(self):
        exc = fails(
            self,
            edit("production: false", "prodution: false"),
            UnknownFieldError, "mapping.prodution",
        )

        #: The hint names the field the author meant.
        self.assertIn("did you mean 'production'", str(exc))

    def test_stagger_as_a_string_is_not_a_boolean(self):
        fails(
            self,
            edit("stagger: true", 'stagger: "no"'),
            InvalidFieldError, "polling_classes.paced.stagger",
        )

    def test_nan_period_is_rejected(self):
        fails(
            self,
            edit("seconds: 0.1", "seconds: .nan"),
            InvalidFieldError, "polling_classes.fast.seconds", "finite",
        )

    def test_infinite_scale_is_rejected(self):
        for where, old in (
            ("requests.test.one.signals.alpha.decode.divide", "divide: 4.0"),
            ("derived.gamma.scale", "scale: 1.0"),
        ):
            with self.subTest(where=where):
                fails(
                    self,
                    edit(old, old.split(":")[0] + ": .inf"),
                    InvalidFieldError, where, "finite",
                )

    def test_empty_payload_is_rejected(self):
        for spelling in ("payload: []", 'payload: ""'):
            with self.subTest(spelling=spelling):
                fails(
                    self,
                    edit("    pid: 0x0C", "    pid: 0x0C\n    " + spelling),
                    InvalidFieldError, "requests.test.one.payload", "empty",
                )

    def test_unknown_request_field_is_rejected(self):
        fails(
            self,
            edit("    pid: 0x0C", "    pid: 0x0C\n    unknown_request_field: whatever"),
            UnknownFieldError, "requests.test.one.unknown_request_field",
        )


class TestUnknownFieldsAtEveryLevel(unittest.TestCase):
    """
    One stray key per schema object, each reported at its own path.

    The list is the set of objects the format has; a new object added to
    the loader without a vocabulary would be missing from here.
    """

    CASES = (
        # (anchor line to extend, indent, expected error path)
        ("schema_version: 1", "", "stray"),
        ("  id: strict-fixture", "  ", "mapping.stray"),
        ("  family: test", "  ", "ecu.stray"),
        ("  match:", "    ", "ecu.match.stray"),
        ("  type: synthetic", "  ", "source.stray"),
        ("  status: discovered", "  ", "verification.stray"),
        ("    transport: diagnostic", "    ", "defaults.request.stray"),
        ("    transport: diagnostic", "  ", "defaults.stray"),
        ("    pid: 0x0C", "    ", "requests.test.one.stray"),
        ("        unit: rpm", "        ", "requests.test.one.signals.alpha.stray"),
        ("    operation: subtract_scale", "    ", "derived.gamma.stray"),
    )

    def test_block_objects(self):
        for anchor, indent, path in self.CASES:
            with self.subTest(path=path):
                fails(
                    self,
                    edit(anchor, anchor + "\n" + indent + "stray: 1"),
                    UnknownFieldError, path,
                )

    def test_flow_objects(self):
        cases = (
            ("{class: fast}", "{class: fast, stray: 1}",
             "requests.test.one.polling.stray"),
            ("{data_length: 2}", "{data_length: 2, stray: 1}",
             "requests.test.one.response.stray"),
            ("{digits: 0, min: 0, max: 5000}", "{digits: 0, stray: 1}",
             "requests.test.one.signals.alpha.display.stray"),
            ("{type: uint16_be, divide: 4.0}", "{type: uint16_be, stray: 1}",
             "requests.test.one.signals.alpha.decode.stray"),
            ("{seconds: 0.1, priority: 0}", "{seconds: 0.1, stray: 1}",
             "polling_classes.fast.stray"),
        )

        for old, new, path in cases:
            with self.subTest(path=path):
                fails(self, edit(old, new), UnknownFieldError, path)

    def test_nested_config_and_requires_objects(self):
        cases = (
            ("max: 5000", "max: {config: tank, stray: 1}",
             "requests.test.one.signals.alpha.display.max.stray"),
            ("scale: 1.0", "scale: {config: tank, stray: 1}",
             "derived.gamma.scale.stray"),
            ("    pid: 0x0C", "    pid: 0x0C\n    requires: {stray: 1}",
             "requests.test.one.requires.stray"),
            ("  target: 0x7E", "  target: {address: 0x7E, stray: 1}",
             "ecu.target.stray"),
        )

        for old, new, path in cases:
            with self.subTest(path=path):
                fails(self, edit(old, new), UnknownFieldError, path)

    def test_every_field_a_request_can_inherit_is_a_request_field(self):
        self.assertTrue(set(FIELDS_REQUEST_DEFAULTS) <= set(FIELDS_REQUEST))

    def test_the_error_names_the_file(self):
        with self.assertRaises(UnknownFieldError) as caught:
            load_text(edit("  id: strict-fixture", "  id: x\n  stray: 1"),
                      "some/file.yaml")

        self.assertEqual(caught.exception.source, "some/file.yaml")
        self.assertIn("some/file.yaml:mapping.stray", str(caught.exception))


class TestRetiredSpellings(unittest.TestCase):
    """An alias silently accepted is a typo silently accepted."""

    def test_response_length_points_at_data_length(self):
        fails(
            self,
            edit("{data_length: 2}", "{length: 2}"),
            InvalidFieldError, "requests.test.one.response.length",
            "'data_length'",
        )

    def test_display_lo_hi_point_at_min_max(self):
        for old, new, path in (
            ("min: 0", "lo: 0", "requests.test.one.signals.alpha.display.lo"),
            ("max: 5000", "hi: 5000", "requests.test.one.signals.alpha.display.hi"),
        ):
            with self.subTest(path=path):
                fails(self, edit(old, new), InvalidFieldError, path, "retired")

    def test_retired_polling_units_still_refused_by_name(self):
        for retired in ("hz", "every", "cycles"):
            with self.subTest(retired=retired):
                fails(
                    self,
                    edit("{seconds: 0.1, priority: 0}", "{" + retired + ": 10}"),
                    InvalidFieldError, "polling_classes.fast." + retired,
                    "'seconds'",
                )

    def test_every_retired_name_maps_to_a_live_one(self):
        from bmwdiag.mapping import loader

        live = {
            "response": loader.FIELDS_RESPONSE,
            "display": loader.FIELDS_DISPLAY,
            "polling_class": loader.FIELDS_POLLING_CLASS,
            #: capability kinds are open, so the live set is the kinds a
            #: provider in this repo actually answers
            "capability": (loader.PROFILE_CAPABILITY, loader.SGBD_CAPABILITY),
        }

        for obj, table in RETIRED_FIELDS.items():
            for old, new in table.items():
                self.assertIn(new, live[obj], f"{obj}.{old} -> {new}")
                self.assertNotIn(old, live[obj], f"{obj}.{old} is both")


class TestTypeStrictness(unittest.TestCase):
    def test_schema_version_true_is_not_version_one(self):
        fails(
            self,
            edit("schema_version: 1", "schema_version: true"),
            UnsupportedSchemaVersion, "schema_version",
        )

    def test_a_zero_sentinel_is_a_sentinel(self):
        """`data.get("invalid") or ()` used to turn `invalid: 0` into nothing."""
        mapping = load_text(
            edit("{type: uint16_be, divide: 4.0}",
                 "{type: uint16_be, invalid: 0, saturated: 0}"),
            "test",
        )

        self.assertEqual(mapping.requests[0].signals[0].decode.invalid, (0,))
        self.assertEqual(mapping.requests[0].signals[0].decode.saturated, (0,))

    def test_free_text_fields_must_be_strings(self):
        cases = (
            ("  notes: unit test", "  notes: [a, b]", "source.notes"),
            ("  method: none", "  method: 3", "verification.method"),
            ("  family: test", "  family: test\n  sgbd: 12", "ecu.sgbd"),
            ("        unit: rpm", "        unit: rpm\n        source_name: 1",
             "requests.test.one.signals.alpha.source_name"),
        )

        for old, new, path in cases:
            with self.subTest(path=path):
                fails(self, edit(old, new), InvalidFieldError, path, "string")

    def test_target_is_an_address_or_a_name_not_both(self):
        fails(
            self,
            edit("  target: 0x7E", "  target: {address: 0x7E, name: engine}"),
            InvalidFieldError, "ecu.target", "not both",
        )

    def test_target_address_range_is_checked_in_both_spellings(self):
        for spelling in ("target: 0x1FF", "target: {address: 0x1FF}"):
            with self.subTest(spelling=spelling):
                fails(
                    self, edit("target: 0x7E", spelling),
                    InvalidFieldError, contains="out of range",
                )

    def test_non_finite_numbers_are_refused_everywhere_a_number_goes(self):
        cases = (
            ("min: 0", "min: -.inf", "requests.test.one.signals.alpha.display.min"),
            ("max: 5000", "max: .nan", "requests.test.one.signals.alpha.display.max"),
            ("{type: uint16_be, divide: 4.0}", "{type: uint16_be, add: .nan}",
             "requests.test.one.signals.alpha.decode.add"),
            ("{type: uint16_be, divide: 4.0}",
             "{type: uint16_be, valid_max: .inf}",
             "requests.test.one.signals.alpha.decode.valid_max"),
            ("    pid: 0x0C", "    pid: 0x0C\n    timeout: .inf",
             "requests.test.one.timeout"),
            #: Reported where it was written, not where it was inherited.
            ("    transport: diagnostic", "    transport: diagnostic\n    timeout: .nan",
             "defaults.request.timeout"),
        )

        for old, new, path in cases:
            with self.subTest(path=path):
                fails(self, edit(old, new), InvalidFieldError, path, "finite")

    def test_negative_min_length_is_rejected(self):
        fails(
            self,
            edit("{data_length: 2}", "{data_length: 2, min_length: -1}"),
            InvalidLengthError, "requests.test.one.response.min_length",
        )


class TestWireLevelConsistency(unittest.TestCase):
    """service / pid / did / payload have to agree with the protocol."""

    def test_service_byte_range(self):
        fails(
            self, edit("service: 0x01", "service: 0x101"),
            InvalidFieldError, "requests.test.one.service", "out of range",
        )

    def test_an_identifier_the_protocol_does_not_send_is_refused(self):
        cases = (
            #: obd is addressed by pid; a did would ride along unsent.
            ("    pid: 0x0C", "    pid: 0x0C\n    did: 0xF300",
             "requests.test.one.did"),
            #: uds is addressed by did.
            ("protocol: obd\n    service: 0x01\n    pid: 0x0C",
             "protocol: uds\n    service: 0x22\n    did: 0xF300\n    pid: 0x0C",
             "requests.test.one.pid"),
            #: raw carries its identifier in the payload.
            ("protocol: obd\n    service: 0x01\n    pid: 0x0C",
             "protocol: raw\n    payload: 22 F3 00\n    did: 0xF300",
             "requests.test.one.did"),
        )

        for old, new, path in cases:
            with self.subTest(path=path):
                fails(self, edit(old, new), InvalidFieldError, path)

    def test_timeout_must_be_positive(self):
        fails(
            self, edit("    pid: 0x0C", "    pid: 0x0C\n    timeout: 0"),
            InvalidFieldError, "requests.test.one.timeout", "positive",
        )


class TestDefaultsAreValidatedWhereWritten(unittest.TestCase):
    """
    A value under `defaults.request` is validated when the block loads,
    not when (or whether) a request inherits it.

    Otherwise a file's validity would depend on which request happened
    to override which default: `timeout: .nan` under defaults with every
    request declaring its own timeout would load clean, and the first
    request added without one would inherit NaN. Every request here
    overrides the field the default gets wrong.
    """

    CASES = (
        ("timeout: .nan", "    timeout: 0.4", "defaults.request.timeout"),
        ("timeout: -1", "    timeout: 0.4", "defaults.request.timeout"),
        ("polling: {class: slow, stray: typo}", "", "defaults.request.polling.stray"),
        ("polling: 7", "", "defaults.request.polling"),
        ("service: 0x1FF", "", "defaults.request.service"),
        ("service: \"1\"", "", "defaults.request.service"),
        ("pid: 256", "    pid: 0x0C", "defaults.request.pid"),
        ("did: 0x10000", "", "defaults.request.did"),
        ("payload: []", "    payload: 01 0C", "defaults.request.payload"),
        ("payload: [0x1FF]", "    payload: 01 0C", "defaults.request.payload[0]"),
        ("target: 0x1FF", "", "defaults.request.target"),
        ("target: {address: 0x12, name: x}", "", "defaults.request.target"),
        ("target: {adress: 0x12}", "", "defaults.request.target.adress"),
        ("protocol: kwp", "", "defaults.request.protocol"),
    )

    def test_an_invalid_default_transport_fails(self):
        fails(
            self, edit("    transport: diagnostic", "    transport: can"),
            MappingError, "defaults.request.transport",
        )

    def test_an_invalid_default_fails_even_when_every_request_overrides_it(self):
        for default, override, path in self.CASES:
            with self.subTest(default=default):
                text = edit(
                    "    transport: diagnostic",
                    "    transport: diagnostic\n    " + default,
                )

                if override:
                    text = edit("    pid: 0x0C", override, text)

                fails(self, text, MappingError, path)

    def test_a_valid_default_is_inherited_and_overridable(self):
        text = edit(
            "    transport: diagnostic",
            "    transport: diagnostic\n    polling: {class: paced}\n    timeout: 0.4",
        )
        text = edit("    polling: {class: fast}\n", "", text)
        mapping = load_text(text, "test")

        self.assertEqual(mapping.requests[0].polling_class, "paced")
        self.assertEqual(mapping.requests[0].timeout, 0.4)

    def test_every_inheritable_field_has_a_validator(self):
        from bmwdiag.mapping.loader import REQUEST_FIELD_VALIDATORS

        self.assertEqual(
            tuple(REQUEST_FIELD_VALIDATORS), FIELDS_REQUEST_DEFAULTS
        )


class TestInheritedTimeout(unittest.TestCase):
    """
    The one behavioural change: `defaults.request.timeout` now applies.

    Two tracked candidates (EGS 0x18, KOMBI 0x63) declare `timeout: 0.4`
    under `defaults.request` - the EGS one is loaded by run_car.sh - and
    the loader read `timeout` from the request block alone, so both
    polled on the transport's 3 s default while execute.py's fault
    budget was written on the assumption of 0.4 s. Honouring the
    declared value is the fix; dropping the key would have preserved a
    silent 3 s that nobody chose.
    """

    def test_a_request_inherits_the_default_timeout(self):
        mapping = load_text(
            edit("    transport: diagnostic",
                 "    transport: diagnostic\n    timeout: 0.4"),
            "test",
        )

        self.assertEqual(mapping.requests[0].timeout, 0.4)

    def test_a_request_can_override_it(self):
        mapping = load_text(
            edit(
                "    pid: 0x0C", "    pid: 0x0C\n    timeout: 1.5",
                edit("    transport: diagnostic",
                     "    transport: diagnostic\n    timeout: 0.4"),
            ),
            "test",
        )

        self.assertEqual(mapping.requests[0].timeout, 1.5)

    def test_the_tracked_egs_candidate_declares_and_gets_its_timeout(self):
        egs = os.path.join(
            support.MAPPINGS, "candidates", "bmw", "egs", "f10_transmission.yaml"
        )
        mapping = load_file(egs)

        self.assertEqual({r.timeout for r in mapping.requests}, {0.4})


class TestExistingFilesStillLoad(unittest.TestCase):
    def test_every_tracked_mapping_loads_under_the_strict_loader(self):
        """
        No tracked file may depend on an ignored key. One did:
        d72n47a0_dynamic.yaml carried `verification.source_vehicle`,
        which is now in `notes` (v4).
        """
        loaded = load_tree(support.MAPPINGS)

        self.assertGreaterEqual(len(loaded), 10)
        self.assertIn("sae-obd-engine", {m.id for m in loaded})

    def test_the_production_mapping_is_unchanged_in_meaning(self):
        mapping = load_file(support.OBD_MAPPING)

        self.assertTrue(mapping.production)
        self.assertTrue(all(r.timeout is None for r in mapping.requests))
        self.assertTrue(all(
            math.isfinite(s.decode.scale) and math.isfinite(s.decode.divide)
            for s in mapping.signals
        ))


if __name__ == "__main__":
    unittest.main()
