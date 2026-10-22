#!/usr/bin/env python3
"""S2 single-turn record extraction. No S3 orchestration or shared-library writes."""
from __future__ import annotations

import argparse
import concurrent.futures
from copy import deepcopy
import fcntl
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

import records
from run import HERE, Pipeline, git, load, sha, template, write_json

PROMPT_VERSION = 'extract-records-s2-2'
CALL_LIMIT = 40
DEFAULT_RUN = HERE / 'runs/s2-20261022'


def protected_state():
    """Hash ALL files, including nested Git metadata; never touch frozen inputs."""
    result = {}
    for rel in ('runs/p1-20261021', 'runs/s1-20261021', 'DESIGN.md', 'DESIGN-v2.md'):
        path = HERE / rel
        files = sorted(path.rglob('*')) if path.is_dir() else [path]
        result[rel] = {str(p.relative_to(HERE)): (hashlib.sha256(
            ('symlink:' + str(p.readlink())).encode()).hexdigest() if p.is_symlink() else sha(p))
            for p in files if p.is_file() or p.is_symlink()}
    result['p1-sample'] = git(HERE / 'runs/p1-20261021/wiki', 'rev-parse', 'refs/tags/p1-sample')
    result['head'] = git(HERE, 'rev-parse', 'HEAD')
    result['branch'] = git(HERE, 'branch', '--show-current')
    return result


def initialize(root):
    root = Path(root).resolve()
    if root.parent != HERE / 'runs' or not root.name.startswith('s2-'):
        raise ValueError('S2 artifacts must live in wiki-lab/runs/s2-*')
    root.mkdir(parents=True, exist_ok=True)
    baseline = root / 'input-baseline.json'
    if not baseline.exists():
        state = protected_state()
        if not state['head'].startswith('afdcf14') or state['branch'] != 'codex/knowledge-wiki-codex-spike':
            raise ValueError('S2 requires the requested afdcf14 branch start')
        write_json(baseline, state)
    return root


def integrity(root):
    before, after = load(root / 'input-baseline.json'), protected_state()
    changed = [key for key in before if before[key] != after.get(key)]
    result = dict(ok=not changed, changed=changed, checked_at=time.time(), current=after)
    write_json(root / 'INPUT-INTEGRITY.json', result)
    if changed:
        raise RuntimeError('Protected input changed: ' + ', '.join(changed))
    return result


def config_identity():
    # Do not copy config/auth files: retain only non-secret inference settings.
    text = (Path.home() / '.codex/config.toml').read_text()
    result = {}
    for key in ('model', 'model_reasoning_effort', 'model_provider'):
        found = re.search(r'^' + key + r'\s*=\s*"([^"\n]+)"', text, re.M)
        result[key] = found.group(1) if found else None
    return result


def snapshot_prompt(root):
    prompt_path = HERE / 'prompts/extract-records.md'
    text = prompt_path.read_text()
    if PROMPT_VERSION not in text.splitlines()[0]:
        raise ValueError('Prompt version constant and template disagree')
    directory = root / 'prompt-versions' / PROMPT_VERSION
    guide = records.field_guide()
    binding = dict(prompt_version=PROMPT_VERSION, template_sha256=sha(prompt_path),
                   field_guide_sha256=hashlib.sha256(guide.encode()).hexdigest())
    path = directory / 'binding.json'
    if path.exists() and load(path) != binding:
        raise ValueError('Same prompt version changed; bump version and rerun all pilots')
    write_json(path, binding)
    (directory / 'template.md').write_text(text)
    (directory / 'field-guide.md').write_text(guide)
    return binding


