#!/usr/bin/env python3
"""Apply an explicit, audited pre-aggregation correction without repeating a model call."""
from __future__ import annotations
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import shutil
from run import HERE, hashes, load, sha, template, write_json
from checks import check_extraction


def apply_review(root: Path, chunk_id: str, edits: dict) -> Path:
    root = root.resolve()
    if not root.is_relative_to(HERE/'runs') or (root/'wiki').exists():
        raise ValueError('Review only an ignored P1 run before source-note aggregation')
    if not edits.get('reason'):
        raise ValueError('An explicit source-readback or schema correction reason is required')
    manifest = load(root/'corpus-manifest.json')
    chunk = next(c for c in load(root/'chunks.json') if c['id'] == chunk_id)
    records = sorted((root/'logs'/('extract-'+chunk_id)).glob('attempt-*/record.json'))
    record = load(records[-1])
    if record.get('exit_code') != 0 or not record.get('completed'):
        raise ValueError('Wait for a completed model attempt; do not modify an active/timeout workspace')
    model_original = Path(record['workspace'])
    model_hashes = hashes(model_original)
    if model_hashes != record['output_hashes']:
        raise ValueError('Original model artifact changed after its recorded final hash')
    state_path = root/'extract'/chunk_id/'success.json'
    old_state = load(state_path) if state_path.exists() else None
    original = Path(old_state['workspace']) if old_state else model_original
    before = hashes(original)
    if old_state and before != old_state['hashes']:
        raise ValueError('Accepted pre-aggregation artifact changed after its recorded hash')
    target = root/'extract'/chunk_id/f'review-{len(list((root/"extract"/chunk_id).glob("review-*")))+1:02d}'
    shutil.copytree(original, target, ignore=shutil.ignore_patterns('.git'))
    allowed = {f'{folder}/{chunk_id}.{suffix}' for folder,suffix in
               [('sources','md'),('mentions','json'),('gaps','json'),('coverage','json'),('market','md')]}
    patch = []
    for item in edits.get('replace', []):
        if item['path'] not in allowed:
            raise ValueError('Correction outside this chunk output allowlist')
        path = target/item['path']; text = path.read_text()
        if text.count(item['old']) != 1:
            raise ValueError('Correction text must match exactly once')
        changed = text.replace(item['old'], item['new'])
        path.write_text(changed)
        patch.extend(difflib.unified_diff(text.splitlines(True),changed.splitlines(True),
                                         fromfile='model/'+item['path'],tofile='review/'+item['path']))
    if edits.get('coverage_pages_from_chunk'):
        path = target/'coverage'/f'{chunk_id}.json'; text = path.read_text(); value = json.loads(text)
        if len(chunk['sources']) != 1 or value['pages'] != chunk['sources'][0]['pages']:
            raise ValueError('Only an exact single-source bare-page-list schema correction is permitted')
        value['pages'] = chunk['sources']
        write_json(path,value)
        patch.extend(difflib.unified_diff(text.splitlines(True),path.read_text().splitlines(True),
                                         fromfile='model/coverage.json',tofile='review/coverage.json'))
    checked = check_extraction(target,chunk,manifest['sources'])
    audit = {'chunk_id':chunk_id,'reason':edits['reason'],'edits':edits,'original_workspace':str(original),
             'original_model_workspace':str(model_original),'original_model_hashes':model_hashes,
             'review_workspace':str(target),'before':before,'after':hashes(target),
             'original_check':check_extraction(original,chunk,manifest['sources']),'checks':checked,
             'model_log':str(records[-1].parent),'model_call_repeated':False,'prior_success':old_state}
    audit_path = root/'reviews'/f'{chunk_id}-{target.name}.json'
    write_json(audit_path,audit)
    audit_path.with_suffix('.patch').write_text(''.join(patch))
    if not checked['ok']:
        raise ValueError('Reviewed candidate rejected: '+str(checked['errors']))
    base = template('extract',CHUNK_ID=chunk_id)
    binding = {'input_sha256':chunk['sha256'],'prompt_sha256':hashlib.sha256(base.encode()).hexdigest(),
               'source_bindings':manifest['manifest_sha256'],'chunk':chunk}
    if sha(Path(chunk['path'])) != chunk['sha256'] or sha(target/'input/source.md') != chunk['sha256']:
        raise ValueError('Source input changed')
    write_json(state_path,{'binding':binding,'workspace':str(target),'hashes':hashes(target),'checks':checked,
                           'actual_prompt_sha256':record['prompt_sha256'],'log':str(records[-1].parent),
                           'operator_review':str(audit_path)})
    return target


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_dir',type=Path);parser.add_argument('chunk_id');parser.add_argument('edits',type=Path)
    args=parser.parse_args()
    print(apply_review(args.run_dir,args.chunk_id,load(args.edits)))
