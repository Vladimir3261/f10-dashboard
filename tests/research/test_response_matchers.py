"""
Response-matcher strategies, expressed as declared prefixes.

Every strategy from the research vocabulary maps onto the runtime's
per-request `response.prefix` / `min_length` mechanism. Looser matching
is a per-mapping declaration; nothing global got more permissive, and a
negative response never decodes as data.
"""

import unittest

from tests import support  # noqa: F401
from tests.support import hexb

from bmwdiag.mapping import decode_response, load_text
from bmwdiag.mapping.errors import ResponseMismatchError


def one_request(response_block: str, decode: str = "{type: uint16_be}") -> str:
    return f"""
schema_version: 1
mapping: {{id: matcher-fixture, version: 1, production: false}}
ecu: {{target: 0x7E}}
requests:
  r1:
    protocol: raw
    payload: "2C 10 04 06"
    target: 0x7E
    response: {response_block}
    signals:
      value:
        label: TEST ONLY - NOT A REAL BMW MAPPING
        unit: x
        decode: {decode}
"""


def load_request(response_block: str, decode: str = "{type: uint16_be}"):
    mapping = load_text(one_request(response_block, decode), source="<matcher>")
    return mapping.requests[0]


class MatcherStrategies(unittest.TestCase):
    def test_echo_full(self):
        """UDS-style: positive service + full identifier echo."""
        request = load_request('{prefix: "6C 10 04 06", data_length: 2}')
        values = decode_response(request, hexb("6C 10 04 06 0E D7"))
        self.assertEqual(values, {"value": 3799.0})

    def test_service_and_identifier_reject_on_wrong_identifier(self):
        request = load_request('{prefix: "6C 10 04 06", data_length: 2}')

        with self.assertRaises(ResponseMismatchError):
            decode_response(request, hexb("6C 10 99 99 0E D7"))

    def test_service_sub_only(self):
        """The DDE7 shape: `6C 10` then data, no identifier echo."""
        request = load_request('{prefix: "6C 10", data_length: 2}')
        values = decode_response(request, hexb("6C 10 0E D7"))
        self.assertEqual(values, {"value": 3799.0})

    def test_service_only(self):
        request = load_request('{prefix: "6C", data_length: 2}')
        values = decode_response(request, hexb("6C 0E D7"))
        self.assertEqual(values, {"value": 3799.0})

    def test_fixed_prefix(self):
        """An arbitrary declared prefix unrelated to the request echo."""
        request = load_request('{prefix: "71 01 AB CD", data_length: 2}')
        values = decode_response(request, hexb("71 01 AB CD 0E D7"))
        self.assertEqual(values, {"value": 3799.0})

    def test_length_only_with_source_guard(self):
        """
        No prefix at all - the loosest declaration. The min_length guard
        still rejects short garbage; source-address guarding lives in
        the HSFZ client, which only returns frames from the queried ECU.
        """
        request = load_request('{min_length: 4, payload_offset: 2}')
        values = decode_response(request, hexb("6C 10 0E D7"))
        self.assertEqual(values, {"value": 3799.0})

        with self.assertRaises(ResponseMismatchError):
            decode_response(request, hexb("6C 10"))

    def test_looser_matching_is_per_request_not_global(self):
        """A strict request stays strict next to a loose one."""
        strict = load_request('{prefix: "6C 10 04 06", data_length: 2}')

        with self.assertRaises(ResponseMismatchError):
            decode_response(strict, hexb("6C 10 0E D7"))

    def test_negative_response_never_decodes_as_data(self):
        """
        `7F 22 31` fails prefix matching (distinguishable from a short
        malformed frame, which fails on length instead). The transport
        layer additionally raises on NRCs before decode is reached.
        """
        request = load_request('{prefix: "62 F3 03", data_length: 2}')

        with self.assertRaises(ResponseMismatchError) as ctx:
            decode_response(request, hexb("7F 22 31"))

        self.assertIn("expected prefix", str(ctx.exception))

        with self.assertRaises(ResponseMismatchError) as ctx:
            decode_response(request, hexb("62"))

        self.assertIn("shorter", str(ctx.exception))


class GroupedResponses(unittest.TestCase):
    def test_multiple_signals_from_one_request(self):
        """One reply carries two values; one exchange, two channels."""
        text = """
schema_version: 1
mapping: {id: grouped-fixture, version: 1, production: false}
ecu: {target: 0x7E}
requests:
  block:
    protocol: uds
    service: 0x22
    did: 0xF303
    target: 0x7E
    response: {data_length: 4}
    signals:
      first:
        label: TEST ONLY - NOT A REAL BMW MAPPING
        unit: x
        decode: {type: uint16_be, offset: 0}
      second:
        label: TEST ONLY - NOT A REAL BMW MAPPING
        unit: x
        decode: {type: uint16_be, offset: 2}
"""
        mapping = load_text(text, source="<grouped>")
        request = mapping.requests[0]
        values = decode_response(request, hexb("62 F3 03 0E 2F 39 08"))
        self.assertEqual(values, {"first": 3631.0, "second": 14600.0})


if __name__ == "__main__":
    unittest.main()
