"""python -m tests : run every test, write the full output to logs/tests-<time>-<commit>.txt, print a short summary."""
import io
import logging
import sys
import unittest

from msc.logs import write_log
from msc.version import commit_id

buf = io.StringIO()
logging.basicConfig(stream=buf, level=logging.WARNING, force=True)    # log lines go to the file too
buf.write(f"msc commit under test: {commit_id()}\n")
suite = unittest.defaultTestLoader.discover("tests", top_level_dir=".")
result = unittest.TextTestRunner(stream=buf, verbosity=1).run(suite)
path = write_log("tests", buf.getvalue())
print(f"commit {commit_id()}: ran {result.testsRun}, failures {len(result.failures)}, errors {len(result.errors)} -> "
      f"{'OK' if result.wasSuccessful() else 'FAILED'}")
for case, _tb in result.failures + result.errors:
    print("  failed:", case.id())
print("full output:", path)
sys.exit(0 if result.wasSuccessful() else 1)
