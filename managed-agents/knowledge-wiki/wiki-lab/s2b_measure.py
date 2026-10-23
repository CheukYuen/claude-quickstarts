#!/usr/bin/env python3
"""S2b measurements and source-bound comparison queues; never generates prose reports.

Matching reuses S2's permissive numeric-or-span rule. A match is a candidate,
not semantic acceptance. Explicit review decisions live in adjudications.json.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
from statistics import mean

import s2_compare
import s2_qa

HERE = Path(__file__).resolve().parent
SAMPLES = ('S46.s01', 'S50.s01', 'S34.s03', 'S34.s09', 'S34.s12', 'S42.s01')
FEATURES = ('characters', 'numeric_tokens', 'table_rows')
TARGET_ITEMS = {
    'S50.c01.I0014': 'S50.s01', 'S50.c01.I0018': 'S50.s01',
    'S50.c01.I0019': 'S50.s01', 'S50.c01.I0020': 'S50.s01',
    'S34.c02.I0307': 'R1', 'S34.c02.I0324': 'R1',
    'S42.c01.I0083': 'R2', 'S42.c01.I0089': 'R2', 'S42.c01.I0099': 'R2',
}
CLAIMED_S2 = {
    'S46.s01': (32285, 632, 47, 2), 'S50.s01': (34605, 644, 55, 5),
    'S34.s03': (38139, 755, 72, 0), 'S34.s09': (72515, 1410, 149, 13),
    'S34.s12': (97082, 1408, 264, 0), 'S42.s01': (31864, 1493, 16, 26),
}


def read(path, default=None):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else default


def dump(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')


def attempts(root):
    value = read(Path(root) / 'usage.json', {})
    values = value.get('attempts', []) if isinstance(value, dict) else value
    if values:
        return values
    return [read(p) for p in sorted((Path(root) / 'logs').glob('*/attempt-*/record.json'))]


def chunks_with_children(root):
    root = Path(root)
    chunks = {c['id']: c for c in read(root / 'chunks.json', [])}
    for path in sorted((root / 'splits').glob('*.json')):
        obj = read(path)
        for child in obj.get('children', []):
            chunks[child['id']] = child
    return chunks


def accepted(root, chunk_id):
    return read(Path(root) / 'accepted' / (chunk_id + '.json'), {})


def accepted_records(root, chunk_id):
    obj = accepted(root, chunk_id)
    return obj.get('records', []) if isinstance(obj, dict) else obj


def choose_attempt(root, chunk_id, values=None):
    """Use the saved accepted call binding, never silently choose a later retry."""
    saved = accepted(root, chunk_id)
    values = attempts(root) if values is None else values
    matches = [a for a in values if a.get('chunk_id') == chunk_id and
               s2_compare.effective_status(a) in ('completed', 'accepted', 'success')]
    if saved.get('log'):
        exact = [a for a in matches if a.get('log') == saved['log']]
        return exact[-1] if exact else None
    if saved.get('call_number') is not None:
        return next((a for a in matches if a.get('call_number') == saved['call_number']), None)
    return max(matches, key=lambda a: (a.get('call_number') or 0, a.get('attempt') or 0)) if matches else None


def core_text(chunk):
    """Use only core frozen spans; shared headers/context never enter predictors."""
    return '\n'.join(f['text'] for f in chunk.get('fragments', []))


def source_features(text):
    lines = text.splitlines()
    count = lambda s: len(s2_compare.NUMBER_RE.findall(s))
    # Operational proxy, not an assertion that PDF table structure was parsed.
    table_rows = sum(count(line) >= 2 and bool(re.search(r'\S\s{2,}\S|\|', line)) for line in lines)
    return dict(characters=len(text), numeric_tokens=count(text), table_rows=table_rows)


def density_row(attempt, chunk=None, saved=None):
    if attempt is None:
        return None
    row = s2_compare.usage_row(attempt)
    chars = attempt.get('source_chars')
    if chars is None and chunk:
        chars = chunk.get('chars', len(core_text(chunk)))
    if saved is not None:
        row['records'] = len(saved.get('records', []))
        row['rejected_records'] = len(saved.get('rejected', []))
    row.update(chunk_id=attempt.get('chunk_id'), call_number=attempt.get('call_number'),
               source_chars=chars, log=attempt.get('log'), chunk_sha256=attempt.get('chunk_sha256'))
    row['accepted_records'] = row['records']
    row['expanded_records'] = saved.get('output_records', row.get('output_records')) if saved is not None else row.get('output_records')
    row['output_tokens_per_record'] = row['output'] / row['records'] if row['output'] is not None and row['records'] else None
    row['output_tokens_per_expanded_record'] = (row['output'] / row['expanded_records']
        if row['output'] is not None and row['expanded_records'] else None)
    row['output_tokens_per_10000_characters'] = row['output'] * 10000 / chars if row['output'] is not None and chars else None
    return row


def attempt_density(attempt, chunk=None):
    """Keep usage from the model log and acceptance from the latest code replay."""
    log = Path(attempt['log']) if attempt.get('log') else None
    path = None
    if log is not None:
        path = next((p for p in (log/'acceptance-revalidated.json', log/'acceptance.json') if p.exists()), None)
    row = density_row(attempt, chunk, read(path) if path is not None else None)
    row['validation_receipt'] = str(path) if path is not None else None
    return row


def reduction(before, after):
    return (1 - after / before) if before and after is not None else None


def compare_costs(s2_root, run):
    old_calls, new_calls = attempts(s2_root), attempts(run)
    old_chunks, new_chunks = chunks_with_children(s2_root), chunks_with_children(run)
    rows = []
    for chunk_id in SAMPLES:
        old = density_row(choose_attempt(s2_root, chunk_id, old_calls), old_chunks.get(chunk_id), accepted(s2_root, chunk_id))
        new = density_row(choose_attempt(run, chunk_id, new_calls), new_chunks.get(chunk_id), accepted(run, chunk_id))
        actual = (old['output'], old['seconds'], old['records'], old['rejected_records']) if old else None
        claimed = CLAIMED_S2[chunk_id]
        differences = ([dict(field=key, kickoff_claim=prior, measured=value)
                        for key, prior, value in zip(('output', 'seconds', 'records', 'rejected'), claimed, actual)
                        if prior != value] if actual else [])
        rows.append(dict(chunk_id=chunk_id, s2=old, s2b=new,
                         same_core_input_hash=(old['chunk_sha256'] == new['chunk_sha256']) if old and new else None,
                         output_reduction=reduction(old['output'], new['output']) if old and new else None,
                         time_reduction=reduction(old['seconds'], new['seconds']) if old and new else None,
                         kickoff_discrepancies=differences))
    paired = [r for r in rows if r['s2'] and r['s2b']]
    totals = {}
    for side in ('s2', 's2b'):
        selected = [r[side] for r in paired]
        totals[side] = {key: sum(r[key] for r in selected) if selected and all(r[key] is not None for r in selected) else None
                        for key in ('input', 'cached', 'output', 'reasoning', 'seconds', 'records', 'expanded_records', 'rejected_records', 'source_chars')}
    for key in ('output', 'seconds'):
        totals[key + '_reduction'] = reduction(totals['s2'][key], totals['s2b'][key])
    probe_calls = [a for a in new_calls if a.get('is_probe') or 'schema' in a.get('stage', '')]
    probe = [density_row(a) for a in probe_calls]
    schemas = list((Path(run) / 'schemas').glob('*transport*.json'))
    if not schemas:
        schemas = list((Path(run) / 'schemas').glob('*compact*.json'))
    schema_chars = [dict(path=str(p), characters=len(p.read_text()), sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in schemas]
    requests = [attempt_density(a, new_chunks.get(a.get('chunk_id')))
                for a in new_calls if a.get('mode') == 'request']
    request_totals = {key: s2_compare.usage_totals([a for a in new_calls
        if a.get('mode') == 'request' and a.get('chunk_id') == key])
        for key in sorted({a['chunk_id'] for a in new_calls if a.get('mode') == 'request'})}
    prompt_versions = [dict(version=path.name,
        guide_characters=len((path / 'field-guide.md').read_text()),
        template_characters=len((path / 'template.md').read_text()))
        for path in sorted((Path(run) / 'prompt-versions').glob('*')) if path.is_dir()]
    ledger = [json.loads(line) for line in (Path(run) / 'calls.jsonl').read_text().splitlines() if line.strip()] if (Path(run) / 'calls.jsonl').exists() else []
    reserved = max((row['number'] for row in ledger), default=len(new_calls))
    return dict(schema='wiki-lab-s2b-cost/1', rows=rows, paired_blocks=len(paired), paired_totals=totals,
                call_budget=dict(limit=16, reserved=reserved, remaining=16-reserved, within_limit=reserved <= 16,
                                 reservation_ledger=ledger),
                all_attempt_totals=s2_compare.usage_totals(new_calls),
                fixed_input=dict(s2_probe_input_tokens=23365, s2_transport_schema_characters=73082,
                                 s2b_probes=probe, s2b_schemas=schema_chars, prompt_versions=prompt_versions),
                requests=requests, request_totals=request_totals,
                all_s2b_calls=[attempt_density(a) for a in new_calls],
                limitations=['Output tokens already include reasoning; never add reasoning twice.',
                             'Probe input measures the complete fixed call envelope, not schema tokens alone.',
                             'Main session and subagent development/review usage is unmeasured.',
                             'Matching core hashes does not mean identical prompt or metadata.'])


def page_map(root):
    result = {}
    manifest = read(Path(root) / 'corpus-manifest.json', {})
    for source in manifest.get('sources', []):
        for page in source['page_records']:
            path = Path(page['corpus_path'])
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != page['text_sha256']:
                raise ValueError('Frozen source changed: %s p%s' % (source['id'], page['page']))
            result[(source['source_id'], page['page'])] = raw.decode()
    return result


def review_for(decisions, key, allowed):
    decision = decisions.get(key)
    if not decision:
        return dict(status='pending', decision=None, reason=None, evidence_pages=[])
    if decision.get('decision') not in allowed or not decision.get('reason') or not decision.get('evidence_pages'):
        raise ValueError('Invalid review decision: ' + key)
    return dict(status='reviewed', **decision)


def _recall_summary(rows):
    return dict(references=len(rows), matched=sum(r['candidate_match'] for r in rows),
                unmatched=sum(not r['candidate_match'] for r in rows),
                decisions=dict(Counter(r['review']['decision'] for r in rows if r['review'].get('decision'))),
                pending=sum(r['review']['status'] == 'pending' for r in rows if not r['candidate_match']))


def checklist_recall(s2_root, run, pages, decisions):
    blocks, all_rows = [], []
    for chunk_id in SAMPLES:
        old, new = accepted(s2_root, chunk_id), accepted_records(run, chunk_id)
        if not accepted(run, chunk_id):
            blocks.append(dict(chunk_id=chunk_id, status='not_run', rows=[], summary=None))
            continue
        by_id = {r['record_id']: r for r in old.get('records', [])}
        rows = []
        for item in old.get('coverage', []):
            if item['status'] != '已抽':
                continue
            refs = [by_id[rid] for rid in item.get('record_ids', []) if rid in by_id]
            matched = s2_compare.pilot_match(refs, new, pages)
            links = defaultdict(list)
            for link in matched['matches']:
                links[link['reference_id']].append(link)
            for reference in refs:
                rid = reference['record_id']
                key = '/'.join((chunk_id, item['item_id'], rid))
                rows.append(dict(review_key=key, chunk_id=chunk_id, checklist_item=item['item_id'],
                                 checklist_name=item.get('name'), reference=reference,
                                 candidate_match=bool(links[rid]), matches=links[rid],
                                 review=review_for(decisions, key, ('S2b漏抽', 'S2那条不属于该清单项', '匹配规则漏判'))))
        all_rows.extend(rows)
        blocks.append(dict(chunk_id=chunk_id, status='compared', summary=_recall_summary(rows), rows=rows))
    # A record may be cited by more than one checklist item: retain both denominators.
    unique = {(r['chunk_id'], r['reference']['record_id']): r for r in all_rows}
    return dict(schema='wiki-lab-s2b-checklist-recall/1', method=s2_compare.pilot_match([], [])['method'],
                blocks=blocks, item_record_summary=_recall_summary(all_rows), unique_record_summary=_recall_summary(list(unique.values())),
                limitation='Same numeric values or overlapping excerpts can create false-positive candidate matches; this is not semantic recall.')


def s1_recall(s1_root, run, pages, decisions):
    old = read(Path(s1_root) / 'records/S46.json', [])
    new = accepted_records(run, 'S46.s01')
    result = s2_compare.pilot_match(old, new, pages)
    links = defaultdict(list)
    for match in result['matches']:
        links[match['reference_id']].append(match)
    rows = []
    for reference in old:
        key = 'S1/' + reference['record_id']
        rows.append(dict(review_key=key, reference=reference, candidate_match=bool(links[reference['record_id']]),
                         matches=links[reference['record_id']],
                         review=review_for(decisions, key, ('清单外', '漏抽', '匹配规则漏判'))))
    return dict(schema='wiki-lab-s2b-s1-recall/1', status='compared' if new else 'not_run',
                summary=_recall_summary(rows) if new else None, rows=rows if new else [])


def targeted_recovery(s2_root, run, pages, decisions):
    inventory = read(Path(s2_root) / 'comparison.json', {}).get('items', [])
    indexed = {row['item_id']: row for row in inventory}
    rows = []
    for item_id, chunk_id in TARGET_ITEMS.items():
        item = indexed.get(item_id)
        if item is None:
            raise ValueError('Missing frozen comparison item: ' + item_id)
        source = item_id.split('.')[0]
        values = s2_compare.numbers_in_text(item['text'])
        candidates = []
        for record in accepted_records(run, chunk_id):
            common = sorted(values & s2_compare.record_numbers(record))
            evidence_pages = record.get('locator', {}).get('pdf_pages', [])
            if common and set(item.get('pages', [])) & set(evidence_pages):
                candidates.append(dict(record_id=record['record_id'], common_numbers=common, pages=evidence_pages,
                                       object_key=record.get('object_key'), numbers=record.get('numbers')))
        key = 'recovery/' + item_id
        rows.append(dict(review_key=key, item_id=item_id, source=source, chunk_id=chunk_id, text=item['text'],
                         pages=item.get('pages', []), candidate_matches=candidates,
                         status='not_run' if not accepted(run, chunk_id) else 'pending_review',
                         review=review_for(decisions, key, ('找回', '未找回', '拿不准'))))
    rejection_rows = []
    rejected = accepted(s2_root, 'S42.s01').get('rejected', [])
    if not rejected:
        rejected = read(Path(s2_root) / 'rejected/extract-S42.s01.json', [])
    for obj in rejected:
        if not any('excerpt' in e.lower() and ('absent' in e.lower() or 'not present' in e.lower() or 'not found' in e.lower()) for e in obj.get('errors', [])):
            continue
        reference = obj['record']
        key = 'recovery/' + reference['record_id']
        matched = s2_compare.pilot_match([reference], accepted_records(run, 'S42.s01'), pages)
        rejection_rows.append(dict(review_key=key, reference=reference, old_errors=obj['errors'], matches=matched['matches'],
                                   review=review_for(decisions, key, ('找回', '未找回', '拿不准'))))
    return dict(schema='wiki-lab-s2b-recovery/1', targeted_items=rows, s42_continuity_rejections=rejection_rows,
                limitation='A numeric candidate does not establish that the complete requested fact was recovered; explicit source readback is required.')


def fit_linear(xs, ys):
    """Ordinary least squares with an intercept, preserving signed coefficients."""
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    xbar, ybar = mean(xs), mean(ys)
    variance = sum((x - xbar) ** 2 for x in xs)
    slope = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys)) / variance if variance else 0.0
    return dict(intercept=ybar - slope * xbar, coefficient=slope)


def fit_features(rows):
    if len(rows) < 3:
        return dict(n=len(rows), models={}, best_feature=None)
    models = {}
    for feature in FEATURES:
        xs, ys = [r[feature] for r in rows], [r['output_tokens'] for r in rows]
        fit = fit_linear(xs, ys)
        predictions = [fit['intercept'] + fit['coefficient'] * x for x in xs]
        held_out = []
        for i, x in enumerate(xs):
            part = fit_linear(xs[:i] + xs[i+1:], ys[:i] + ys[i+1:])
            held_out.append(part['intercept'] + part['coefficient'] * x)
        loo_abs = [abs(y - p) for y, p in zip(ys, held_out)]
        models[feature] = dict(**fit, training_mae=mean(abs(y-p) for y, p in zip(ys, predictions)),
                               leave_one_out_mae=mean(loo_abs),
                               leave_one_out_mape=mean(error/y for error, y in zip(loo_abs, ys) if y),
                               predictions=[dict(chunk_id=r['chunk_id'], actual=y, fitted=p, held_out=q,
                                                 absolute_held_out_error=e)
                                            for r, y, p, q, e in zip(rows, ys, predictions, held_out, loo_abs)])
    return dict(n=len(rows), models=models, best_feature=min(models, key=lambda k: models[k]['leave_one_out_mae']))


def fit_rows(s2_root, run=None):
    chunks = chunks_with_children(s2_root)
    if run is None:
        ids = [r['id'] for r in read(Path(s2_root) / 'firstpass-chunk-status.json', {}).get('rows', []) if r.get('accepted_complete')]
        selected_root = s2_root
    else:
        ids, selected_root = SAMPLES, run
    values, excluded = [], []
    calls = attempts(selected_root)
    for chunk_id in ids:
        attempt = choose_attempt(selected_root, chunk_id, calls)
        if not attempt or attempt.get('usage', {}).get('output_tokens') is None:
            excluded.append(dict(chunk_id=chunk_id, reason='no bound completed measured call'))
            continue
        chunk = chunks[chunk_id]
        features = source_features(core_text(chunk))
        # fragments joined by a separator add artificial characters: use frozen core count.
        features['characters'] = chunk['chars']
        values.append(dict(chunk_id=chunk_id, call_number=attempt.get('call_number'),
                           chunk_sha256=chunk['sha256'], **features,
                           output_tokens=attempt['usage']['output_tokens'], seconds=attempt.get('seconds')))
    return values, excluded


def proposed_output_rule(model, feature, target_output_tokens=40000):
    """An exploratory bound must be algebraically feasible, not just printable."""
    margin = max(0, max((p['actual'] - p['fitted'] for p in model['predictions']), default=0))
    floor = max(0, model['intercept']) + margin
    monotonic = model['coefficient'] > 0
    usable = monotonic and floor < target_output_tokens
    maximum = ((target_output_tokens - margin - model['intercept']) / model['coefficient']) if usable else None
    return dict(feature=feature, intercept=model['intercept'], coefficient=model['coefficient'],
                observed_positive_residual_margin=margin, target_output_tokens=target_output_tokens,
                zero_feature_prediction_with_margin=floor, algebraically_feasible=usable,
                maximum_feature_count=int(maximum) if maximum is not None else None,
                status='exploratory_proposal' if usable else 'derivation_not_feasible',
                formula='max(0, intercept + coefficient * feature) + observed_positive_residual_margin <= target_output_tokens',
                recommendation=('For S3 discussion: split on section/table boundaries before the estimate exceeds the exploratory cap; keep headers with rows.'
                                if usable else 'No usable split threshold can be derived from this fitted model and cap.'),
                s3_user_decision='Whether to trial this output cap and class-specific recalibration before broader extraction.',
                limitation='Six mixed section samples, no future confidence bound or verified runtime guarantee; the cap is a proposal, not an S3 execution authorization.')


def output_fits(s2_root, run):
    old, old_excluded = fit_rows(s2_root)
    new, new_excluded = fit_rows(s2_root, run)
    old_fit, new_fit = fit_features(old), fit_features(new)
    best = new_fit.get('best_feature')
    rule = proposed_output_rule(new_fit['models'][best], best) if best else None
    return dict(schema='wiki-lab-s2b-output-fit/1', feature_definitions=dict(
                    characters='Frozen core character count, excluding shared context and prompt wrapper.',
                    numeric_tokens='S2 NUMBER_RE matches in core text, including years and numeric headings.',
                    table_rows='Core lines with >=2 numeric tokens and either multiple-space columns or a pipe; a layout proxy.'),
                selection='One accepted-bound successful call per current S2 leaf; split children included, timeout parent and duplicate pilots excluded.',
                error_metric='Leave-one-out mean absolute error in output tokens; in-sample MAE also shown.',
                s2=dict(rows=old, excluded=old_excluded, **old_fit), s2b=dict(rows=new, excluded=new_excluded, **new_fit),
                proposed_rule=rule)


def section_class(chunk):
    short = chunk['id'].split('.')[0]
    if short == 'S42':
        return '研报'
    if short in ('S46', 'S48', 'S50'):
        return '公告'
    section = ' '.join(chunk.get('sections', []))
    if chunk.get('note_group') == '七、合并财务报表项目注释' or '七、合并财务报表项目注释' in section:
        return '附注七'
    if any(s in section for s in ('重要会计政策', '税项', '审计报告', '财务报表的编制基础')):
        return '会计政策'
    if re.search(r'(?:八|九|十|十一|十二|十三|十四|十五|十六|十七|十八|十九|二十)、', section):
        return '其他附注'
    return '经营讨论与治理'


def extrapolate(s2_root, run, p0_root):
    chunks = chunks_with_children(s2_root)
    samples = defaultdict(list)
    for chunk_id in SAMPLES:
        attempt = choose_attempt(run, chunk_id)
        if attempt and attempt.get('usage', {}).get('output_tokens') is not None:
            samples[section_class(chunks[chunk_id])].append(dict(chunk_id=chunk_id, chars=chunks[chunk_id]['chars'],
                output=attempt['usage']['output_tokens']))
    rates = {}
    for kind, rows in samples.items():
        rates[kind] = dict(blocks=rows, output_tokens_per_10000_characters=sum(r['output'] for r in rows)*10000/sum(r['chars'] for r in rows),
                           proxy_note='第二节公司简介和主要财务指标代理经营讨论与治理，尚无该类直接样本' if kind == '经营讨论与治理' else None)
    char_mix = defaultdict(int)
    split_parents = {read(p).get('parent', {}).get('id') for p in (Path(s2_root)/'splits').glob('*.json')}
    for chunk_id, chunk in chunks.items():
        if chunk_id.startswith('S34.') and chunk_id not in split_parents:
            char_mix[section_class(chunk)] += chunk['chars']
    manifest = read(Path(p0_root) / 'corpus-manifest.json', {})
    p0_chars = Counter()
    for source in manifest.get('sources', []):
        kind = ('研报' if source['kind'] == 'report' else
                '公告' if source['doc_type'] == 'announcement' else
                '定期报告' if source['doc_type'] in ('annual', 'interim') else '招股书及通函')
        p0_chars[kind] += sum(p['chars'] for p in source['page_records'])
    s2_manifest = read(Path(s2_root) / 'corpus-manifest.json', {})
    s34_source = next((s for s in s2_manifest.get('sources', []) if s['id'] == 'S34'), {})
    s34_full_chars = sum(p['chars'] for p in s34_source.get('page_records', []))
    all_core_chars = sum(c['chars'] for cid, c in chunks.items() if cid not in split_parents)
    scenarios = []
    if all(k in rates for k in ('经营讨论与治理', '会计政策', '附注七', '公告', '研报')):
        # Other notes are deliberately not present in the six-block design.
        bounds = sorted(rates[k]['output_tokens_per_10000_characters'] for k in ('会计政策', '附注七'))
        for name, other in zip(('较低密度情景', '较高密度情景'), bounds):
            r = {k: v['output_tokens_per_10000_characters'] for k, v in rates.items()}
            r['其他附注'] = other
            s34_detail = [dict(section_class=k, core_characters=n, token_rate_per_10000=r[k], projected_output_tokens=n*r[k]/10000)
                         for k, n in sorted(char_mix.items())]
            s34 = sum(x['projected_output_tokens'] for x in s34_detail)
            periodic_rate = s34 / sum(char_mix.values()) * 10000
            p0_detail = []
            for k, n in p0_chars.items():
                proxy_rate = r[k] if k in ('公告', '研报') else periodic_rate
                # Prospectus/circular structural mix is unknown: use same periodic mix, explicit scenario.
                p0_detail.append(dict(document_class=k, characters=n, rate_per_10000=proxy_rate,
                                      projected_output_tokens=n*proxy_rate/10000))
            scenarios.append(dict(name=name, other_notes_proxy_rate=other, s34_core_characters=sum(char_mix.values()),
                                   s34_projected_output_tokens=s34, s34_sections=s34_detail,
                                   s34_full_pdf_characters=s34_full_chars,
                                   s34_full_pdf_proxy_output_tokens=s34_full_chars*periodic_rate/10000,
                                   s34_full_pdf_proxy_scope='同一结构平均密度应用到全部PDF字符；包括原本排除主表的代理成本，非逐页实测',
                                   p0_projected_output_tokens=sum(x['projected_output_tokens'] for x in p0_detail), p0_groups=p0_detail))
    return dict(schema='wiki-lab-s2b-extrapolation/1', measured_rates=rates, s34_core_character_mix=dict(char_mix),
                p0_character_mix=dict(p0_chars), p0_sources=len(manifest.get('sources', [])), scenarios=scenarios,
                core_scope_check=dict(all_sources_effective_core_characters=all_core_chars,
                    s2_reported_core_characters=370186, matches_s2_reported_core=all_core_chars == 370186,
                    s34_core_characters=sum(char_mix.values()), s34_full_pdf_characters=s34_full_chars),
                request_mode_volume=None, limitations=[
                    '其他附注未直接实测，以会计政策和附注七两端做代理情景；经营讨论与治理仅第二节代理。',
                    'S34外推仅S2保留的core范围，排除主表；不能称包含Choice主表的全部PDF覆盖成本。',
                    'P0定期报告按S34结构比例外推；招股书及通函也暂用同一结构代理，未验证同分布。',
                    'P0按全部页面字符外推，尚未估计各来源被Choice排除的比例，通常会高估保留core。',
                    '请求频率和请求范围未知，请求模式另计，不并入覆盖模式全量情景。'])


def _soft_categories(queue):
    return dict(Counter(issue for row in queue for issue in row.get('issues', [])))


def _rule_row(chunk_id, result):
    records = result.get('records', [])
    rejected = result.get('rejected', [])
    soft = result.get('soft_queue', [])
    return dict(chunk_id=chunk_id, status=result.get('status', 'not_run'),
                expanded_records=result.get('output_records'), accepted_records=len(records),
                hard_rejected_records=len(rejected), soft_flagged_records=len(soft), soft_rejected_records=0,
                hard_error_occurrences=dict(Counter(error for row in rejected for error in row.get('errors', []))),
                soft_issue_occurrences=_soft_categories(soft),
                suspicious_missing_column_rows=result.get('suspicious_missing_columns', 0),
                suspicious_missing_column_record_ids=[r['record_id'] for r in soft if '疑似漏列' in r.get('issues', [])],
                blank_cells_skipped=result.get('blank_cells', 0),
                outside_index_items=len(result.get('outside_index', [])),
                expansion_issues=result.get('expansion_issues', []),
                hard_rejections=rejected, soft_queue=soft)


def naming_diagnostics(run, result_by_chunk):
    registry = read(HERE / 'entities/issuers.json', {}).get('issuers', {})
    manifest = read(Path(run) / 'corpus-manifest.json', {})
    sources = {s['id']: s for s in manifest.get('sources', [])}
    normalized = lambda text: re.sub(r'[^a-z0-9\u3400-\u9fff]', '', text.casefold())
    rows = []
    by_source = defaultdict(list)
    for chunk_id, result in result_by_chunk.items():
        source = result.get('source', chunk_id.split('.')[0])
        # Request IDs do not encode a source; their stored result does.
        metadata = sources.get(source, {}).get('metadata_record', {})
        codes = ([metadata['company_code']] if metadata.get('company_code') else
                 [c['company_code'] for c in metadata.get('coverage', []) if c.get('company_code')])
        expected = [dict(code=code, legal_names={lang: name['name'] for lang, name in registry[code].items()})
                    for code in codes if code in registry]
        entities = result.get('entities', [])
        company_rows = []
        for issuer in expected:
            legal = list(issuer['legal_names'].values())
            norm_legal = [normalized(name) for name in legal]
            matches = []
            for entity in entities:
                if entity.get('type') != 'company':
                    continue
                aliases = [entity.get('canonical', '')] + entity.get('names', [])
                # Include abbreviated names as diagnostics while avoiding one-character matches.
                if any(len(normalized(alias)) >= 4 and any(normalized(alias) in name or name in normalized(alias)
                       for name in norm_legal) for alias in aliases):
                    matches.append(entity)
            subjects = Counter(r['object_key']['subject'] for r in result.get('records', [])
                               if any(r['object_key']['subject'] == entity['canonical'] for entity in matches))
            company_rows.append(dict(**issuer, matched_entities=matches, issuer_entity_present=bool(matches),
                                     canonical_legal_name_matches=all(e['canonical'] in legal for e in matches) if matches else None,
                                     issuer_record_subjects=dict(subjects)))
        row = dict(chunk_id=chunk_id, source=source, entities=entities, issuer_checks=company_rows,
                   all_entity_canonical_names_listed=all(e.get('canonical') in e.get('names', []) for e in entities))
        rows.append(row)
        by_source[source].extend(entities)
    pairs = []
    for entity_type in ('transaction', 'company'):
        for left in by_source['S46']:
            if left.get('type') != entity_type:
                continue
            for right in by_source['S50']:
                if right.get('type') != entity_type:
                    continue
                left_names = {normalized(n) for n in left.get('names', []) + left.get('renamed_from', [])}
                right_names = {normalized(n) for n in right.get('names', []) + right.get('renamed_from', [])}
                common = sorted(left_names & right_names)
                if common or (entity_type == 'transaction'):
                    pairs.append(dict(type=entity_type, left=left, right=right, common_normalized_names=common,
                                      candidate_aligned=bool(common), status='name-based candidate; source review required'))
    return dict(blocks=rows, s46_s50_name_candidates=pairs,
                s48_status='S48未在S2b抽取样本内；不以S2已有实体记录冒充本轮实体输出。跨三份文档对齐需回读S48冻结原文。')


def rule_outcomes(run):
    result_by_chunk = {}
    rows = []
    for chunk_id in SAMPLES + ('R1', 'R2'):
        result = accepted(run, chunk_id)
        if not result:
            result = read(Path(run) / 'rejected/whole-blocks' / (chunk_id + '.json'), {})
        rows.append(_rule_row(chunk_id, result))
        if result:
            result_by_chunk[chunk_id] = result
    totals = {key: sum(row[key] for row in rows) for key in (
        'accepted_records', 'hard_rejected_records', 'soft_flagged_records', 'soft_rejected_records',
        'suspicious_missing_column_rows', 'blank_cells_skipped', 'outside_index_items')}
    for key in ('hard_error_occurrences', 'soft_issue_occurrences'):
        counts = Counter()
        for row in rows:
            counts.update(row[key])
        totals[key] = dict(counts)
    all_attempt_rows = []
    for attempt in attempts(run):
        log = Path(attempt.get('log', ''))
        replay = log / 'acceptance-revalidated.json'
        path = replay if replay.exists() else log / 'acceptance.json'
        result = read(path, {})
        if result and not attempt.get('is_probe'):
            all_attempt_rows.append(dict(call_number=attempt.get('call_number'), stage=attempt.get('stage'),
                                         validation_receipt=str(path),
                                         **_rule_row(attempt.get('chunk_id'), result)))
    semantic = read(Path(run) / 'semantic-review.json', {})
    semantic_rows = semantic.get('rows', [])
    semantic_counts = Counter(row.get('review', {}).get('decision') for row in semantic_rows
                              if row.get('review', {}).get('decision'))
    errors = Counter(error for row in semantic_rows if row.get('review', {}).get('decision') == '错误'
                     for error in row.get('review', {}).get('error_types', []))
    uncertainties = Counter(error for row in semantic_rows if row.get('review', {}).get('decision') == '拿不准'
                            for error in row.get('review', {}).get('error_types', []))
    return dict(schema='wiki-lab-s2b-rule-outcomes/1', blocks=rows, selected_output_totals=totals,
                all_attempt_rows=all_attempt_rows, naming=naming_diagnostics(run, result_by_chunk),
                semantic_sample_summary=dict(selected=len(semantic_rows), decisions=dict(semantic_counts),
                    pending=len(semantic_rows)-sum(semantic_counts.values()), error_types=dict(errors),
                    uncertainty_types=dict(uncertainties),
                    seed=semantic.get('seed'), population_sha256=semantic.get('population_sha256')),
                limitation='Selected-output counts exclude earlier retry outputs; all attempt outcomes are listed separately. Soft categories may overlap within one accepted record.')


# Diagnostic candidates only: these names never change an economic match key.
_A01_NAME_CANDIDATES = {
    '归属于上市公司股东的净利润': '归属于母公司股东的净利润',
    '归属于上市公司股东的净资产': '归属于母公司所有者权益（或股东权益）合计',
    '总资产': '资产总计',
    '基本每股收益（元／股）': '基本每股收益',
    '稀释每股收益（元／股）': '稀释每股收益',
}


def _alignment_reason_rows(rows, pdf, choice):
    by_id = {r['record_id']: r for r in pdf}
    periods = {r['period']['label'] for r in choice}
    diagnostics = []
    for row in rows:
        if row['status'] != '未对齐':
            continue
        record = by_id[row['record_ids'][0]]
        indicator, period = (record['object_key'][key] for key in ('indicator', 'period'))
        candidate_name = _A01_NAME_CANDIDATES.get(indicator)
        aliases = [r for r in choice if r['object_key']['indicator'] == candidate_name and
                   r['period']['label'] == period and r['object_key']['subject'] == record['object_key']['subject']]
        if row.get('base_key_candidates'):
            category = '完整对象键范围或维度不同'
        elif re.search(r'增减|同比|环比', indicator):
            category = '增减列无同名Choice原表科目'
        elif period not in periods:
            category = 'Choice未选取该期间'
        elif aliases:
            category = '原文与Choice科目名称不同'
        else:
            category = 'Choice原表转换未含该同名指标'
        diagnostics.append(dict(record_id=record['record_id'], object_key=record['object_key'], category=category,
                                choice_period_present=period in periods,
                                base_key_candidates=row.get('base_key_candidates', []),
                                name_only_candidates=[dict(record_id=r['record_id'], object_key=r['object_key']) for r in aliases],
                                limitation='名称候选仅解释未对齐；未修改键、未按数值猜匹配。'))
    return diagnostics


def choice_alignment(run, s2_root=None):
    pdf = accepted_records(run, 'S34.s03')
    choice = read(Path(run) / 'choice/records.json', [])
    a01 = [rid for item in accepted(run, 'S34.s03').get('coverage', []) if item['item_id'] == 'A01'
           for rid in item.get('record_ids', [])]
    rows = s2_qa.choice_alignment(pdf + choice, a01)
    conflicts = s2_qa.build_qa(choice)['same_key_conflicts'] if choice else []
    reason_rows = _alignment_reason_rows(rows, pdf, choice)
    same_block, whole_run = None, None
    if s2_root is not None:
        old = accepted(s2_root, 'S34.s03')
        old_pdf = old.get('records', [])
        old_choice = read(Path(s2_root) / 'choice/records.json', [])
        old_ids = [rid for item in old.get('coverage', []) if item['item_id'] == 'A01' for rid in item.get('record_ids', [])]
        old_rows = s2_qa.choice_alignment(old_pdf + old_choice, old_ids)
        same_block = dict(chunk_id='S34.s03', pdf_a01_record_ids=old_ids, pdf_a01_records=len(old_ids),
                          alignment_rows=len(old_rows), counts=dict(Counter(row['status'] for row in old_rows)),
                          rows=old_rows, source='S2 accepted/S34.s03.json coverage A01 references plus S2 frozen Choice records')
        stored = read(Path(s2_root) / 'qa-queue.json', {})
        stored_rows = stored.get('choice_pdf_a01', [])
        chunk_for_record = {}
        for path in (Path(s2_root) / 'accepted').glob('*.json'):
            for record in read(path, {}).get('records', []):
                chunk_for_record[record['record_id']] = path.stem
        whole_run = dict(source='S2 frozen qa-queue.json choice_pdf_a01 (not a current-code regeneration)',
                         alignment_rows=len(stored_rows),
                         unique_pdf_records=len({r['record_ids'][0] for r in stored_rows}),
                         counts=dict(Counter(row['status'] for row in stored_rows)),
                         participating_blocks=dict(Counter(chunk_for_record.get(r['record_ids'][0], 'unknown') for r in stored_rows)),
                         limitation='覆盖多个块；Choice同键重复可令同一PDF记录对应多行，不与本轮单块分母直接相减。')
    return dict(schema='wiki-lab-s2b-choice-alignment/1', status='compared' if pdf and choice else 'not_ready',
                pdf_a01_record_ids=a01, pdf_a01_records=len(a01), choice_records=len(choice),
                counts=dict(Counter(row['status'] for row in rows)), rows=rows,
                unmatched_reason_counts=dict(Counter(row['category'] for row in reason_rows)),
                unmatched_reason_rows=reason_rows,
                choice_same_key_conflicts=len(conflicts), same_key_conflict_details=conflicts,
                s2_baseline_counts=dict(相等=11, 不相等=1, 未对齐=98),
                s2_same_block=same_block, s2_whole_run_saved=whole_run,
                denominator_note='S2b只比较S34.s03的A01；S2全run报告11/1/98含S34.s03、S34.s04、S34.s08且可含重复Choice候选。以同块基线评估变化。',
                method='s2_qa.choice_alignment: strict economic object key; same disclosure date preferred; no numeric guessing')


def build_measurements(s2_root, run, s1_root, p0_root=None):
    s2_root, run, s1_root = map(Path, (s2_root, run, s1_root))
    p0_root = Path(p0_root) if p0_root else HERE / 'runs/p0-20261021'
    for protected in (s2_root, s1_root, p0_root):
        if run.resolve().is_relative_to(protected.resolve()):
            raise ValueError('Output must be outside protected baseline directories')
    decisions = read(run / 'adjudications.json', {})
    if 'decisions' in decisions:
        decisions = decisions['decisions']
    pages = page_map(s2_root)
    outputs = {
        'cost-comparison.json': compare_costs(s2_root, run),
        'checklist-recall.json': checklist_recall(s2_root, run, pages, decisions),
        's1-recall.json': s1_recall(s1_root, run, pages, decisions),
        'targeted-recovery.json': targeted_recovery(s2_root, run, pages, decisions),
        'output-fit.json': output_fits(s2_root, run),
        'extrapolation.json': extrapolate(s2_root, run, p0_root),
        'rule-outcomes.json': rule_outcomes(run),
        'choice-alignment.json': choice_alignment(run, s2_root),
    }
    for name, value in outputs.items():
        dump(run / name, value)
    return dict(outputs=list(outputs), paired_blocks=outputs['cost-comparison.json']['paired_blocks'],
                s2_fit_calls=outputs['output-fit.json']['s2']['n'], s2b_fit_calls=outputs['output-fit.json']['s2b']['n'],
                s2_best_feature=outputs['output-fit.json']['s2']['best_feature'],
                checklist_summary=outputs['checklist-recall.json']['item_record_summary'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--s2', type=Path, default=HERE/'runs/s2-20261022')
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--s1', type=Path, default=HERE/'runs/s1-20261021')
    parser.add_argument('--p0', type=Path, default=HERE/'runs/p0-20261021')
    args = parser.parse_args()
    print(json.dumps(build_measurements(args.s2, args.run, args.s1, args.p0), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
