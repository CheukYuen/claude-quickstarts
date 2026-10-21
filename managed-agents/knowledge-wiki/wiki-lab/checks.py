#!/usr/bin/env python3
"""Structural gates for wiki-lab. A pass is not a factual/content acceptance.

Entity frontmatter is strict JSON between --- lines (a YAML subset), so these
checks use only the Python standard library. Paths passed to the write gate are
relative POSIX paths. Directory permissions must end in '/'.
"""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

SOURCE_ID = re.compile(r"S\d{2,}")
CITE_START = re.compile(r"\[\s*S\d+")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MARKET_WORDS = re.compile(
    r"目标(?:股)?价|评级|市盈率|市净率|估值倍数|每股作价|股价|"
    r"target\s+price|price\s+target|\b(?:P/E|P/B|EV/EBITDA)\b", re.I
)
GAP_STATUSES = {"已解决", "资料里没有", "没查完"}
TAGS = ("公司:trigiant", "公司:yofc", "行业:optical-fiber", "宏观", "市场")


def _report(errors: list[str], **extra: Any) -> dict:
    return {"ok": not errors, "errors": errors, **extra}


def source_map(sources: Any) -> dict[str, dict]:
    """Accept a source list, id->record map, or {'sources': ...} wrapper."""
    if isinstance(sources, dict) and "sources" in sources:
        return source_map(sources["sources"])
    if isinstance(sources, list):
        return {row["id"]: row for row in sources}
    if isinstance(sources, dict):
        return {key: {"id": key, **value} for key, value in sources.items()}
    raise ValueError("sources must be a list or mapping")


def _pages(record: dict) -> set[int]:
    return {int(page) for page in record.get("pages", [])}


def parse_references(text: str) -> tuple[list[tuple[str, int]], list[str]]:
    """Expand every citation candidate, including multi-source/page combinations.

    Accepted forms include [S12 p34], [S12 pp34–36], [S12 p5, p8],
    [S12 p5-7,9; S40 pp8–10]. Every '[S<digits>' candidate is consumed
    completely, except source-only navigation labels with an explicit Markdown
    target. A page-bearing link label is still a fully checked citation.
    """
    references: list[tuple[str, int]] = []
    errors: list[str] = []
    for match in CITE_START.finditer(text):
        close = text.find("]", match.start())
        newline = text.find("\n", match.start())
        if close < 0 or (newline >= 0 and newline < close):
            errors.append(f"Unterminated citation at offset {match.start()}")
            continue
        raw = text[match.start():close + 1]
        body = raw[1:-1].strip().replace("–", "-").replace("—", "-")
        # Source/chunk navigation is not a page citation. Keep this exemption
        # narrow: a bare [S01], an arbitrary label, or any p/pp label still fails
        # or receives normal source/page checking below.
        navigation_label = re.fullmatch(r"S\d{2,}(?:\.c\d{2,})?(?:\s+来源笔记)?", body)
        explicit_target = re.match(r"\((?:<[^>\n]+>|[^\s()\n]+)(?:\s+\"[^\"\n]*\")?\)", text[close + 1:])
        if navigation_label and explicit_target:
            continue
        parsed: list[tuple[str, int]] = []
        valid = True
        for segment in re.split(r"\s*;\s*", body):
            item = re.fullmatch(r"(S\d{2,})\s+(.+)", segment.strip())
            if not item:
                valid = False
                break
            sid, expression = item.groups()
            # The first page needs p/pp. Subsequent comma terms may omit it.
            for index, part in enumerate(re.split(r"\s*,\s*", expression)):
                pattern = r"pp?\s*(\d+)(?:\s*-\s*(\d+))?" if index == 0 else r"(?:pp?\s*)?(\d+)(?:\s*-\s*(\d+))?"
                page_match = re.fullmatch(pattern, part.strip())
                if not page_match:
                    valid = False
                    break
                first = int(page_match.group(1))
                last = int(page_match.group(2) or first)
                if first < 1 or last < first or last - first > 10000:
                    valid = False
                    break
                parsed.extend((sid, page) for page in range(first, last + 1))
            if not valid:
                break
        if valid:
            references.extend(parsed)
        else:
            errors.append(f"Malformed citation: {raw}")
    return references, errors