def reserve_call(root, stage):
    """A process-safe append BEFORE launch; crashes and failures consume budget."""
    with (root / 'calls.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger = root / 'calls.jsonl'
        rows = [json.loads(line) for line in ledger.read_text().splitlines()] if ledger.exists() else []
        if len(rows) >= CALL_LIMIT:
            raise RuntimeError('S2 call limit 40 reached; stop')
        row = dict(number=len(rows)+1, stage=stage, reserved_at=time.time())
        with ledger.open('a') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
            stream.flush()
            import os
            os.fsync(stream.fileno())
        return row['number']


def summarize_usage(root):
    attempts = []
    for path in sorted((root / 'logs').glob('*/attempt-*/record.json')):
        item = load(path)
        item['log'] = str(path.parent)
        usage = item.get('usage') or {}
        item['uncached_input_tokens'] = (usage.get('input_tokens', 0)-usage.get('cached_input_tokens', 0)
                                          if 'input_tokens' in usage else None)
        attempts.append(item)
    write_json(root / 'usage.json', dict(attempts=attempts,
        calls_reserved=len((root / 'calls.jsonl').read_text().splitlines()) if (root / 'calls.jsonl').exists() else 0,
        call_limit=CALL_LIMIT))
    return attempts


def truncated_output(log):
    """Observe unclosed JSON and runtime output-limit signals, even on failure."""
    final = log / 'final.json'
    text = final.read_text() if final.exists() else ''
    stack, quoted, escaped = [], False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '[{':
            stack.append(char)
        elif char in ']}' and stack:
            stack.pop()
    trace_parts = [(log / 'stderr.log').read_text()] if (log / 'stderr.log').exists() else []
    if (log / 'events.jsonl').exists():
        for line in (log / 'events.jsonl').read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            # Do not mistake licensed source text quoted by an agent_message
            # for a runtime output-limit signal.
            if event.get('type') in ('error', 'turn.failed', 'response.incomplete') or event.get('item', {}).get('type') == 'error':
                trace_parts.append(json.dumps(event))
    trace = '\n'.join(trace_parts)
    signal = bool(re.search(r'max[_ ]output|output.{0,30}(token.{0,15}limit|truncat)|'
                           r'(maximum|exceeded).{0,25}(output|token)|finish_reason.{0,8}length', trace, re.I))
    return bool((text.lstrip().startswith('{') and (stack or quoted)) or signal)


def tool_free(record):
    return record.get('command_count') == 0 and not record.get('tools')


class S2Pipeline(Pipeline):
    def __init__(self, root, args):
        self.root, self.args, self.lock = Path(root).resolve(), args, threading.Lock()

    def invoke(self, stage, prompt, schema, metadata=None):
        snapshot_prompt(self.root)
        number = reserve_call(self.root, stage)
        workspace = self.root / 'empty-workspaces' / ('call-%02d' % number)
        workspace.mkdir(parents=True)
        executable = Path(shutil.which(self.args.codex) or self.args.codex).resolve()
        cli = dict(path=str(executable), sha256=sha(executable),
                   version=subprocess.check_output([str(executable), '--version'], text=True).strip())
        try:
            record = super().call(stage, workspace, prompt, [], output_schema=schema, read_only=True,
                metadata=dict(call_number=number, configuration=config_identity(), cli=cli,
                              prompt_version=PROMPT_VERSION, schema_sha256=sha(schema),
                              field_guide_sha256=hashlib.sha256(records.field_guide().encode()).hexdigest(),
                              **(metadata or {})))
        except RuntimeError:
            logs = sorted((self.root / 'logs' / stage).glob('attempt-*/record.json'))
            if not logs:
                raise
            record = load(logs[-1])
            record['log'] = str(logs[-1].parent)
        summarize_usage(self.root)
        return record

    def schemas(self, transport=False):
        path = self.root / 'schemas' / ('model-transport.json' if transport else 'model-full.json')
        write_json(path, records.model_output_schema(transport))
        write_json(self.root / 'schemas/stored-records.json', records.extract_output_schema())
        return path

    def probe(self, round_number, transport=False):
        stage = 'pilot-r%d-schema' % round_number
        synthetic = ('合成公司股份有限公司2025年营业收入125元。'
                     '单位：元；期间：2025年；合并报表；期末2025年12月31日。')
        checklist = dict(items=[dict(id='T01', name='营业收入')])
        prompt = render_prompt(dict(source_id='synthetic-only', id='SYN', published_date='2026-03-01'),
                               synthetic, checklist)
        record = self.invoke(stage, prompt, self.schemas(transport), dict(is_probe=True, source_chars=len(synthetic)))
        used_transport = transport
        if record['status'] != 'completed':
            error = (Path(record['log']) / 'stderr.log').read_text()
            events = (Path(record['log']) / 'events.jsonl').read_text()
            # Only a schema rejection permits transport relaxation, never a
            # generic CLI/network failure. Preserve the unmodified error.
            if re.search(r'(invalid|unsupported|not supported|not permitted).{0,100}(schema|keyword)|'
                         r'(schema|keyword).{0,100}(invalid|unsupported|not supported|not permitted)',
                         error + events, re.I | re.S):
                record = self.invoke(stage + '-transport', prompt, self.schemas(True),
                                     dict(is_probe=True, source_chars=len(synthetic)))
                used_transport = True
        result = dict(record=record, transport=used_transport)
        final = Path(record['log']) / 'final.json'
        try:
            output = load(final)
            if isinstance(output, dict) and isinstance(output.get('records'), list):
                record['output_records'] = len(output['records'])
                record['raw_output_count_observed'] = True
            accepted = records.accept_model_output(output, source_id='synthetic-only',
                disclosed_on='2026-03-01', extraction_version=PROMPT_VERSION, prefix='SYN',
                pages={('synthetic-only', 1): synthetic}, checklist=checklist)
            result.update(valid_json_object=isinstance(output, dict), acceptance=accepted)
            record.update(output_records=accepted['output_records'], accepted_records=len(accepted['records']),
                          rejected_records=len(accepted['rejected']), rejection_rate=accepted['rejection_rate'])
        except (OSError, ValueError, TypeError, KeyError) as error:
            result.update(valid_json_object=False, error=str(error))
        result['ok'] = (record['status'] == 'completed' and result.get('valid_json_object', False)
                        and tool_free(record) and not result.get('acceptance', {}).get('rejected'))
        record['s2_status'] = 'completed' if result['ok'] else 'failed_probe'
        write_json(Path(record['log']) / 'record.json', record)
        summarize_usage(self.root)
        write_json(self.root / ('pilot-r%d-schema.json' % round_number), result)
        return result

    def extract_record_chunk(self, chunk, *, stage=None, transport=False, mode='coverage', request=''):
        manifest = load(self.root / 'corpus-manifest.json')
        source = next(s for s in manifest['sources'] if s['id'] == chunk.get('source', chunk.get('source_short', chunk['id'].split('.')[0]))
                      or s['source_id'] == chunk['source_id'])
        text_path = Path(chunk['path'])
        if sha(text_path) != chunk['sha256']:
            raise ValueError('Chunk hash changed: ' + chunk['id'])
        checklist = checklist_for(source['id'])
        prompt = render_prompt(source, text_path.read_text(), checklist, mode, request)
        stage = stage or 'extract-' + chunk['id']
        record = self.invoke(stage, prompt, self.schemas(transport),
            dict(chunk_id=chunk['id'], source_chars=chunk.get('chars', len(text_path.read_text())), mode=mode,
                 chunk_sha256=chunk['sha256'], prompt_template_sha256=sha(HERE / 'prompts/extract-records.md')))
        write_json(Path(record['log']) / 'input-chunk.json', chunk)
        write_json(Path(record['log']) / 'input-mode.json', dict(mode=mode, request=request))
        result = dict(chunk_id=chunk['id'], log=record['log'], call_number=record['call_number'],
                      status=record['status'], prompt_version=PROMPT_VERSION)
        final = Path(record['log']) / 'final.json'
        # A CLI failure can still leave a legal final response. Count that raw
        # output for cost accounting without accepting any of its records.
        output = None
        try:
            output = load(final)
            if isinstance(output, dict) and isinstance(output.get('records'), list):
                record['output_records'] = len(output['records'])
                record['raw_output_count_observed'] = True
                result['output_records'] = len(output['records'])
        except (OSError, ValueError, TypeError):
            pass
        if record['status'] == 'completed':
            try:
                if output is None:
                    output = load(final)
                page_map = frozen_pages(self.root, source)
                # Limit evidence to the exact pages actually supplied in prompt.
                allowed = set(chunk.get('core_pages', chunk.get('pages', []))) | set(chunk.get('context_pages', []))
                page_map = {key: value for key, value in page_map.items() if key[1] in allowed}
                # Frozen-page truth AND supplied-span truth: never accept an
                # excerpt from an excluded part of a shared boundary page.
                fragments = chunk.get('fragments', []) + chunk.get('context_fragments', [])
                supplied = {}
                for fragment in fragments:
                    supplied.setdefault(fragment['page'], []).append(fragment['text'])
                accepted = records.accept_model_output(output, source_id=source['source_id'],
                    disclosed_on=source['date'], extraction_version=PROMPT_VERSION,
                    prefix=stage, pages=page_map, checklist=checklist, context_pages=chunk.get('context_pages', []),
                    supplied_spans=supplied)
                result.update(accepted)
                result['status'] = 'completed' if tool_free(record) else 'tools_executed'
                record.update(output_records=accepted['output_records'], accepted_records=len(accepted['records']),
                              rejected_records=len(accepted['rejected']), rejection_rate=accepted['rejection_rate'])
            except (OSError, ValueError, TypeError, KeyError) as error:
                result.update(status='invalid_output', error=str(error))
                text = final.read_text() if final.exists() else ''
                result['suspected_truncation'] = truncated_output(Path(record['log']))
        else:
            result['suspected_truncation'] = truncated_output(Path(record['log']))
        record['s2_status'] = result['status']
        write_json(Path(record['log']) / 'record.json', record)
        write_json(self.root / 'rejected' / (stage + '.json'), result.get('rejected', []))
        result['binding'] = dict(chunk_sha256=chunk['sha256'], prompt_template_sha256=record['prompt_template_sha256'],
                                 prompt_version=PROMPT_VERSION, schema_sha256=record['schema_sha256'],
                                 field_guide_sha256=record['field_guide_sha256'],
                                 final_sha256=sha(final) if final.exists() else None)
        write_json(Path(record['log']) / 'acceptance.json', result)
        if result['status'] != 'completed':
            write_json(self.root / 'rejected/whole-blocks' / (stage + '.json'), result)
        if result['status'] == 'completed':
            write_json(self.root / 'accepted' / (chunk['id'] + '.json'), result)
        summarize_usage(self.root)
        return result

    def run_chunk(self, chunk, transport=True, retry=True):
        """Resume bound success; split timeout/truncation, retry other failure once."""
        saved = self.root / 'accepted' / (chunk['id'] + '.json')
        if saved.exists():
            previous = load(saved)
            # Refuse changed provenance before schemas() can refresh a shared
            # schema file or full extraction can dispatch another model call.
            check_collected_response(self.root, previous, collection_inputs(self.root))
            binding = previous['binding']
            if (binding['chunk_sha256'] == chunk['sha256'] and
                binding['prompt_template_sha256'] == sha(HERE / 'prompts/extract-records.md') and
                binding['prompt_version'] == PROMPT_VERSION and
                binding['schema_sha256'] == sha(self.schemas(transport)) and
                binding.get('field_guide_sha256') == hashlib.sha256(records.field_guide().encode()).hexdigest()):
                final_path = Path(previous['log']) / 'final.json'
                if sha(final_path) != binding.get('final_sha256'):
                    raise ValueError('Final response changed')
                raw = load(final_path)
                # Audit resume against immutable original response as well.
                if sha(Path(previous['log']) / 'prompt.md') != load(Path(previous['log']) / 'record.json')['prompt_sha256']:
                    raise ValueError('Prompt log changed')
                checked = validate_saved_output(self.root, chunk, raw, Path(previous['log']).parent.name)
                for key in ('records', 'rejected', 'discarded', 'coverage', 'output_records', 'rejection_rate'):
                    if checked[key] != previous[key]:
                        raise ValueError('Accepted response changed: ' + key)
                return previous
            raise ValueError('Successful binding changed; use explicit new pilot round')
        split_path = self.root / 'splits' / (chunk['id'] + '.json')
        if split_path.exists():
            split = load(split_path)
            if (split.get('parent', {}).get('id') != chunk['id'] or
                split.get('parent', {}).get('sha256') != chunk['sha256'] or
                split.get('prompt_template_sha256') != sha(HERE / 'prompts/extract-records.md')):
                raise ValueError('Split binding changed: ' + chunk['id'])
            if not split.get('children'):
                raise ValueError('Empty split: ' + chunk['id'])
            return dict(chunk_id=chunk['id'], status='split',
                        children=[self.run_chunk(child, transport) for child in split['children']])
        previous_attempts = []
        for path in (self.root / 'logs').glob('*/attempt-*/record.json'):
            attempt = load(path)
            if (attempt.get('chunk_id') == chunk['id'] and
                attempt.get('chunk_sha256') == chunk['sha256'] and
                attempt.get('prompt_template_sha256') == sha(HERE / 'prompts/extract-records.md')):
                previous_attempts.append((path, attempt))
        previous_attempts.sort(key=lambda pair: pair[1].get('call_number', 0))
        if previous_attempts:
            path, latest = previous_attempts[-1]
            acceptance = load(path.parent / 'acceptance.json') if (path.parent / 'acceptance.json').exists() else {}
            if latest['status'] == 'running':
                raise RuntimeError('Unreconciled running attempt; never launch a duplicate')
            if latest['status'] == 'completed' and acceptance.get('status') == 'completed':
                return adopt_pilot(self.root, chunk, path.parent)
            if latest['status'] == 'timed_out' or acceptance.get('suspected_truncation'):
                return self.split_and_run(chunk, transport, acceptance.get('status', latest['status']))
            if len(previous_attempts) >= 2:
                return dict(chunk_id=chunk['id'], status='failed', error='one retry already used')
            return self.run_chunk_retry(chunk, transport)
        result = self.extract_record_chunk(chunk, transport=transport)
        if result['status'] == 'completed':
            return result
        if result['status'] == 'timed_out' or result.get('suspected_truncation'):
            return self.split_and_run(chunk, transport, result['status'])
        if retry:
            return self.run_chunk_retry(chunk, transport)
        return result

    def run_chunk_retry(self, chunk, transport):
        result = self.extract_record_chunk(chunk, stage='retry-' + chunk['id'], transport=transport)
        if result['status'] == 'timed_out' or result.get('suspected_truncation'):
            # Keep the same policy even if a non-truncation first attempt failed.
            return self.split_and_run(chunk, transport, result['status'])
        return result

    def split_and_run(self, chunk, transport, reason):
        import s2_chunks
        source = next(s for s in load(self.root / 'corpus-manifest.json')['sources'] if s['id'] == chunk['source'])
        children = s2_chunks.split_chunk(chunk, source, self.root)
        write_json(self.root / 'splits' / (chunk['id'] + '.json'),
                   dict(reason=reason, parent=chunk, children=children,
                        prompt_template_sha256=sha(HERE / 'prompts/extract-records.md')))
        return dict(chunk_id=chunk['id'], status='split', children=[self.run_chunk(child, transport) for child in children])


def validate_saved_output(root, chunk, output, prefix):
    source = next(s for s in load(root / 'corpus-manifest.json')['sources'] if s['id'] == chunk['source'])
    allowed = set(chunk['core_pages']) | set(chunk.get('context_pages', []))
    pages = {key: text for key, text in frozen_pages(root, source).items() if key[1] in allowed}
    supplied = {}
    for fragment in chunk['fragments'] + chunk.get('context_fragments', []):
        supplied.setdefault(fragment['page'], []).append(fragment['text'])
    return records.accept_model_output(output, source_id=source['source_id'], disclosed_on=source['date'],
        extraction_version=PROMPT_VERSION, prefix=prefix, pages=pages, checklist=checklist_for(source['id']),
        context_pages=chunk.get('context_pages', []), supplied_spans=supplied)


def adopt_pilot(root, chunk, log):
    """Revalidate an already completed call after runner-only changes; zero calls."""
    record = load(log / 'record.json')
    if record['status'] != 'completed' or not tool_free(record):
        raise ValueError('Cannot adopt an incomplete/command-using call')
    if (record['chunk_sha256'] != chunk['sha256'] or
        record['prompt_template_sha256'] != sha(HERE / 'prompts/extract-records.md') or
        record['prompt_sha256'] != sha(log / 'prompt.md')):
        raise ValueError('Cannot adopt stale/tampered call')
    source = next(s for s in load(root / 'corpus-manifest.json')['sources'] if s['id'] == chunk['source'])
    expected_prompt = render_prompt(source, Path(chunk['path']).read_text(), checklist_for(source['id']))
    if hashlib.sha256(expected_prompt.encode()).hexdigest() != record['prompt_sha256']:
        raise ValueError('Cannot adopt response from a different generated field guide')
    schema_path = Path(record['command'][record['command'].index('--output-schema')+1])
    if record.get('schema_sha256') is not None and record['schema_sha256'] != sha(schema_path):
        raise ValueError('Cannot adopt response whose logged schema changed')
    accepted = validate_saved_output(root, chunk, load(log / 'final.json'), record['stage'])
    result = dict(chunk_id=chunk['id'], log=str(log), call_number=record['call_number'], status='completed',
        prompt_version=PROMPT_VERSION, **accepted,
        binding=dict(chunk_sha256=chunk['sha256'], prompt_template_sha256=record['prompt_template_sha256'],
            prompt_version=PROMPT_VERSION, schema_sha256=sha(schema_path),
            field_guide_sha256=hashlib.sha256(records.field_guide().encode()).hexdigest(),
            final_sha256=sha(log / 'final.json')))
    write_json(root / 'adoptions' / (record['stage'] + '.json'), result)
    write_json(root / 'accepted' / (chunk['id'] + '.json'), result)
    return result


def pilot_gate(root, round_number=1):
    import s2_compare
    chunks = load(root / 'chunks.json')
    designated = [('S46', next(c for c in chunks if c['source'] == 'S46')),
                  ('S34-section2', next(c for c in chunks if c['source'] == 'S34' and any(t.startswith('第二节') for t in c['sections']))),
                  ('S34-note7', next(c for c in chunks if c.get('note_group') == '七、合并财务报表项目注释'))]
    schema = load(root / ('pilot-r%d-schema.json' % round_number))
    results, errors = [], []
    for name, chunk in designated:
        logs = sorted((root / 'logs' / ('pilot-r%d-' % round_number + name)).glob('attempt-*/acceptance.json'))
        if not logs:
            errors.append(name + ': pilot not completed')
            continue
        result = load(logs[-1])
        record = load(logs[-1].parent / 'record.json')
        if result['status'] != 'completed' or result.get('suspected_truncation'):
            errors.append(name + ': ' + result['status'] + ('; truncated' if result.get('suspected_truncation') else ''))
        if not tool_free(record):
            errors.append(name + ': model executed tools/commands')
        if result.get('rejection_rate') is None:
            errors.append(name + ': individual validation not completed')
        elif result['rejection_rate'] > .1:
            errors.append(name + ': rejected records exceed 10%')
        if record['prompt_template_sha256'] != sha(HERE / 'prompts/extract-records.md'):
            errors.append(name + ': stale prompt version')
        if result['status'] == 'completed':
            try:
                check_collected_response(root, result, collection_inputs(root))
            except (ValueError, OSError, KeyError, TypeError) as error:
                errors.append(name + ': pilot provenance check failed: ' + str(error))
        results.append(dict(name=name, chunk_id=chunk['id'], acceptance=str(logs[-1]),
                            status=result['status'], output_records=result.get('output_records'),
                            rejected=len(result.get('rejected', [])), rejection_rate=result.get('rejection_rate')))
        if name == 'S46':
            reference = load(HERE / 'runs/s1-20261021/records/S46.json')
            source = next(s for s in load(root / 'corpus-manifest.json')['sources'] if s['id'] == 'S46')
            matching = s2_compare.pilot_match(reference, result.get('records', []), frozen_pages(root, source))
            write_json(root / ('pilot-r%d-S46-match.json' % round_number), matching)
            if not matching['passed']:
                errors.append('S46: reference recall below 80%')
    if not schema['ok']:
        errors.append('schema probe failed')
    gate = dict(ok=not errors, round=round_number, errors=errors, results=results,
                prompt_version=PROMPT_VERSION, prompt_template_sha256=sha(HERE / 'prompts/extract-records.md'),
                transport_schema=schema['transport'])
    write_json(root / ('pilot-r%d-gate.json' % round_number), gate)
    return gate


def collection_inputs(root):
    """Current structural leaves and historically bound one-shot requests."""
    current = sha(HERE / 'prompts/extract-records.md')
    inputs = {}

    def visit(chunk):
        split_path = root / 'splits' / (chunk['id'] + '.json')
        if split_path.exists():
            split = load(split_path)
            if (split.get('prompt_template_sha256') != current or
                split.get('parent', {}).get('id') != chunk['id'] or
                split.get('parent', {}).get('sha256') != chunk['sha256']):
                raise ValueError('Split binding changed: ' + chunk['id'])
            if not split.get('children'):
                raise ValueError('Empty split: ' + chunk['id'])
            for child in split['children']:
                visit(child)
        else:
            inputs[chunk['id']] = (chunk, 'coverage', '')

    for chunk in load(root / 'chunks.json'):
        visit(chunk)
    for path in sorted((root / 'request-plans').rglob('*.json')):
        plan = load(path)
        chunk = plan.get('chunk')
        if not chunk:
            continue
        key = (chunk['id'], chunk['sha256'])
        value = (chunk, 'request', plan['request'])
        candidates = inputs.setdefault(key, [])
        if value not in candidates:
            candidates.append(value)
    return inputs


def check_collected_response(root, result, inputs):
    """Recheck provenance and content before any document-level merge."""
    binding = result['binding']
    chunk_id = result['chunk_id']
    candidate = inputs.get((chunk_id, binding['chunk_sha256'])) or inputs.get(chunk_id)
    if candidate is None:
        raise ValueError('Accepted chunk is not an active input: ' + chunk_id)
    log = Path(result['log'])
    record = load(log / 'record.json')
    if isinstance(candidate, list):
        matching = []
        for chunk, mode, request in candidate:
            source = next(s for s in load(root / 'corpus-manifest.json')['sources'] if s['id'] == chunk['source'])
            expected = render_prompt(source, Path(chunk['path']).read_text(), checklist_for(source['id']), mode, request)
            if hashlib.sha256(expected.encode()).hexdigest() == record.get('prompt_sha256'):
                matching.append((chunk, mode, request))
        if len(matching) != 1:
            raise ValueError('Accepted request has no unique logged plan: ' + chunk_id)
        candidate = matching[0]
    chunk, mode, request = candidate
    if (result.get('status') != 'completed' or result.get('prompt_version') != PROMPT_VERSION or
        binding.get('prompt_version') != PROMPT_VERSION or
        binding.get('field_guide_sha256') != hashlib.sha256(records.field_guide().encode()).hexdigest() or
        binding['chunk_sha256'] != chunk['sha256'] or sha(Path(chunk['path'])) != chunk['sha256']):
        raise ValueError('Accepted input/version binding changed: ' + chunk_id)
    if record['status'] != 'completed' or not tool_free(record):
        raise ValueError('Accepted call is not tool-free completed: ' + chunk_id)
    if (record.get('prompt_template_sha256') != binding['prompt_template_sha256'] or
        record.get('prompt_version') != binding['prompt_version'] or
        record.get('field_guide_sha256') != binding['field_guide_sha256'] or
        record.get('chunk_sha256') != binding['chunk_sha256'] or
        record.get('prompt_sha256') != sha(log / 'prompt.md')):
        raise ValueError('Accepted call log binding changed: ' + chunk_id)
    source = next(s for s in load(root / 'corpus-manifest.json')['sources'] if s['id'] == chunk['source'])
    expected = render_prompt(source, Path(chunk['path']).read_text(), checklist_for(source['id']), mode, request)
    if hashlib.sha256(expected.encode()).hexdigest() != record['prompt_sha256']:
        raise ValueError('Accepted generated prompt changed: ' + chunk_id)
    command = record['command']
    schema = Path(command[command.index('--output-schema') + 1])
    if (sha(schema) != binding.get('schema_sha256') or
        record.get('schema_sha256') != binding.get('schema_sha256') or
        load(schema) not in (records.model_output_schema(False), records.model_output_schema(True))):
        raise ValueError('Accepted schema binding changed: ' + chunk_id)
    if sha(log / 'final.json') != binding.get('final_sha256'):
        raise ValueError('Accepted final response changed: ' + chunk_id)
    checked = validate_saved_output(root, chunk, load(log / 'final.json'), record['stage'])
    for key in ('records', 'rejected', 'discarded', 'coverage', 'output_records', 'rejection_rate'):
        if checked[key] != result[key]:
            raise ValueError('Accepted response changed: ' + chunk_id + ': ' + key)


def collect_records(root):
    """Only current-version accepted responses; old pilot logs remain immutable."""
    sources = load(root / 'corpus-manifest.json')['sources']
    inputs = collection_inputs(root)
    by_short, coverage = {s['id']: [] for s in sources}, {s['id']: [] for s in sources}
    for path in sorted((root / 'accepted').glob('*.json')):
        result = load(path)
        if result['binding']['prompt_template_sha256'] != sha(HERE / 'prompts/extract-records.md'):
            continue
        if path.stem != result['chunk_id']:
            raise ValueError('Accepted filename/chunk mismatch: ' + str(path))
        check_collected_response(root, result, inputs)
        short = result['chunk_id'].split('.')[0]
        by_short[short].extend(result['records'])
        coverage[short].extend(result['coverage'])
    document_coverage = {}
    for short, items in coverage.items():
        merged = []
        for expected in checklist_for(short)['items']:
            rows = [item for item in items if item['item_id'] == expected['id']]
            extracted = [r for r in rows if r['status'] == '已抽']
            absent = [r for r in rows if r['status'] == '本文档无' and r['checked_pages'] and r['basis']]
            chosen = extracted or absent or rows
            merged.append(dict(item_id=expected['id'], name=expected['name'],
                status='已抽' if extracted else '本文档无' if absent else '未覆盖',
                record_ids=sorted({key for r in extracted for key in r['record_ids']}),
                checked_pages=sorted({page for r in chosen for page in r['checked_pages']}),
                basis=[r['basis'] for r in chosen]))
        write_json(root / 'records' / (short + '.json'), by_short[short])
        write_json(root / 'coverage' / (short + '.json'), merged)
        document_coverage[short] = merged
    write_json(root / 'document-coverage.json', document_coverage)
    return [r for rows in by_short.values() for r in rows], document_coverage


def full_extract(pipeline, round_number=1, workers=2):
    if workers != 2:
        raise ValueError('S2 full extraction requires --workers 2')
    gate = pilot_gate(pipeline.root, round_number)
    if not gate['ok']:
        raise ValueError('Pilot gate failed; full extraction is forbidden')
    chunks = load(pipeline.root / 'chunks.json')
    # Complete the shorter documents first so a later dense-note failure does
    # not consume the shared budget before cross-document evidence is present.
    priority = {'S46': 0, 'S48': 1, 'S50': 2, 'S42': 3, 'S34': 4}
    chunks = sorted(chunks, key=lambda c: (priority.get(c['source'], 9), c['id']))
    outcomes = []
    # Bounded two-worker dispatch; the persistent call ledger enforces 40 even
    # across processes, crashes, probes, failures, retries and supplements.
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(pipeline.run_chunk, chunk, gate['transport_schema']): chunk for chunk in chunks}
        for future in concurrent.futures.as_completed(futures):
            chunk = futures[future]
            try:
                outcomes.append(future.result())
            except (ValueError, RuntimeError) as error:
                outcomes.append(dict(chunk_id=chunk['id'], status='stopped', error=str(error)))
    write_json(pipeline.root / 'full-extraction.json', dict(outcomes=outcomes))
    collect_records(pipeline.root)
    return outcomes


