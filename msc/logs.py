"""Long outputs go to a text file, the terminal gets a short summary and the path.

Folder: MSC_LOG_DIR, default ./logs (git-ignored). File names carry the time and the git commit."""
import os
import time

from .version import commit_id


def log_path(kind, ext="txt"):
    folder = os.environ.get("MSC_LOG_DIR", "logs")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{commit_id()}.{ext}")


def write_log(kind, text):
    path = log_path(kind)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    return path
