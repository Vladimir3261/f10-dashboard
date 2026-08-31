"""
Primitive decoders, transformations and response matching.

The formulas the vehicle mapping relies on are pinned here against the
exact arithmetic the application used before mappings existed. If a
refactor ever changes the last float digit of a channel, these fail.
"""

import unittest

from . import support
from bmwdiag.mapping import (
    Reading,
    decode_response,
    decode_signal,
    decode_value,
    load_file,
    read_response,
    read_value,
)
from bmwdiag.mapping.decoder import QUALITIES, match_prefix
from bmwdiag.mapping.errors import ResponseMismatchError, UnknownDecoderError
from bmwdiag.mapping.model import Decode
from bmwdiag.mapping.registry import MappingRegistry
from tests.support import hexb


class DecoderCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mapping = load_file(support.OBD_MAPPING)
        cls.registry = MappingRegistry([cls.mapping])
        cls.example = load_file(support.EXAMPLE_MAPPING)
        cls.example_registry = MappingRegistry([cls.example])

    def decode(self, key, response, registry=None):
        registry = registry or self.registry
        signal = registry.find_signal(key)
        self.assertIsNotNone(signal, f"no signal {key!r}")
        request = registry.find_request(signal.request_id)

        return decode_signal(signal, request, response)


class TestExistingObdFormulas(DecoderCase):
    def test_rpm(self):
        #
        # 41 0C 0C 3C -> (0x0C * 256 + 0x3C) / 4 = 783 rpm
        #
        value = self.decode("rpm", hexb("41 0C 0C 3C"))

        self.assertEqual(value, 783.0)
        self.assertEqual(value, (0x0C * 256 + 0x3C) / 4.0)

    def test_rpm_across_the_whole_range(self):
        request = self.registry.find_request("obd.mode01.0C")
        signal = self.registry.find_signal("rpm")

        for raw in (0, 1, 4, 1000, 16383, 32000, 65535):
            response = bytes([0x41, 0x0C, raw >> 8, raw & 0xFF])

            self.assertEqual(
                decode_signal(signal, request, response),
                round((((raw >> 8) * 256) + (raw & 0xFF)) / 4.0, 3),
            )

    def test_coolant(self):
        #
        # 41 05 5B -> 0x5B - 40 = 51 degC
        #
        self.assertEqual(self.decode("coolant", hexb("41 05 5B")), 51.0)
        self.assertEqual(self.decode("coolant", hexb("41 05 00")), -40.0)
        self.assertEqual(self.decode("coolant", hexb("41 05 FF")), 215.0)

    def test_rail_pressure(self):
        #
        # The historical formula is (A*256+B) * 10 / 100, in that order.
        #
        raw = 0x1F40
        response = bytes([0x41, 0x23, raw >> 8, raw & 0xFF])

        self.assertEqual(
            self.decode("rail", response), round(raw * 10.0 / 100.0, 3)
        )
        self.assertEqual(self.decode("rail", response), 800.0)

    def test_signed_style_offset_torque(self):
        #
        # Actual torque is an unsigned byte biased by -125, which is how
        # the standard expresses a signed percentage.
        #
        self.assertEqual(self.decode("torque", hexb("41 62 7D")), 0.0)
        self.assertEqual(self.decode("torque", hexb("41 62 00")), -125.0)
        self.assertEqual(self.decode("torque", hexb("41 62 FF")), 130.0)

    def test_pre_add_then_scale_egr_error(self):
        for raw in (0, 64, 128, 200, 255):
            self.assertEqual(
                self.decode("egrerr", bytes([0x41, 0x2D, raw])),
                round((raw - 128.0) * 100.0 / 128.0, 3),
            )

    def test_lambda_reads_only_the_first_two_of_four_bytes(self):
        response = hexb("41 24 80 00 12 34")

        self.assertEqual(self.decode("lambda", response), 1.0)

    def test_cat_temp_divide_then_offset(self):
        raw = 0x1234
        response = bytes([0x41, 0x3C, raw >> 8, raw & 0xFF])

        self.assertEqual(
            self.decode("cattemp", response), round(raw / 10.0 - 40.0, 3)
        )


