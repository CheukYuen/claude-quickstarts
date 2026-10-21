"""Runner transition checks using real temporary Git repos and no model calls."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import sys
from unittest import mock

_SPEC = importlib.util.spec_from_file_location("wiki_lab_run_tests", Path(__file__).with_name("run.py"))
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)


class RunnerTransitions(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        corpus = self.root / "corpus/chunks/S01.c01.md"
        corpus.parent.mkdir(parents=True)
        corpus.write_text("[SOURCE: S01]\n[PAGE: 1]\nFixture original page.\n")
        self.chunk = {"id": "S01.c01", "path": str(corpus), "sha256": runner.sha(corpus),
                      "sources": [{"id": "S01", "pages": [1]}], "tags": ["公司:trigiant"]}
        from corpus import CUTOFF, P1_IDS
        self.sources = [{"id": f"S{index:02d}", "source_id": source_id, "date": "2026-09-13", "pages": [1]}
                        for index, source_id in enumerate(P1_IDS, 1)]
        runner.write_json(self.root / "corpus-manifest.json", {"scope": "p1", "cutoff": CUTOFF,
                          "sources": self.sources, "manifest_sha256": "fixture-binding"})
        runner.write_json(self.root / "chunks.json", [self.chunk])
        self.pipeline = runner.Pipeline(self.root, SimpleNamespace(codex="unused", stage_timeout=1))

    def log_record(self, stage: str, task: str = "") -> dict:
        log = self.root / "logs" / stage / "attempt-01"
        record = {"stage": stage, "status": "completed", "prompt_sha256": runner.hashlib.sha256(task.encode()).hexdigest()}
        runner.write_json(log / "record.json", record)
        return {"log": str(log), **record}

    def seed_wiki(self) -> tuple[str, dict]:
        note = self.pipeline.wiki / "sources/S01.c01.md"
        note.parent.mkdir(parents=True)
        note.write_text("# 原始笔记\n公司:trigiant\n- 原始事实 [S01 p1]\n")
        runner.write_json(self.pipeline.wiki / "_sources.json", self.sources)
        runner.git(self.pipeline.wiki, "init", "-b", "main")
        head = self.pipeline.commit("Fixture accepted source")
        before = runner.hashes(self.pipeline.wiki)
        runner.write_json(self.root / "source-note-hashes.json", {"sources/S01.c01.md": before["sources/S01.c01.md"]})
        runner.write_json(self.root / "accepted/aggregate.json", {"commit": head, "outputs": before})
        return head, before

    def test_rejected_stage_preserves_main_and_records_rejected_candidate(self) -> None:
        manifest_path = self.root / "corpus-manifest.json"
        manifest = runner.load(manifest_path)
        runner.write_json(manifest_path, {**manifest, "scope": "p0"})
        try:
            with self.assertRaisesRegex(ValueError, "five-source P1 sample"):
                runner.Pipeline(self.root, SimpleNamespace(codex="unused", stage_timeout=1))
        finally:
            runner.write_json(manifest_path, manifest)
        head, before = self.seed_wiki()
        for stage, failure in (("unauthorized", "paths"), ("malformed-entity", "structure")):
            with self.subTest(failure=failure):
                def fake_call(name: str, workspace: Path, task: str, allowed: list[str]) -> dict:
                    if failure == "paths":
                        # Simulates call's existing allowlist rejecting an attempted source write.
                        (workspace / "sources/S01.c01.md").write_text("Attempted unauthorized overwrite")
                        raise RuntimeError("rejected_paths: sources/S01.c01.md")
                    path = workspace / "entities/broken.md"
                    path.parent.mkdir()
                    path.write_text("Malformed entity without required frontmatter [S01 p1]\n")
                    return self.log_record(name, task)

                self.pipeline.call = fake_call
                with self.assertRaises((RuntimeError, ValueError)):
                    self.pipeline.stage(stage, ["entities/"], "Fixture staging task")
                self.assertEqual(runner.git(self.pipeline.wiki, "rev-parse", "HEAD"), head)
                self.assertEqual(runner.git(self.pipeline.wiki, "branch", "--show-current"), "main")
                self.assertEqual(runner.hashes(self.pipeline.wiki), before)
                self.assertEqual(runner.git(self.pipeline.wiki, "status", "--porcelain"), "")
                self.assertFalse((self.root / "accepted" / f"{stage}.json").exists())
                refs = runner.git(self.pipeline.wiki, "for-each-ref", "--format=%(refname)", f"refs/heads/rejected/{stage}-").splitlines()
                # Git's for-each-ref pattern is prefix-by-component, so enumerate all refs if needed.
                if not refs:
                    refs = [ref for ref in runner.git(self.pipeline.wiki, "for-each-ref", "--format=%(refname)", "refs/heads/rejected/").splitlines()
                            if f"/rejected/{stage}-" in ref]
                self.assertEqual(len(refs), 1)
                rejection = json.loads(runner.git(self.pipeline.wiki, "show", refs[0] + ":_rejection.json"))
                self.assertEqual(rejection["stage"], stage)
                self.assertTrue(rejection["errors"])
                retained = "sources/S01.c01.md" if failure == "paths" else "entities/broken.md"
                self.assertIn("Attempted" if failure == "paths" else "Malformed", runner.git(self.pipeline.wiki, "show", refs[0] + ":" + retained))
                if failure == "structure":
                    record = runner.load(self.root / "logs" / stage / "attempt-01/record.json")
                    self.assertEqual(record["status"], "checks_rejected")

    def test_view_resume_accepts_later_approved_update_but_rejects_dirty_or_foreign_head(self) -> None:
        _, before = self.seed_wiki()
        relative = "companies/trigiant/overview.md"
        calls: list[str] = []
        def fake_call(stage: str, workspace: Path, task: str, allowed: list[str]) -> dict:
            calls.append(stage)
            page = workspace / relative
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text(f"# 小样概览\n公司:trigiant\n阶段 {stage} 的已接收摘要 [S01 p1]\n")
            return self.log_record(stage, task)
        self.pipeline.call = fake_call
        self.pipeline.stage("view-first", [relative], "Initial view")
        first = runner.load(self.root / "accepted/view-first.json")
        self.pipeline.stage("view-second", [relative], "Later accepted correction to the same view")
        latest = runner.load(self.root / "accepted/view-second.json")
        self.assertNotEqual(first["outputs"][relative], latest["outputs"][relative])
        page = self.pipeline.wiki / relative
        accepted_content = page.read_text()
        self.pipeline.stage("view-first", [relative], "Resume historical stage")
        self.assertEqual(calls, ["view-first", "view-second"])
        self.assertEqual(runner.git(self.pipeline.wiki, "rev-parse", "HEAD"), latest["commit"])
        self.assertEqual(page.read_text(), accepted_content)
        page.write_text(accepted_content + "工作区未审核改动 [S01 p1]\n")
        with self.assertRaisesRegex(ValueError, "uncommitted changes"):
            self.pipeline.stage("view-first", [relative], "Dirty resume must fail")
        page.write_text(accepted_content)
        self.assertEqual(runner.git(self.pipeline.wiki, "status", "--porcelain"), "")
        page.write_text(accepted_content + "陌生提交新增内容 [S01 p1]\n")
        foreign = self.pipeline.commit("Fixture unaccepted edit")
        self.assertNotEqual(foreign, latest["commit"])
        self.assertEqual(runner.git(self.pipeline.wiki, "status", "--porcelain"), "")
        with self.assertRaisesRegex(ValueError, "not the latest accepted stage commit"):
            self.pipeline.stage("view-first", [relative], "Unaccepted commit must fail")
        self.assertEqual(calls, ["view-first", "view-second"])
        self.assertEqual(runner.sha(self.pipeline.wiki / "sources/S01.c01.md"), before["sources/S01.c01.md"])

    def test_sequential_reviews_preserve_the_first_accepted_correction_and_model_original(self) -> None:
        spec = importlib.util.spec_from_file_location("wiki_lab_review_tests", Path(__file__).with_name("review.py"))
        review = importlib.util.module_from_spec(spec)
        # review imports this project's runner; isolate it from the spike module
        # whose directory the runner also uses for its event observer.
        with mock.patch.dict(sys.modules, {"run": runner}):
            spec.loader.exec_module(review)
        root = self.root / "runs/review-fixture"
        runner.write_json(root / "corpus-manifest.json", runner.load(self.root / "corpus-manifest.json"))
        runner.write_json(root / "chunks.json", [self.chunk])
        model = root / "extract/S01.c01/attempt-01"
        original_input = Path(self.chunk["path"]).read_text()
        (model / "input").mkdir(parents=True)
        (model / "input/source.md").write_text(original_input)
        runner.write_json(model / "input/chunk.json", self.chunk)
        note_path = "sources/S01.c01.md"
        (model / "sources").mkdir()
        (model / note_path).write_text("公司:trigiant\n- 原始主体口径 [S01 p1]\n- 原始期限口径 [S01 p1]\n")
        runner.write_json(model / "mentions/S01.c01.json", [])
        runner.write_json(model / "gaps/S01.c01.json", [])
        runner.write_json(model / "coverage/S01.c01.json", {"chunk_id": "S01.c01", "pages": [{"id": "S01", "pages": [1]}], "important_items": ["主体和期限"], "omissions": []})
        model_hashes = runner.hashes(model)
        record_path = root / "logs/extract-S01.c01/attempt-01/record.json"
        runner.write_json(record_path, {"exit_code": 0, "completed": True, "workspace": str(model),
                                       "output_hashes": model_hashes, "prompt_sha256": "fixture-prompt"})
        original_record = record_path.read_bytes()
        with mock.patch.object(review, "HERE", self.root.resolve()):
            first = review.apply_review(root, "S01.c01", {"reason": "原文回读主体修正", "replace": [
                {"path": note_path, "old": "原始主体口径", "new": "首次已修正主体口径"}]})
            first_hashes = runner.hashes(first)
            second = review.apply_review(root, "S01.c01", {"reason": "第二次仅修期限", "replace": [
                {"path": note_path, "old": "原始期限口径", "new": "第二次已修正期限口径"}]})
        final = (second / note_path).read_text()
        self.assertIn("首次已修正主体口径", final)
        self.assertIn("第二次已修正期限口径", final)
        self.assertNotIn("原始主体口径", final)
        self.assertNotIn("原始期限口径", final)
        self.assertEqual(runner.hashes(model), model_hashes)
        self.assertEqual(runner.hashes(first), first_hashes)
        self.assertEqual(record_path.read_bytes(), original_record)
        state = runner.load(root / "extract/S01.c01/success.json")
        self.assertEqual(Path(state["workspace"]), second)
        self.assertEqual(state["hashes"], runner.hashes(second))

    def install_successful_extractor(self) -> list[str]:
        calls: list[str] = []
        def fake_call(stage: str, workspace: Path, task: str, allowed: list[str]) -> dict:
            calls.append(stage)
            note = workspace / "sources/S01.c01.md"
            note.parent.mkdir()
            note.write_text("公司:trigiant\n- Fixture fact [S01 p1]\n")
            runner.write_json(workspace / "mentions/S01.c01.json", [])
            runner.write_json(workspace / "gaps/S01.c01.json", [])
            runner.write_json(workspace / "coverage/S01.c01.json", {"chunk_id": "S01.c01", "pages": [{"id": "S01", "pages": [1]}], "important_items": ["Fixture fact"], "omissions": []})
            return self.log_record(stage, task)
        self.pipeline.call = fake_call
        return calls

    def test_successful_chunk_resume_skips_model_and_rejects_prompt_or_output_tamper(self) -> None:
        calls = self.install_successful_extractor()
        self.pipeline.extract(self.chunk)
        state_path = self.root / "extract/S01.c01/success.json"
        saved_state = state_path.read_bytes()
        self.pipeline.extract(self.chunk)
        self.assertEqual(len(calls), 1)
        self.assertEqual(state_path.read_bytes(), saved_state)
        with mock.patch.object(runner, "template", return_value="Changed prompt version"):
            with self.assertRaisesRegex(RuntimeError, "Success binding changed"):
                self.pipeline.extract(self.chunk)
        state = runner.load(state_path)
        (Path(state["workspace"]) / "sources/S01.c01.md").write_text("公司:trigiant\n- Tampered but structurally valid fact [S01 p1]\n")
        with self.assertRaisesRegex(RuntimeError, "Success binding changed"):
            self.pipeline.extract(self.chunk)
        self.assertEqual(len(calls), 1)
        self.assertEqual(state_path.read_bytes(), saved_state)

    def test_report_handles_completed_and_running_calls_without_inventing_usage(self) -> None:
        completed = {"stage": "extract-S02.c01", "attempt": 1, "status": "completed", "seconds": 12,
                     "usage": {"input_tokens": 100, "cached_input_tokens": 30, "output_tokens": 20}}
        running = {"stage": "extract-S01.c01", "attempt": 1, "status": "running", "started_at": 1000}
        paths = [self.root / "logs" / record["stage"] / "attempt-01/record.json"
                 for record in (completed, running)]
        for path, record in zip(paths, (completed, running)):
            runner.write_json(path, record)
        original_records = [path.read_bytes() for path in paths]
        with mock.patch.object(runner.time, "time", return_value=1010), \
             mock.patch.object(self.pipeline, "call", side_effect=AssertionError("report must not rerun extraction")):
            self.pipeline.report()
        usage = runner.load(self.root / "usage.json")
        self.assertEqual(usage["totals"], {"input": 100, "cached": 30, "uncached": 70, "output": 20,
                                         "seconds": 22, "calls": 2, "amount": None, "usage_unreported_calls": 1})
        pending = next(record for record in usage["attempts"] if record["status"] == "running")
        self.assertNotIn("seconds", pending)
        self.assertNotIn("usage", pending)
        markdown = (self.root / "USAGE.md").read_text()
        self.assertIn("|extract-S01.c01/1|running|10|未报告|未报告|未报告|未报告|", markdown)
        self.assertIn("|extract-S02.c01/1|completed|12|100|30|70|20|", markdown)
        self.assertEqual([path.read_bytes() for path in paths], original_records)

    def test_resume_rejects_changed_canonical_input_even_if_manifest_hash_is_unchanged(self) -> None:
        calls = self.install_successful_extractor()
        self.pipeline.extract(self.chunk)
        Path(self.chunk["path"]).write_text("Changed corpus input after successful extraction")
        with self.assertRaisesRegex((ValueError, RuntimeError), "input changed|Input changed|binding changed"):
            self.pipeline.extract(self.chunk)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
