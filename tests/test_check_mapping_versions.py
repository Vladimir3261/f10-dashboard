"""
tools/check_mapping_versions.py - the git-diff version guard.

The guard is what stops a mapping (or the drive-mode table) from changing
content under an unchanged version, which would let two different decodes
share one version number in the lake. These tests run the real tool, as a
subprocess, inside a throwaway git repository built here: a mapping file
and a `config/modes.yaml`, committed, then edited in every way the guard
has to tell apart - a content change, a comment-only edit, a bump, a `#`
that is data because it sits inside a string.

Needs `git` on PATH (it is, in CI); skipped otherwise. No car, no network.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests import support

TOOL = os.path.join(support.ROOT, "tools", "check_mapping_versions.py")

MAPPING = """\
schema_version: 1
mapping:
  id: guard-fixture
  version: 3            # the data version
  description: "fixture #1"   # a '#' inside a string is content
requests:
  - id: rpm_read        # declaration order is the rotation order
    pid: 0x0C
  - id: speed_read
    pid: 0x0D
channels:
  - id: rpm
    scale: 0.25         # quarter-rpm per bit
"""

REQUESTS_SWAPPED = MAPPING.replace(
    """  - id: rpm_read        # declaration order is the rotation order
    pid: 0x0C
  - id: speed_read
    pid: 0x0D
""",
    """  - id: speed_read
    pid: 0x0D
  - id: rpm_read        # declaration order is the rotation order
    pid: 0x0C
""")
assert REQUESTS_SWAPPED != MAPPING

MODES = """\
schema_version: 1
id: drive-modes
version: 2
# the drive-mode table
modes:
  normal:
    multipliers:
      motion: 1.0       # the declared rate