def supplement(pipeline, max_chars=20000):
    import s2_report
    import s2_request
    chunks = load(pipeline.root / 'chunks.json')
    if not s2_report.extraction_complete(pipeline.root, chunks, 'full'):
        raise ValueError('Supplement requires completed full extraction first')
    collect_records(pipeline.root)
    plans = s2_request.plan_requests(pipeline.root, max_chars)
    outcomes = []
    for plan in plans:
        if not plan['eligible']:
            outcomes.append(plan)
            continue
        try:
            result = pipeline.extract_record_chunk(plan['chunk'], stage=plan['stage'],
                transport=True, mode='request', request=plan['request'])
            outcomes.append(dict(source=plan['source'], eligible=True, result=result))
        except (ValueError, RuntimeError) as error:
            outcomes.append(dict(source=plan['source'], eligible=True, status='stopped', error=str(error)))
    write_json(pipeline.root / 'supplements.json', dict(outcomes=outcomes))
    collect_records(pipeline.root)
    return outcomes


def pilot_diagnostics(root, round_number=1):
    from collections import Counter
    rows = []
    for path in sorted((root / 'logs').glob('pilot-r%d-*/attempt-*/acceptance.json' % round_number)):
        result = load(path)
        errors, examples = Counter(), []
        final_path = path.parent / 'final.json'
        raw = load(final_path) if final_path.exists() else {}
        for entry in result.get('rejected', []):
            for error in entry['errors']:
                errors[re.sub(r'\[\d+\]', '[]', error)] += 1
            examples.append(dict(record_id=entry['record'].get('record_id'), errors=entry['errors'],
                excerpts=entry['record'].get('excerpts'), numbers=entry['record'].get('numbers'),
                period=entry['record'].get('period'), as_of=entry['record'].get('as_of'),
                date_notes=entry['record'].get('date_notes')))
        raw_records = raw.get('records', []) if isinstance(raw, dict) else []
        if isinstance(raw, dict) and isinstance(raw.get('records'), list):
            attempt = load(path.parent / 'record.json')
            attempt['output_records'] = len(raw_records)
            attempt['raw_output_count_observed'] = True
            write_json(path.parent / 'record.json', attempt)
        raw_errors = Counter(error for item in raw_records for error in records._fields(
            item, records.model_output_schema()['properties']['records']['items']))
        rows.append(dict(stage=path.parent.parent.name, status=result['status'],
            records=result.get('output_records'), rejection_rate=result.get('rejection_rate'),
            raw_record_count=len(raw_records), raw_json_chars=len(final_path.read_text()) if final_path.exists() else None,
            raw_field_errors=dict(raw_errors),
            coverage_diagnostics=[dict(item_id=item['item_id'],status=item['status'],record_references=len(item['record_ids']))
                                  for item in raw.get('coverage', [])],
            error_counts=dict(errors), examples=examples))
    write_json(root / ('pilot-r%d-diagnostics.json' % round_number), rows)
    return rows


