"""
Mapping schema loading and validation.

Every failure mode listed in the mapping format's contract gets a test
here, because a mapping file that loads but is subtly wrong is the one
class of bug this whole subsystem exists to make impossible.
"""

import unittest

from . import support
from bmwdiag.mapping import load_file, load_text
from bmwdiag.mapping.errors import (
    DuplicateRequestError,
    DuplicateSignalError,
    InvalidEnumError,
    InvalidFieldError,
    InvalidLengthError,
    InvalidOffsetError,
    MappingSyntaxError,
    MissingFieldError,
    UnknownDecoderError,
    UnknownDerivedInputError,
    UnsupportedSchemaVersion,
)
from bmwdiag.mapping.registry import AllCapabilities, MappingRegistry

BASE = """
schema_version: 1

mapping:
  id: test-mapping
  description: unit test fixture

ecu:
  family: test
  target: 0x7E

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
"""


def variant(**replacements):
    text = BASE

    for old, new in replacements.items():
        text = text.replace(old.replace("__", " "), new)

    return text


class TestBasicLoading(unittest.TestCase):
    def test_loads_minimal_mapping(self):
        mapping = load_text(BASE, "test")

        self.assertEqual(mapping.schema_version, 1)
        self.assertEqual(mapping.id, "test-mapping")
        self.assertEqual(len(mapping.requests), 1)
        self.assertEqual(mapping.requests[0].signals[0].key, "alpha")

    def test_loads_production_obd_mapping(self):
        mapping = load_file(support.OBD_MAPPING)

        self.assertEqual(mapping.id, "sae-obd-engine")
        self.assertTrue(mapping.production)
        self.assertEqual(mapping.ecu.family, "engine")
        self.assertEqual(mapping.verification.status, "verified")

    def test_example_mapping_is_not_production(self):
        mapping = load_file(support.EXAMPLE_MAPPING)

        self.assertFalse(mapping.production)
        #
        # The fixture must never be able to masquerade as knowledge.
        #
        self.assertEqual(mapping.verification.status, "rejected")
        self.assertEqual(mapping.provenance.type, "synthetic")

    def test_obd_defaults_are_applied(self):
        request = load_text(BASE, "test").requests[0]

        self.assertEqual(request.service, 0x01)
        self.assertEqual(request.response.prefix, (0x41, 0x0C))
        self.assertEqual(request.response.payload_offset, 2)
        self.assertEqual(request.target.address, 0x7E)

    def test_obd_capability_is_derived_from_the_pid(self):
        request = load_text(BASE, "test").requests[0]

        self.assertEqual(len(request.requires), 1)
        self.assertEqual(request.requires[0].kind, "obd_mode01_pid")
        self.assertEqual(request.requires[0].value, 0x0C)


class TestProvenance(unittest.TestCase):
    """Provenance must survive loading and stay queryable."""

    def test_file_level_provenance_flows_to_signals(self):
        mapping = load_file(support.OBD_MAPPING)
        signal = mapping.signals[0]

        self.assertEqual(signal.provenance.type, "obd_standard")
        self.assertEqual(signal.verification.status, "verified")
        self.assertEqual(
            signal.verification.vehicle,
            "F10-520d-dev (BMW F10 520d, N47 diesel)",
        )

    def test_signal_can_override_provenance(self):
        text = BASE.replace(
            "        decode: {type: uint16_be, divide: 4.0}",
            "        decode: {type: uint16_be, divide: 4.0}\n"
            "        source: {type: tool32, sgbd: EXAMPLE, job: STATUS_X, "
            "result: VALUE_Y}\n"
            "        verification: {status: candidate, method: bench}",
        )
        signal = load_text(text, "test").signals[0]

        self.assertEqual(signal.provenance.type, "tool32")
        self.assertEqual(signal.provenance.job, "STATUS_X")
        self.assertEqual(signal.provenance.result, "VALUE_Y")
        self.assertEqual(signal.verification.status, "candidate")
        self.assertFalse(signal.verification.is_verified)

    def test_unknown_verification_status_is_rejected(self):
        text = BASE.replace(
            "  description: unit test fixture",
            "  description: unit test fixture\n",
        ) + "\nverification:\n  status: probably-fine\n"

        with self.assertRaises(InvalidEnumError):
            load_text(text, "test")

    def test_unknown_source_type_is_rejected(self):
        text = BASE + "\nsource:\n  type: vibes\n"

        with self.assertRaises(InvalidEnumError):
            load_text(text, "test")


class TestSchemaVersion(unittest.TestCase):
    def test_missing_schema_version_fails(self):
        with self.assertRaises(UnsupportedSchemaVersion):
            load_text(BASE.replace("schema_version: 1\n", ""), "test")

    def test_future_schema_version_fails_cleanly(self):
        with self.assertRaises(UnsupportedSchemaVersion) as ctx:
            load_text(BASE.replace("schema_version: 1", "schema_version: 99"), "test")

        self.assertIn("99", str(ctx.exception))
        self.assertIn("not supported", str(ctx.exception))

    def test_non_integer_schema_version_fails_cleanly(self):
        with self.assertRaises(UnsupportedSchemaVersion):
            load_text(BASE.replace("schema_version: 1", "schema_version: '1'"), "test")