default: normal
"""

OTHER = "note: not versioned data\n"


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout


@unittest.skipUnless(shutil.which("git"), "git is not on PATH")
class VersionGuard(unittest.TestCase):

    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="version-guard-")
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "guard@test")
        git(self.repo, "config", "user.name", "guard")
        git(self.repo, "config", "commit.gpgsign", "false")
        self.write("mappings/obd/engine.yaml", MAPPING)
        self.write("config/modes.yaml", MODES)
        self.write("config/other.yaml", OTHER)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "baseline")

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def run_guard(self):
        r = subprocess.run(
            [sys.executable, TOOL], cwd=self.repo, capture_output=True, text=True,
        )
        return r.returncode, r.stdout + r.stderr

    # -- the drive-mode table (N4 on PR #46: it was not watched at all) --

    def test_modes_content_change_without_a_bump_fails(self):
        self.write("config/modes.yaml", MODES.replace("motion: 1.0", "motion: 2.0"))
        code, out = self.run_guard()
        self.assertEqual(code, 1, out)
        self.assertIn("config/modes.yaml", out)
        self.assertIn("was 2, now 2", out)
        self.assertIn("bump `version`", out)

    def test_modes_content_change_with_a_bump_passes(self):
        self.write("config/modes.yaml",
                   MODES.replace("motion: 1.0", "motion: 2.0")
                        .replace("version: 2", "version: 3"))
        code, out = self.run_guard()
        self.assertEqual(code, 0, out)
        self.assertIn("1 changed file(s) properly bumped", out)

    def test_modes_comment_only_edit_needs_no_bump(self):
        edited = (MODES.replace("# the drive-mode table",
                                "# the drive-mode table, measured on the car")
                       .replace("# the declared rate", "# 23 members, ~1/11.5 s each"))
        self.assertNotEqual(edited, MODES)
        self.write("config/modes.yaml", edited)
        code, out = self.run_guard()
        self.assertEqual(code, 0, out)
        self.assertIn("0 changed file(s)", out)
        self.assertIn("config/modes.yaml: comments/version only", out)

    def test_modes_version_line_only_edit_passes(self):
        self.write("config/modes.yaml", MODES.replace("version: 2", "version: 3"))
        code, out = self.run_guard()
        self.assertEqual(code, 0, out)

    def test_modes_version_going_backwards_fails(self):
        self.write("config/modes.yaml",
                   MODES.replace("motion: 1.0", "motion: 2.0")
                        .replace("version: 2", "version: 1"))
        code, out = self.run_guard()
        self.assertEqual(code, 1, out)
        self.assertIn("was 2, now 1", out)

    # -- mapping files: the original behaviour, kept --

    def test_mapping_content_change_without_a_bump_fails(self):
        self.write("mappings/obd/engine.yaml", MAPPING.replace("scale: 0.25", "scale: 0.5"))
        code, out = self.run_guard()
        self.assertEqual(code, 1, out)
        self.assertIn("mappings/obd/engine.yaml", out)
        self.assertIn("was 3, now 3", out)
        self.assertIn("bump `mapping.version`", out)

    def test_mapping_content_change_with_a_bump_passes(self):
        self.write("mappings/obd/engine.yaml",
                   MAPPING.replace("scale: 0.25", "scale: 0.5")
                          .replace("version: 3", "version: 4"))
        code, out = self.run_guard()
        self.assertEqual(code, 0, out)

    def test_mapping_comment_only_edit_needs_no_bump(self):
        self.write("mappings/obd/engine.yaml",
                   MAPPING.replace("# quarter-rpm per bit", "# 0.25 rpm per bit, SAE J1979"))
        code, out = self.run_guard()
        self.assertEqual(code, 0, out)
        self.assertIn("mappings/obd/engine.yaml: comments/version only", out)

    def test_reordering_requests_is_content(self):
        """
        The loader numbers requests by position and the rotation is
        sorted by that number: swapping two requests changes the order
        the car is polled in (B1 on PR #47 - a dict comparison missed it).
        """
        self.write("mappings/obd/engine.yaml", REQUESTS_SWAPPED)
        code, out = self.run_guard()
        self.assertEqual(code, 1, out)
        self.assertIn("mappings/obd/engine.yaml", out)
        self.assertIn("was 3, now 3", out)

    def test_reordering_requests_with_a_bump_passes(self):
        self.write("mappings/obd/engine.yaml",
                   REQUESTS_SWAPPED.replace("version: 3", "version: 4"))
        code, out = self.run_guard()
        self.assertEqual(code, 0, out)
        self.assertIn("1 changed file(s) properly bumped", out)

    def test_a_hash_inside_a_string_is_content(self):
        """`description: "fixture #1"` -> "#2": the loader sees that."""
        self.write("mappings/obd/engine.yaml", MAPPING.replace('"fixture #1"', '"fixture #2"'))
        code, out = self.run_guard()
        self.assertEqual(code, 1, out)

    def test_an_unparseable_file_compares_as_changed(self):
        """A broken file must not slip through as 'unchanged'."""
        self.write("mappings/obd/engine.yaml", MAPPING + "channels:\n\t- tab: indent\n")
        code, out = self.run_guard()
        self.assertEqual(code, 1, out)

    # -- scope --

    def test_other_yaml_under_config_is_not_watched(self):
        self.write("config/other.yaml", "note: changed, and that is fine\n")
        code, out = self.run_guard()
        self.assertEqual(code, 0, out)
        self.assertNotIn("other.yaml", out)

    def test_nothing_changed_passes(self):
        code, out = self.run_guard()
        self.assertEqual(code, 0, out)
        self.assertIn("0 changed file(s)", out)


class ContentFunction(unittest.TestCase):
    """The comparison itself, without git."""

    def setUp(self):
        sys.path.insert(0, os.path.join(support.ROOT, "tools"))
        self.addCleanup(sys.path.remove, os.path.join(support.ROOT, "tools"))
        import importlib
        self.tool = importlib.import_module("check_mapping_versions")

    def test_watched_paths(self):
        self.assertTrue(self.tool.is_watched("config/modes.yaml"))
        self.assertTrue(self.tool.is_watched("mappings/obd/engine.yaml"))
        self.assertTrue(self.tool.is_watched("mappings/candidates/bmw/egs/x.yaml"))
        self.assertFalse(self.tool.is_watched("config/other.yaml"))
        self.assertFalse(self.tool.is_watched("mappings/README.md"))
        self.assertFalse(self.tool.is_watched("live.py"))

    def test_content_drops_both_version_locations_and_comments(self):
        a = self.tool.content(MAPPING)
        b = self.tool.content(MAPPING.replace("version: 3", "version: 9")
                                     .replace("# quarter-rpm per bit", ""))
        self.assertEqual(a, b)
        self.assertNotIn('"version"', a)          # schema_version stays; it is data
        self.assertIn('"schema_version": 1', a)
        self.assertNotIn("quarter-rpm", a)
        m = self.tool.content(MODES)
        self.assertNotIn('"version"', m)
        self.assertIn('"motion": 1.0', m)

    def test_content_sees_declaration_order(self):
        self.assertNotEqual(self.tool.content(MAPPING),
                            self.tool.content(REQUESTS_SWAPPED))

    def test_content_keeps_a_hash_inside_a_string(self):
        self.assertNotEqual(self.tool.content(MAPPING),
                            self.tool.content(MAPPING.replace("#1", "#2")))

    def test_the_real_files_parse_to_a_document(self):
        """The guard must be able to read what it guards (not fall back to text)."""
        for rel in ("config/modes.yaml", "mappings/obd/engine.yaml"):
            with open(os.path.join(support.ROOT, rel), encoding="utf-8") as fh:
                text = fh.read()
            doc = self.tool.content(text)
            self.assertTrue(doc.startswith("{"), rel)
            self.assertNotEqual(doc, self.tool.strip_version(text), rel)


if __name__ == "__main__":
    unittest.main()
