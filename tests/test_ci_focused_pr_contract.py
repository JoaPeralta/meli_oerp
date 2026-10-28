"""Keep focused PR delivery on the existing complete native module workflow."""
import ast
import hashlib
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
# Historical5905 workflow bytes, normalizing only the reviewed PR-base list.
UNCHANGED_WORKFLOW_SHA256 = "53f4f8ce65e7046d1e3c10e36947a4ef272eb51e3634a212757a743040b72de1"


def branches(source, event):
    match = re.search(r"(?m)^  " + re.escape(event) + r":\n    branches: (\[[^\n]*\])$", source)
    if not match:
        raise AssertionError("Expected explicit workflow event branch list")
    return ast.literal_eval(match.group(1))


class FocusedPullRequestContract(unittest.TestCase):
    def setUp(self):
        self.source = WORKFLOW.read_text(encoding="utf-8")

    def test_focused_review_base_runs_the_existing_native_job(self):
        self.assertEqual(branches(self.source, "pull_request"),
                         ["19.0", "codex/meli-bulk-contract"])

    def test_existing_push_trigger_is_unchanged(self):
        self.assertEqual(branches(self.source, "push"), ["19.0"])

    def test_only_the_pull_request_base_list_changes(self):
        normalized, count = re.subn(
            r"(?m)(^  pull_request:\n    branches:) \[[^\n]*\]$",
            r"\1 <review-bases>", self.source, count=1)
        self.assertEqual(count, 1)
        self.assertEqual(hashlib.sha256(normalized.encode()).hexdigest(),
                         UNCHANGED_WORKFLOW_SHA256)
        self.assertIn("--test-tags=/meli_oerp", self.source)
        self.assertIn("--test-enable", self.source)
        self.assertIn("--stop-after-init", self.source)


if __name__ == "__main__":
    unittest.main()