class TestPrimitives(unittest.TestCase):
    def test_unsigned_and_signed_widths(self):
        cases = [
            ("uint8", b"\xFF", 255.0),
            ("int8", b"\xFF", -1.0),
            ("uint16_be", b"\x12\x34", 4660.0),
            ("uint16_le", b"\x34\x12", 4660.0),
            ("int16_be", b"\xFF\xFE", -2.0),
            ("int16_le", b"\xFE\xFF", -2.0),
            ("uint24_be", b"\x01\x00\x00", 65536.0),
            ("uint32_be", b"\x00\x00\x01\x00", 256.0),
            ("uint32_le", b"\x00\x01\x00\x00", 256.0),
            ("int32_be", b"\xFF\xFF\xFF\xFF", -1.0),
            ("int32_le", b"\xFF\xFF\xFF\xFF", -1.0),
        ]

        for kind, data, expected in cases:
            with self.subTest(kind=kind):
                self.assertEqual(decode_value(Decode(kind), data), expected)

    def test_float32(self):
        import struct

        data = struct.pack(">f", 1.5)

        self.assertEqual(decode_value(Decode("float32_be"), data), 1.5)
        self.assertEqual(
            decode_value(Decode("float32_le"), data[::-1]), 1.5
        )

    def test_raw_bytes(self):
        value = decode_value(Decode("bytes", length=3), b"\x01\x02\x03\x04")

        self.assertEqual(value, b"\x01\x02\x03")

    def test_ascii(self):
        value = decode_value(Decode("ascii", length=6), b"F10   ")

        self.assertEqual(value, "F10")

    def test_ascii_stops_at_a_nul(self):
        value = decode_value(Decode("ascii", length=6), b"AB\x00XY!")

        self.assertEqual(value, "AB")

    def test_individual_bit(self):
        data = bytes([0b0000_1000])

        self.assertEqual(decode_value(Decode("bit", bit=3), data), 1.0)
        self.assertEqual(decode_value(Decode("bit", bit=2), data), 0.0)

    def test_bit_across_a_two_byte_window(self):
        data = bytes([0b0000_0001, 0x00])

        self.assertEqual(decode_value(Decode("bit", length=2, bit=8), data), 1.0)

    def test_bitfield_auto_shifts_by_the_mask(self):
        data = bytes([0b0101_0000])

        self.assertEqual(
            decode_value(Decode("bitfield", mask=0xF0), data), 5.0
        )

    def test_bitfield_explicit_shift(self):
        data = bytes([0b0101_0000])

        self.assertEqual(
            decode_value(Decode("bitfield", mask=0xF0, shift=0), data), 80.0
        )

    def test_bitfield_without_a_mask_is_the_whole_window(self):
        self.assertEqual(
            decode_value(Decode("bitfield", length=2), b"\x01\x00"), 256.0
        )

    def test_unknown_type_rejected(self):
        with self.assertRaises(UnknownDecoderError):
            decode_value(Decode("uint12_be"), b"\x00\x00")

    def test_reading_past_the_payload_is_rejected(self):
        with self.assertRaises(ResponseMismatchError):
            decode_value(Decode("uint16_be", offset=1), b"\x00\x01")


class TestTransformations(unittest.TestCase):
    def test_transformation_order_is_pre_add_scale_divide_add(self):
        decode = Decode("uint8", pre_add=-128.0, scale=100.0,
                        divide=128.0, add=1.0, round=None)

        self.assertEqual(
            decode_value(decode, bytes([200])),
            (200 - 128.0) * 100.0 / 128.0 + 1.0,
        )

    def test_enum_mapping(self):
        decode = Decode("uint8", enum=((0, "idle"), (1, "running")),
                        enum_default="unknown")

        self.assertEqual(decode_value(decode, b"\x00"), "idle")
        self.assertEqual(decode_value(decode, b"\x01"), "running")
        self.assertEqual(decode_value(decode, b"\x09"), "unknown")

    def test_lookup_table_interpolates_and_clamps(self):
        decode = Decode("uint8", lookup=((0, 0.0), (128, 100.0), (255, 400.0)))

        self.assertEqual(decode_value(decode, b"\x00"), 0.0)
        self.assertEqual(decode_value(decode, b"\x80"), 100.0)
        self.assertEqual(decode_value(decode, b"\xFF"), 400.0)
        self.assertEqual(decode_value(decode, b"\x40"), 50.0)

    def test_sentinel_values_produce_nothing(self):
        decode = Decode("uint8", invalid=(0xFF,))

        self.assertIsNone(decode_value(decode, b"\xFF"))
        self.assertEqual(decode_value(decode, b"\x10"), 16.0)

    def test_sanity_range_suppresses_impossible_readings(self):
        decode = Decode("uint8", add=-40.0, valid_min=-40.0, valid_max=120.0)

        self.assertEqual(decode_value(decode, b"\x28"), 0.0)
        self.assertIsNone(decode_value(decode, b"\xFF"))

    def test_rounding_is_declarative(self):
        self.assertEqual(
            decode_value(Decode("uint8", divide=3.0), b"\x01"), 0.333
        )
        self.assertEqual(
            decode_value(Decode("uint8", divide=3.0, round=6), b"\x01"), 0.333333
        )


