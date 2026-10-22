#!/usr/bin/env python3
"""Bounded Choice capture; raw payloads and append-only manifests stay under runs/.

pack, sdk_result, login/error checks and finally/logout are copied from
/Users/leon/Stock/invest-lab/runs/research-handoff-20261003/data-transcribe/t04b-3/choice_capture.py
at invest-lab commit 317c9b0. No import from that repository.
Function/indicator documentation remains in invest-lab's
.agents/skills/choice-quantapi-skill/references/ (not copied here).
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
from pathlib import Path
import re

from corpus import HERE, dump, sha256


def pack(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return {"__type__": type(value).__name__, "hex": bytes(value).hex()}
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return {"__type__": type(value).__name__, "isoformat": value.isoformat()}
    if isinstance(value, dict):
        return {str(k): pack(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return {"__type__": type(value).__name__, "items": [pack(v) for v in value]}
    return {"__type__": f"{type(value).__module__}.{type(value).__qualname__}", "repr": repr(value)}


def sdk_result(result):
    attrs = {}
    for key in ("ErrorCode", "ErrorMsg", "Codes", "Indicators", "Dates", "Data"):
        try:
            attrs[key] = pack(getattr(result, key))
        except AttributeError:
            attrs[key] = {"__missing_attribute__": True}
    attrs["python_type"] = f"{type(result).__module__}.{type(result).__qualname__}"
    return attrs


def now():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat()


def append(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n')
        stream.flush()
        import os
        os.fsync(stream.fileno())


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def query_identity(request):
    query = {key: request.get(key, {} if key == 'kwargs' else [])
             for key in ('function', 'args', 'kwargs')}
    return hashlib.sha256(json.dumps(query, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def has_values(value):
    """An empty container/all-missing return is not successful acquisition."""
    if isinstance(value, dict):
        return any(has_values(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(has_values(item) for item in value)
    return value is not None and value not in ('', '--')


def event_digest(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def active_terminal_stop(rows):
    """Resolve append-only, explicitly authorized resumptions of exact failures."""
    active = None
    for row in rows:
        if row['event'] == 'terminal_stop':
            active = row
        elif row['event'] == 'network_resume_authorized':
            if active is None or row.get('terminal_stop_sha256') != event_digest(active):
                raise ValueError('Resume event does not bind the active terminal failure')
            active = None
    return active


def validate_requests(document):
    if document.get('schema') != 'wiki-lab-requests/1' or not document.get('requests'):
        raise ValueError('Expected wiki-lab-requests/1 and nonempty requests')
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', document.get('source_slug', '')):
        raise ValueError('Invalid source_slug')
    ids, identities = set(), set()
    for request in document['requests']:
        if not isinstance(request.get('id'), str) or not request['id'] or not request.get('purpose'):
            raise ValueError('Each request needs id and purpose')
        if request['id'] in ids or query_identity(request) in identities:
            raise ValueError('Duplicate request id or query')
        ids.add(request['id']); identities.add(query_identity(request))
        fn, args, kwargs = request.get('function'), request.get('args'), request.get('kwargs', {})
        if fn not in ('ctr', 'csd', 'css', 'sector') or not isinstance(args, list) or not isinstance(kwargs, dict):
            raise ValueError('Unsupported function or invalid arguments')
        option_index = {'ctr': 2, 'csd': 4, 'css': 2, 'sector': 2}[fn]
        if len(args) != option_index + 1 or kwargs:
            raise ValueError('Use explicit positional arguments, including options; kwargs must be empty')
        options = dict(piece.strip().lower().split('=', 1) for piece in args[-1].split(',') if piece.strip())
        if options.get('ispandas') != '0' or options.get('showblank') is not None:
            raise ValueError('Ispandas=0 is required; do not replace missing values with zero')
        if fn == 'ctr' and args[0].endswith('SHSZ'):
            if options.get('reporttype') != '1':
                raise ValueError('A-share statements require ReportType=1')
        if fn == 'ctr' and args[0].endswith('StatementHK'):
            if options.get('reporttype') != '1':
                raise ValueError('S1 HK tests use ReportType=1')


def run_capture(request_path, output_dir, max_calls=8, sdk=None, resume_reason=None):
    """One login, fail-fast queries, finally logout. Never retry a reserved request.

    call_started is durably appended before the SDK call, including failed/crashed
    calls. Budget is shared across all request files in output_dir and cannot grow.
    Login/logout are logged separately; they are not data-query budget units.
    """
    request_path, output_dir = Path(request_path), Path(output_dir)
    document = json.loads(request_path.read_text(encoding='utf-8'))
    validate_requests(document)
    if not 1 <= max_calls <= 8:
        raise ValueError('S1 query cap must be between 1 and 8')
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / '.writer.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        budget_path, log = output_dir / 'budget.json', output_dir / 'calls.jsonl'
        if budget_path.exists():
            if json.loads(budget_path.read_text())['max_calls'] != max_calls:
                raise ValueError('Existing budget cannot be reset or changed')
        else:
            dump(budget_path, {'max_calls': max_calls, 'created_at': now(), 'unit': 'data_query'})
        previous = read_rows(log)
        stopped = active_terminal_stop(previous)
        if stopped and not resume_reason:
            raise ValueError('A terminal login/network failure is recorded; user intervention required')
        if resume_reason is not None:
            if not resume_reason.strip() or stopped is None:
                raise ValueError('Network resume needs a reason and an active stopped failure')
            if not re.search(r'10002\d{3}', stopped.get('reason', '')):
                raise ValueError('Network resume cannot clear account/activation or other failures')
        started = [row for row in previous if row['event'] == 'call_started']
        for request in document['requests']:
            if any(row['request_id'] == request['id'] or row['query_sha256'] == query_identity(request) for row in started):
                raise ValueError('Request/query already attempted; no automatic retries')
        if len(started) + len(document['requests']) > max_calls:
            raise ValueError('Requests exceed remaining call budget')
        source_id = 'choice-' + now()[:10].replace('-', '') + '-' + document['source_slug']
        target = output_dir / 'vendor' / 'choice' / (source_id + '.json')
        if target.exists():
            raise ValueError('Source ID already exists; use a distinct source_slug')
        archived_request = output_dir / 'requests' / (source_id + '.json')
        if archived_request.exists():
            raise ValueError('Request archive already exists')
        dump(archived_request, document)
        if stopped:
            append(log, {'event': 'network_resume_authorized', 'at': now(),
                         'authorization': resume_reason,
                         'terminal_stop_sha256': event_digest(stopped),
                         'previous_log_sha256': sha256(log),
                         'data_calls_used': len(started), 'max_calls': max_calls,
                         'remaining_calls': max_calls - len(started)})
        append(log, {'event': 'batch_started', 'at': now(), 'source_id': source_id,
                     'requests_sha256': sha256(archived_request), 'request_ids': [r['id'] for r in document['requests']]})
        records = {'captured_at': now(), 'queries': []}
        status, reason, terminal = 'failed', None, False
        request_results = []
        try:
            if sdk is None:
                from EmQuantAPI import c
                sdk = c
            login = sdk.start()
            records['login'] = sdk_result(login)
            append(log, {'event': 'login', 'at': now(), 'result': records['login']})
            if getattr(login, 'ErrorCode', None) != 0:
                terminal = True
                reason = 'login_failed: ' + str(records['login'].get('ErrorCode')) + ' ' + str(records['login'].get('ErrorMsg'))
            else:
                try:
                    for request in document['requests']:
                        query = {key: request[key] for key in ('function', 'args')}
                        if request.get('kwargs'):
                            query['kwargs'] = request['kwargs']
                        append(log, {'event': 'call_started', 'at': now(), 'request_id': request['id'],
                                     'purpose': request['purpose'], 'query_sha256': query_identity(request), 'query': query})
                        entry = {'query': query}
                        try:
                            result = getattr(sdk, request['function'])(*request['args'], **request.get('kwargs', {}))
                            entry['result'] = sdk_result(result)
                        except Exception as exc:
                            entry['exception'] = {'type': type(exc).__name__, 'message': str(exc)}
                        records['queries'].append(entry)
                        # Preserve the full response immediately; registration follows final logout.
                        dump(target, records)
                        code = entry.get('result', {}).get('ErrorCode')
                        content_status = ('failed' if 'exception' in entry or code not in (0, '0') else
                                          'data' if has_values(getattr(result, 'Data', None)) else 'no_data')
                        request_result = {'request_id': request['id'], 'content_status': content_status,
                                          'reason': 'SDK returned no values; no retry' if content_status == 'no_data' else
                                                    str(entry.get('exception') or entry.get('result', {}).get('ErrorMsg')) if content_status == 'failed' else None}
                        request_results.append(request_result)
                        append(log, {'event': 'call_finished', 'at': now(), **request_result,
                                     'result': entry.get('result'), 'exception': entry.get('exception')})
                        if 'exception' in entry or code not in (0, '0'):
                            reason = 'query_failed: ' + str(code) + ' ' + str(entry.get('result', {}).get('ErrorMsg', entry.get('exception')))
                            records['stopped_on_sdk_error'] = {'query': query, 'ErrorCode': code, 'reason': reason}
                            terminal = ('exception' in entry or str(code).startswith('10002') or
                                        str(code) in ('10001009', '10001019', '10001020'))
                            break
                    else:
                        status = 'completed' if all(r['content_status'] == 'data' for r in request_results) else 'no_data'
                        if status == 'no_data':
                            reason = 'One or more requests returned no values; see per-request call_finished events'
                finally:
                    try:
                        records['logout'] = sdk_result(sdk.stop())
                        if records['logout'].get('ErrorCode') not in (0, '0'):
                            status, reason, terminal = 'failed', 'logout_failed; session state requires review', True
                    except Exception as exc:
                        records['logout'] = {'exception': {'type': type(exc).__name__, 'message': str(exc)}}
                        status, reason, terminal = 'failed', 'logout_failed; session state requires review', True
        except Exception as exc:
            reason, terminal = f'{type(exc).__name__}: {exc}', True
            records['exception'] = {'type': type(exc).__name__, 'message': str(exc)}
        dump(target, records)
        manifest = {key: None for key in ('source_id', 'kind', 'company_code', 'market', 'doc_type', 'title',
                    'period', 'published_date', 'source_url', 'retrieved_at', 'path', 'sha256', 'pages', 'text_dir', 'vendor', 'query', 'note')}
        manifest.update(source_id=source_id, kind='vendor', company_code=document.get('company_code'),
                        doc_type='vendor_data', title=document.get('title', document['source_slug']),
                        retrieved_at=records['captured_at'], path=str(target.relative_to(output_dir)),
                        sha256=sha256(target), vendor='choice',
                        query={'requests_path': str(archived_request.relative_to(output_dir)),
                               'requests_sha256': sha256(archived_request), 'queries': [e['query'] for e in records['queries']]},
                        note=reason or 'S1 interface test; captured values await field and source review')
        append(output_dir / 'manifests' / 'sources.jsonl', manifest)
        attempted = len(records['queries'])
        summary = {'event': 'batch_finished', 'at': now(), 'source_id': source_id, 'status': status,
                   'attempted': attempted, 'reason': reason, 'sha256': manifest['sha256'],
                   'request_results': request_results,
                   'unattempted_request_ids': [r['id'] for r in document['requests'][attempted:]]}
        append(log, summary)
        if terminal:
            append(log, {'event': 'terminal_stop', 'at': now(), 'source_id': source_id, 'reason': reason})
        return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('requests', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--max-calls', type=int, default=8)
    parser.add_argument('--resume-network', metavar='USER_AUTHORIZATION',
                        help='Explicit user authorization; appends a bound resume event without deleting failures or resetting budget')
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to((HERE / 'runs').resolve()):
        parser.error('CLI writes are confined to wiki-lab/runs/')
    result = run_capture(args.requests, output, args.max_calls, resume_reason=args.resume_network)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
