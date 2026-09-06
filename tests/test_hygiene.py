"""
tools/check_hygiene.py: the public-repo guard finds what it must and
never prints what it found.

The VIN-shaped and token-shaped strings below are assembled at runtime
so this file itself carries none of them.
"""

import contextlib
import io
import os
import tempfile
import unittest

from tests import support  # noqa: F401

import importlib.util

SPEC = importlib.util.spec_from_file_location(
    "check_hygiene", os.path.join(support.ROOT, "tools", "check_hygiene.py")
)
hygiene = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hygiene)

FAKE_VIN = "WBA" + "K" * 14                  # VIN alphabet, 17 characters
#: Token-shaped, built so the guard's own `TOKEN=` rule does not read it
#: as a leaked value in this file.
LONG_SECRET = "a1b2c3d4" * 4


class Content(unittest.TestCase):
    def hits(self, path, text):
        return hygiene.content_problems(path, text.encode("utf-8"))

    def test_vin_is_found_and_only_the_line_number_is_reported(self):
        hits = self.hits("docs/x.md", f"the car {FAKE_VIN} answered\n")
        self.assertEqual(hits, [(1, "17-character VIN")])

    def test_marked_fake_vin_is_allowed(self):
        line = f'VIN = "{FAKE_VIN}"  # {hygiene.FAKE_VIN_MARK}\n'
        self.assertEqual(self.hits("tests/t.py", line), [])

    def test_lowercase_or_short_strings_are_not_vins(self):
        self.assertEqual(self.hits("a.md", "wba" + "k" * 14 + "\n"), [])
        self.assertEqual(self.hits("a.md", "WBA" + "K" * 13 + "\n"), [])
        self.assertEqual(self.hits("a.md", "WBA" + "K" * 15 + "\n"), [])

    def test_env_token_with_a_real_value_is_found(self):
        hits = self.hits("infra/.env", f"INGEST_TOKEN={LONG_SECRET}\n")
        self.assertEqual(hits, [(1, "TOKEN= with a value")])

    def test_env_token_placeholders_and_code_are_not_flagged(self):
        for line in (
            "INGEST_TOKEN=change-me-long-random-token",
            "INGEST_TOKEN=${INGEST_TOKEN}",
            "INGEST_TOKEN={{ dotenv.get('INGEST_TOKEN', '') }}",
            'TOKEN = os.environ.get("INGEST_TOKEN", "")',
            "INGEST_TOKEN=",
            "SYNC_TOKEN=short",
        ):
            self.assertEqual(self.hits("x", line + "\n"), [], line)

    def test_example_files_are_exempt_from_the_value_rules(self):
        line = f"INGEST_TOKEN={LONG_SECRET}\n"
        self.assertEqual(self.hits("infra/.env.example", line), [])
        self.assertEqual(
            self.hits("infra/sync/config.example.json", f'"token": "{LONG_SECRET}"\n'),
            [],
        )

    def test_json_token_with_a_real_value_is_found(self):
        hits = self.hits("infra/sync/config.json", f'  "token": "{LONG_SECRET}",\n')
        self.assertEqual(hits, [(1, "JSON token with a value")])
        self.assertEqual(self.hits("t.py", '"token": "secret"\n'), [])

    def test_digitalocean_and_bearer_tokens_are_found_everywhere(self):
        do = "dop_v1_" + "0" * 64
        self.assertEqual(self.hits("x.example", do + "\n"),
                         [(1, "DigitalOcean token")])
        self.assertEqual(
            self.hits("x.py", f'"Authorization": "Bearer {LONG_SECRET}"\n'),
            [(1, "literal Bearer token")],
        )
        # An f-string that reads the token from config is not a literal.
        self.assertEqual(
            self.hits("x.py", '"Authorization": f"Bearer {self.cfg[\'token\']}"\n'),
            [],
        )

    def test_binary_files_are_skipped(self):
        data = b"\0\1\2" + FAKE_VIN.encode("ascii")
        self.assertEqual(hygiene.content_problems("x.bin", data), [])

    def test_oversized_text_files_are_skipped_visibly(self):
        data = b"x" * (hygiene.TEXT_MAX_BYTES + 1) + b"\n" + FAKE_VIN.encode("ascii")
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            self.assertEqual(hygiene.content_problems("big.html", data), [])

        self.assertIn("big.html: skipped (size", out.getvalue())
        self.assertNotIn(FAKE_VIN, out.getvalue())

    # -- the prefix-independent nets -------------------------------------

    def test_shape_net_catches_a_non_bmw_prefix(self):
        # 11 VIN-alphabet characters with letters, then a 6-digit serial.
        vin = "ZFA" + "C" * 8 + "123456"
        self.assertEqual(self.hits("a.md", vin + "\n"),
                         [(1, "17-character VIN (shape)")])
        self.assertEqual(self.hits("a.md", f"x {vin} # {hygiene.FAKE_VIN_MARK}\n"), [])

    def test_shape_net_ignores_plain_numbers_and_hex(self):
        self.assertEqual(self.hits("a.md", "20260825191658000\n"), [])   # 17 digits
        self.assertEqual(self.hits("a.md", "867133a886aefd540\n"), [])   # lowercase hex
        self.assertEqual(self.hits("a.md", "ZFA" + "C" * 8 + "12345Z\n"), [])

    def test_check_digit_net_catches_a_north_american_vin(self):
        # Any 17-character token whose position-9 check digit validates.
        # The serial ends in a letter so the shape net stays out of it.
        body = "1HGCM826" + "_" + "3A00435Z"
        valid = [body.replace("_", d) for d in "0123456789X"
                 if hygiene.vin_check_digit_valid(body.replace("_", d))]
        self.assertEqual(len(valid), 1)
        self.assertEqual(self.hits("a.md", valid[0] + "\n"),
                         [(1, "17-character VIN (check digit)")])

    def test_check_digit_net_ignores_a_wrong_check_digit(self):
        body = "1HGCM826" + "_" + "3A00435Z"
        wrong = [body.replace("_", d) for d in "0123456789X"
                 if not hygiene.vin_check_digit_valid(body.replace("_", d))]
        self.assertEqual(len(wrong), 10)

        for token in wrong:
            self.assertEqual(self.hits("a.md", token + "\n"), [])


