"""S2b runner controls with synthetic input and mocked CLI; no model calls."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import s2


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False))


def pipeline(root, executable=s2.S2B_CLI):
    return s2.S2bPipeline(root, SimpleNamespace(codex=executable, stage_timeout=1))


class InitializeS2bTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.here = Path(self.temp.name).resolve()
        self.root = self.here / 'runs/s2b-synthetic'
        self.baseline = self.here / 'runs/s2-20261022'
        self.chunks = []
        for identifier in s2.S2B_CHUNKS:
            path = self.baseline / 'chunks' / (identifier + '.md')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('Synthetic revenue 125 for ' + identifier)
            self.chunks.append(dict(id=identifier, path=str(path), sha256=s2.sha(path)))
        save(self.baseline / 'chunks.json', self.chunks)
        save(self.baseline / 'corpus-manifest.json', {'sources': []})
        for rel in ('runs/p1-20261021', 'runs/s1-20261021'):
            (self.here / rel).mkdir(parents=True)
            (self.here / rel / 'immutable.txt').write_text('Synthetic immutable text')
        for rel in ('DESIGN.md', 'DESIGN-v2.md'):
            (self.here / rel).write_text('Synthetic immutable design')
        self.patches = [mock.patch.object(s2, 'HERE', self.here),
                        mock.patch.object(s2, 'git', side_effect=lambda path, *args:
                            'codex/knowledge-wiki-codex-spike' if 'branch' in args else 'synthetic-tag'),
                        mock.patch.object(s2.subprocess, 'check_output', return_value='codex-cli 0.163.0\n')]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.temp.cleanup)

    def test_six_input_chunks_are_exact_frozen_s2_hashes(self):
        self.assertEqual(s2.initialize_s2b(self.root), self.root)
        self.assertEqual(s2.load(self.root / 'chunks.json'), self.chunks)
        integrity = s2.load(self.root / 'INPUT-CHUNKS.json')
        self.assertTrue(integrity['ok'])
        self.assertTrue(all(row['matches_s2'] for row in integrity['chunks']))
        self.assertTrue(s2.s2b_integrity(self.root)['ok'])

    def test_changed_chunk_and_incomplete_selection_fail_before_model_call(self):
        Path(self.chunks[0]['path']).write_text('Synthetic changed revenue 999')
        with self.assertRaisesRegex(ValueError, 'hash changed'):
            s2.initialize_s2b(self.root)
        Path(self.chunks[0]['path']).write_text('Synthetic revenue 125 for ' + self.chunks[0]['id'])
        save(self.baseline / 'chunks.json', self.chunks[:-1])
        with self.assertRaisesRegex(ValueError, 'chunk|six|6|selection'):
            s2.initialize_s2b(self.root)

    def test_fixed_cli_version_and_branch_are_required(self):
        with mock.patch.object(s2.subprocess, 'check_output', return_value='codex-cli 0.144.2\n') as check:
            with self.assertRaisesRegex(ValueError, '0.163.0'):
                s2.initialize_s2b(self.root)
            check.assert_called_once_with([s2.S2B_CLI, '--version'], text=True)
        with mock.patch.object(s2, 'git', return_value='main'):
            with self.assertRaisesRegex(ValueError, 'branch'):
                s2.initialize_s2b(self.root)

    def test_coverage_api_refuses_modified_manifest_even_with_unchanged_bytes(self):
        s2.initialize_s2b(self.root)
        changed = deepcopy(self.chunks[0])
        changed['synthetic_override'] = True
        with mock.patch.object(s2.Pipeline, 'call') as launch:
            with self.assertRaisesRegex(ValueError, 'frozen S2 baseline'):
                pipeline(self.root).extract_record_chunk(changed)
            launch.assert_not_called()

    def test_protected_file_or_tag_change_fails_integrity(self):
        s2.initialize_s2b(self.root)
        (self.here / 'DESIGN.md').write_text('Synthetic changed design')
        with self.assertRaisesRegex(ValueError, 'protected input changed'):
            s2.s2b_integrity(self.root)
        (self.here / 'DESIGN.md').write_text('Synthetic immutable design')
        with mock.patch.object(s2, 'git', return_value='changed-tag'):
            with self.assertRaisesRegex(ValueError, 'protected input changed'):
                s2.s2b_integrity(self.root)


class S2bCallBudgetTests(unittest.TestCase):
    def test_new_launch_failure_never_reuses_old_success_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schema = root / 'schema.json'
            schema.write_text('{}')
            (root / 'calls.jsonl').write_text(json.dumps(dict(number=1, stage='probe')) + '\n')
            save(root / 'logs/probe/attempt-01/record.json',
                 dict(call_number=1, status='completed', usage={}))
            original_sha = s2.sha
            with mock.patch.object(s2.subprocess, 'check_output', return_value='codex-cli 0.163.0'), \
                 mock.patch.object(s2, 'config_identity', return_value={}), \
                 mock.patch.object(s2, 'sha', side_effect=lambda path:
                     'synthetic-cli-sha' if str(path) == s2.S2B_CLI else original_sha(path)), \
                 mock.patch.object(s2.Pipeline, 'call', side_effect=RuntimeError('Synthetic early failure')):
                with self.assertRaisesRegex(RuntimeError, 'newly reserved S2b call'):
                    pipeline(root).invoke('probe', 'Synthetic prompt', schema)
            self.assertEqual(len((root / 'calls.jsonl').read_text().splitlines()), 2)

    def test_fixed_path_rejects_path_cli_without_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(s2.subprocess, 'check_output') as version, mock.patch.object(s2.Pipeline, 'call') as launch:
                with self.assertRaisesRegex(ValueError, 'fixed S2b CLI path'):
                    pipeline(root, 'codex').invoke('probe', 'Synthetic prompt', root / 'schema.json')
                version.assert_not_called()
                launch.assert_not_called()
            self.assertFalse((root / 'calls.jsonl').exists())

    def test_failed_launches_consume_all_sixteen_slots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schema = root / 'schema.json'
            schema.write_text('{}')
            original_sha = s2.sha
            def synthetic_sha(path):
                return 'synthetic-cli-sha' if str(path) == s2.S2B_CLI else original_sha(path)
            with mock.patch.object(s2.subprocess, 'check_output', return_value='codex-cli 0.163.0'), \
                 mock.patch.object(s2, 'config_identity', return_value={}), \
                 mock.patch.object(s2, 'sha', side_effect=synthetic_sha), \
                 mock.patch.object(s2.Pipeline, 'call', side_effect=RuntimeError('Synthetic launch failed')) as launch:
                runner = pipeline(root)
                for number in range(1, 17):
                    with self.assertRaisesRegex(RuntimeError, 'Synthetic launch failed'):
                        runner.invoke('probe', 'Synthetic prompt', schema)
                    self.assertEqual(len((root / 'calls.jsonl').read_text().splitlines()), number)
                with self.assertRaisesRegex(RuntimeError, 'limit 16'):
                    runner.invoke('retry', 'Synthetic prompt', schema)
                self.assertEqual(launch.call_count, 16)
                self.assertTrue(all(call.kwargs['read_only'] for call in launch.call_args_list))
                self.assertTrue(all(call.kwargs['metadata']['cli']['path'] == s2.S2B_CLI
                                    for call in launch.call_args_list))
                self.assertEqual([json.loads(line)['number'] for line in (root / 'calls.jsonl').read_text().splitlines()],
                                 list(range(1, 17)))


class S2bIssuerPromptTests(unittest.TestCase):
    def test_legal_name_verbatim_hash_and_coverage_code_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            here = Path(directory)
            page = here / 'runs/frozen/p0001.txt'
            page.parent.mkdir(parents=True)
            page.write_text('合成股份有限公司\nSynthetic Corporation Limited')
            legal = {'zh': {'name': '合成股份有限公司', 'path': str(page.relative_to(here)), 'sha256': s2.sha(page)},
                     'en': {'name': 'Synthetic Corporation Limited', 'path': str(page.relative_to(here)), 'sha256': s2.sha(page)}}
            save(here / 'entities/issuers.json', {'issuers': {'000001.SH': legal}})
            with mock.patch.object(s2, 'HERE', here):
                for metadata in ({'company_code': '000001.SH'}, {'coverage': [{'company_code': '000001.SH'}, {'product': 'Synthetic product'}]}):
                    names = s2.s2b_issuer_names({'metadata_record': metadata})
                    self.assertEqual(names, [{'company_code': '000001.SH', 'legal_names':
                        {'zh': '合成股份有限公司', 'en': 'Synthetic Corporation Limited'}}])
                legal['zh']['name'] = '合成股份公司'
                save(here / 'entities/issuers.json', {'issuers': {'000001.SH': legal}})
                with self.assertRaisesRegex(ValueError, 'Unverified legal name'):
                    s2.s2b_issuer_names({'metadata_record': {'company_code': '000001.SH'}})
                legal['zh']['name'] = '合成股份有限公司'
                save(here / 'entities/issuers.json', {'issuers': {'000001.SH': legal}})
                page.write_text(page.read_text() + '\nSynthetic changed page')
                with self.assertRaisesRegex(ValueError, 'Unverified legal name'):
                    s2.s2b_issuer_names({'metadata_record': {'company_code': '000001.SH'}})

    def test_prompt_allowlist_scope_and_contiguous_excerpt_rules(self):
        source = dict(id='SYN', source_id='synthetic-source', title='Synthetic title', date='2026-01-01',
                      doc_type='synthetic', path='DO_NOT_INCLUDE', p1_notes='DO_NOT_INCLUDE',
                      s1_records='DO_NOT_INCLUDE', s2_records='DO_NOT_INCLUDE')
        with mock.patch.object(s2, 's2b_issuer_names', return_value=[]):
            prompt = s2.render_s2b_prompt(source, '=== PDF PAGE 7 ===\nSynthetic revenue 125',
                                          {'items': [{'id': 'T01', 'name': 'Synthetic revenue'}]})
        self.assertNotIn('DO_NOT_INCLUDE', prompt)
        self.assertNotIn('"additionalProperties"', prompt)
        self.assertNotIn('{{', prompt)
        for required in (s2.S2B_VERSION, 'coverage只抽属于必抽清单', 'outside_index',
                         'request仅抽请求指定', 'READ-ONLY CONTEXT', '分成多个连续摘录', '全部', 'scope=consolidated'):
            with self.subTest(required=required):
                self.assertIn(required, prompt)


class S2bRequestIsolationTests(unittest.TestCase):
    def test_custom_request_preserves_requested_page_order_and_header_is_context_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save(root / 'corpus-manifest.json', {'sources': [dict(id='SYN', source_id='synthetic-source')]})
            pages = {('synthetic-source', p): f'Synthetic page {p}' for p in (1, 2, 3, 4)}
            request = dict(request_id='REQ-custom.1', source='SYN', pages=[3, 2],
                           fact='Synthetic asset additions by category', header_pages=[1, 2, 1])
            with mock.patch.object(s2, 'frozen_pages', return_value=pages):
                chunk, query = s2.s2b_request_chunk(root, request['request_id'], request_spec=request)
            self.assertEqual(chunk['core_pages'], [3, 2])
            self.assertEqual([f['page'] for f in chunk['fragments']], [3, 2])
            self.assertEqual(chunk['context_pages'], [1])
            self.assertEqual([f['page'] for f in chunk['context_fragments']], [1])
            text = Path(chunk['path']).read_text()
            self.assertIn('=== READ-ONLY CONTEXT PDF PAGE 1 ===', text)
            self.assertNotIn('Synthetic page 4', text)
            self.assertEqual(query, request['fact'])
            saved = s2.load(Path(chunk['path']).with_suffix('.json'))
            self.assertEqual(saved['request_id'], request['request_id'])

    def test_custom_request_rejects_unsafe_or_disagreeing_identifiers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for identifier, provided in [('../escape', '../escape'), ('REQ1', 'REQ2'), ('', '')]:
                request = dict(request_id=provided, source='SYN', pages=[2], fact='Synthetic value')
                with self.subTest(identifier=identifier), self.assertRaisesRegex(ValueError, 'request id'):
                    s2.s2b_request_chunk(root, identifier, request_spec=request)
            self.assertFalse((root / 'requests').exists())

    def test_custom_request_rejects_invalid_pages_fact_and_header_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = dict(request_id='REQ1', source='SYN', pages=[2], fact='Synthetic value')
            invalid = [dict(pages=[]), dict(pages=[0]), dict(pages=[-1]), dict(pages=[True]),
                       dict(pages=['2']), dict(pages=[2, 2]), dict(pages='2'),
                       dict(fact='  '), dict(fact=None), dict(header_pages=[0]), dict(header_pages=['1'])]
            for change in invalid:
                request = dict(base, **change)
                with self.subTest(change=change), self.assertRaises(ValueError):
                    s2.s2b_request_chunk(root, 'REQ1', request_spec=request)
            self.assertFalse((root / 'requests').exists())

    def test_request_contains_only_requested_pages_unless_header_context_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save(root / 'corpus-manifest.json', {'sources': [dict(id='S34', source_id='synthetic-a'),
                                                           dict(id='S42', source_id='synthetic-b')]})
            pages = {('synthetic-a', p): f'Synthetic A page {p}' for p in (110, 111, 112, 113)}
            pages.update({('synthetic-b', p): f'Synthetic B page {p}' for p in (1, 2, 3, 4)})
            with mock.patch.object(s2, 'frozen_pages', return_value=pages):
                for identifier, core, context, absent in [('R1', [111, 112], [], 'Synthetic A page 110'),
                                                          ('R2', [3], [], 'Synthetic B page 2')]:
                    chunk, query = s2.s2b_request_chunk(root, identifier)
                    self.assertEqual(chunk['core_pages'], core)
                    self.assertEqual(chunk['context_pages'], context)
                    self.assertEqual([f['page'] for f in chunk['fragments']], core)
                    self.assertEqual([f['page'] for f in chunk['context_fragments']], context)
                    text = Path(chunk['path']).read_text()
                    self.assertNotIn('READ-ONLY CONTEXT PDF PAGE', text)
                    self.assertNotIn(absent, text)
                    self.assertEqual(chunk['sha256'], s2.sha(Path(chunk['path'])))
                    self.assertEqual(chunk['chars'], sum(len(f['text']) for f in chunk['fragments']))
                chunk, query = s2.s2b_request_chunk(root, 'R1', header_pages=[110])
                self.assertEqual(chunk['context_pages'], [110])
                self.assertEqual([f['page'] for f in chunk['context_fragments']], [110])
                text = Path(chunk['path']).read_text()
                self.assertIn('=== READ-ONLY CONTEXT PDF PAGE 110 ===', text)
                self.assertNotIn('Synthetic A page 113', text)

    def test_acceptance_receives_only_supplied_pages_spans_and_context_discard_list(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = dict(id='S34', source_id='synthetic-source', date='2026-01-01')
            save(root / 'corpus-manifest.json', {'sources': [source]})
            text = root / 'requests/R1.md'
            text.parent.mkdir()
            text.write_text('Synthetic header 2026\nSynthetic asset 125')
            chunk = dict(id='R1', source_id='S34', path=str(text), sha256=s2.sha(text), chars=19,
                         core_pages=[2], context_pages=[1],
                         fragments=[dict(page=2, text='Synthetic asset 125')],
                         context_fragments=[dict(page=1, text='Synthetic header 2026')])
            save(root / 'chunks.json', [])
            log = root / 'logs/request-R1/attempt-01'
            save(log / 'final.json', {})
            record = dict(log=str(log), call_number=1, status='completed', command_count=0, tools=[],
                          schema_sha256='synthetic-schema', prompt_template_sha256='synthetic-template')
            accepted = dict(records=[], rejected=[], discarded=[], output_records=0,
                            rejection_rate=0, soft_queue=[], outside_index=[], entities=[])
            runner = pipeline(root)
            with mock.patch.object(runner, 'invoke', return_value=record) as invoke, \
                 mock.patch.object(runner, 'schemas', return_value=root / 'schema.json'), \
                 mock.patch.object(s2, 'render_s2b_prompt', return_value='Synthetic request prompt'), \
                 mock.patch.object(s2, 'summarize_s2b_usage'), \
                 mock.patch.object(s2, 'frozen_pages', return_value={('synthetic-source', 1): 'Synthetic header 2026',
                     ('synthetic-source', 2): 'Synthetic asset 125', ('synthetic-source', 3): 'Hidden value 999'}), \
                 mock.patch.object(s2.records, 'accept_model_output', return_value=accepted) as accept:
                result = runner.extract_record_chunk(chunk, mode='request', request='Synthetic asset request')
                self.assertEqual(result['status'], 'completed')
                kwargs = accept.call_args.kwargs
                self.assertEqual(set(kwargs['pages']), {('synthetic-source', 1), ('synthetic-source', 2)})
                self.assertEqual(kwargs['context_pages'], [1])
                self.assertEqual(kwargs['supplied_spans'], {1: ['Synthetic header 2026'], 2: ['Synthetic asset 125']})
                self.assertTrue(kwargs['compact'])
                self.assertEqual(kwargs['checklist']['items'][0]['id'], 'R1')
                text.write_text('Synthetic changed asset 999')
                with self.assertRaisesRegex(ValueError, 'input hash changed'):
                    runner.extract_record_chunk(chunk, mode='request', request='Synthetic asset request')
                self.assertEqual(invoke.call_count, 1)


class S2bRevalidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.chunk_id = 'SYN.s01'
        self.log = self.root / 'logs' / ('extract-' + self.chunk_id) / 'attempt-01'
        page = self.root / 'synthetic-input.md'
        page.write_text('Synthetic revenue 125')
        self.chunk = dict(id=self.chunk_id, source_id='SYN', path=str(page), sha256=s2.sha(page),
                          core_pages=[2], context_pages=[], fragments=[dict(page=2, text=page.read_text())])
        self.receipt = dict(status='completed', command_count=0, tools=[], call_number=7,
                            prompt_version='synthetic-original-prompt-1',
                            schema_sha256='synthetic-original-schema',
                            prompt_template_sha256='synthetic-original-template',
                            s2_status='invalid_output', usage={'output_tokens': 20})
        save(self.log / 'record.json', self.receipt)
        save(self.log / 'input-chunk.json', self.chunk)
        save(self.log / 'input-mode.json', dict(mode='coverage', request=''))
        save(self.log / 'final.json', dict(synthetic_output='Synthetic original response'))
        save(self.log / 'acceptance.json', dict(status='invalid_output', error='Synthetic old validation error'))
        save(self.root / 'corpus-manifest.json', {'sources': [dict(id='SYN', source_id='synthetic-source', date='2026-01-01')]})
        accepted = dict(records=[], rejected=[], discarded=[], output_records=0, rejection_rate=0,
                        soft_queue=[], outside_index=[], entities=[])
        self.accept_mock = mock.patch.object(s2.records, 'accept_model_output', return_value=accepted)
        self.accept = self.accept_mock.start()
        self.addCleanup(self.accept_mock.stop)
        for patch in (mock.patch.object(s2, 'checklist_for', return_value={'items': []}),
                      mock.patch.object(s2, 'frozen_pages', return_value={('synthetic-source', 2): page.read_text()}),
                      mock.patch.object(s2, 'summarize_s2b_usage')):
            patch.start()
            self.addCleanup(patch.stop)

    def test_original_receipt_acceptance_and_model_response_survive_revalidation(self):
        original_acceptance = (self.log / 'acceptance.json').read_bytes()
        original_output = (self.log / 'final.json').read_bytes()
        result = s2.s2b_revalidate(self.root, self.chunk_id)
        self.assertEqual(s2.load(self.log / 'receipt-before-revalidation.json'), self.receipt)
        self.assertEqual((self.log / 'acceptance.json').read_bytes(), original_acceptance)
        self.assertEqual((self.log / 'final.json').read_bytes(), original_output)
        self.assertEqual(s2.load(self.log / 'acceptance-revalidated.json'), result)
        self.assertEqual(s2.load(self.root / 'accepted' / (self.chunk_id + '.json')), result)
        backup = (self.log / 'receipt-before-revalidation.json').read_bytes()
        s2.s2b_revalidate(self.root, self.chunk_id)
        self.assertEqual((self.log / 'receipt-before-revalidation.json').read_bytes(), backup)
        self.assertEqual((self.log / 'acceptance.json').read_bytes(), original_acceptance)
        self.assertEqual(s2.load(self.log / 'record.json')['s2_status'], 'completed')

    def test_recorded_prompt_version_is_reused_instead_of_current_global_version(self):
        with mock.patch.object(s2, 'S2B_VERSION', 'synthetic-current-prompt-99'):
            result = s2.s2b_revalidate(self.root, self.chunk_id)
        self.assertEqual(self.accept.call_args.kwargs['extraction_version'], 'synthetic-original-prompt-1')
        self.assertEqual(result['prompt_version'], 'synthetic-original-prompt-1')
        self.assertEqual(result['binding']['prompt_version'], 'synthetic-original-prompt-1')
        self.assertEqual(result['binding']['schema_sha256'], 'synthetic-original-schema')
        self.assertEqual(result['binding']['prompt_template_sha256'], 'synthetic-original-template')
        self.assertEqual(s2.load(self.log / 'record.json')['prompt_version'], 'synthetic-original-prompt-1')


if __name__ == '__main__':
    unittest.main()