def check_references(text: str, sources: Any) -> list[str]:
    references, errors = parse_references(text)
    records = source_map(sources)
    for sid, page in sorted(set(references)):
        if sid not in records:
            errors.append(f"Unknown citation source: {sid}")
        elif page not in _pages(records[sid]):
            errors.append(f"Citation page outside selected source: {sid} p{page}")
    return errors


def tree_hashes(root: Path) -> dict[str, str]:
    hashes = {}
    for path in sorted(Path(root).rglob("*")):
        if ".git" in path.relative_to(root).parts:
            continue
        if path.is_symlink():
            raise ValueError(f"Symlink is not an artifact: {path}")
        if path.is_file():
            hashes[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _safe_relative(path: str) -> bool:
    parsed = PurePosixPath(path)
    return bool(path) and not parsed.is_absolute() and ".." not in parsed.parts and "\\" not in path and ".git" not in parsed.parts


def path_allowed(path: str, allowed_paths: Iterable[str]) -> bool:
    if not _safe_relative(path):
        return False
    return any(path.startswith(allowed) if allowed.endswith("/") else path == allowed
               for allowed in allowed_paths if _safe_relative(allowed))


def check_changes(before: dict[str, str], after: dict[str, str], allowed_paths: Iterable[str]) -> list[str]:
    """Reject unapproved writes/deletes; callers only publish a passing tree."""
    permissions = list(allowed_paths)
    return [f"Path outside stage allowlist: {path}" for path in sorted(before.keys() | after.keys())
            if before.get(path) != after.get(path) and not path_allowed(path, permissions)]


def _read_json(path: Path, errors: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        errors.append(f"{path.name}: invalid/missing JSON: {exc}")
        return None


def _nonempty(value: Any) -> bool:
    return bool(value) and (not isinstance(value, str) or bool(value.strip()))


def _check_gaps(value: Any, label: str, sources: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, list):
        return [f"{label}: gaps must be an array"]
    seen = set()
    for index, row in enumerate(value):
        item = f"{label}[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{item}: expected object")
            continue
        for key in ("id", "question", "cite", "likely_source", "status", "checked_scope"):
            if key not in row:
                errors.append(f"{item}: missing {key}")
        for key in ("id", "question", "cite", "likely_source"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                errors.append(f"{item}: {key} must be a nonempty string")
        if isinstance(row.get("id"), str):
            if row["id"] in seen:
                errors.append(f"{item}: duplicate gap id")
            seen.add(row["id"])
        if not isinstance(row.get("status"), str) or row["status"] not in GAP_STATUSES:
            errors.append(f"{item}: invalid status")
        if not isinstance(row.get("checked_scope"), (list, str)):
            errors.append(f"{item}: checked_scope must be a list or string")
        if row.get("status") == "资料里没有" and (not _nonempty(row.get("checked_scope")) or row.get("raw_checked") is not True):
            errors.append(f"{item}: absence requires checked_scope and raw_checked=true")
        if row.get("status") == "已解决" and not _nonempty(row.get("answer")):
            errors.append(f"{item}: resolved gap requires answer")
        cite = row.get("cite", "")
        if isinstance(cite, str):
            if not parse_references(cite)[0]:
                errors.append(f"{item}: cite must contain a source/page citation")
            errors.extend(f"{item}: {error}" for error in check_references(cite, sources))
    return errors


def _check_page_gaps(text: str, label: str, sources: Any) -> list[str]:
    """Check the simpler, consolidated gap schema in company/industry pages."""
    errors: list[str] = []
    blocks = re.findall(r"```json\s*\n(.*?)\n```", text, re.S)
    if not blocks:
        # A page may honestly have no candidate gaps; it must say so explicitly.
        if not any(word in text for word in ("无新增缺口", "未发现新增缺口", "没有新增缺口")):
            errors.append(f"{label}: gaps page needs JSON gap objects or explicit no-new-gap statement")
        return errors
    seen = set()
    for index, block in enumerate(blocks):
        item = f"{label} gap[{index}]"
        try:
            value = json.loads(block)
        except ValueError as exc:
            errors.append(f"{item}: invalid JSON: {exc}")
            continue
        if not isinstance(value, dict):
            errors.append(f"{item}: expected one gap object per JSON block")
            continue
        for key in ("id", "question", "status", "checked_scope", "answer", "next_source"):
            if key not in value:
                errors.append(f"{item}: missing {key}")
        for key in ("id", "question"):
            if not isinstance(value.get(key), str) or not value[key].strip():
                errors.append(f"{item}: {key} must be a nonempty string")
        if isinstance(value.get("id"), str):
            if value["id"] in seen:
                errors.append(f"{item}: duplicate gap id")
            seen.add(value["id"])
        status = value.get("status")
        if not isinstance(status, str) or status not in GAP_STATUSES:
            errors.append(f"{item}: invalid status")
        if not isinstance(value.get("checked_scope"), list):
            errors.append(f"{item}: checked_scope must be an array")
        if status == "资料里没有" and (not value.get("checked_scope") or value.get("raw_checked") is not True):
            errors.append(f"{item}: absence requires checked_scope and raw_checked=true")
        if status == "已解决":
            if not _nonempty(value.get("answer")):
                errors.append(f"{item}: resolved gap requires answer")
            cite = value.get("cite", "")
            if not isinstance(cite, str) or not parse_references(cite)[0]:
                errors.append(f"{item}: resolved gap requires cite")
        elif not _nonempty(value.get("next_source")):
            errors.append(f"{item}: unresolved gap requires next_source")
    return errors



def check_resolutions(value: Any, sources: Any, *, page_budget: int,
                      allowed_pages: Iterable[tuple[str, int]]) -> dict:
    """Validate the bounded resolver receipt without trusting its self-report.

    Source/page existence and declared read scope can be checked here. Whether a
    tool actually read that page remains a trace/readback question.
    """
    errors: list[str] = []
    if type(page_budget) is not int or page_budget < 0:
        return _report(["Resolver page_budget argument must be a nonnegative integer"])
    try:
        records = source_map(sources)
        allowed = set()
        for entry in allowed_pages:
            sid, page = entry
            if not isinstance(sid, str) or type(page) is not int or page < 1:
                raise ValueError("expected (source id, positive PDF page) pairs")
            allowed.add((sid, page))
    except (TypeError, ValueError, KeyError) as exc:
        return _report([f"Invalid resolver source/allowed-pages binding: {exc}"])
    if not isinstance(value, dict):
        return _report(["resolutions must be an object"])
    for key in ("page_budget", "raw_pages_read", "items"):
        if key not in value:
            errors.append(f"resolutions: missing {key}")
    if type(value.get("page_budget")) is not int or value["page_budget"] != page_budget:
        errors.append("resolutions: page_budget must equal the program's fixed budget")
    raw_read: set[tuple[str, int]] = set()
    raw_rows = value.get("raw_pages_read")
    if not isinstance(raw_rows, list):
        errors.append("resolutions: raw_pages_read must be an array")
    else:
        for index, row in enumerate(raw_rows):
            label = f"raw_pages_read[{index}]"
            if not isinstance(row, dict):
                errors.append(f"{label}: expected {{id,pages}} object")
                continue
            sid, pages = row.get("id"), row.get("pages")
            if not isinstance(sid, str) or sid not in records:
                errors.append(f"{label}: invalid/unknown source id")
                continue
            if not isinstance(pages, list) or not pages:
                errors.append(f"{label}: pages must be a nonempty array")
                continue
            for page in pages:
                if type(page) is not int or page < 1:
                    errors.append(f"{label}: pages must contain positive PDF page integers")
                    continue
                pair = (sid, page)
                if pair in raw_read:
                    errors.append(f"{label}: duplicate raw page {sid} p{page}")
                raw_read.add(pair)
                if page not in _pages(records[sid]):
                    errors.append(f"{label}: page outside selected source {sid} p{page}")
                if pair not in allowed:
                    errors.append(f"{label}: raw page outside program allowlist {sid} p{page}")
    if len(raw_read) > page_budget:
        errors.append(f"Resolver exceeded raw-page budget: {len(raw_read)} > {page_budget}")
    items = value.get("items")
    if not isinstance(items, list):
        errors.append("resolutions: items must be an array")
        items = []
    seen = set()
    for index, row in enumerate(items):
        label = f"items[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{label}: expected object")
            continue
        for key in ("gap_id", "status", "checked_scope", "answer", "cite", "next_source"):
            if key not in row:
                errors.append(f"{label}: missing {key}")
        gap_id = row.get("gap_id")
        if not isinstance(gap_id, str) or not gap_id.strip():
            errors.append(f"{label}: gap_id must be a nonempty string")
        elif gap_id in seen:
            errors.append(f"{label}: duplicate gap_id")
        else:
            seen.add(gap_id)
        status = row.get("status")
        if not isinstance(status, str) or status not in GAP_STATUSES:
            errors.append(f"{label}: invalid status")
        scope = row.get("checked_scope")
        scope_pages: set[tuple[str, int]] = set()
        if not isinstance(scope, list):
            errors.append(f"{label}: checked_scope must be an array")
            scope = []
        for scope_index, entry in enumerate(scope):
            location = f"{label} checked_scope[{scope_index}]"
            if isinstance(entry, str) and entry.strip():
                references, issues = parse_references(entry)
                if not references and re.match(r"^S\d+\s+p", entry.strip()):
                    references, issues = parse_references("[" + entry.strip() + "]")
                if not references:
                    raw_name = re.search(r"(?:^|/)(S\d+)-p(\d+)\.txt$", entry)
                    if raw_name:
                        references = [(raw_name.group(1), int(raw_name.group(2)))]
                scope_pages.update(references)
                errors.extend(f"{location}: {issue}" for issue in issues)
            elif isinstance(entry, dict):
                sid, pages = entry.get("id"), entry.get("pages")
                if not isinstance(sid, str) or not isinstance(pages, list) or not pages:
                    errors.append(f"{location}: expected {{id,pages}} or a nonempty scope string")
                    continue
                for page in pages:
                    if type(page) is not int or page < 1:
                        errors.append(f"{location}: pages must contain positive integers")
                    else:
                        scope_pages.add((sid, page))
            else:
                errors.append(f"{location}: expected {{id,pages}} or a nonempty scope string")
        for sid, page in scope_pages:
            if sid not in records or page not in _pages(records[sid]):
                errors.append(f"{label}: checked_scope names an unavailable source/page {sid} p{page}")
        answer, cite, next_source = row.get("answer"), row.get("cite"), row.get("next_source")
        if answer is not None and not isinstance(answer, str):
            errors.append(f"{label}: answer must be a string or null")
        if not isinstance(cite, str):
            errors.append(f"{label}: cite must be a string (empty is allowed for an unresolved gap)")
        if next_source is not None and not isinstance(next_source, str):
            errors.append(f"{label}: next_source must be a string or null")
        if status == "已解决":
            if not isinstance(answer, str) or not answer.strip():
                errors.append(f"{label}: resolved gap requires answer")
            if not isinstance(cite, str) or not parse_references(cite)[0]:
                errors.append(f"{label}: resolved gap requires source/page cite")
        elif status in ("资料里没有", "没查完"):
            if not isinstance(next_source, str) or not next_source.strip():
                errors.append(f"{label}: unresolved gap requires next_source")
        if status == "资料里没有":
            if row.get("raw_checked") is not True or not scope_pages:
                errors.append(f"{label}: absence requires raw_checked=true and concrete raw-page checked_scope")
            if not scope_pages.issubset(raw_read):
                errors.append(f"{label}: absence checked_scope exceeds declared raw_pages_read")
            if not isinstance(answer, str) or not answer.strip():
                errors.append(f"{label}: absence requires a scoped answer")
    try:
        errors.extend(check_references(json.dumps(value, ensure_ascii=False), records))
    except (TypeError, ValueError) as exc:
        errors.append(f"resolutions: value is not valid JSON: {exc}")
    return _report(errors, raw_page_count=len(raw_read))


def _chunk_pages(chunk: dict) -> dict[str, set[int]]:
    rows = chunk.get("sources", chunk.get("pages", []))
    if isinstance(rows, dict):
        rows = [{"id": sid, "pages": pages} for sid, pages in rows.items()]
    if not isinstance(rows, list):
        raise ValueError("Page scope must be an array of {id,pages} objects")
    result: dict[str, set[int]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Page scope entries must be {id,pages} objects")
        sid, pages = row.get("id", row.get("source_id")), row.get("pages")
        if not isinstance(sid, str) or not SOURCE_ID.fullmatch(sid):
            raise ValueError("Page scope requires a short source id")
        if not isinstance(pages, list) or not pages or any(type(page) is not int or page < 1 for page in pages):
            raise ValueError("Page scope pages must be a nonempty array of positive integers")
        if sid in result:
            raise ValueError("Page scope repeats a source id")
        result[sid] = set(pages)
    return result


def check_extraction(root: Path, chunk: dict, sources: Any) -> dict:
    """Validate the four required artifacts of one block and optional market note."""
    root = Path(root)
    chunk_id = chunk.get("id", chunk.get("chunk_id"))
    if not chunk_id or not re.fullmatch(r"(?:S\d{2,}\.c\d{2,}|C\d{3,})", chunk_id):
        return _report(["Invalid/missing chunk id"])
    errors: list[str] = []
    allowed = {f"sources/{chunk_id}.md", f"mentions/{chunk_id}.json", f"gaps/{chunk_id}.json", f"coverage/{chunk_id}.json", f"market/{chunk_id}.md"}
    try:
        actual = tree_hashes(root)
    except ValueError as exc:
        return _report([str(exc)])
    # Runner keeps these immutable inputs beside outputs and hash-checks them.
    input_paths = {"input/source.md", "input/chunk.json"}
    artifacts = {path: digest for path, digest in actual.items() if path not in input_paths}
    errors.extend(f"Unexpected extraction artifact: {path}" for path in artifacts if path not in allowed)
    note = root / "sources" / f"{chunk_id}.md"
    if not note.is_file() or not note.read_text(encoding="utf-8").strip():
        errors.append("Missing/empty source note")
    else:
        text = note.read_text(encoding="utf-8")
        if not any(tag in text for tag in TAGS):
            errors.append("Source note missing level tag")
        if not parse_references(text)[0]:
            errors.append("Source note contains no citations")
        errors.extend(check_references(text, sources))
        if MARKET_WORDS.search(text):
            errors.append("Market content outside market/ in source note")
    mentions = _read_json(root / "mentions" / f"{chunk_id}.json", errors)
    if not isinstance(mentions, list):
        errors.append("mentions must be an array")
    else:
        for index, row in enumerate(mentions):
            label = f"mentions[{index}]"
            if not isinstance(row, dict):
                errors.append(f"{label}: expected object")
                continue
            for key in ("name", "name_zh", "role", "identifiers", "facts", "cite"):
                if key not in row:
                    errors.append(f"{label}: missing {key}")
            for key in ("name", "role", "cite"):
                if not isinstance(row.get(key), str) or not row[key].strip():
                    errors.append(f"{label}: {key} must be a nonempty string")
            if not isinstance(row.get("identifiers"), dict) or not isinstance(row.get("facts"), list):
                errors.append(f"{label}: identifiers must be object and facts array")
            cite = row.get("cite", "")
            if isinstance(cite, str):
                if not parse_references(cite)[0]:
                    errors.append(f"{label}: cite must contain source/page citation")
                errors.extend(f"{label}: {error}" for error in check_references(cite, sources))
    gaps = _read_json(root / "gaps" / f"{chunk_id}.json", errors)
    errors.extend(_check_gaps(gaps, "gaps", sources))
    coverage = _read_json(root / "coverage" / f"{chunk_id}.json", errors)
    if not isinstance(coverage, dict):
        errors.append("coverage must be an object")
    else:
        if coverage.get("chunk_id") != chunk_id:
            errors.append("coverage chunk_id differs from input")
        for key in ("pages", "important_items", "omissions"):
            if not isinstance(coverage.get(key), list):
                errors.append(f"coverage {key} must be an array")
        try:
            if _chunk_pages({"pages": coverage["pages"]}) != _chunk_pages(chunk):
                errors.append("coverage pages differ from the complete input block")
        except (KeyError, TypeError, ValueError):
            errors.append("coverage pages must contain {id,pages} objects")
        errors.extend(check_references(json.dumps(coverage, ensure_ascii=False), sources))
    market = root / "market" / f"{chunk_id}.md"
    if market.exists():
        errors.extend(check_references(market.read_text(encoding="utf-8"), sources))
    # A block cannot cite other blocks just because those pages exist in P1.
    selected = _chunk_pages(chunk)
    for path in artifacts:
        text = (root / path).read_text(encoding="utf-8")
        for sid, page in parse_references(text)[0]:
            if page not in selected.get(sid, set()):
                errors.append(f"{path}: citation not in this input block: {sid} p{page}")
    return _report(errors, chunk_id=chunk_id)


def _frontmatter(path: Path) -> tuple[dict | None, str, list[str]]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
    if not match:
        return None, text, ["Missing JSON frontmatter delimited by ---"]
    try:
        metadata = json.loads(match.group(1))
        if not isinstance(metadata, dict):
            raise ValueError("frontmatter must be an object")
    except ValueError as exc:
        return None, text[match.end():], [f"Invalid JSON frontmatter: {exc}"]
    return metadata, text[match.end():], []


def _date_error(row: dict, key: str, label: str, required: bool = False) -> list[str]:
    if key not in row:
        return [f"{label}: missing {key}"] if required else []
    if row[key] is None:
        return [] if _nonempty(row.get("note", row.get("reason"))) else [f"{label}: null {key} requires note/reason"]
    try:
        date.fromisoformat(row[key])
    except (TypeError, ValueError):
        return [f"{label}: {key} must be ISO date or null"]
    return []


def _names(text: str) -> set[str]:
    return {re.sub(r"\s+", " ", part).strip().casefold() for part in re.split(r"\s+/\s+", text) if part.strip()}


def check_entities(root: Path, sources: Any) -> list[str]:
    errors: list[str] = []
    names: dict[str, str] = {}
    directory = root / "entities"
    if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
        return ["entities/ must be a real directory of direct entity .md files"]
    if directory.exists():
        for child in directory.rglob("*"):
            if (len(child.relative_to(directory).parts) != 1 or child.is_symlink() or
                    not child.is_file() or child.suffix != ".md"):
                errors.append(f"{child.relative_to(root).as_posix()}: entities/ allows only direct entity .md files; nested content is forbidden")
    paths = [path for path in directory.glob("*.md") if path.is_file() and not path.is_symlink()] if directory.exists() else []
    slugs = {path.stem for path in paths}
    for path in paths:
        label = f"entities/{path.name}"
        metadata, body, issues = _frontmatter(path)
        errors.extend(f"{label}: {issue}" for issue in issues)
        if metadata is None:
            continue
        for key in ("name", "canonical", "type", "aliases", "relations", "candidate_links"):
            if key not in metadata:
                errors.append(f"{label}: missing {key}")
        if metadata.get("name") != path.stem or not SLUG.fullmatch(path.stem):
            errors.append(f"{label}: name must match a lowercase slug filename")
        if not isinstance(metadata.get("type"), str) or metadata["type"] not in {"company", "person", "organization", "fund", "government", "other"}:
            errors.append(f"{label}: invalid entity type")
        canonical = metadata.get("canonical")
        if not isinstance(canonical, str) or not canonical.strip():
            errors.append(f"{label}: canonical must be a nonempty string")
            canonical = ""
        local_names = _names(canonical)
        for key in ("aliases", "relations", "candidate_links"):
            if not isinstance(metadata.get(key), list):
                errors.append(f"{label}: {key} must be an array")
        for index, alias in enumerate(metadata.get("aliases", []) or []):
            item = f"{label} aliases[{index}]"
            if not isinstance(alias, dict):
                errors.append(f"{item}: expected object")
                continue
            for key in ("text", "kind", "cite"):
                if not isinstance(alias.get(key), str) or not alias[key].strip():
                    errors.append(f"{item}: missing/empty {key}")
            if isinstance(alias.get("text"), str):
                local_names.update(_names(alias["text"]))
                if SOURCE_ID.fullmatch(alias["text"]) or alias["text"] in {row.get("source_id") for row in source_map(sources).values()}:
                    errors.append(f"{item}: a source identifier cannot be an alias")
            if alias.get("kind") == "former_name":
                errors.extend(_date_error(alias, "valid_to", item, required=True))
            errors.extend(_date_error(alias, "valid_from", item))
            if alias.get("kind") != "former_name":
                errors.extend(_date_error(alias, "valid_to", item))
            if not parse_references(str(alias.get("cite", "")))[0]:
                errors.append(f"{item}: missing source/page citation")
        for name in local_names:
            if name in names and names[name] != label:
                errors.append(f"Alias/canonical conflict: {name!r} in {names[name]} and {label}")
            names[name] = label
        for index, relation in enumerate(metadata.get("relations", []) or []):
            item = f"{label} relations[{index}]"
            if not isinstance(relation, dict):
                errors.append(f"{item}: expected object")
                continue
            if not isinstance(relation.get("rel"), str) or not relation["rel"].strip():
                errors.append(f"{item}: missing rel")
            if not isinstance(relation.get("target"), str) or relation["target"] not in slugs:
                errors.append(f"{item}: relation target does not exist: {relation.get('target')}")
            errors.extend(_date_error(relation, "valid_from", item, required=True))
            errors.extend(_date_error(relation, "valid_to", item))
            if relation.get("valid_from") and relation.get("valid_to") and str(relation["valid_from"]) > str(relation["valid_to"]):
                errors.append(f"{item}: reversed validity dates")
            if not parse_references(str(relation.get("cite", "")))[0]:
                errors.append(f"{item}: missing source/page citation")
        for index, candidate in enumerate(metadata.get("candidate_links", []) or []):
            if not isinstance(candidate, dict) or not _nonempty(candidate.get("reason")):
                errors.append(f"{label} candidate_links[{index}]: object with reason required")
        for line in body.splitlines():
            if re.match(r"^\s*[-*]\s+", line) and not parse_references(line)[0]:
                errors.append(f"{label}: entity fact bullet lacks citation: {line[:90]}")
    return errors


def check_links(path: Path, root: Path, link_roots: Iterable[Path] = ()) -> list[str]:
    errors: list[str] = []
    text = path.read_text(encoding="utf-8")
    # Exclude fenced code so examples do not become pretend navigation links.
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    destinations = []
    for match in re.finditer(r"\[\[([^\]\n]+)\]\]", text):
        target = match.group(1).split("|", 1)[0].split("#", 1)[0]
        destinations.append((root / "entities" / f"{target}.md", match.group(0)))
    for match in re.finditer(r"!?\[[^\]\n]*\]\(([^\n]*?)\)", text):
        target = match.group(1).strip()
        if target.startswith("<"):
            target = target[1:target.find(">")]
        else:
            target = target.split(' "', 1)[0].split(" '", 1)[0]
        try:
            split = urlsplit(target)
        except ValueError:
            errors.append(f"{path.name}: malformed link URL: {target}")
            continue
        if split.scheme in {"http", "https", "mailto"} or not split.path:
            continue
        if split.scheme:
            errors.append(f"{path.name}: unsupported local link scheme: {target}")
            continue
        destinations.append((path.parent / unquote(split.path), target))
    allowed = [root.resolve(), *(Path(item).resolve() for item in link_roots)]
    for destination, label in destinations:
        resolved = destination.resolve()
        if (not path.resolve().is_relative_to(root.resolve()/'market') and
                resolved.is_relative_to(root.resolve()/'market')):
            errors.append(f"{path.name}: non-market page links into market/: {label}")
        if not any(resolved == scope or scope in resolved.parents for scope in allowed):
            errors.append(f"{path.name}: link escapes authorized roots: {label}")
        elif not resolved.exists():
            errors.append(f"{path.name}: broken link: {label}")
    return errors


def check_wiki(root: Path, sources: Any, *, immutable: dict[str, str] | None = None,
               changed_paths: Iterable[str] | None = None, allowed_paths: Iterable[str] | None = None,
               link_roots: Iterable[Path] = ()) -> dict:
    root = Path(root)
    errors: list[str] = []
    try:
        hashes = tree_hashes(root)
    except ValueError as exc:
        return _report([str(exc)])
    for path, expected in (immutable or {}).items():
        if hashes.get(path) != expected:
            errors.append(f"Immutable artifact changed/deleted: {path}")
    if changed_paths is not None and allowed_paths is not None:
        errors.extend(f"Path outside stage allowlist: {path}" for path in changed_paths if not path_allowed(path, allowed_paths))
    for name in hashes:
        path = root / name
        if path.suffix in {".md", ".json"}:
            text = path.read_text(encoding="utf-8")
            errors.extend(f"{name}: {error}" for error in check_references(text, sources))
            if path.suffix == ".md":
                errors.extend(check_links(path, root, link_roots))
                if not name.startswith("market/") and MARKET_WORDS.search(text):
                    errors.append(f"{name}: market content outside market/")
        if path.name == "gaps.md" and name.startswith(("companies/", "industries/")):
            errors.extend(_check_page_gaps(path.read_text(encoding="utf-8"), name, sources))
        if name.startswith("gaps/") and path.suffix == ".json":
            value = _read_json(path, errors)
            errors.extend(_check_gaps(value, name, sources))
    errors.extend(check_entities(root, sources))
    return _report(errors, files=len(hashes))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wiki", type=Path)
    parser.add_argument("--sources", type=Path, help="Defaults to wiki/_sources.json")
    parser.add_argument("--link-root", action="append", default=[], type=Path)
    args = parser.parse_args()
    sources = json.loads((args.sources or args.wiki / "_sources.json").read_text(encoding="utf-8"))
    result = check_wiki(args.wiki, sources, link_roots=args.link_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
