#!/usr/bin/env python3
"""S1 record contract and checks; no model, network, or third-party packages.

The JSON Schema below is also the definition interpreted by field validation.
Evidence checks add source-dependent constraints that JSON Schema cannot express.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re

VERSION = 'wiki-records/1.0'
UNITS = ('元', '千元', '万元', '百万元', '十亿元', '亿元', '元/股',
         '元/芯公里', '股', '千股', '万股', '百万股', '%', '百分点', '基点', '倍', '吨',
         '吨/年', '公里', '芯公里', '万芯公里', '亿芯公里', '个', '家', '席', '轮',
         'dB/km', 'GB', '亿GB', '份额', '万份额', '人', '户',
         '天', '工作日', '月', '年', 'HKD/CNY', 'CNY/HKD', '无单位', '未知')
CURRENCIES = ('CNY', 'HKD', 'USD', 'EUR', 'GBP', 'JPY', 'IDR', 'MYR', 'PLN',
              'COP', 'MXN', 'EGP', 'AED', 'BRL', 'ZAR', 'PEN', 'VND', 'INR',
              'RUB', 'AUD', 'CHF', 'SGD', 'CAD', 'TRY', 'KRW', 'THB')
PERIODS = ('instant', 'annual', 'half_year', 'quarter', 'range', 'event', 'unspecified')
NATURES = ('actual', 'guidance', 'institution_forecast', 'judgment', 'secondary')
STAGES = ('bidding', 'proposed', 'signed', 'paid', 'transferred', 'completed',
          'operating', 'planned', 'unspecified')
VERIFY_STATUSES = ('待核验', '代码通过', '人工复核通过', '待复核', '未查值', '拒收')


def string(nullable=False, **rules):
    return dict(type=['string', 'null'] if nullable else 'string', **rules)


def enum(values, nullable=False):
    return dict(type=['string', 'null'] if nullable else 'string',
                enum=list(values) + ([None] if nullable else []))


def obj(properties):
    return dict(type='object', properties=properties, required=list(properties),
                additionalProperties=False)


def array(items, **rules):
    return dict(type='array', items=items, **rules)


DATE = string(True, format='date')
TEXT = string(minLength=1)
NUMBER = obj(dict(value=string(pattern=r'^-?(?:0|[1-9]\d*)(?:\.\d+)?$'),
                  unit=enum(UNITS), currency=enum(CURRENCIES, True),
                  source_unit=enum(UNITS), decimals=dict(type='integer', minimum=0, maximum=12),
                  qualifier=enum(('exact', 'approximate', 'more_than', 'less_than', 'at_least', 'at_most')),
                  label=string()))


def metric_detail(statuses, note_required=False):
    """Both the schema and runtime enforce an explanation for NA/NM values."""
    note = string(minLength=1, pattern=r'[\s\S]*\S[\s\S]*') if note_required else string()
    return obj(dict(scope=enum(('consolidated', 'parent', 'segment', 'subsidiary', 'company_model', 'unspecified')),
                    scope_note=string(), value_status=enum(statuses), value_note=note))


DETAILS = {
    'metric': {'anyOf': [metric_detail(('reported',)),
                         metric_detail(('not_available', 'not_meaningful'), note_required=True)]},
    'event': obj(dict(event_type=enum(('auction', 'signing', 'payment', 'transfer', 'completion', 'incorporation',
                                     'production_start', 'disclosure', 'other')),
                      occurred_on=DATE, stage=enum(STAGES), description=TEXT)),
    'obligation': obj(dict(obligor=TEXT, beneficiary=string(True), action=TEXT,
                           deadline=obj(dict(amount=dict(type=['integer', 'null'], minimum=0),
                                             unit=enum(('business_days', 'calendar_days', 'months', 'years', 'none')),
                                             from_event=string(), from_date=DATE, deadline_on=DATE)),
                           conditions=array(TEXT), consequences=array(TEXT),
                           payment_status=enum(('unpaid', 'paid', 'part_paid', 'due', 'unspecified')))),
    'relationship': obj(dict(subject=TEXT, object=TEXT,
                             relationship_type=enum(('ownership', 'subsidiary', 'renamed_to', 'director',
                                                     'identity_candidate', 'independent_party', 'other')),
                             starts_on=DATE, ends_on=DATE,
                             status=enum(('current', 'prospective', 'historical', 'unspecified')),
                             description=TEXT)),
    'statement': obj(dict(speaker=TEXT, content=TEXT,
                          statement_type=enum(('explanation', 'forecast', 'guidance', 'judgment', 'definition',
                                               'risk', 'attribution', 'market_rating', 'market_target', 'other')),
                          attribution_note=string())),
}
COMMON = dict(
    record_id=TEXT, record_type=enum(DETAILS),
    source=obj(dict(kind=enum(('pdf', 'choice')), source_id=TEXT,
                    choice_field=string(True), report_period=string(True))),
    locator=obj(dict(pdf_pages=array(dict(type='integer', minimum=1), uniqueItems=True),
                     table=string(True), section=string(True))),
    excerpts=array(obj(dict(page=dict(type='integer', minimum=1), text=TEXT))),
    disclosed_on=DATE, as_of=DATE,
    date_notes=array(obj(dict(field=TEXT, reason=TEXT))),
    is_market=dict(type='boolean'), extraction_version=TEXT,
    verification=obj(dict(status=enum(VERIFY_STATUSES), note=string())),
    object_key=obj(dict(subject=TEXT, indicator=TEXT, period=TEXT)),
    period=obj(dict(kind=enum(PERIODS), label=TEXT)), nature=enum(NATURES),
    numbers=array(NUMBER), components=array(TEXT, uniqueItems=True),
    additivity=obj(dict(group=string(True), role=enum(('standalone', 'total', 'component', 'non_additive')),
                        reason=string())),
    dimensions=array(obj(dict(name=TEXT, value=TEXT))),
    origin=obj(dict(speaker=string(True), original_source=string(True), common_origin_id=string(True))),
    limitations=array(TEXT), supersedes=array(TEXT, uniqueItems=True),
)
RECORD_SCHEMA = {'anyOf': [obj(dict(COMMON, record_type=enum((kind,)), detail=detail))
                           for kind, detail in DETAILS.items()]}
COVERAGE_SCHEMA = obj(dict(
    item_id=TEXT, status=enum(('已抽', '本文档无', '本块未覆盖')),
    record_ids=array(TEXT, uniqueItems=True),
    checked_pages=array(dict(type='integer', minimum=1), uniqueItems=True),
    basis=TEXT))
OUTPUT_SCHEMA = obj(dict(schema_version=enum((VERSION,)), records=array(RECORD_SCHEMA),
                         coverage=array(COVERAGE_SCHEMA)))


def extract_output_schema():
    return dict({'$schema': 'https://json-schema.org/draft/2020-12/schema',
                 'title': 'wiki-lab single-turn extraction output'}, **deepcopy(OUTPUT_SCHEMA))


def _fields(value, schema, path='record'):
    errors = []
    if 'anyOf' in schema:
        choices = [_fields(value, option, path) for option in schema['anyOf']]
        return [] if any(not choice for choice in choices) else min(choices, key=len)
    allowed = schema.get('type', [])
    allowed = [allowed] if isinstance(allowed, str) else allowed
    actual = ('null' if value is None else 'boolean' if isinstance(value, bool) else
              'integer' if isinstance(value, int) else 'number' if isinstance(value, float) else
              'string' if isinstance(value, str) else 'array' if isinstance(value, list) else
              'object' if isinstance(value, dict) else 'invalid')
    if allowed and actual not in allowed:
        return [f'{path}: expected {allowed}, got {actual}']
    if 'enum' in schema and value not in schema['enum']:
        errors.append(f'{path}: illegal enum {value!r}')
    if value is None:
        return errors
    if actual == 'object':
        properties = schema.get('properties', {})
        for key in schema.get('required', []):
            if key not in value:
                errors.append(f'{path}.{key}: missing field')
        for key, item in value.items():
            if key in properties:
                errors.extend(_fields(item, properties[key], f'{path}.{key}'))
            elif schema.get('additionalProperties') is False:
                errors.append(f'{path}.{key}: unknown field')
    if actual == 'array':
        if len(value) < schema.get('minItems', 0):
            errors.append(f'{path}: too few items')
        if schema.get('uniqueItems') and len({json.dumps(x, sort_keys=True) for x in value}) != len(value):
            errors.append(f'{path}: repeated items')
        for index, item in enumerate(value):
            errors.extend(_fields(item, schema['items'], f'{path}[{index}]'))
    if actual == 'string':
        if len(value) < schema.get('minLength', 0) or ('minLength' in schema and not value.strip()):
            errors.append(f'{path}: empty text')
        if 'pattern' in schema and not re.fullmatch(schema['pattern'], value):
            errors.append(f'{path}: illegal string format')
        if schema.get('format') == 'date':
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError(value)
            except ValueError:
                errors.append(f'{path}: invalid ISO date')
    if actual in ('integer', 'number'):
        if value < schema.get('minimum', value) or value > schema.get('maximum', value):
            errors.append(f'{path}: out of range')
    return errors


def normalize_whitespace(value):
    """Ignore page-layout whitespace, without changing characters or punctuation."""
    return re.sub(r'\s+', '', value)


SCALES = {'元': ('money', Decimal(1)), '千元': ('money', Decimal(1000)),
          '万元': ('money', Decimal(10000)), '百万元': ('money', Decimal(1000000)),
          '十亿元': ('money', Decimal(1000000000)), '亿元': ('money', Decimal(100000000)),
          '股': ('shares', Decimal(1)), '千股': ('shares', Decimal(1000)),
          '万股': ('shares', Decimal(10000)),
          '百万股': ('shares', Decimal(1000000)), '芯公里': ('fiber_length', Decimal(1)),
          '万芯公里': ('fiber_length', Decimal(10000)), '亿芯公里': ('fiber_length', Decimal(100000000)),
          'GB': ('data_volume', Decimal(1)), '亿GB': ('data_volume', Decimal(100000000)),
          '份额': ('interests', Decimal(1)), '万份额': ('interests', Decimal(10000))}


def number(value, unit, currency=None, source_unit=None, decimals=None, qualifier='exact', label=''):
    value = str(value)
    if decimals is None:
        decimals = len(value.partition('.')[2])
    return dict(value=value, unit=unit, currency=currency, source_unit=source_unit or unit,
                decimals=decimals, qualifier=qualifier, label=label)


def formatted_number(item):
    value = Decimal(item['value'])
    left, right = item['unit'], item['source_unit']
    if left != right:
        if left not in SCALES or right not in SCALES or SCALES[left][0] != SCALES[right][0]:
            raise ValueError(f'incompatible units: {left} -> {right}')
        value = value * SCALES[left][1] / SCALES[right][1]
    quantum = Decimal(1).scaleb(-item['decimals'])
    # Source precision is explicit. Never silently round a supposedly exact value.
    rounded = value.quantize(quantum)
    if rounded != value and item['qualifier'] == 'exact':
        raise ValueError('exact value loses precision at stated source decimals')
    return format(rounded, f'.{item["decimals"]}f')


def number_in_excerpt(item, excerpt):
    """Match a whole numeric token, including comma grouping and bracket negatives."""
    token = formatted_number(item)
    negative = token.startswith('-')
    token = token.lstrip('-')
    integer, dot, fraction = token.partition('.')
    grouped = format(int(integer), ',') + (dot + fraction if dot else '')
    alternatives = [token, grouped]
    escaped = '(?:' + '|'.join(re.escape(x) for x in dict.fromkeys(alternatives)) + ')'
    if negative:
        escaped = '(?:[-−]\\s*' + escaped + r'|\(\s*' + escaped + r'\s*\)|（\s*' + escaped + r'\s*）)'
    # A positive value must not match inside a negative, a larger integer or decimal.
    for match in re.finditer(r'(?<![\d.,\-−(（])' + escaped + r'(?!\d|[.,]\d)', excerpt):
        if not negative and excerpt[:match.start()].rstrip().endswith(('-', '−', '(', '（')):
            continue
        return True
    return False


def _integer_words(value):
    """Deliberately bounded English/Chinese forms; no general numeral NLP."""
    if not 0 <= value <= 99:
        return []
    small = ('zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight',
             'nine', 'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen',
             'sixteen', 'seventeen', 'eighteen', 'nineteen')
    tens = ('', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy', 'eighty', 'ninety')
    if value < 20:
        english = [small[value]]
    elif value % 10:
        english = [tens[value // 10] + ' ' + small[value % 10],
                   tens[value // 10] + '-' + small[value % 10]]
    else:
        english = [tens[value // 10]]
    digits = '零一二三四五六七八九'
    chinese = (digits[value] if value < 10 else
               (digits[value // 10] if value >= 20 else '') + '十' +
               (digits[value % 10] if value % 10 else ''))
    return english + [chinese] + (['两'] if value == 2 else [])


_DURATION_UNITS = {
    'business_days': r'(?:(?:business|working)\s+days?|(?:个)?工作日)',
    'calendar_days': r'(?:(?:calendar\s+)?days?|(?:个)?(?:自然日|日历日|天|日))',
    'months': r'(?:months?|(?:个)?月)',
    'years': r'(?:years?|(?:个)?年)',
}
_DATE_SPANS = re.compile(
    r'\d{4}\s*[-/]\s*\d{1,2}\s*[-/]\s*\d{1,2}'
    r'|\d{4}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*[日号])?)?'
    r'|\d{1,2}\s*月\s*\d{1,2}\s*[日号]'
    r'|[〇零一二三四五六七八九]{4}\s*年(?:\s*[一二三四五六七八九十两]{1,3}\s*月'
    r'(?:\s*[一二三四五六七八九十两]{1,3}\s*[日号])?)?'
    r'|[一二三四五六七八九十两]{1,3}\s*月\s*[一二三四五六七八九十两]{1,3}\s*[日号]')


def deadline_in_excerpt(deadline, excerpt):
    """Verify the structured duration's amount and unit in the same phrase.

    Numeric integers and bounded 0--99 English/Chinese integer words are accepted.
    Calendar-date spans are excluded; word fragments and negative quantities are
    not accepted as positive durations. Parenthesized bilingual digits are allowed
    only after the matching word, e.g. ``six (6) months``.
    """
    value, unit = deadline['amount'], deadline['unit']
    if value is None or unit not in _DURATION_UNITS:
        return False
    numeric = str(value)
    word_forms = [re.escape(word).replace(r'\ ', r'\s+') for word in _integer_words(value)]
    words = '|'.join(word_forms)
    repeated = r'(?:\s*(?:\(\s*' + re.escape(numeric) + r'\s*\)|（\s*' + re.escape(numeric) + r'\s*）))?'
    quantity = '(?:' + re.escape(numeric) + ('|(?:' + words + ')' + repeated if words else '') + ')'
    boundary = r'A-Za-z0-9〇零一二三四五六七八九十百千万两.,\-−(（'
    pattern = re.compile(r'(?<![' + boundary + '])' + quantity + r'\s*' +
                         _DURATION_UNITS[unit] + r'(?![A-Za-z])', re.IGNORECASE)
    dates = [(match.start(), match.end()) for match in _DATE_SPANS.finditer(excerpt)]
    for match in pattern.finditer(excerpt):
        if any(start < match.end() and match.start() < end for start, end in dates):
            continue
        if excerpt[:match.start()].rstrip().endswith(('-', '−', '(', '（')):
            continue
        return True
    return False


def _null_dates(value, schema, prefix=''):
    paths = []
    if schema.get('format') == 'date' and value is None:
        return [prefix]
    if schema.get('type') == 'object' and isinstance(value, dict):
        for key, field in schema['properties'].items():
            if key in value:
                paths.extend(_null_dates(value[key], field, f'{prefix}.{key}'.lstrip('.')))
    return paths


def validate_record(record, pages=None):
    """Return errors. pages maps (source_id, PDF page sequence) to frozen text.

    Omit pages for field-only validation. Pass an empty mapping to require evidence
    and reject missing pages. Choice records are never tested against PDF excerpts.
    """
    errors = _fields(record, RECORD_SCHEMA)
    if errors:
        return errors
    kind = record['record_type']
    definition = obj(dict(COMMON, detail=DETAILS[kind]))
    notes = {note['field']: note['reason'] for note in record['date_notes']}
    for path in _null_dates(record, definition):
        if not notes.get(path, '').strip():
            errors.append(f'{path}: null date requires date_notes reason')
    if record['object_key']['period'] != record['period']['label']:
        errors.append('object_key.period: must equal period.label')
    if record['source']['kind'] == 'pdf':
        if not record['locator']['pdf_pages'] or not record['excerpts']:
            errors.append('PDF source requires page sequence and verbatim excerpts')
        if record['source']['choice_field'] is not None or record['source']['report_period'] is not None:
            errors.append('PDF source cannot contain Choice field/report period')
        if pages is not None:
            for page in record['locator']['pdf_pages']:
                if (record['source']['source_id'], page) not in pages:
                    errors.append(f'locator.pdf_pages: frozen page missing: {page}')
        for index, quote in enumerate(record['excerpts']):
            page = quote['page']
            if page not in record['locator']['pdf_pages']:
                errors.append(f'excerpts[{index}]: page is outside locator')
            if pages is not None:
                text = pages.get((record['source']['source_id'], page))
                if text is None:
                    errors.append(f'excerpts[{index}]: frozen page missing')
                elif normalize_whitespace(quote['text']) not in normalize_whitespace(text):
                    errors.append(f'excerpts[{index}]: excerpt is absent from frozen page')
        for index, item in enumerate(record['numbers']):
            try:
                if not any(number_in_excerpt(item, quote['text']) for quote in record['excerpts']):
                    errors.append(f'numbers[{index}]: value is absent from excerpts')
            except (InvalidOperation, ValueError) as exc:
                errors.append(f'numbers[{index}]: {exc}')
        if kind == 'obligation' and record['detail']['deadline']['amount'] is not None:
            if not any(deadline_in_excerpt(record['detail']['deadline'], quote['text'])
                       for quote in record['excerpts']):
                errors.append('detail.deadline.amount: duration amount and unit are absent from excerpts')
    else:
        if record['locator']['pdf_pages'] or record['excerpts']:
            errors.append('Choice source must not invent PDF pages or verbatim excerpts')
        if not record['source']['choice_field'] or not record['source']['report_period']:
            errors.append('Choice source requires field name and report period')
        if record['disclosed_on'] is None:
            errors.append('Choice source requires FIRSTNOTICEDATE as disclosed_on')
    if kind == 'metric' and record['detail']['value_status'] == 'reported' and not record['numbers']:
        errors.append('reported metric requires a numeric value')
    if kind == 'metric' and record['detail']['value_status'] != 'reported' and record['numbers']:
        errors.append('NA/NM metric must not have a fabricated numeric value')
    if any(item['unit'] == '未知' or item['source_unit'] == '未知' for item in record['numbers']) and not record['limitations']:
        errors.append('unknown numeric unit requires an explicit limitation')
    if record['nature'] == 'secondary' and (not record['origin']['original_source'] or
                                            not record['origin']['common_origin_id']):
        errors.append('secondary attribution requires original_source and common_origin_id')
    if record['nature'] == 'institution_forecast' and kind != 'statement' and not (record['origin']['speaker'] or '').strip():
        errors.append('institution_forecast requires origin.speaker for non-statement records')
    if kind == 'statement' and record['detail']['statement_type'] in ('market_rating', 'market_target') and not record['is_market']:
        errors.append('rating and target price must be marked as market content')
    if record['additivity']['role'] in ('total', 'component') and not record['additivity']['group']:
        errors.append('additive total/component requires an explicit group')
    return errors


def validate_output(output, pages=None, checklist=None):
    errors = _fields(output, OUTPUT_SCHEMA, 'output')
    if errors:
        return errors
    ids = [record['record_id'] for record in output['records']]
    if len(ids) != len(set(ids)):
        errors.append('duplicate record_id')
    for record in output['records']:
        errors.extend(f'{record["record_id"]}: {error}' for error in validate_record(record, pages))
    by_id = {record['record_id']: record for record in output['records']}
    for record in output['records']:
        for previous_id in record['supersedes']:
            previous = by_id.get(previous_id)
            # References outside this batch are checked when the record store is loaded.
            if previous is not None and (object_key(previous) != object_key(record) or
                    previous['disclosed_on'] is None or record['disclosed_on'] is None or
                    previous['disclosed_on'] >= record['disclosed_on']):
                errors.append(f'{record["record_id"]}: invalid supersedes relation to {previous_id}')
    for item in output['coverage']:
        unknown = set(item['record_ids']) - set(ids)
        if unknown:
            errors.append(f'{item["item_id"]}: nonexistent record_ids {sorted(unknown)}')
        if item['status'] == '已抽' and not item['record_ids']:
            errors.append(f'{item["item_id"]}: extracted coverage requires records')
        if item['status'] != '已抽' and item['record_ids']:
            errors.append(f'{item["item_id"]}: absent/uncovered coverage cannot cite records')
        if item['status'] == '本文档无' and not item['checked_pages']:
            errors.append(f'{item["item_id"]}: absence requires checked PDF pages')
    coverage_ids = [item['item_id'] for item in output['coverage']]
    if len(set(coverage_ids)) != len(coverage_ids):
        errors.append('duplicate coverage item_id')
    if checklist is not None:
        expected = {item['id'] for item in checklist['items']}
        if set(coverage_ids) != expected:
            errors.append('coverage items must exactly match the supplied checklist')
    return errors


def object_key(record):
    """Keep record kind, nature, scope and attributed voices on separate chains.

    Units are intentionally excluded: a later statement can clarify tonnes/year
    without losing the explicit correction link to the earlier tonnes wording.
    """
    key = record['object_key']
    kind, nature, origin = record['record_type'], record['nature'], record['origin']
    scope = record['detail'].get('scope') if kind == 'metric' else None
    attribution = ()
    if kind == 'statement' or nature in ('institution_forecast', 'secondary'):
        speaker = record['detail'].get('speaker') or origin['speaker']
        attribution = (speaker, origin['common_origin_id'], origin['original_source'])
    return (key['subject'], key['indicator'], key['period'], kind, nature, scope,
            tuple(sorted((x['name'], x['value']) for x in record['dimensions'])), attribution)


def coverage_relations(records, cutoff=None):
    """Describe later-disclosed replacements; never mutate or delete old records.

    Same-date conflicts are preserved as ties, not arbitrarily ordered. Null-date
    records are ineligible for historical latest selection.
    """
    groups = {}
    for record in records:
        disclosed = record['disclosed_on']
        if disclosed is None or (cutoff and disclosed > cutoff):
            continue
        groups.setdefault(object_key(record), []).append(record)
    result = []
    for values in groups.values():
        for old in values:
            later = [new for new in values if new['disclosed_on'] > old['disclosed_on']]
            if later:
                nearest = min(new['disclosed_on'] for new in later)
                result.append(dict(record_id=old['record_id'], covered_by=sorted(
                    new['record_id'] for new in later if new['disclosed_on'] == nearest)))
    return sorted(result, key=lambda x: x['record_id'])


def make_record(kind, record_id, source_id, subject, indicator, period_label, disclosed_on,
                as_of, excerpts, detail, **overrides):
    """Convenience constructor, followed by explicit validate_record/output."""
    pages = sorted({quote['page'] for quote in excerpts})
    record = dict(record_id=record_id, record_type=kind,
                  source=dict(kind='pdf', source_id=source_id, choice_field=None, report_period=None),
                  locator=dict(pdf_pages=pages, table=None, section=None), excerpts=excerpts,
                  disclosed_on=disclosed_on, as_of=as_of, date_notes=[], is_market=False,
                  extraction_version=VERSION, verification=dict(status='待复核', note='S1人工转写格式样例'),
                  object_key=dict(subject=subject, indicator=indicator, period=period_label),
                  period=dict(kind='unspecified', label=period_label), nature='actual', numbers=[],
                  components=[], additivity=dict(group=None, role='standalone', reason=''), dimensions=[],
                  origin=dict(speaker=None, original_source=None, common_origin_id=None),
                  limitations=[], supersedes=[], detail=detail)
    record.update(overrides)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    schema_parser = sub.add_parser('schema')
    schema_parser.add_argument('output', type=Path)
    check_parser = sub.add_parser('validate')
    check_parser.add_argument('input', type=Path)
    check_parser.add_argument('--pages', type=Path, help='JSON array: source_id, page, text')
    check_parser.add_argument('--checklist', type=Path)
    args = parser.parse_args()
    if args.command == 'schema':
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(extract_output_schema(), ensure_ascii=False, indent=2) + '\n')
        return
    pages = None
    if args.pages:
        pages = {(x['source_id'], x['page']): x['text'] for x in json.loads(args.pages.read_text())}
    checklist = json.loads(args.checklist.read_text()) if args.checklist else None
    errors = validate_output(json.loads(args.input.read_text()), pages, checklist)
    print(json.dumps(dict(ok=not errors, errors=errors), ensure_ascii=False, indent=2))
    raise SystemExit(bool(errors))


if __name__ == '__main__':
    main()