def backfill_output_counts(root):
    paths = sorted((root / 'logs').glob('*/attempt-*/record.json'))
    if any(load(path)['status'] == 'running' for path in paths):
        raise RuntimeError('Wait for active calls before backfilling historical metrics')
    for path in paths:
        record = load(path)
        acceptance = path.parent / 'acceptance.json'
        if acceptance.exists():
            result = load(acceptance)
            if result['status'] != 'completed':
                write_json(root / 'rejected/whole-blocks' / (record['stage'] + '.json'), result)
        final = path.parent / 'final.json'
        if not final.exists():
            continue
        try:
            output = load(final)
        except ValueError:
            continue
        if isinstance(output, dict) and isinstance(output.get('records'), list):
            record['output_records'] = len(output['records'])
            record['raw_output_count_observed'] = True
            write_json(path, record)
    return summarize_usage(root)


def checklist_for(short):
    kind = 'a-share-periodic' if short == 'S34' else 'research-report' if short == 'S42' else 'hk-transaction-announcement'
    return load(HERE / 'checklists' / (kind + '.json'))


def render_prompt(source, text, checklist, mode='coverage', request=''):
    # Explicit allowlist: never expose P1 notes, reference records, or local paths.
    metadata = {key: source.get(key) for key in ('id', 'source_id', 'title', 'document_type')}
    metadata['disclosed_on'] = source.get('date', source.get('published_date'))
    return template('extract-records', MODE=mode, SOURCE_METADATA=json.dumps(metadata, ensure_ascii=False),
        REQUEST=request or '无', CHECKLIST=json.dumps(checklist, ensure_ascii=False), FIELD_GUIDE=records.field_guide(),
        PAGE_TEXT=text if 'PDF PAGE' in text or '[PAGE:' in text else '\n=== PDF PAGE 1 ===\n' + text)