class TestValidation(unittest.TestCase):
    def test_duplicate_signal_key_in_one_file(self):
        text = BASE + """
  test.two:
    protocol: obd
    pid: 0x0D
    polling: {class: fast}
    response: {data_length: 1}
    signals:
      alpha:
        label: Alpha again
        decode: {type: uint8}
"""

        with self.assertRaises(DuplicateSignalError):
            load_text(text, "test")

    def test_duplicate_signal_key_across_files(self):
        first = load_text(BASE, "a.yaml")
        second = load_text(BASE.replace("id: test-mapping", "id: other")
                               .replace("test.one", "test.other"), "b.yaml")

        with self.assertRaises(DuplicateSignalError):
            MappingRegistry([first, second])

    def test_duplicate_request_id_across_files(self):
        first = load_text(BASE, "a.yaml")
        second = load_text(
            BASE.replace("id: test-mapping", "id: other")
                .replace("      alpha:", "      beta:"),
            "b.yaml",
        )

        with self.assertRaises(DuplicateRequestError):
            MappingRegistry([first, second])

    def test_duplicate_request_id_in_one_file_is_a_yaml_error(self):
        #
        # Two identical keys in one YAML mapping never reach the loader's
        # own duplicate check, so the parser has to catch it.
        #
        text = BASE + """
  test.one:
    protocol: obd
    pid: 0x0D
    polling: {class: fast}
    response: {data_length: 1}
    signals:
      beta:
        decode: {type: uint8}
"""

        with self.assertRaises(MappingSyntaxError):
            load_text(text, "test")

    def test_unknown_decoder_type(self):
        with self.assertRaises(UnknownDecoderError) as ctx:
            load_text(BASE.replace("type: uint16_be", "type: uint13_be"), "test")

        self.assertIn("uint13_be", str(ctx.exception))

    def test_missing_decode_block(self):
        with self.assertRaises(MissingFieldError):
            load_text(
                BASE.replace("        decode: {type: uint16_be, divide: 4.0}", ""),
                "test",
            )

    def test_missing_signals_block(self):
        text = BASE[: BASE.index("    signals:")]

        with self.assertRaises(MissingFieldError):
            load_text(text, "test")

    def test_request_without_pid_or_payload(self):
        with self.assertRaises(MissingFieldError):
            load_text(BASE.replace("    pid: 0x0C\n", ""), "test")

    def test_offset_outside_declared_response(self):
        with self.assertRaises(InvalidOffsetError) as ctx:
            load_text(
                BASE.replace(
                    "decode: {type: uint16_be, divide: 4.0}",
                    "decode: {type: uint16_be, offset: 1, divide: 4.0}",
                ),
                "test",
            )

        self.assertIn("alpha", str(ctx.exception))

    def test_negative_offset(self):
        with self.assertRaises(InvalidOffsetError):
            load_text(
                BASE.replace(
                    "decode: {type: uint16_be, divide: 4.0}",
                    "decode: {type: uint16_be, offset: -1}",
                ),
                "test",
            )

    def test_variable_width_decoder_without_length(self):
        with self.assertRaises(InvalidLengthError):
            load_text(
                BASE.replace(
                    "decode: {type: uint16_be, divide: 4.0}",
                    "decode: {type: ascii}",
                ),
                "test",
            )

    def test_impossible_length(self):
        with self.assertRaises(InvalidLengthError):
            load_text(
                BASE.replace(
                    "decode: {type: uint16_be, divide: 4.0}",
                    "decode: {type: ascii, length: 0}",
                ),
                "test",
            )

    def test_bit_index_outside_window(self):
        with self.assertRaises(InvalidLengthError):
            load_text(
                BASE.replace(
                    "decode: {type: uint16_be, divide: 4.0}",
                    "decode: {type: bit, length: 1, bit: 9}",
                ),
                "test",
            )

    def test_mask_wider_than_window(self):
        with self.assertRaises(InvalidLengthError):
            load_text(
                BASE.replace(
                    "decode: {type: uint16_be, divide: 4.0}",
                    "decode: {type: bitfield, length: 1, mask: 0xFF00}",
                ),
                "test",
            )

    def test_divide_by_zero_rejected(self):
        with self.assertRaises(InvalidFieldError):
            load_text(BASE.replace("divide: 4.0", "divide: 0.0"), "test")

    def test_invalid_enum_keys(self):
        with self.assertRaises(InvalidEnumError):
            load_text(
                BASE.replace(
                    "decode: {type: uint16_be, divide: 4.0}",
                    "decode:\n"
                    "          type: uint8\n"
                    "          enum:\n"
                    "            idle: 0\n",
                ),
                "test",
            )

    def test_invalid_enum_values(self):
        with self.assertRaises(InvalidEnumError):
            load_text(
                BASE.replace(
                    "decode: {type: uint16_be, divide: 4.0}",
                    "decode:\n"
                    "          type: uint8\n"
                    "          enum:\n"
                    "            0: 17\n",
                ),
                "test",
            )

    def test_enum_and_lookup_together_rejected(self):
        with self.assertRaises(InvalidFieldError):
            load_text(
                BASE.replace(
                    "decode: {type: uint16_be, divide: 4.0}",
                    "decode:\n"
                    "          type: uint8\n"
                    "          enum:\n"
                    "            0: idle\n"
                    "          lookup:\n"
                    "            - [0, 0.0]\n"
                    "            - [1, 1.0]\n",
                ),
                "test",
            )

    def test_non_monotonic_lookup_rejected(self):
        with self.assertRaises(InvalidEnumError):
            load_text(
                BASE.replace(
                    "decode: {type: uint16_be, divide: 4.0}",
                    "decode:\n"
                    "          type: uint8\n"
                    "          lookup:\n"
                    "            - [10, 0.0]\n"
                    "            - [5, 1.0]\n",
                ),
                "test",
            )

    def test_unknown_polling_class_is_caught_when_planning(self):
        from bmwdiag.mapping.errors import PollingError
        from bmwdiag.mapping.polling import PollingPlan

        mapping = load_text(BASE.replace("class: fast", "class: warp"), "test")

        with self.assertRaises(PollingError):
            PollingPlan(mapping.requests)


