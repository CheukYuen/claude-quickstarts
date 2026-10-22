"""Synthetic collection provenance; no model calls or licensed page text."""
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import records
import s2
from run import load, sha, write_json
from test_s2 import model_record, output


class CollectionProvenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.checklist = dict(items=[dict(id='T01', name='Synthetic revenue')])
        patch = mock.patch.object(s2, 'checklist_for', return_value=self.checklist)
        patch.start()
        self.addCleanup(patch.stop)
        page = self.root / 'pages/p2.txt'
        page.parent.mkdir()
        page.write_text('Revenue 1,250.50')
        self.source = dict(id='S46', source_id='synthetic-report', title='Synthetic report', date='2026-03-01',
            pages=[2], page_records=[dict(page=2, corpus_path=str(page), text_sha256=sha(page))])
        self.fragment = dict(page=2, start=0, end=len(page.read_text()), text=page.read_text(), page_sha256=sha(page))
        self.chunk = self.make_chunk('S46.s01', [self.fragment])
        write_json(self.root / 'corpus-manifest.json', dict(sources=[self.source]))
        write_json(self.root / 'chunks.json', [self.chunk])
        self.schema = self.root / 'schemas/model-transport.json'
        write_json(self.schema, records.model_output_schema(True))
        self.result = self.make_accepted(self.chunk)

    def make_chunk(self, identifier, fragments):
        path = self.root / 'inputs' / (identifier + '.md')
        path.parent.mkdir(exist_ok=True)
        path.write_text('\n'.join('[PAGE: %s]\n%s' % (f['page'], f['text']) for f in fragments))
        return dict(id=identifier, source='S46', source_id='synthetic-report', path=str(path), sha256=sha(path),
            chars=sum(len(f['text']) for f in fragments), core_pages=sorted({f['page'] for f in fragments}),
            context_pages=[], fragments=deepcopy(fragments), context_fragments=[], sections=['Synthetic section'])

    def make_accepted(self, chunk, mode='coverage', request='', stage=None):
        stage = stage or ('request-S46' if mode == 'request' else 'extract-' + chunk['id'])
        log = self.root / 'logs' / stage / 'attempt-01'
        log.mkdir(parents=True, exist_ok=True)
        prompt = s2.render_prompt(self.source, Path(chunk['path']).read_text(), self.checklist, mode, request)
        (log / 'prompt.md').write_text(prompt)
        write_json(log / 'final.json', output([model_record()]))
        template_sha = sha(s2.HERE / 'prompts/extract-records.md')
        guide_sha = hashlib.sha256(records.field_guide().encode()).hexdigest()
        record = dict(stage=stage, chunk_id=chunk['id'], chunk_sha256=chunk['sha256'],
            status='completed', s2_status='completed', command_count=0, tools=[], mode=mode,
            request=request, request_text=request, prompt_version=s2.PROMPT_VERSION,
            prompt_template_sha256=template_sha, field_guide_sha256=guide_sha,
            schema_sha256=sha(self.schema), prompt_sha256=sha(log / 'prompt.md'),
            command=['synthetic-cli', 'exec', '--output-schema', str(self.schema)])
        write_json(log / 'record.json', record)
        write_json(log / 'input-chunk.json', chunk)
        write_json(log / 'input-mode.json', dict(mode=mode, request=request))
        checked = s2.validate_saved_output(self.root, chunk, load(log / 'final.json'), stage)
        result = dict(status='completed', chunk_id=chunk['id'], prompt_version=s2.PROMPT_VERSION,
            call_number=1, log=str(log), **checked, binding=dict(chunk_sha256=chunk['sha256'],
            prompt_template_sha256=template_sha, prompt_version=s2.PROMPT_VERSION,
            field_guide_sha256=guide_sha, schema_sha256=sha(self.schema), final_sha256=sha(log / 'final.json')))
        write_json(log / 'acceptance.json', result)
        write_json(self.root / 'accepted' / (chunk['id'] + '.json'), result)
        return result

    def save_result(self):
        write_json(self.root / 'accepted' / (self.result['chunk_id'] + '.json'), self.result)

    def test_valid_current_response_is_collected(self):
        values, coverage = s2.collect_records(self.root)
        self.assertEqual(len(values), 1)
        self.assertEqual(coverage['S46'][0]['status'], '已抽')

    def test_only_older_template_is_silently_skipped(self):
        self.result['binding']['prompt_template_sha256'] = 'older-template'
        self.result['binding']['field_guide_sha256'] = 'obsolete'
        self.save_result()
        values, coverage = s2.collect_records(self.root)
        self.assertEqual(values, [])
        self.assertEqual(coverage['S46'][0]['status'], '未覆盖')

    def test_same_template_stale_version_and_field_guide_fail(self):
        original = deepcopy(self.result)
        for key in ('prompt_version', 'field_guide_sha256'):
            self.result = deepcopy(original)
            self.result['binding'][key] = 'stale'
            self.save_result()
            with self.subTest(key=key), self.assertRaises(ValueError):
                s2.collect_records(self.root)

    def test_logged_version_and_guide_metadata_must_equal_accepted_binding(self):
        path = Path(self.result['log']) / 'record.json'
        original = load(path)
        self.assertEqual(original['prompt_version'], self.result['binding']['prompt_version'])
        self.assertEqual(original['field_guide_sha256'], self.result['binding']['field_guide_sha256'])
        for key in ('prompt_version', 'field_guide_sha256'):
            changed = deepcopy(original)
            changed[key] = 'metadata-only-tampering'
            write_json(path, changed)
            with self.subTest(key=key), self.assertRaises(ValueError):
                s2.collect_records(self.root)
        write_json(path, original)
        self.assertEqual(len(s2.collect_records(self.root)[0]), 1)

    def test_logged_prompt_tampering_and_final_tampering_fail(self):
        log = Path(self.result['log'])
        prompt = (log / 'prompt.md').read_bytes()
        (log / 'prompt.md').write_text('tampered synthetic prompt')
        with self.assertRaises(ValueError):
            s2.collect_records(self.root)
        (log / 'prompt.md').write_bytes(prompt)
        (log / 'final.json').write_text('{"tampered":true}')
        with self.assertRaises(ValueError):
            s2.collect_records(self.root)

    def test_canonical_schema_is_required_even_if_hashes_are_coherently_rewritten(self):
        write_json(self.schema, {'type': 'object'})
        self.result['binding']['schema_sha256'] = sha(self.schema)
        self.save_result()
        log = Path(self.result['log'])
        record = load(log / 'record.json')
        record['schema_sha256'] = sha(self.schema)
        write_json(log / 'record.json', record)
        with self.assertRaises(ValueError):
            s2.collect_records(self.root)

    def test_accepted_payload_cannot_diverge_from_original_final(self):
        self.result['records'][0]['object_key']['subject'] = 'Tampered subject'
        self.save_result()
        with self.assertRaises(ValueError):
            s2.collect_records(self.root)

    def test_obsolete_chunk_not_in_active_topology_fails(self):
        write_json(self.root / 'chunks.json', [])
        with self.assertRaises(ValueError):
            s2.collect_records(self.root)

    def test_active_split_leaf_can_be_collected_but_parent_cannot(self):
        second = self.root / 'pages/p3.txt'
        second.write_text('Synthetic second page 50')
        fragment = dict(page=3, start=0, end=len(second.read_text()), text=second.read_text(), page_sha256=sha(second))
        self.source['pages'].append(3)
        self.source['page_records'].append(dict(page=3, corpus_path=str(second), text_sha256=sha(second)))
        write_json(self.root / 'corpus-manifest.json', dict(sources=[self.source]))
        parent = self.make_chunk('S46.parent', [self.fragment, fragment])
        sibling = self.make_chunk('S46.s02', [fragment])
        write_json(self.root / 'chunks.json', [parent])
        write_json(self.root / 'splits/S46.parent.json', dict(parent=parent, children=[self.chunk, sibling],
            prompt_template_sha256=sha(s2.HERE / 'prompts/extract-records.md')))
        values, _ = s2.collect_records(self.root)
        self.assertEqual(len(values), 1)
        self.make_accepted(parent)
        with self.assertRaises(ValueError):
            s2.collect_records(self.root)

    def test_request_uses_original_history_after_top_plan_marks_attempted(self):
        request_chunk = self.make_chunk('S46.request1', [self.fragment])
        request = 'Only synthetic T01 revenue.'
        self.make_accepted(request_chunk, mode='request', request=request)
        plan = dict(source='S46', eligible=True, chunk=request_chunk, request=request)
        write_json(self.root / 'request-plans/history/S46-original.json', plan)
        write_json(self.root / 'request-plans/S46.json', dict(source='S46', eligible=False,
            chunk=None, request='', reason='already_attempted'))
        values, _ = s2.collect_records(self.root)
        self.assertEqual(len(values), 2)
        (self.root / 'request-plans/history/S46-original.json').unlink()
        with self.assertRaises(ValueError):
            s2.collect_records(self.root)

    def test_altered_request_text_cannot_reuse_identical_source_span_hash(self):
        request_chunk = self.make_chunk('S46.request1', [self.fragment])
        self.make_accepted(request_chunk, mode='request', request='Synthetic original T01.')
        write_json(self.root / 'request-plans/S46.json', dict(source='S46', eligible=True,
            chunk=request_chunk, request='Different synthetic request T02.'))
        with self.assertRaises(ValueError):
            s2.collect_records(self.root)

    def test_preflight_history_with_same_spans_selects_exact_logged_request(self):
        request_chunk = self.make_chunk('S46.request1', [self.fragment])
        self.make_accepted(request_chunk, mode='request', request='Synthetic final T01 request.')
        write_json(self.root / 'request-plans/history/S46-preflight.json', dict(source='S46', eligible=True,
            chunk=request_chunk, request='Different unexecuted synthetic preflight request.'))
        write_json(self.root / 'request-plans/S46.json', dict(source='S46', eligible=True,
            chunk=request_chunk, request='Synthetic final T01 request.'))
        values, _ = s2.collect_records(self.root)
        self.assertEqual(len(values), 2)


if __name__ == '__main__':
    unittest.main()
