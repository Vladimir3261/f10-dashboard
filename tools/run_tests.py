#!/usr/bin/env python3
"""
Run the test suite and make every skip visible.

    python3 tools/run_tests.py            # whole suite
    python3 tools/run_tests.py tests.research   # one package / module

Same discovery as `python3 -m unittest discover`, same exit status, one
difference: a skipped test is printed with its reason and the total is
printed on the last line. The suite is required to pass with no car, no
network and no BMW source cache, and a few tests can only run when the
gitignored cache is present - those skip. A silent skip would make a
green run look like proof of something it never checked, so CI and the
owner see the count either way.

Stdlib only, like everything else here.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main(argv) -> int:
    os.chdir(ROOT)

    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)

    loader = unittest.TestLoader()

    if argv:
        suite = loader.loadTestsFromNames(argv)
    else:
        suite = loader.discover(ROOT)

    runner = unittest.TextTestRunner(verbosity=1, stream=sys.stdout)
    result = runner.run(suite)

    if result.skipped:
        print()
        print(f"skipped ({len(result.skipped)}):")

        for test, reason in result.skipped:
            print(f"  {test.id()}")
            print(f"      {reason}")

    print()
    print(
        f"tests={result.testsRun} failures={len(result.failures)} "
        f"errors={len(result.errors)} skipped={len(result.skipped)} "
        f"expected_failures={len(result.expectedFailures)} "
        f"unexpected_successes={len(result.unexpectedSuccesses)}"
    )

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