def frozen_pages(root, source):
    result = {}
    for page in source['page_records']:
        path = Path(page['corpus_path'])
        if sha(path) != page['text_sha256']:
            raise ValueError('Frozen page hash changed: ' + str(path))
        result[(source['source_id'], page['page'])] = path.read_bytes().decode('utf-8')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=('init', 'integrity', 'probe', 'extract', 'gate', 'full', 'collect', 'adopt', 'supplement', 'diagnose', 'snapshot', 'metrics'))
    p.add_argument('--run-dir', type=Path, default=DEFAULT_RUN)
    p.add_argument('--round', type=int, default=1)
    p.add_argument('--chunk')
    p.add_argument('--stage')
    p.add_argument('--transport', action='store_true')
    p.add_argument('--workers', type=int, default=2)
    p.add_argument('--codex', default='codex')
    p.add_argument('--stage-timeout', type=int, default=1800)
    args = p.parse_args()
    root = initialize(args.run_dir)
    pipeline = S2Pipeline(root, args)
    if args.command == 'init':
        print(root)
    elif args.command == 'snapshot':
        print(json.dumps(snapshot_prompt(root)))
    elif args.command == 'metrics':
        print(json.dumps(dict(attempts=len(backfill_output_counts(root)))))
    elif args.command == 'integrity':
        print(json.dumps(dict(ok=integrity(root)['ok'])))
    elif args.command == 'probe':
        result = pipeline.probe(args.round, args.transport)
        print(json.dumps(dict(ok=result['ok'], transport=result['transport'], log=result['record']['log'])))
    elif args.command == 'gate':
        print(json.dumps(pilot_gate(root, args.round), ensure_ascii=False))
    elif args.command == 'collect':
        extracted, coverage = collect_records(root)
        print(json.dumps(dict(records=len(extracted), documents=len(coverage))))
    elif args.command == 'full':
        print(json.dumps(dict(chunks=len(full_extract(pipeline, args.round, args.workers)))))
    elif args.command == 'supplement':
        print(json.dumps(dict(documents=len(supplement(pipeline)))))
    elif args.command == 'diagnose':
        rows = pilot_diagnostics(root, args.round)
        print(json.dumps([{k:v for k,v in row.items() if k != 'examples'} for row in rows], ensure_ascii=False))
    elif args.command == 'adopt':
        chunk = next(c for c in load(root / 'chunks.json') if c['id'] == args.chunk)
        log = sorted((root / 'logs' / args.stage).glob('attempt-*/record.json'))[-1].parent
        result = adopt_pilot(root, chunk, log)
        print(json.dumps(dict(status=result['status'], records=len(result['records']), rejection_rate=result['rejection_rate'])))
    else:
        chunks = load(root / 'chunks.json')
        if isinstance(chunks, dict):
            chunks = chunks['chunks']
        chunk = next(c for c in chunks if c['id'] == args.chunk)
        result = pipeline.extract_record_chunk(chunk, stage=args.stage, transport=args.transport)
        print(json.dumps({key: result.get(key) for key in ('status','log','output_records','rejection_rate','error')}))


if __name__ == '__main__':
    main()
