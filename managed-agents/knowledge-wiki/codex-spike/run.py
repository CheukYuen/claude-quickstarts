#!/usr/bin/env python3
"""Small, inspectable knowledge-wiki pipeline. Python orchestrates; Codex edits files.

No SDK, vector database, training, or Claude services. Each call starts a new CLI
session. The Codex runtime, not this file, executes the model's tool calls.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

from prepare_corpus import prepare

HERE = Path(__file__).resolve().parent
QUESTIONS = [
    ("q01", "按这批资料，青海中利收购从拟议、签约到完成经历了哪些时间节点？分别给出来源和页码。"),
    ("q02", "股权收购对价、承接债务或其他义务、会计负债分别是什么口径？能否直接相加？资料哪里有说明，哪里仍不明确？"),
    ("q03", "语料中的光棒产能、销量、售价和利润数字分别来自公司披露还是DBS估计？哪些是已有经营数字，哪些带有预测假设？"),
    ("q04", "仅靠这批资料，能否确认交割后的实际产量、实际毛利和最终现金支出？若不能，缺哪些证据？"),
]


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hashes(root: Path) -> dict:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"Unexpected symlink in artifact: {path}")
        if path.is_file():
            result[str(path.relative_to(root))] = sha256(path)
    return result


def prompt(name: str, **values) -> str:
    text = (HERE / "prompts" / f"{name}.md").read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def inspect_events(path: Path) -> dict:
    """Observe CLI events; never execute anything described by an event."""
    result = {"thread_id": None, "completed": False, "usage": {}, "tools": []}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("type") == "thread.started":
            result["thread_id"] = event.get("thread_id")
        elif event.get("type") == "turn.completed":
            result["completed"] = True
            result["usage"] = event.get("usage", {})
        elif event.get("type") == "turn.failed":
            result["completed"] = False
        elif event.get("type") == "item.completed":
            item = event.get("item", {})
            if item.get("type") in {"command_execution", "file_change", "mcp_tool_call", "web_search"}:
                result["tools"].append(item)
    return result


class Pipeline:
    def __init__(self, run_dir: Path, args):
        self.run_dir = run_dir
        self.args = args
        self.deadline = time.monotonic() + args.minutes * 60
        self.records = []
        self.lock = threading.Lock()

    def call(self, stage: str, workspace: Path, task: str, read_only=False) -> dict:
        """Launch a fresh Codex runtime; persist inputs, outputs, events and changes."""
        log = self.run_dir / "logs" / stage
        log.mkdir(parents=True)
        (log / "prompt.md").write_text(task, encoding="utf-8")
        before = tree_hashes(workspace)
        # Keep existing CLI auth/model. Disable memory/apps/plugins/subagents for
        # this invocation, so research history is not intentionally injected.
        command = [
            self.args.codex, "exec", "--ephemeral", "--json", "--color", "never",
            "--sandbox", "read-only" if read_only else "workspace-write",
            "--disable", "memories", "--disable", "apps", "--disable", "plugins",
            "--disable", "multi_agent", "--config", 'approval_policy="never"',
            "--config", f'model_reasoning_effort="{self.args.effort}"',
            "-C", str(workspace), "--output-last-message", str(log / "final.md"), "-",
        ]
        record = {"stage": stage, "workspace": str(workspace), "command": command,
                  "sandbox": "read-only" if read_only else "workspace-write",
                  "before": before, "status": "running"}
        write_json(log / "record.json", record)
        started = time.monotonic()
        remaining = min(self.args.stage_timeout, self.deadline - started)
        if remaining <= 0:
            raise TimeoutError("Overall experiment time budget exhausted")
        print(f"START {stage} ({record['sandbox']})", flush=True)
        with (log / "events.jsonl").open("w", encoding="utf-8") as events, \
             (log / "stderr.log").open("w", encoding="utf-8") as errors:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=events,
                                       stderr=errors, text=True, start_new_session=True)
            try:
                process.communicate(input=task, timeout=remaining)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                record["status"] = "timed_out"
            record["exit_code"] = process.returncode
        record["seconds"] = round(time.monotonic() - started, 2)
        record.update(inspect_events(log / "events.jsonl"))
        after = tree_hashes(workspace)
        record["after"] = after
        record["changed"] = sorted(key for key in before.keys() | after.keys()
                                   if before.get(key) != after.get(key))
        if record["status"] != "timed_out":
            record["status"] = "completed" if record["exit_code"] == 0 and record["completed"] else "failed"
        if read_only and record["changed"]:
            record["status"] = "unexpected_write"
        write_json(log / "record.json", record)
        with self.lock:
            self.records.append(record)
            write_json(self.run_dir / "stages.json", sorted(self.records, key=lambda x: x["stage"]))
        print(f"END   {stage}: {record['status']} ({record['seconds']}s)", flush=True)
        if record["status"] != "completed":
            raise RuntimeError(f"{stage}: {record['status']}; inspect {log / 'stderr.log'}")
        if not (log / "final.md").is_file():
            raise RuntimeError(f"{stage}: CLI did not write a final answer")
        return record

    def extract(self, source: dict) -> Path:
        source_id = source["id"]
        workspace = self.run_dir / "extract" / source_id
        (workspace / "input").mkdir(parents=True)
        input_path = workspace / "input" / "source.md"
        shutil.copyfile(source["normalized_path"], input_path)
        input_hash = sha256(input_path)
        self.call(f"extract-{source_id}", workspace, prompt("extract", SOURCE_ID=source_id))
        if sha256(input_path) != input_hash:
            raise RuntimeError(f"{source_id}: input was modified")
        for folder in ("sources", "open_questions"):
            expected = workspace / "notes" / folder / f"{source_id}.md"
            if not expected.is_file() or not expected.read_text(encoding="utf-8").strip():
                raise RuntimeError(f"Missing extractor output: {expected}")
        return workspace / "notes"

    def run(self):
        sources = prepare(self.run_dir)
        # Fix questions before any model output is available.
        write_json(self.run_dir / "questions.json", dict(QUESTIONS))
        source_pages = {s["id"]: sorted(set(int(p) for p in re.findall(
            r"\[PAGE:\s*(\d+)\]", Path(s["normalized_path"]).read_text(encoding="utf-8"))))
            for s in sources}
        write_json(self.run_dir / "page-inventory.json", source_pages)
        if self.args.prepare_only:
            return {"status": "prepared_only", "sources": len(sources)}

        # Independent notes avoid shared-file overwrites during extraction.
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.args.workers) as pool:
            notes = list(pool.map(self.extract, sources))
        v0 = self.run_dir / "wiki-v0"
        v0.mkdir()
        for directory in notes:
            for file in directory.rglob("*.md"):
                dest = v0 / file.relative_to(directory)
                if dest.exists():
                    raise RuntimeError(f"Source note collision: {dest}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, dest)
        # Queryable provenance inventory excludes raw paths. The full manifest
        # stays outside the query workspace for human source readback.
        write_json(v0 / "_sources.json", [
            {key: s[key] for key in ("id", "title", "date", "kind")} | {"pages": source_pages[s["id"]]}
            for s in sources])
        frozen = {"wiki-v0": tree_hashes(v0)}
        source_hashes = tree_hashes(v0 / "sources")

        v1 = self.run_dir / "wiki-v1"
        shutil.copytree(v0, v1)
        self.call("resolve", v1, prompt("resolve"))
        if not (v1 / "resolutions.md").is_file():
            raise RuntimeError("Resolver did not create resolutions.md")
        if tree_hashes(v1 / "sources") != source_hashes:
            raise RuntimeError("Resolver changed immutable source notes")
        frozen["wiki-v1"] = tree_hashes(v1)

        v2 = self.run_dir / "wiki-v2"
        shutil.copytree(v1, v2)
        self.call("consolidate", v2, prompt("consolidate"))
        for name in ("_index.md", "metrics.md", "timeline.md", "escalations.md"):
            if not (v2 / name).is_file():
                raise RuntimeError(f"Consolidator did not create {name}")
        if not list((v2 / "entities").glob("*.md")):
            raise RuntimeError("Consolidator did not create entity pages")
        if tree_hashes(v2 / "sources") != source_hashes:
            raise RuntimeError("Consolidator changed immutable source notes")
        frozen["wiki-v2"] = tree_hashes(v2)
        self.validate_citations(v2, source_pages)

        def query(item):
            key, question = item
            workspace = self.run_dir / "query" / key
            shutil.copytree(v2, workspace / "wiki")
            return self.call(key, workspace, prompt("query", QUESTION=question), read_only=True)

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.args.workers) as pool:
            list(pool.map(query, QUESTIONS))
        for name, hashes in frozen.items():
            if tree_hashes(self.run_dir / name) != hashes:
                raise RuntimeError(f"Frozen snapshot changed: {name}")
        write_json(self.run_dir / "snapshots.json", frozen)
        return {"status": "runtime_complete", "sources": len(sources), "questions": len(QUESTIONS),
                "checks": {"source_notes_preserved": True, "snapshots_unchanged": True,
                           "query_workspaces_unchanged": True, "citation_source_pages_exist": True},
                "acceptance": "Program checks only; semantic correctness requires source readback."}

    @staticmethod
    def validate_citations(wiki: Path, source_pages: dict):
        found = 0
        for file in wiki.rglob("*.md"):
            for source_id, page in re.findall(r"\[(S\d{2}) p(\d+)\]", file.read_text(encoding="utf-8")):
                found += 1
                if int(page) not in source_pages.get(source_id, []):
                    raise RuntimeError(f"Unknown source/page [{source_id} p{page}] in {file}")
        if not found:
            raise RuntimeError("Wiki contains no recognized source citations")

    def report(self, outcome: dict):
        write_json(self.run_dir / "result.json", outcome)
        lines = ["# Codex knowledge-wiki experiment", "", f"Status: `{outcome['status']}`", "",
                 "## Program / model boundary", "",
                 "Python selects files, launches fresh CLI sessions, copies versions, saves traces and checks hashes.",
                 "The model extracts, resolves, organizes, searches and answers. Codex runtime executes its tools.", "",
                 "## Executed stages", "", "| Stage | Status | Seconds | Input / cached / output tokens | Tools |",
                 "|---|---|---:|---|---:|"]
        for item in sorted(self.records, key=lambda x: x["stage"]):
            usage = item["usage"]
            tokens = " / ".join(str(usage.get(k, "unknown")) for k in
                                ("input_tokens", "cached_input_tokens", "output_tokens"))
            lines.append(f"| {item['stage']} | {item['status']} | {item['seconds']} | {tokens} | {len(item['tools'])} |")
        lines += ["", "Token fields are CLI-reported; cached tokens may be included in input tokens. No price estimate.",
                  "", "## Answers (model generated; inspect citations)", ""]
        for key, question in QUESTIONS:
            answer = self.run_dir / "logs" / key / "final.md"
            lines += [f"### {key}: {question}", "", answer.read_text(encoding="utf-8") if answer.exists() else "Not executed.", ""]
        lines += ["## Limits", "", "A successful CLI turn and valid source IDs/pages do not prove correct research.",
                  "Read-only constrains writes; source-only reading also depends on instructions and trace review.",
                  "Only the explicitly selected source pages are in scope. This is not formal research acceptance."]
        (self.run_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=30)
    parser.add_argument("--stage-timeout", type=float, default=300)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "ultra"), default="medium")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    if args.minutes <= 0 or args.stage_timeout <= 0 or args.workers < 1:
        parser.error("minutes/stage-timeout/workers must be positive")
    if not args.prepare_only and not shutil.which(args.codex):
        parser.error("Codex CLI not found; run codex login first")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = HERE / "runs" / stamp
    run_dir.mkdir(parents=True)
    write_json(run_dir / "run.json", {"started_utc": stamp, "settings": vars(args),
                                      "model": "Existing Codex CLI configured model; no model override"})
    print(f"RUN_DIR={run_dir}", flush=True)
    pipeline = Pipeline(run_dir, args)
    try:
        outcome = pipeline.run()
    except Exception as error:
        pipeline.report({"status": "incomplete", "error": str(error)})
        print(f"INCOMPLETE: {error}\nInspect {run_dir / 'REPORT.md'}", file=sys.stderr)
        return 1
    pipeline.report(outcome)
    print(f"RESULT={run_dir / 'REPORT.md'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
