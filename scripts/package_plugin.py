"""Create a QGIS plugin zip without local caches or heavy data files."""

import os
import zipfile


PLUGIN_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PLUGIN_NAME = os.path.basename(PLUGIN_DIR)
DIST_DIR = os.path.join(PLUGIN_DIR, "dist")
OUTPUT = os.path.join(DIST_DIR, "{}_release.zip".format(PLUGIN_NAME))

EXCLUDED_DIRS = {
    ".git",
    "__pycache__",
    "data",
}
EXCLUDED_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".download",
    ".zip",
)


def should_include(path):
    rel_parts = os.path.relpath(path, PLUGIN_DIR).split(os.sep)
    if any(part in EXCLUDED_DIRS for part in rel_parts):
        return False
    return not path.endswith(EXCLUDED_SUFFIXES)


def main():
    os.makedirs(DIST_DIR, exist_ok=True)
    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as archive:
        for root, dirs, files in os.walk(PLUGIN_DIR):
            dirs[:] = [item for item in dirs if should_include(os.path.join(root, item))]
            for filename in files:
                path = os.path.join(root, filename)
                if not should_include(path):
                    continue
                rel = os.path.relpath(path, PLUGIN_DIR)
                archive.write(path, os.path.join(PLUGIN_NAME, rel))
    print(OUTPUT)


if __name__ == "__main__":
    main()