class TestResponseMatching(DecoderCase):
    def test_expected_prefix_accepted(self):
        payload = match_prefix(
            self.registry.find_request("obd.mode01.0C"), hexb("41 0C 0C 3C")
        )

        self.assertEqual(payload, hexb("0C 3C"))

    def test_wrong_prefix_rejected(self):
        with self.assertRaises(ResponseMismatchError) as ctx:
            self.decode("rpm", hexb("41 0D 0C 3C"))

        self.assertIn("expected prefix", str(ctx.exception))

    def test_negative_response_rejected(self):
        #
        # 7F 01 12 is a negative response, not data. It must never decode.
        #
        with self.assertRaises(ResponseMismatchError):
            self.decode("rpm", hexb("7F 01 12"))

    def test_truncated_response_rejected(self):
        with self.assertRaises(ResponseMismatchError):
            self.decode("rpm", hexb("41 0C 0C"))

    def test_empty_response_rejected(self):
        with self.assertRaises(ResponseMismatchError):
            self.decode("rpm", b"")

    def test_trailing_bytes_are_ignored(self):
        self.assertEqual(self.decode("rpm", hexb("41 0C 0C 3C FF FF")), 783.0)

    def test_raw_job_prefix(self):
        request = self.example_registry.find_request("example.raw.job")

        self.assertEqual(request.response.prefix, (0x71, 0x01, 0xAB, 0xCD))
        self.assertEqual(
            match_prefix(request, hexb("71 01 AB CD 01 08 40 7F")),
            hexb("01 08 40 7F"),
        )

        with self.assertRaises(ResponseMismatchError):
            match_prefix(request, hexb("71 01 AB CE 01 08 40 7F"))


class TestGroupedResponse(DecoderCase):
    """One response, several normalised signals - the whole point."""

    def test_three_signals_from_one_uds_response(self):
        request = self.example_registry.find_request("example.uds.block")
        #
        # 62 F0 01 | speed 04B0 | temp FF38 | gear 07 | pad 00
        #
        values = decode_response(request, hexb("62 F0 01 04 B0 FF 38 07 00"))

        self.assertEqual(
            values, {"example_speed": 120.0, "example_temp": -100.0,
                     "example_gear": 7.0},
        )

    def test_four_signals_from_one_raw_job_response(self):
        request = self.example_registry.find_request("example.raw.job")
        values = decode_response(request, hexb("71 01 AB CD 01 08 40 7F"))

        self.assertEqual(values["example_state"], "running")
        self.assertEqual(values["example_flag"], 1.0)
        self.assertEqual(values["example_curve"], 50.0)
        self.assertEqual(values["example_invalid"], 50.0)

    def test_sentinel_signal_is_omitted_not_zeroed(self):
        request = self.example_registry.find_request("example.raw.job")
        values = decode_response(request, hexb("71 01 AB CD 02 00 00 FF"))

        self.assertEqual(values["example_state"], "fault")
        self.assertNotIn("example_invalid", values)

    def test_ascii_signal(self):
        request = self.example_registry.find_request("example.uds.text")
        values = decode_response(
            request, hexb("62 F0 02") + b"TESTONLY"
        )

        self.assertEqual(values["example_label"], "TESTONLY")


if __name__ == "__main__":
    unittest.main()


