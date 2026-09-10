"""
`./run_car.sh` - the command the car is actually launched with.

The script is executed, not grepped: a stub `python3` on PATH records
the argv `exec python3 live.py ...` composes, so what is asserted is
the command live.py would receive. Two things are pinned:

  * a bare launch composes exactly the command it always did - the five
    verified files, `--rate 10`, a timestamped `--db` - so the default
    set stays the production/verified one and nothing a validation
    drive adds can leak into a normal one;
  * `--candidate <name>` (issue #15) adds one candidate file by its
    stem, after the verified set and before the pass-through flags;
    an unknown name refuses to start and names the list; `--db` and
    `--mode` still pass through untouched.

No car, no network: the stub never runs live.py.
"""

import os
import re
import subprocess
import tempfile
import unittest

from tests import support

LAUNCHER = os.path.join(support.ROOT, "run_car.sh")

VERIFIED = [
    "mappings/candidates/bmw/dde/n47/d72n47a0_dynamic.yaml",
    "mappings/candidates/bmw/dde/n47/d72n47a0_flow.yaml",
    "mappings/candidates/bmw/dde/n47/d72n47a0_dpf_egr.yaml",
    "mappings/candidates/bmw/dde/n47/d72n47a0_gearbox.yaml",
    "mappings/candidates/bmw/egs/f10_transmission.yaml",
]

#: name -> file, as docs/TELEMETRY_CANDIDATES.md lists them.
CANDIDATES = {
    "injectors": "mappings/candidates/bmw/dde/n47/d72n47a0_injectors.yaml",
    "egr": "mappings/candidates/bmw/dde/n47/d72n47a0_egr.yaml",
    "airpath": "mappings/candidates/bmw/dde/n47/d72n47a0_airpath.yaml",
    "ibs": "mappings/candidates/bmw/dde/n47/d72n47a0_ibs.yaml",
    "tank": "mappings/candidates/bmw/dde/n47/d72n47a0_tank.yaml",
    "egs-speeds": "mappings/candidates/bmw/egs/f10_transmission_speeds.yaml",
    "sae-extra": "mappings/candidates/obd/engine_sae_extra.yaml",
}

DB_DEFAULT = re.compile(r"^local/sessions/drive-\d{8}T\d{6}Z\.db$")


def extra_mappings(pairs):
    out = []

    for path in pairs:
        out += ["--extra-mappings", path]

    return out


def run_launcher(*args):
    """
    Run run_car.sh with a stub python3 that records its argv.

    Returns (returncode, argv or None, stderr). argv is what live.py
    would have been exec'd with (`live.py` first); None when the script
    refused before exec.
    """
    with tempfile.TemporaryDirectory() as tmp:
        record = os.path.join(tmp, "argv")
        stub = os.path.join(tmp, "bin", "python3")
        os.makedirs(os.path.dirname(stub))

        with open(stub, "w", encoding="utf-8") as fh:
            fh.write("#!/usr/bin/env bash\n"
                     f"printf '%s\\n' \"$@\" > {record!r}\n")

        os.chmod(stub, 0o755)
        env = dict(os.environ)
        env["PATH"] = os.path.dirname(stub) + os.pathsep + env.get("PATH", "")
        run = subprocess.run(
            ["bash", LAUNCHER, *args], cwd=support.ROOT, env=env,
            capture_output=True, text=True, timeout=30,
        )

        argv = None

        if os.path.exists(record):
            with open(record, encoding="utf-8") as fh:
                argv = fh.read().split("\n")[:-1]

        return run.returncode, argv, run.stderr


