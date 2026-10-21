#!/usr/bin/env python3
"""Apply an explicit P1 view correction without another model call.

The original CLI workspace, its log receipt, and rejected Git branches remain
untouched. A separate review receipt binds the accepted correction and commit.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
from pathlib import Path
import re
import shutil
from types import SimpleNamespace
import subprocess

from checks import check_changes, check_wiki, path_allowed
from run import HERE, Pipeline, git, hashes, load, sha, write_json


def stage_policy(stage: str) -> tuple[list[str], list[str]]:
    """Keep the same P1 view boundaries and page budgets as the runner."""
    if stage in ('company-trigiant', 'company-yofc'):
        directory = 'companies/' + stage.removeprefix('company-')
        return [directory + '/'], [f'{directory}/{name}.md' for name in
                                   ('overview', 'segments', 'timeline', 'metrics', 'risks', 'gaps')]
    if stage == 'industry':
        directory = 'industries/optical-fiber'
        return [directory + '/'], [f'{directory}/{name}.md' for name in
                                   ('overview', 'products', 'demand', 'prices', 'supply', 'players', 'gaps')]
    if stage == 'entities':
        return ['entities/'], []
    if stage == 'index':
        return ['_index.md', 'changes.md'], ['_index.md', 'changes.md']
    raise ValueError('Review only P1 entities, company-trigiant, company-yofc, industry or index; resolver is separate')


def text_diff(before: dict[str, bytes], after: dict[str, bytes], before_label: str,
              after_label: str) -> str:
    result = []
    for path in sorted(before.keys() | after.keys()):
        old, new = before.get(path, b''), after.get(path, b'')
        if old != new:
            result.extend(difflib.unified_diff(old.decode('utf-8').splitlines(True),
                                             new.decode('utf-8').splitlines(True),
                                             fromfile=before_label + '/' + path,
                                             tofile=after_label + '/' + path))
    return ''.join(result)


def apply_view_review(root: Path, stage: str, edits_json: Path | dict) -> Path:
    """Review a rejected draft or current accepted view; publish a passing tree.

    edits_json: {"reason": "...", "replace": [{"path": "...", "old": "...", "new": "..."}]}.
    An empty replace array permits rechecking an unchanged draft after a checker
    correction. Every nonempty replacement must match exactly once.
    """
    root = Path(root).resolve()
    if not root.is_relative_to(HERE / 'runs'):
        raise ValueError('Review outputs must remain inside ignored wiki-lab/runs/')
    allowed, required = stage_policy(stage)
    edits = edits_json if isinstance(edits_json, dict) else load(Path(edits_json))
    if not isinstance(edits, dict) or not isinstance(edits.get('reason'), str) or not edits['reason'].strip():
        raise ValueError('An explicit correction/recheck reason is required')
    replacements = edits.get('replace', [])
    if not isinstance(replacements, list):
        raise ValueError('replace must be an array')
    for item in replacements:
        if not isinstance(item, dict) or not path_allowed(str(item.get('path', '')), allowed):
            raise ValueError('Correction outside this stage output allowlist')
        if not isinstance(item.get('old'), str) or not item['old'] or not isinstance(item.get('new'), str):
            raise ValueError('Every replacement needs nonempty old text and string new text')

    pipeline = Pipeline(root, SimpleNamespace(codex='unused-no-model-call', stage_timeout=0))
    pipeline.assert_accepted_wiki()
    base_commit = git(pipeline.wiki, 'rev-parse', 'HEAD')
    base_hashes = hashes(pipeline.wiki)
    logs = sorted((root / 'logs' / stage).glob('attempt-*/record.json'))
    if not logs:
        raise ValueError('No real CLI attempt for this stage')
    record_path = logs[-1]
    record_hash = sha(record_path)
    record = load(record_path)
    original_marker = root / 'accepted' / (stage + '.json')
    accepted_view = original_marker.exists()
    if (record.get('stage') != stage or record.get('status') not in ('checks_rejected', 'completed') or
            record.get('exit_code') != 0 or record.get('completed') is not True):
        raise ValueError('Latest CLI attempt must have completed with completed/checks_rejected status')
    if record['status'] == 'completed' and not accepted_view:
        raise ValueError('A completed CLI view requires its original accepted stage marker')
    original_marker_hash = sha(original_marker) if accepted_view else None
    original_marker_state = load(original_marker) if accepted_view else None
    if original_marker_state:
        for relative, digest in original_marker_state['outputs'].items():
            payload = subprocess.check_output(['git', '-C', str(pipeline.wiki), 'show',
                                               original_marker_state['commit'] + ':' + relative])
            if hashlib.sha256(payload).hexdigest() != digest:
                raise ValueError('Original accepted stage receipt hash changed: ' + relative)
    if set(record.get('allowed_paths', [])) != set(allowed):
        raise ValueError('CLI receipt stage permissions differ from the P1 stage policy')
    original = Path(record['workspace']).resolve()
    if not original.is_relative_to(root / 'work' / stage):
        raise ValueError('Model workspace escapes this run and stage')
    original_hashes = hashes(original)
    if original_hashes != record.get('output_hashes'):
        raise ValueError('Original CLI workspace changed after its recorded final hash')
    original_path_errors = check_changes(record.get('input_hashes', {}), original_hashes, allowed)
    original_path_errors.extend('Deletion is not permitted: ' + path
                                for path in record.get('input_hashes', {}).keys() - original_hashes.keys())
    if record.get('path_errors') or original_path_errors:
        raise ValueError('Rejected-path CLI attempts cannot be accepted as a view review')

    stage_root = root / 'work' / stage
    numbers = [int(path.name.split('-')[-1]) for path in stage_root.glob('review-*')
               if re.fullmatch(r'review-\d+', path.name)]
    number = max(numbers, default=0) + 1
    review_name = f'review-{stage}-{number:02d}'
    target = stage_root / f'review-{number:02d}'
    shutil.copytree(pipeline.wiki, target, ignore=shutil.ignore_patterns('.git'))
    model_stage_bytes = {}
    for relative in original_hashes:
        if not path_allowed(relative, allowed):
            continue
        payload = (original / relative).read_bytes()
        model_stage_bytes[relative] = payload
        if not accepted_view:
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
    # Once a stage is accepted, its current pages can contain later reviews or
    # resolver corrections. Never overwrite them with the first model draft.
    review_stage_bytes = ({relative: (pipeline.wiki / relative).read_bytes()
                           for relative in base_hashes if path_allowed(relative, allowed)}
                          if accepted_view else model_stage_bytes)

    audit_path = root / 'reviews' / f'{review_name}.json'
    audit = {'schema': 'wiki-lab-view-review/1', 'stage': stage, 'review': review_name,
             'status': 'reviewing', 'reason': edits['reason'], 'edits': edits,
             'original_model_workspace': str(original), 'model_record_path': str(record_path),
             'model_record_sha256': record_hash, 'original_model_hashes': original_hashes,
             'original_check': record.get('checks'), 'original_status': record['status'],
             'base_commit': base_commit, 'base_hashes': base_hashes,
             'review_base_mode': 'accepted_current_view' if accepted_view else 'rejected_model_draft',
             'original_stage_marker': str(original_marker) if accepted_view else None,
             'original_stage_marker_sha256': original_marker_hash,
             'original_stage_receipt': original_marker_state,
             'review_workspace': str(target), 'allowed_paths': allowed,
             'model_call_repeated': False, 'amount': None}
    write_json(audit_path, audit)

    def save_candidate(status: str, errors: list[str], checked: dict | None = None) -> None:
        candidate_hashes = hashes(target)
        changed = sorted(path for path in base_hashes.keys() | candidate_hashes.keys()
                         if base_hashes.get(path) != candidate_hashes.get(path))
        before_bytes = {path: (pipeline.wiki / path).read_bytes() for path in changed
                        if (pipeline.wiki / path).is_file()}
        after_bytes = {path: (target / path).read_bytes() for path in changed if (target / path).is_file()}
        correction_bytes = {path: (target / path).read_bytes() for path in review_stage_bytes}
        audit_path.with_suffix('.patch').write_text(text_diff(before_bytes, after_bytes, 'accepted-before', 'reviewed'), encoding='utf-8')
        correction_label = 'accepted-current' if accepted_view else 'original-model'
        audit_path.with_suffix('.edits.patch').write_text(text_diff(review_stage_bytes, correction_bytes, correction_label, 'reviewed'), encoding='utf-8')
        audit.update(status=status, candidate_hashes=candidate_hashes, changed_paths=changed,
                     errors=errors, checks=checked,
                     patch_path=str(audit_path.with_suffix('.patch')),
                     correction_patch_path=str(audit_path.with_suffix('.edits.patch')))
        write_json(audit_path, audit)

    try:
        for item in replacements:
            path = target / item['path']
            if item['path'] not in review_stage_bytes:
                raise ValueError('Replacement must target an actual stage page in the review base')
            text = path.read_bytes().decode('utf-8')
            if text.count(item['old']) != 1:
                raise ValueError('Correction text must match exactly once: ' + item['path'])
            path.write_bytes(text.replace(item['old'], item['new']).encode('utf-8'))
        candidate_hashes = hashes(target)
        errors = check_changes(base_hashes, candidate_hashes, allowed)
        errors.extend('Deletion is not permitted: ' + path for path in base_hashes.keys() - candidate_hashes.keys())
        for relative in required:
            if not (target / relative).is_file() or not (target / relative).read_bytes().strip():
                errors.append('Missing/empty required stage output: ' + relative)
        if stage == 'entities':
            entity_count = len(list((target / 'entities').glob('*.md')))
            if entity_count < 1 or entity_count > 16:
                errors.append('P1 core entity-page count must be between 1 and 16')
        checked = check_wiki(target, pipeline.sources,
                             immutable=load(root / 'source-note-hashes.json'),
                             link_roots=pipeline.link_roots)
        errors.extend(checked['errors'])
        checked = {**checked, 'ok': not errors, 'errors': errors}
        save_candidate('checks_passed' if checked['ok'] else 'checks_rejected', errors, checked)
        if errors:
            raise ValueError('Reviewed candidate rejected: ' + '; '.join(errors))
        # Optimistic transaction check: no other accepted writer may have moved
        # the wiki or changed the original receipt while review was prepared.
        pipeline.assert_accepted_wiki()
        if git(pipeline.wiki, 'rev-parse', 'HEAD') != base_commit or hashes(pipeline.wiki) != base_hashes:
            raise ValueError('Accepted wiki changed during review; preserve candidate and retry')
        if sha(record_path) != record_hash or hashes(original) != original_hashes:
            raise ValueError('Original model receipt/artifacts changed during review')
        if accepted_view and sha(original_marker) != original_marker_hash:
            raise ValueError('Original accepted stage marker changed during review')
        for relative in audit['changed_paths']:
            destination = pipeline.wiki / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target / relative, destination)
        commit = pipeline.commit('P1: explicit view review ' + stage)
        receipt = {'commit': commit, 'outputs': {path: sha(pipeline.wiki / path) for path in audit['changed_paths']},
                   'checks': checked, 'operator_review': str(audit_path),
                   'original_model_record_sha256': record_hash}
        write_json(root / 'accepted' / (review_name + '.json'), receipt)
        if not original_marker.exists():
            write_json(original_marker, receipt)
        audit.update(status='accepted', accepted_commit=commit,
                     accepted_receipt=str(root / 'accepted' / (review_name + '.json')))
        write_json(audit_path, audit)
        pipeline.assert_accepted_wiki()
        return target
    except Exception as error:
        if audit.get('status') not in ('accepted', 'checks_rejected'):
            save_candidate('review_failed', [str(error)], audit.get('checks'))
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('stage')
    parser.add_argument('edits_json', type=Path)
    arguments = parser.parse_args()
    print(apply_view_review(arguments.run_dir, arguments.stage, arguments.edits_json))
