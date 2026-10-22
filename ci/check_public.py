"""Fail when a file in the knowledge base cites anything internal to MFX Hub.

Everything here is published. Nothing may cite the team's internal designs, decisions, work items
or test cases, or name an internal repository, a private customer or another provider.

    python ci/check_public.py                  # the files git tracks
    python ci/check_public.py --tree <dir>     # every text file under a directory (the export)

Two kinds of check, the same as the Python SDK's:

* **Patterns** for identifiers whose *shape* is the giveaway: work-item, decision and
  architecture-record ids, test-case ids, internal design paths and section references.
* **Names** of private customers, internal repositories and other products, read from
  ``ci/internal-names.txt``. A list of words a public repository must never contain cannot be
  published in it, so that file stays in the private repository and the export leaves it out; the
  public repository runs the pattern check alone. Hyphenated tokens are checked whole and part by
  part.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: Identifiers that are internal by their shape.
INTERNAL = re.compile(
    # Written so that these lines do not match themselves when this file is scanned.
    r"design[/]|\bDEC-\d+|AB#\d+|\bADR-\d+|TC-S[1]-|§ ?\d"
    # A tracker area path is upper case - MFX, a backslash, a capital. A sample's own lower-case
    # file path that starts the same way is not one.
    r"|(?-i:MFX[\\]{1,2}[A-Z])|\bLane \d|-intern[a]l\b",
    re.IGNORECASE,
)

#: One lower-case name a line, ``#`` starts a comment. Absent in the public repository.
NAMES_FILE = REPO / "ci" / "internal-names.txt"

_TEXT = (".py", ".md", ".txt", ".toml", ".json", ".yaml", ".yml", ".mq5", ".mqh", ".ini", ".cfg")
_TEXT_NAMES = ("LICENSE", "CODEOWNERS", ".gitignore")
_SKIP = frozenset({".git", ".venv", "dist", "build", "__pycache__", ".pytest_cache", ".ruff_cache"})
_TOKEN = re.compile(r"[a-z0-9][a-z0-9-]*")


def load_names(path: Path = NAMES_FILE) -> frozenset[str]:
    """The names in ``path``, lower case; none when the file is absent."""
    if not path.is_file():
        return frozenset()
    lines = (line.split("#", 1)[0].strip().lower() for line in path.read_text("utf-8").splitlines())
    return frozenset(line for line in lines if line)


NAMES = load_names()


def _text(name: str) -> bool:
    return name.endswith(_TEXT) or name.endswith(_TEXT_NAMES)


def match(line: str, names: frozenset[str] = NAMES) -> str | None:
    """What in ``line`` is internal, or None. A name is reported by the token that carried it."""
    found = INTERNAL.search(line)
    if found:
        return found.group(0)
    for token in _TOKEN.findall(line.lower()):
        for word in (token, *token.split("-")):
            if word and word in names:
                return token
    return None


def scan(sources: Iterable[tuple[str, str]], names: frozenset[str] = NAMES) -> list[str]:
    """Every internal line of ``sources`` (pairs of where and text), as ``where:line: match``."""
    out: list[str] = []
    for where, text in sources:
        for number, line in enumerate(text.splitlines(), 1):
            hit = match(line, names)
            if hit:
                out.append(f"{where}:{number}: {hit}")
    return out


def _tracked(repo: Path) -> Iterator[tuple[str, str]]:
    listed = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-C", str(repo), "ls-files"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for name in listed.splitlines():
        path = repo / name
        if path.resolve() == NAMES_FILE.resolve():
            continue  # the names themselves: private, and left out of the export
        if path.is_file() and _text(path.name):
            yield name, path.read_text(encoding="utf-8", errors="replace")


def _tree(root: Path) -> Iterator[tuple[str, str]]:
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if _SKIP.intersection(relative.parts) or not path.is_file() or not _text(path.name):
            continue
        yield relative.as_posix(), path.read_text(encoding="utf-8", errors="replace")


def tree_findings(root: Path, names: frozenset[str] = NAMES) -> list[str]:
    """Every line under ``root`` that would publish something internal."""
    return scan(_tree(root), names)


def main(argv: list[str] | None = None) -> int:
    """Run the check; 0 when clean, 1 when something internal was found, 2 on bad usage."""
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["--tree"]:
        root = Path(args[1]) if len(args) > 1 else REPO
        if not root.is_dir():
            print(f"no directory at {root}", file=sys.stderr)
            return 2
        found, what = tree_findings(root), f"the tree at {root}"
    else:
        found, what = scan(_tracked(REPO)), "the knowledge base"
    for line in found:
        print(line)
    if found:
        print(f"{len(found)} internal reference(s)", file=sys.stderr)
        return 1
    print(f"nothing internal in {what}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