class BareLaunch(unittest.TestCase):
    def test_composes_the_command_it_always_did(self):
        """The production/verified set, `--rate 10`, a timestamped db."""
        code, argv, err = run_launcher()

        self.assertEqual(code, 0, err)
        self.assertEqual(argv[:1 + 2 * len(VERIFIED) + 2],
                         ["live.py", *extra_mappings(VERIFIED), "--rate", "10"])
        self.assertEqual(argv[-2], "--db")
        self.assertRegex(argv[-1], DB_DEFAULT)
        self.assertEqual(len(argv), 1 + 2 * len(VERIFIED) + 4)

    def test_loads_no_candidate(self):
        _, argv, _ = run_launcher()

        for name, path in CANDIDATES.items():
            with self.subTest(candidate=name):
                self.assertNotIn(path, argv)

    def test_db_and_mode_pass_through(self):
        code, argv, err = run_launcher("--db", "my.db", "--mode", "long")

        self.assertEqual(code, 0, err)
        self.assertEqual(
            argv,
            ["live.py", *extra_mappings(VERIFIED), "--rate", "10",
             "--db", "my.db", "--mode", "long"],
        )
        #: `--db` given: no default db is added beside it.
        self.assertEqual(argv.count("--db"), 1)

    def test_no_db_suppresses_the_default_db(self):
        _, argv, _ = run_launcher("--no-db")

        self.assertNotIn("--db", argv)
        self.assertEqual(argv[-1], "--no-db")


class CandidateFlag(unittest.TestCase):
    def test_one_candidate_follows_the_verified_set(self):
        code, argv, err = run_launcher("--candidate", "egr")

        self.assertEqual(code, 0, err)
        self.assertEqual(
            argv[:-2],
            ["live.py", *extra_mappings(VERIFIED + [CANDIDATES["egr"]]),
             "--rate", "10"],
        )
        self.assertEqual(argv[-2], "--db")
        self.assertRegex(argv[-1], DB_DEFAULT)

    def test_two_candidates_in_the_order_given(self):
        code, argv, err = run_launcher(
            "--candidate", "injectors", "--candidate=ibs", "--mode", "long",
        )

        self.assertEqual(code, 0, err)
        self.assertEqual(
            argv[:-4],
            ["live.py",
             *extra_mappings(VERIFIED + [CANDIDATES["injectors"], CANDIDATES["ibs"]]),
             "--rate", "10"],
        )
        self.assertEqual(argv[-4], "--db")
        self.assertRegex(argv[-3], DB_DEFAULT)
        #: Everything that is not --candidate passes through, in order.
        self.assertEqual(argv[-2:], ["--mode", "long"])

    def test_every_documented_name_resolves_to_its_file(self):
        for name, path in CANDIDATES.items():
            with self.subTest(candidate=name):
                code, argv, err = run_launcher("--candidate", name)

                self.assertEqual(code, 0, err)
                self.assertEqual(argv.count("--extra-mappings"), len(VERIFIED) + 1)
                self.assertEqual(argv[2 * len(VERIFIED) + 2], path)
                self.assertTrue(os.path.exists(os.path.join(support.ROOT, path)), path)

    def test_an_unknown_name_refuses_and_lists_the_names(self):
        code, argv, err = run_launcher("--candidate", "boost")

        self.assertEqual(code, 2)
        self.assertIsNone(argv, "live.py must not be started")
        self.assertIn("unknown candidate 'boost'", err)

        for name in CANDIDATES:
            self.assertIn(name, err)

    def test_a_name_given_twice_refuses(self):
        """The registry would refuse the duplicate anyway; say it early."""
        code, argv, err = run_launcher("--candidate", "egr", "--candidate", "egr")

        self.assertEqual(code, 2)
        self.assertIsNone(argv)
        self.assertIn("twice", err)

    def test_a_missing_name_refuses(self):
        code, argv, err = run_launcher("--candidate")

        self.assertEqual(code, 2)
        self.assertIsNone(argv)
        self.assertIn("--candidate needs a name", err)

    def test_the_names_are_the_ones_the_doc_lists(self):
        """
        The stems the script accepts and the files the doc plans a drive
        for are the same seven; a new candidate file has to be added to
        both, and to CANDIDATES above.
        """
        with open(os.path.join(support.ROOT, "docs", "TELEMETRY_CANDIDATES.md"),
                  encoding="utf-8") as fh:
            doc = fh.read()

        for name, path in CANDIDATES.items():
            with self.subTest(candidate=name):
                self.assertIn(os.path.basename(path), doc)


if __name__ == "__main__":
    unittest.main()
