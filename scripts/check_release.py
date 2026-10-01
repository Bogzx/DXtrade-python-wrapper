"""Checks that the package is ready to release as `tag` (e.g. v0.2.0).

    python scripts/check_release.py            # the version has a CHANGELOG entry
    python scripts/check_release.py v0.2.0     # ...dated, and the tag matches the version

Run by CI on every push and by the release workflow before anything is uploaded.
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def package_version():
    text = (ROOT / "dxtrade_wrapper" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'^__version__ = "([^"]+)"', text, re.M).group(1)


def changelog_entry(version):
    """The heading line of `version` in CHANGELOG.md, or None."""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(rf"^## {re.escape(version)}\b.*$", text, re.M)
    return match.group(0) if match else None


def problems(tag=None):
    version = package_version()
    found = []
    if tag is not None and tag.removeprefix("v") != version:
        found.append(f"tag {tag} does not match __version__ {version}")
    heading = changelog_entry(version)
    if heading is None:
        found.append(f"CHANGELOG.md has no '## {version}' section")
    elif tag is not None and not re.search(r"\(\d{4}-\d{2}-\d{2}\)", heading):
        found.append(f"CHANGELOG.md heading for {version} has no release date: {heading!r}")
    return found


if __name__ == "__main__":
    errors = problems(sys.argv[1] if len(sys.argv) > 1 else None)
    for error in errors:
        print(f"error: {error}")
    if not errors:
        print(f"ok: {package_version()} is ready to release")
    sys.exit(1 if errors else 0)