class Paths(unittest.TestCase):
    def test_private_paths_are_refused(self):
        self.assertEqual(hygiene.path_problems("local/VEHICLES.md"),
                         ["tracked path under local/"])
        self.assertEqual(hygiene.path_problems("infra/.env"),
                         ["tracked .env file"])
        self.assertEqual(hygiene.path_problems("infra/sync/config.json"),
                         ["tracked private config.json"])
        self.assertEqual(hygiene.path_problems("telemetry.db"),
                         ["tracked database file"])

    def test_templates_and_docs_mentioning_local_are_fine(self):
        for path in ("infra/.env.example", "infra/sync/config.example.json",
                     "local.md", "docs/local-setup.md", "tools/localize.py"):
            self.assertEqual(hygiene.path_problems(path), [], path)


class Output(unittest.TestCase):
    def test_report_never_contains_the_matched_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            rel = os.path.relpath(tmp, hygiene.ROOT)
            path = os.path.join(tmp, "leak.md")

            with open(path, "w", encoding="utf-8") as handle:
                handle.write(f"vin {FAKE_VIN}\nINGEST_TOKEN={LONG_SECRET}\n")

            out = io.StringIO()

            with contextlib.redirect_stdout(out):
                status = hygiene.main([os.path.join(rel, "leak.md")])

        self.assertEqual(status, 1)
        text = out.getvalue()
        self.assertIn("leak.md:1: 17-character VIN", text)
        self.assertIn("leak.md:2: TOKEN= with a value", text)
        self.assertNotIn(FAKE_VIN, text)
        self.assertNotIn(LONG_SECRET, text)

    def test_tracked_tree_is_clean(self):
        """The guard passes on the repository as committed."""
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            status = hygiene.main([])

        self.assertEqual(status, 0, out.getvalue())


if __name__ == "__main__":
    unittest.main()
