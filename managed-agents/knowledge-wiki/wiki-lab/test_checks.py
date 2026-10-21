"""Bounded failure-path checks; no model calls or source corpus required."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import checks


SOURCES = [{"id": "S01", "pages": [1, 2, 3], "date": "2026-09-13"},
           {"id": "S02", "pages": [4, 5], "date": "2026-09-20"}]


class GateTests(unittest.TestCase):
    def test_compound_citations_never_drop_invalid_terms(self) -> None:
        text = "[S01 pp1–3; S02 p4, p5]"
        self.assertEqual(checks.check_references(text, SOURCES), [])
        self.assertEqual(set(checks.parse_references(text)[0]),
                         {("S01", 1), ("S01", 2), ("S01", 3), ("S02", 4), ("S02", 5)})
        for navigation in ("[S01](../../sources/S01.c01.md)",
                           "[S01.c01](../../sources/S01.c01.md)",
                           "[S01 来源笔记](../../sources/S01.c01.md)"):
            self.assertEqual(checks.parse_references(navigation), ([], []))
        self.assertEqual(checks.check_references("[S01 p1](source.md)", SOURCES), [])
        for broken in ("[S01 p1; S02 p99]", "[S01 p1; S99 p1]", "[S01 p1 and S02 p4]",
                       "[S01 p1, nonsense]", "[S01 p1", "[S01 pp3-1]", "[S01 p0]",
                       "[S01]", "[S01.c01]", "[S01 来源笔记]", "[S01]()",
                       "[S01 任意文字](source.md)", "[S01 p99](source.md)",
                       "[S01 p1; S02 p99](source.md)"):
            with self.subTest(citation=broken):
                self.assertTrue(checks.check_references(broken, SOURCES))

    def test_unapproved_changes_and_immutable_deletes_fail(self) -> None:
        before = {"sources/S01.c01.md": "a", "entities/one.md": "old"}
        after = {"sources/S01.c01.md": "tampered", "entities/one.md": "new"}
        self.assertEqual(len(checks.check_changes(before, after, ["entities/"])), 1)
        self.assertFalse(checks.path_allowed("entities-evil/one.md", ["entities/"]))
        self.assertFalse(checks.path_allowed("entities/../sources/x.md", ["entities/"]))
        self.assertFalse(checks.path_allowed("/entities/x.md", ["entities/"]))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = checks.check_wiki(root, SOURCES, immutable={"sources/deleted.md": "hash"})
            self.assertFalse(result["ok"])

    def test_extraction_rejects_partial_coverage_and_cross_block_citation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = {
                "sources/S01.c01.md": "# 来源\n公司:trigiant\n- 该事件 [S01 p1]\n",
                "mentions/S01.c01.json": [{"name": "One", "name_zh": "甲", "role": "标的", "identifiers": {}, "facts": [], "cite": "[S01 p1]"}],
                "gaps/S01.c01.json": [{"id": "g1", "question": "何时完成", "cite": "[S01 p1]", "likely_source": "完成公告", "status": "没查完", "checked_scope": []}],
                "coverage/S01.c01.json": {"chunk_id": "S01.c01", "pages": [{"id": "S01", "pages": [1, 2]}], "important_items": ["事件"], "omissions": []},
            }
            for name, value in data.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), encoding="utf-8")
            (root / "input").mkdir()
            (root / "input/source.md").write_text("Raw input may contain malformed example [S999 q1] and must not be scanned")
            (root / "input/chunk.json").write_text("{}")
            chunk = {"id": "S01.c01", "sources": [{"id": "S01", "pages": [1, 2]}]}
            self.assertTrue(checks.check_extraction(root, chunk, SOURCES)["ok"])
            # Real S34.c01 failure: the model wrote a bare page array. Reject it
            # as a schema error instead of crashing on integer.get(...).
            (root / "coverage/S01.c01.json").write_text(json.dumps({**data["coverage/S01.c01.json"], "pages": [1, 2]}))
            malformed = checks.check_extraction(root, chunk, SOURCES)
            self.assertFalse(malformed["ok"])
            self.assertTrue(any("coverage pages must contain {id,pages}" in error for error in malformed["errors"]))
            data["coverage/S01.c01.json"]["pages"][0]["pages"] = [1]
            (root / "coverage/S01.c01.json").write_text(json.dumps(data["coverage/S01.c01.json"]))
            (root / "sources/S01.c01.md").write_text("公司:trigiant\n事实 [S02 p4]")
            result = checks.check_extraction(root, chunk, SOURCES)
            self.assertFalse(result["ok"])
            self.assertTrue(any("coverage pages" in error for error in result["errors"]))
            self.assertTrue(any("not in this input block" in error for error in result["errors"]))

    def test_entity_alias_conflict_dates_and_dangling_links(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "entities").mkdir()
            for slug, alias in (("one", "Shared"), ("two", " shared ")):
                metadata = {"name": slug, "canonical": slug, "type": "company",
                            "aliases": [{"text": alias, "kind": "former_name", "valid_to": None, "note": "本批未明示", "cite": "[S01 p1]"}],
                            "relations": [], "candidate_links": []}
                (root / "entities" / f"{slug}.md").write_text("---\n" + json.dumps(metadata) + "\n---\n- 实体事实 [S01 p1]\n")
            (root / "market").mkdir()
            (root / "market/sample.md").write_text("市场隔离内容 [S01 p1]\n")
            (root / "_index.md").write_text("[[missing]]\n[逃逸](../outside.md)\n[隔离内容](market/sample.md)\n")
            result = checks.check_wiki(root, SOURCES)
            self.assertFalse(result["ok"])
            self.assertTrue(any("conflict" in error for error in result["errors"]))
            self.assertTrue(any("broken link" in error for error in result["errors"]))
            self.assertTrue(any("escapes" in error for error in result["errors"]))
            self.assertTrue(any("non-market page links into market/" in error for error in result["errors"]))
            metadata["aliases"][0].pop("note")
            (root / "entities/two.md").write_text("---\n" + json.dumps(metadata) + "\n---\n")
            self.assertTrue(any("null valid_to" in error for error in checks.check_entities(root, SOURCES)))
            nested = root / "entities/company-pages/overview.md"
            nested.parent.mkdir()
            nested.write_text("公司衍生页面不能藏在实体目录 [S01 p1]\n")
            self.assertTrue(any("entities/company-pages" in error and "nested content is forbidden" in error
                                for error in checks.check_entities(root, SOURCES)))

    def test_absence_claim_requires_original_readback(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "gaps").mkdir()
            gap = {"id": "g1", "question": "多少", "cite": "[S01 p1]", "likely_source": "后续披露", "status": "资料里没有", "checked_scope": ["S01 p1"]}
            path = root / "gaps/g1.json"
            path.write_text(json.dumps([gap]))
            self.assertFalse(checks.check_wiki(root, SOURCES)["ok"])
            gap["raw_checked"] = True
            path.write_text(json.dumps([gap]))
            self.assertTrue(checks.check_wiki(root, SOURCES)["ok"])

    def test_consolidated_gap_schema_and_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "companies/one/gaps.md"
            path.parent.mkdir(parents=True)
            gap = {"id": "g1", "question": "何时完成", "status": "没查完", "checked_scope": ["来源笔记"], "answer": None, "next_source": "后续完成公告"}
            def write() -> None:
                path.write_text("# 缺口\n```json\n" + json.dumps(gap) + "\n```\n")
            write()
            self.assertTrue(checks.check_wiki(root, SOURCES)["ok"])
            gap["status"] = "已解决"
            write()
            self.assertFalse(checks.check_wiki(root, SOURCES)["ok"])
            gap["answer"] = "已完成"
            gap["cite"] = "[S01 p1]"
            write()
            self.assertTrue(checks.check_wiki(root, SOURCES)["ok"])

    def test_resolver_schema_absence_and_page_budget_are_checked(self) -> None:
        valid = {"page_budget": 1, "raw_pages_read": [{"id": "S01", "pages": [1]}],
                 "items": [{"gap_id": "g1", "status": "资料里没有", "checked_scope": [{"id": "S01", "pages": [1]}], "answer": "仅已查S01 p1未见后续完成披露", "cite": "[S01 p1]", "next_source": "后续完成公告", "raw_checked": True}]}
        def check(value: object) -> dict:
            return checks.check_resolutions(value, SOURCES, page_budget=1, allowed_pages=[("S01", 1)])
        self.assertTrue(check(valid)["ok"])
        malformed = [None, [], {}, {"page_budget": True, "raw_pages_read": "wrong", "items": [None, {"gap_id": [], "status": [], "checked_scope": 4, "answer": {}, "cite": [], "next_source": []}]}]
        for value in malformed:
            with self.subTest(value=value):
                self.assertFalse(check(value)["ok"])
        over = json.loads(json.dumps(valid))
        over["raw_pages_read"][0]["pages"] = [1, 2]
        errors = check(over)["errors"]
        self.assertTrue(any("allowlist" in error for error in errors))
        self.assertTrue(any("budget" in error for error in errors))
        absence = json.loads(json.dumps(valid))
        absence["items"][0]["checked_scope"] = ["来源笔记"]
        self.assertFalse(check(absence)["ok"])
        absence["items"][0]["checked_scope"] = ["[S01 p2]"]
        self.assertTrue(any("exceeds declared" in error for error in check(absence)["errors"]))
        absence["items"][0]["checked_scope"] = ["S01 p1"]
        absence["items"][0]["raw_checked"] = False
        self.assertFalse(check(absence)["ok"])

    def test_symlinks_fail_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "escape.md").symlink_to("/etc/hosts")
            result = checks.check_wiki(root, SOURCES)
            self.assertFalse(result["ok"])
            self.assertIn("Symlink", result["errors"][0])


if __name__ == "__main__":
    unittest.main()
