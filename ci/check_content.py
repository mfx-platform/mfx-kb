"""Fail when a guide, a question or the topic list is not in the shape the website and portal read.

Every guide (``guides/*.md``) and every question (``faq/*.md``) opens with front matter, and every
topic it names is in ``topics.yaml``:

    python ci/check_content.py            # this repository
    python ci/check_content.py <dir>      # another tree, such as the public export

A guide's front matter::

    ---
    title: "Your first terminal"     # the same text as the guide's own "# " heading
    topic: getting-started           # a slug from topics.yaml, list guides
    order: 10                        # an integer; lowest first within the topic
    summary: "One plain sentence."
    public: false                    # optional: only for a guide held back on purpose
    ---

A question's::

    ---
    question: "Is a retry safe?"
    topic: developers                # a slug from topics.yaml, list faq
    order: 10
    guide: when-a-call-fails         # optional: a related guide, by its file name
    ---
    The answer, in Markdown.

The front matter and ``topics.yaml`` are YAML, read here without a YAML library (the samples job
installs nothing), so they keep to a small subset: ``key: value`` pairs whose value is a
double-quoted or single-quoted string, an integer, ``true``/``false`` or a plain word; and, in
``topics.yaml``, top-level lists of such mappings. Anything outside it is reported, not guessed at.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

GUIDE_KEYS = {"title": str, "topic": str, "order": int, "summary": str, "public": bool}
GUIDE_REQUIRED = ("title", "topic", "order", "summary")
FAQ_KEYS = {"question": str, "topic": str, "order": int, "guide": str}
FAQ_REQUIRED = ("question", "topic", "order")
TOPIC_LISTS = ("guides", "faq")

_KEY = re.compile(r"^([a-z_]+):(?: (.*))?$")
_INT = re.compile(r"^-?[0-9]+$")
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
#: A plain (unquoted) value: no character YAML would read as anything but text.
_PLAIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._/-]*$")


class FormatError(ValueError):
    """Text outside the subset this check reads."""


def scalar(raw: str) -> str | int | bool:
    """One value: a quoted string, an integer, a boolean or a plain word."""
    raw = raw.strip()
    if raw.startswith('"'):
        if len(raw) < 2 or not raw.endswith('"'):  # noqa: PLR2004
            raise FormatError(f"unterminated string {raw}")
        body = raw[1:-1]
        if re.search(r'(?<!\\)(?:\\\\)*"', body):
            raise FormatError(f"unescaped quote in {raw}")
        if re.search(r"\\[^\\\"]", body):
            raise FormatError(f'only \\" and \\\\ are read as escapes: {raw}')
        return body.replace('\\"', '"').replace("\\\\", "\\")
    if raw.startswith("'"):
        if len(raw) < 2 or not raw.endswith("'") or re.search(r"(?<!')'(?!')", raw[1:-1]):  # noqa: PLR2004
            raise FormatError(f"bad single-quoted string {raw}")
        return raw[1:-1].replace("''", "'")
    if raw in ("true", "false"):
        return raw == "true"
    if _INT.match(raw):
        return int(raw)
    if not _PLAIN.match(raw):
        raise FormatError(f"quote this value: {raw}")
    return raw


def _pairs(lines: list[str]) -> dict[str, str | int | bool]:
    out: dict[str, str | int | bool] = {}
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        found = _KEY.match(line)
        if not found or found.group(2) is None:
            raise FormatError(f"not a key: value line: {line!r}")
        key = found.group(1)
        if key in out:
            raise FormatError(f"{key} is given twice")
        out[key] = scalar(found.group(2))
    return out


def front_matter(text: str) -> tuple[dict[str, str | int | bool], str]:
    """The front matter of ``text`` and the body after it."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0] != "---":
        raise FormatError("no front matter: the file must open with a line of ---")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise FormatError("the front matter is never closed with a line of ---") from None
    return _pairs(lines[1:end]), "\n".join(lines[end + 1 :])


def load_topics(text: str) -> dict[str, list[dict[str, str | int | bool]]]:
    """``topics.yaml``: each top-level list, as its entries."""
    lists: dict[str, list[dict[str, str | int | bool]]] = {}
    current: list[dict[str, str | int | bool]] | None = None
    entry: list[str] = []

    def close() -> None:
        if entry and current is not None:
            current.append(_pairs(entry))
        entry.clear()

    for line in text.replace("\r\n", "\n").split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if re.match(r"^[a-z_]+:$", line):
            close()
            name = line[:-1]
            if name in lists:
                raise FormatError(f"{name} is given twice")
            current = lists[name] = []
        elif line.startswith("  - "):
            close()
            entry.append(line[4:])
        elif line.startswith("    ") and entry:
            entry.append(line[4:])
        else:
            raise FormatError(f"not in the shape topics.yaml keeps: {line!r}")
        if current is None:
            raise FormatError("an entry before any list")
    close()
    return lists


@dataclass
class Content:
    """What the check read, for the tests and for anything else that wants it."""

    topics: dict[str, list[dict[str, str | int | bool]]] = field(default_factory=dict)
    guides: dict[str, dict[str, str | int | bool]] = field(default_factory=dict)
    faq: dict[str, dict[str, str | int | bool]] = field(default_factory=dict)