class TestDerivedValidation(unittest.TestCase):
    DERIVED = BASE + """
derived:
  gamma:
    label: Gamma
    unit: bar
    operation: subtract_scale
    inputs:
      value: alpha
      reference: %s
    fallback:
      reference: 100.0
    divide: 100.0
"""

    def test_derived_loads(self):
        mapping = load_text(self.DERIVED % "alpha", "test")

        self.assertEqual(len(mapping.derived), 1)
        self.assertEqual(mapping.derived[0].trigger, ("alpha",))

    def test_unknown_derived_input(self):
        with self.assertRaises(UnknownDerivedInputError) as ctx:
            load_text(self.DERIVED % "nonexistent", "test")

        self.assertIn("nonexistent", str(ctx.exception))

    def test_unknown_derived_trigger(self):
        text = self.DERIVED % "alpha"
        text += "    trigger: [not_a_signal]\n"

        with self.assertRaises(UnknownDerivedInputError):
            load_text(text, "test")

    def test_fallback_for_unknown_role(self):
        text = (self.DERIVED % "alpha").replace(
            "      reference: 100.0", "      typo: 100.0"
        )

        with self.assertRaises(UnknownDerivedInputError):
            load_text(text, "test")

    def test_unknown_operation(self):
        with self.assertRaises(InvalidEnumError):
            load_text(
                (self.DERIVED % "alpha").replace(
                    "operation: subtract_scale", "operation: fourier_transform"
                ),
                "test",
            )

    def test_derived_key_colliding_with_a_signal(self):
        with self.assertRaises(DuplicateSignalError):
            load_text(
                (self.DERIVED % "alpha").replace("  gamma:", "  alpha:"), "test"
            )


class TestSyntaxErrors(unittest.TestCase):
    def test_tab_indentation_rejected(self):
        with self.assertRaises(MappingSyntaxError):
            load_text("schema_version: 1\nmapping:\n\tid: x\n", "test")

    def test_anchors_rejected(self):
        with self.assertRaises(MappingSyntaxError):
            load_text("schema_version: 1\nmapping: &anchor\n  id: x\n", "test")

    def test_empty_file_rejected(self):
        with self.assertRaises(MissingFieldError):
            load_text("", "test")

    def test_top_level_list_rejected(self):
        with self.assertRaises(InvalidFieldError):
            load_text("- one\n- two\n", "test")


class TestTreeLoading(unittest.TestCase):
    def test_production_only_excludes_the_fixture(self):
        from bmwdiag.mapping import load_tree

        everything = load_tree(support.MAPPINGS)
        production = load_tree(support.MAPPINGS, production_only=True)

        self.assertGreater(len(everything), len(production))
        self.assertNotIn(
            "example-synthetic-uds", [m.id for m in production]
        )
        self.assertIn("example-synthetic-uds", [m.id for m in everything])

    def test_whole_tree_validates(self):
        from bmwdiag.mapping import load_tree

        registry = MappingRegistry(load_tree(support.MAPPINGS))
        profile = registry.resolve(AllCapabilities())

        self.assertGreater(len(profile.requests), 24)


if __name__ == "__main__":
    unittest.main()
