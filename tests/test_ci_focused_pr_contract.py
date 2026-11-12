"""Keep focused PR delivery on the existing complete native module workflow."""
import ast
import hashlib
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
# Exact pre-iteration workflow (765a39f0), including the 6875a993 security cohort.
# Normalize only the reviewed PR-base list and the two newly approved selectors.
UNCHANGED_WORKFLOW_SHA256 = "8442b3d91f9c3a794c4f0905636885e42792a28044cafb1e59b692f6a46f4bfe"


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

    def test_only_reviewed_branch_and_standalone_selector_differences(self):
        normalized, count = re.subn(
            r"(?m)(^  pull_request:\n    branches:) \[[^\n]*\]$",
            r"\1 <review-bases>", self.source, count=1)
        self.assertEqual(count, 1)
        for suite in ("test_posting_metadata_source.py", "test_meli_c6_stock_confirmation.py"):
            line = f"          python3 -I -B -X utf8 -m unittest discover -s tests -p '{suite}' -v\n"
            self.assertEqual(normalized.count(line), 1)
            normalized = normalized.replace(line, "")
        self.assertEqual(hashlib.sha256(normalized.encode()).hexdigest(),
                         UNCHANGED_WORKFLOW_SHA256)
        self.assertIn("--test-tags=/meli_oerp", self.source)
        self.assertIn("--test-enable", self.source)
        self.assertIn("--stop-after-init", self.source)


if __name__ == "__main__":
    unittest.main()
