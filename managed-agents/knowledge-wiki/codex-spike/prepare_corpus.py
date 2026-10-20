"""Copy selected original pages into an auditable, bounded input corpus.

This module contains no model calls and creates no research conclusions.
Existing page text is copied verbatim; DBS pages use local pdftotext.
Only the supplied run directory is written. Source PDFs are never copied.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List


CONFIG_PATH = Path(__file__).with_name("corpus_config.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _check_hash(path: Path, expected: str) -> None:
    actual = _sha256(path)
    if actual != expected:
        raise ValueError("Source hash mismatch: {} (expected {}, got {})".format(
            path, expected, actual))


def prepare(run_dir: Path) -> List[Dict]:
    """Verify source bindings and write corpus/*.md plus a JSON manifest.

    Requires pdftotext on PATH for the two selected DBS pages. Every configured
    PDF and existing source text must retain its pinned SHA-256. A new run_dir
    is recommended; existing generated corpus files are never overwritten.
    """
    run_dir = Path(run_dir).expanduser().resolve()
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    records_path = Path(config["source_records_path"])
    _check_hash(records_path, config["source_records_sha256"])
    pdftotext = shutil.which("pdftotext")
    if not pdftotext:
        raise RuntimeError("pdftotext is required; install Poppler before preparing the corpus")

    # Preflight every input before writing any output. Hash checks establish file
    # identity; they do not establish factual correctness or research acceptance.
    for source in config["sources"]:
        _check_hash(Path(source["pdf_path"]), source["pdf_sha256"])
        if source.get("text_dir"):
            for page in source["pages"]:
                text_path = Path(source["text_dir"]) / "p{:04d}.txt".format(page)
                _check_hash(text_path, source["text_sha256"][str(page)])
        output_path = run_dir / "corpus" / (source["id"] + ".md")
        if output_path.exists():
            raise FileExistsError("Use a new run directory: {}".format(output_path))
    manifest_path = run_dir / "corpus-manifest.json"
    if manifest_path.exists():
        raise FileExistsError("Use a new run directory: {}".format(manifest_path))

    corpus_dir = run_dir / "corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for source in config["sources"]:
        text_paths = []
        text_hashes = {}
        page_records = []
        body = [
            "# {}".format(source["title"]),
            "", "Source ID: {}".format(source["id"]),
            "Original source ID: {}".format(source["original_source_id"]),
            "Date: {}".format(source["date"]),
            "Kind: {}".format(source["kind"]),
            "Original PDF: {}".format(source["pdf_path"]),
            "Original PDF SHA-256: {}".format(source["pdf_sha256"]),
            "Selected PDF pages: {} / {}".format(
                ", ".join(map(str, source["pages"])), source["pdf_page_count"]),
            "Text method: {}".format(source["text_method"]),
            "Input is original page text, not a research summary.",
        ]
        if source.get("selection_note"):
            body.append("Selection limit: {}".format(source["selection_note"]))
        for page in source["pages"]:
            if source.get("text_dir"):
                text_path = Path(source["text_dir"]) / "p{:04d}.txt".format(page)
                original_text = text_path.read_text(encoding="utf-8")
            else:
                completed = subprocess.run(
                    [pdftotext, "-layout", "-f", str(page), "-l", str(page),
                     source["pdf_path"], "-"],
                    check=True, capture_output=True, text=True, encoding="utf-8",
                )
                original_text = completed.stdout
                text_path = run_dir / "source-text" / source["id"] / "p{:04d}.txt".format(page)
                text_path.parent.mkdir(parents=True, exist_ok=True)
                text_path.write_text(original_text, encoding="utf-8")
            if not original_text.strip():
                raise ValueError("Empty source text: {} page {}".format(source["id"], page))
            text_paths.append(str(text_path))
            text_hashes[str(text_path)] = _sha256(text_path)
            page_records.append({"page": page, "text_path": str(text_path),
                                 "text_sha256": text_hashes[str(text_path)],
                                 "chars": len(original_text)})
            body.extend([
                "", "---", "",
                "[SOURCE: {} | PDF_PAGE: {} | DATE: {}]".format(
                    source["id"], page, source["date"]),
                "[PAGE: {}]".format(page),
                "[TEXT_PATH: {}]".format(text_path), "", original_text,
            ])
        # Wrappers and page delimiters are deterministic. The page text itself
        # is not rewritten, corrected, translated, summarized, or truncated.
        normalized_text = "\n".join(body) + "\n"
        normalized_path = corpus_dir / (source["id"] + ".md")
        normalized_path.write_text(normalized_text, encoding="utf-8")
        manifest.append({
            "id": source["id"], "title": source["title"],
            "original_source_id": source["original_source_id"],
            "date": source["date"], "kind": source["kind"],
            "pdf_path": source["pdf_path"], "pdf_sha256": source["pdf_sha256"],
            "pdf_hash_status": "verified_against_pinned_config",
            "source_url": source.get("source_url"),
            "pdf_page_count": source["pdf_page_count"], "pages": source["pages"],
            "text_method": source["text_method"], "text_paths": text_paths,
            "text_sha256": text_hashes, "page_records": page_records,
            "normalized_path": str(normalized_path),
            "normalized_sha256": _sha256(normalized_path),
            "chars": len(normalized_text),
            "selection_note": source.get("selection_note"),
        })
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    result = prepare(args.run_dir)
    print(json.dumps({"sources": len(result), "chars": sum(row["chars"] for row in result),
                      "manifest": str(args.run_dir.resolve() / "corpus-manifest.json")},
                     ensure_ascii=False, indent=2))
