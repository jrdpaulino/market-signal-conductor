"""Which code is running: the git commit, so a test result or a backfill report can be tied to exact code.

Order: MSC_COMMIT (set at deploy time, since the container has no .git) -> `git` in the source folder -> "unknown"."""
import os
import subprocess


def commit_id():
    env = os.environ.get("MSC_COMMIT", "").strip()
    if env:
        return env[:40]
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        run = lambda *a: subprocess.run(["git", "-C", here, *a], capture_output=True, text=True, timeout=5)
        head = run("rev-parse", "--short", "HEAD")
        if head.returncode != 0 or not head.stdout.strip():
            return "unknown"
        dirty = bool(run("status", "--porcelain", "--untracked-files=no").stdout.strip())
        return head.stdout.strip() + ("-dirty" if dirty else "")
    except Exception:
        return "unknown"