class TestReadingQuality(DecoderCase):
    """
    The data-quality layer: a flagged reading keeps its value and says why.

    The point of these is the distinction storage could not previously
    make - "the ECU answered, and said no-value" versus "nobody asked".
    Both used to be an absent row.
    """

    def test_labels_match_the_lake_enum(self):
        #
        # telemetry.samples.quality is Enum8 over exactly these names, and
        # ClickHouse fails a whole insert batch on an unknown enum value
        # (unlike an unknown column, which it drops silently). If this
        # tuple grows without an ALTER MODIFY COLUMN migration, sync dies
        # on the first flagged sample.
        #
        self.assertEqual(
            QUALITIES,
            ("ok", "saturated", "sentinel", "stale", "clipped", "decode_fail"),
        )

    def test_plain_reading_is_ok_and_usable(self):
        reading = read_value(Decode(type="uint8"), bytes([42]))

        self.assertEqual(reading, Reading(42.0, "ok"))
        self.assertTrue(reading.usable)

    def test_sentinel_keeps_its_decoded_value(self):
        #
        # The case that motivated the layer: lambda's raw 0xFFFF decodes to
        # exactly 2.0 and was stored as if the mixture were 2.0. It is now
        # labelled - and the 2.0 is kept, because that is what the bytes
        # said. Inventing a placeholder would be a different lie.
        #
        decode = Decode(type="uint16_be", divide=32768.0, invalid=(0xFFFF,))
        reading = read_value(decode, hexb("FF FF"))

        self.assertEqual(reading.value, 2.0)
        self.assertEqual(reading.quality, "sentinel")
        self.assertFalse(reading.usable)

    def test_saturated_is_labelled_at_the_declared_rail(self):
        #
        # OBD MAP is a single byte, so 255 kPa means "255 or more" - 6,756
        # samples sat on that rail in the lake against ~180 on each
        # neighbouring value.
        #
        decode = Decode(type="uint8", saturated=(255,))

        self.assertEqual(read_value(decode, bytes([255])).quality, "saturated")
        self.assertEqual(read_value(decode, bytes([254])).quality, "ok")

    def test_out_of_range_is_clipped(self):
        decode = Decode(type="uint8", valid_max=100.0)
        reading = read_value(decode, bytes([200]))

        self.assertEqual(reading.value, 200.0)
        self.assertEqual(reading.quality, "clipped")

    def test_sentinel_wins_over_a_range_violation(self):
        #
        # A sentinel usually decodes outside the sane range as well. The
        # more specific fact - the ECU declared it unavailable - is the one
        # worth keeping.
        #
        decode = Decode(type="uint8", invalid=(0xFF,), valid_max=100.0)

        self.assertEqual(read_value(decode, bytes([255])).quality, "sentinel")

    def test_raw_domain_not_value_domain(self):
        #
        # `invalid` lists bit patterns, so the test survives a scale
        # correction. Here the sentinel decodes to 51.0, and 51.0 arrived
        # at any other way stays usable.
        #
        decode = Decode(type="uint8", divide=5.0, invalid=(0xFF,))

        self.assertEqual(read_value(decode, bytes([255])).quality, "sentinel")
        self.assertEqual(read_value(decode, bytes([250])).quality, "ok")

    def test_read_response_keeps_what_decode_response_drops(self):
        request = self.example_registry.find_request("example.raw.job")
        response = hexb("71 01 AB CD 02 00 00 FF")

        readings = read_response(request, response)
        values = decode_response(request, response)

        self.assertNotIn("example_invalid", values)
        self.assertIn("example_invalid", readings)
        self.assertEqual(readings["example_invalid"].quality, "sentinel")
        self.assertFalse(readings["example_invalid"].usable)

    def test_narrow_view_drops_a_saturated_reading(self):
        #
        # Defined rather than incidental. Between the decoder gaining
        # `saturated:` and storage gaining a quality column, no mapping
        # declares it - but the wrapper's behaviour for that case must not
        # be "unreachable, therefore unspecified". It maps every non-ok
        # reading to None, saturated included, which is what keeps the
        # wrapper byte-identical for callers that cannot carry a label.
        #
        # This is also the reason the mapping declarations land last: the
        # moment `map` declares saturated: [255] while the executor still
        # calls this wrapper, MAP=255 goes from stored to dropped.
        #
        decode = Decode(type="uint8", saturated=(255,))

        self.assertIsNone(decode_value(decode, bytes([255])))
        self.assertEqual(decode_value(decode, bytes([254])), 254.0)

    def test_narrow_view_drops_a_clipped_reading(self):
        decode = Decode(type="uint8", valid_max=100.0)

        self.assertIsNone(decode_value(decode, bytes([200])))

    def test_narrow_view_is_unchanged(self):
        #
        # decode_value/decode_response are still exactly what they were.
        # Every existing caller sees the old behaviour.
        #
        decode = Decode(type="uint8", invalid=(0xFF,))

        self.assertIsNone(decode_value(decode, bytes([255])))
        self.assertEqual(decode_value(decode, bytes([10])), 10.0)
