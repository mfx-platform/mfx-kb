"""The guides, the questions and the topic list are in the shape the website and portal read.

    python -m unittest discover -s ci/tests -v
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

CI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CI))

import check_content  # noqa: E402 - after the path insert, by necessity
import check_public  # noqa: E402

REPO = CI.parent

TOPICS = textwrap.dedent(
    """\
    guides:
      - slug: getting-started
        label: Getting started
        order: 10
    faq:
      - slug: developers
        label: Developers
        order: 10
    """
)
GUIDE = '---\ntitle: "First"\ntopic: getting-started\norder: 10\nsummary: "One sentence."\n---\n\n# First\n\nText.\n'
QUESTION = '---\nquestion: "Is it safe?"\ntopic: developers\norder: 10\nguide: first\n---\n\nYes.\n'


def tree(files: dict[str, str]) -> Path:
    """A throwaway content tree holding ``files``."""
    root = Path(tempfile.mkdtemp(prefix="kb-content-"))
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")
    return root


G, Q, T = "guides/first.md", "faq/is-it-safe.md", "topics.yaml"


def problems(changes: dict[str, str | None] | None = None) -> list[str]:
    """The check's findings on a small valid tree with ``changes`` (file name -> text, None drops it)."""
    files = {T: TOPICS, G: GUIDE, Q: QUESTION}
    for name, text in (changes or {}).items():
        if text is None:
            files.pop(name, None)
        else:
            files[name] = text
    return check_content.check(tree(files))[0]


class TheKnowledgeBase(unittest.TestCase):
    def test_every_guide_and_question_has_front_matter_and_a_known_topic(self) -> None:
        found, content = check_content.check(REPO)
        self.assertEqual(found, [])
        self.assertEqual(len(content.guides), len(list((REPO / "guides").glob("*.md"))))
        self.assertEqual(len(content.faq), len(list((REPO / "faq").glob("*.md"))))

    def test_the_topic_list_is_valid_yaml_when_a_yaml_library_reads_it(self) -> None:
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            self.skipTest("no YAML library here; the website reads it with one")
        self.assertEqual(yaml.safe_load((REPO / "topics.yaml").read_text("utf-8")), check_content.load_topics((REPO / "topics.yaml").read_text("utf-8")))
        for path in [*sorted((REPO / "guides").glob("*.md")), *sorted((REPO / "faq").glob("*.md"))]:
            text = path.read_text("utf-8")
            ours, _ = check_content.front_matter(text)
            theirs = yaml.safe_load(text.replace("\r\n", "\n").split("---\n")[1])
            self.assertEqual(ours, theirs, path.name)

    def test_the_export_carries_the_questions_and_the_topics_and_nothing_internal(self) -> None:
        if not (CI / "export_public.py").is_file():
            self.skipTest("the export is made where the knowledge base is written, not in the public repository")
        import export_public

        names = export_public.tracked(REPO)
        if "topics.yaml" not in names:
            self.skipTest("topics.yaml is not tracked yet (run after git add)")
        out = Path(tempfile.mkdtemp(prefix="kb-export-")) / "tree"
        out.mkdir()
        self.assertEqual(export_public.export(out), [])
        self.assertTrue((out / "topics.yaml").is_file())
        self.assertEqual(sorted(p.name for p in (out / "faq").glob("*.md")), sorted(p.name for p in (REPO / "faq").glob("*.md")))
        self.assertEqual(check_content.check(out)[0], [])
        self.assertEqual(check_public.tree_findings(out), [])


class TheCheck(unittest.TestCase):
    def test_a_valid_tree_passes(self) -> None:
        self.assertEqual(problems(), [])

    def test_a_guide_without_front_matter_fails(self) -> None:
        self.assertIn("no front matter", " ".join(problems({G: "# First\n\nText.\n"})))

    def test_an_unknown_topic_fails(self) -> None:
        self.assertIn("topic nowhere is not in topics.yaml's guides", " ".join(problems({G: GUIDE.replace("getting-started", "nowhere")})))
        self.assertIn("topic nowhere is not in topics.yaml's faq", " ".join(problems({Q: QUESTION.replace("developers", "nowhere")})))

    def test_a_guide_topic_is_not_a_question_topic(self) -> None:
        self.assertTrue(problems({Q: QUESTION.replace("developers", "getting-started")}))

    def test_a_missing_field_and_an_unknown_field_fail(self) -> None:
        found = " ".join(problems({G: GUIDE.replace('summary: "One sentence."\n', "colour: blue\n")}))
        self.assertIn("summary is missing", found)
        self.assertIn("colour is not a field", found)

    def test_an_order_must_be_an_integer(self) -> None:
        self.assertIn("order must be int", " ".join(problems({G: GUIDE.replace("order: 10", "order: ten")})))

    def test_the_title_is_the_guides_own_heading(self) -> None:
        self.assertIn("is not the heading", " ".join(problems({G: GUIDE.replace("# First", "# Second")})))

    def test_a_question_ends_with_a_question_mark_and_has_an_answer(self) -> None:
        found = " ".join(problems({Q: QUESTION.replace("safe?", "safe").replace("Yes.\n", "")}))
        self.assertIn("question mark", found)
        self.assertIn("no answer", found)

    def test_a_question_points_only_to_a_guide_that_is_published(self) -> None:
        self.assertIn("guide second is not a guide", " ".join(problems({Q: QUESTION.replace("guide: first", "guide: second")})))
        held = GUIDE.replace("---\n\n#", "public: false\n---\n\n#")
        self.assertIn("is held back", " ".join(problems({G: held})))

    def test_two_entries_in_one_topic_do_not_share_an_order(self) -> None:
        self.assertIn("also guides/first.md's", " ".join(problems({"guides/second.md": GUIDE.replace("First", "Second")})))

    def test_the_topic_list_is_required_and_its_slugs_unique(self) -> None:
        self.assertIn("topics.yaml: missing", problems({T: None}))
        doubled = TOPICS.replace("faq:", "  - slug: getting-started\n    label: Again\n    order: 20\nfaq:")
        self.assertIn("names getting-started twice", " ".join(problems({T: doubled})))

    def test_values_outside_the_subset_are_reported_not_guessed(self) -> None:
        self.assertEqual(check_content.scalar('"say \\"hi\\""'), 'say "hi"')
        self.assertEqual(check_content.scalar("'it''s'"), "it's")
        self.assertEqual(check_content.scalar("42"), 42)
        self.assertIs(check_content.scalar("false"), False)
        for raw in ("a: b", '"open', "[1, 2]", '"a\\nb"', "# c"):
            with self.assertRaises(check_content.FormatError, msg=raw):
                check_content.scalar(raw)


if __name__ == "__main__":
    unittest.main()
