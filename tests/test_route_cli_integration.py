"""Integration tests: cite -> export -> route-grade via the real CLI paths."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MATERIALS = ROOT / "materials" / "routes"


class RouteCliIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "c.db"
        self.u = self.tmp / "u.jsonl"
        self.a = self.tmp / "a.jsonl"

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, *map(str, args)],
            cwd=str(ROOT), capture_output=True, text=True,
        )

    def build_pipeline(self):
        r = self.run_cli("-m", "citation_system", "init", "--db", self.db,
                         "--pages", MATERIALS / "pages.json", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_cli("-m", "citation_system", "add-references", "--db", self.db,
                         "--references", MATERIALS / "references.jsonl")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_cli("-m", "citation_system", "export", "--db", self.db,
                         "--usable", self.u, "--needs-attention", self.a)
        self.assertEqual(r.returncode, 0, r.stderr)

    def grade(self):
        return self.run_cli(
            "-m", "route_grading", "grade",
            "--products", self.u, self.a,
            "--holds", MATERIALS / "holds.json",
            "--spec", MATERIALS / "spec.json",
        )

    def test_usable_route_grades_to_two(self):
        self.build_pipeline()
        r = self.grade()
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertTrue(out["gradable"])
        self.assertEqual(out["grade"], 2)

    def test_reversed_range_suspends_route(self):
        self.build_pipeline()
        # Move the range end (Rf) before the range start (Ls).
        r = self.run_cli(
            "-m", "citation_system", "apply-operations", "--db", self.db,
            "--operations", self._ops_bad(),
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        # Re-export and try again.
        r = self.run_cli("-m", "citation_system", "export", "--db", self.db,
                         "--usable", self.u, "--needs-attention", self.a)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.grade()
        self.assertEqual(r.returncode, 2)
        out = json.loads(r.stdout)
        self.assertEqual(out["status"], "suspended")
        self.assertEqual(out["reason"], "range_endpoints_reversed")

    def _ops_bad(self) -> Path:
        ops = [{"operation": "move", "volume_id": "JIA",
                "page_id": "Rf", "before_page_id": "Ls"}]
        p = self.tmp / "ops.jsonl"
        p.write_text("".join(json.dumps(o) + "\n" for o in ops), encoding="utf-8")
        return p


if __name__ == "__main__":
    unittest.main()
