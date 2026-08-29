"""
`log: false` - decode and display a channel, but do not persist it.

For a channel whose finding is that it never changes. `egs_da2e_b0` shares a
response with `gear`, so reading it costs nothing on the wire, but it wrote
124,485 rows carrying one distinct value over three days. The finding is
made; re-recording it forever adds no information. Keeping it decoded means
a future non-zero value still surfaces on the dashboard.
"""

import unittest

from tests import support  # noqa: F401

import live
from bmwdiag.mapping.errors import InvalidFieldError
from bmwdiag.mapping.loader import load_text
from bmwdiag.mapping.registry import AllCapabilities, MappingRegistry

BASE = """
schema_version: 1

mapping:
  id: log-fixture
  version: 1
  production: false

ecu:
  family: test
  target: 0x7E

requests:
  probe:
    protocol: obd
    service: 0x01
    pid: 0x0C
    response: {data_length: 4}
    signals:
      kept:
        label: Kept
        unit: rpm
        decode: {type: uint8, offset: 0}
      dropped:
        label: Dropped
        unit: ""
        decode: {type: uint8, offset: 1}
        log: false

derived:
  derived_kept:
    operation: linear
    label: Derived kept
    unit: rpm
    inputs: {value: kept}
  derived_dropped:
    operation: linear
    label: Derived dropped
    unit: rpm
    inputs: {value: kept}
    log: false
"""


def profile(text=BASE):
    return MappingRegistry([load_text(text, "test")]).resolve(
        AllCapabilities(), config={}
    )


class TheFlag(unittest.TestCase):
    def test_signals_default_to_logged(self):
        self.assertTrue(profile().is_logged("kept"))

    def test_log_false_is_honoured_on_a_signal(self):
        self.assertFalse(profile().is_logged("dropped"))

    def test_log_false_is_honoured_on_a_derived_channel(self):
        p = profile()
        self.assertTrue(p.is_logged("derived_kept"))
        self.assertFalse(p.is_logged("derived_dropped"))

    def test_an_unknown_channel_is_logged(self):
        """Not knowing a channel is no reason to silently discard it."""
        self.assertTrue(profile().is_logged("never_heard_of_it"))

    def test_a_non_boolean_is_rejected(self):
        """`log: maybe` is a mistake, not a truthy value."""
        with self.assertRaises(InvalidFieldError):
            load_text(BASE.replace("log: false", "log: maybe"), "test")


class TheRecorderFilter(unittest.TestCase):
    def test_a_log_false_channel_is_not_stored(self):
        stored = live.numeric_only({"kept": 1.0, "dropped": 0.0}, profile())

        self.assertEqual(sorted(stored), ["kept"])

    def test_it_still_reaches_the_dashboard(self):
        """
        The filter is the RECORDER's, not the telemetry state's - the point
        is to keep watching the channel without paying to store it.
        """
        values = {"kept": 1.0, "dropped": 0.0}
        self.assertIn("dropped", values)          # what the dashboard is fed
        self.assertNotIn("dropped", live.numeric_only(values, profile()))

    def test_without_a_profile_nothing_is_dropped(self):
        """A missing profile must not silently discard channels."""
        stored = live.numeric_only({"kept": 1.0, "dropped": 0.0})

        self.assertEqual(sorted(stored), ["dropped", "kept"])

    def test_non_numeric_values_are_still_filtered(self):
        """The original reason this filter exists: samples.value is REAL."""
        stored = live.numeric_only({"kept": 1.0, "text": "P"}, profile())

        self.assertEqual(sorted(stored), ["kept"])


class TheRealMapping(unittest.TestCase):
    def test_the_constant_egs_byte_is_not_logged(self):
        import os
        from bmwdiag.mapping.loader import load_file

        p = MappingRegistry([load_file(os.path.join(
            support.MAPPINGS, "candidates", "bmw", "egs", "f10_transmission.yaml"
        ))]).resolve(AllCapabilities(), config={})

        self.assertFalse(p.is_logged("egs_da2e_b0"))
        self.assertTrue(p.is_logged("gear"))


if __name__ == "__main__":
    unittest.main()