def _fields(where: str, data: dict, keys: dict[str, type], required: tuple[str, ...]) -> list[str]:
    out = [f"{where}: {key} is missing" for key in required if key not in data]
    for key, value in data.items():
        if key not in keys:
            out.append(f"{where}: {key} is not a field ({', '.join(keys)})")
        elif type(value) is not keys[key]:
            out.append(f"{where}: {key} must be {keys[key].__name__}, not {value!r}")
        elif isinstance(value, str) and not value.strip():
            out.append(f"{where}: {key} is empty")
    return out


def _topics(root: Path, content: Content) -> list[str]:
    path = root / "topics.yaml"
    if not path.is_file():
        return ["topics.yaml: missing"]
    try:
        content.topics = load_topics(path.read_text("utf-8"))
    except FormatError as error:
        return [f"topics.yaml: {error}"]
    out: list[str] = []
    for name in TOPIC_LISTS:
        entries = content.topics.get(name)
        if not entries:
            out.append(f"topics.yaml: the list {name} is missing or empty")
            continue
        slugs = [entry.get("slug") for entry in entries]
        for number, entry in enumerate(entries, 1):
            where = f"topics.yaml: {name} entry {number}"
            out += _fields(where, entry, {"slug": str, "label": str, "order": int}, ("slug", "label", "order"))
            if isinstance(entry.get("slug"), str) and not _SLUG.match(entry["slug"]):
                out.append(f"{where}: {entry['slug']} is not a slug")
        out += [f"topics.yaml: {name} names {s} twice" for s in sorted({s for s in slugs if slugs.count(s) > 1})]
        orders = [entry.get("order") for entry in entries]
        out += [f"topics.yaml: {name} has order {o} twice" for o in sorted({o for o in orders if orders.count(o) > 1})]
    out += [f"topics.yaml: {name} is not a list this check knows" for name in content.topics if name not in TOPIC_LISTS]
    return out


def _orders(kind: str, items: dict[str, dict]) -> list[str]:
    seen: dict[tuple, str] = {}
    out: list[str] = []
    for slug, data in sorted(items.items()):
        key = (data.get("topic"), data.get("order"))
        if key in seen:
            out.append(f"{kind}/{slug}.md: order {key[1]} in {key[0]} is also {kind}/{seen[key]}.md's")
        seen.setdefault(key, slug)
    return out


def check(root: Path = REPO) -> tuple[list[str], Content]:
    """Every problem with the content under ``root`` (empty when there is none), and what was read."""
    content = Content()
    out = _topics(root, content)
    guide_topics = {e.get("slug") for e in content.topics.get("guides", [])}
    faq_topics = {e.get("slug") for e in content.topics.get("faq", [])}

    guides = sorted((root / "guides").glob("*.md"))
    if not guides:
        out.append("guides/: no guides")
    for path in guides:
        where = f"guides/{path.name}"
        try:
            data, body = front_matter(path.read_text("utf-8"))
        except FormatError as error:
            out.append(f"{where}: {error}")
            continue
        content.guides[path.stem] = data
        out += _fields(where, data, GUIDE_KEYS, GUIDE_REQUIRED)
        if "topic" in data and data["topic"] not in guide_topics:
            out.append(f"{where}: topic {data['topic']} is not in topics.yaml's guides")
        heading = re.search(r"^# (.+)$", body, re.MULTILINE)
        if not heading:
            out.append(f"{where}: no '# ' heading after the front matter")
        elif isinstance(data.get("title"), str) and heading.group(1).strip() != data["title"]:
            out.append(f"{where}: title {data['title']!r} is not the heading {heading.group(1).strip()!r}")
        summary = data.get("summary")
        if isinstance(summary, str) and not summary.rstrip().endswith("."):
            out.append(f"{where}: summary must be one sentence, ending with a full stop")

    questions = sorted((root / "faq").glob("*.md"))
    if not questions:
        out.append("faq/: no questions")
    for path in questions:
        where = f"faq/{path.name}"
        if not _SLUG.match(path.stem):
            out.append(f"{where}: the file name is not a slug")
        try:
            data, body = front_matter(path.read_text("utf-8"))
        except FormatError as error:
            out.append(f"{where}: {error}")
            continue
        content.faq[path.stem] = data
        out += _fields(where, data, FAQ_KEYS, FAQ_REQUIRED)
        if "topic" in data and data["topic"] not in faq_topics:
            out.append(f"{where}: topic {data['topic']} is not in topics.yaml's faq")
        question = data.get("question")
        if isinstance(question, str) and not question.rstrip().endswith("?"):
            out.append(f"{where}: question must end with a question mark")
        if not body.strip():
            out.append(f"{where}: no answer after the front matter")
        guide = data.get("guide")
        if isinstance(guide, str):
            if guide not in content.guides:
                out.append(f"{where}: guide {guide} is not a guide in guides/")
            elif content.guides[guide].get("public") is False:
                out.append(f"{where}: guide {guide} is held back (public: false), so a question cannot point to it")

    return out + _orders("guides", content.guides) + _orders("faq", content.faq), content


def main(argv: list[str] | None = None) -> int:
    """Run the check; 0 when the content is in shape, 1 when not, 2 on bad usage."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 1:
        print("usage: check_content.py [<dir>]", file=sys.stderr)
        return 2
    root = Path(args[0]) if args else REPO
    found, content = check(root)
    for line in found:
        print(line)
    if found:
        print(f"{len(found)} problem(s) with the guides, questions or topics", file=sys.stderr)
        return 1
    print(f"{len(content.guides)} guides and {len(content.faq)} questions, every topic known")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
