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
MAX_EXCERPT_CHARS = 500  # Count non-whitespace characters, not JSON string length.
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
                           payment_status=enum(('unpaid', 'paid', 'part_paid', 'due', 'unspecified', 'not_applicable')))),
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


CODE_FIELDS = frozenset(('source', 'disclosed_on', 'extraction_version', 'verification'))


def model_output_schema(transport=False, compact=False):
    """S2 projection from the SAME contract; S1's full schema stays unchanged.

    Transport relaxation is used only after the real CLI rejects a keyword.
    Full field and evidence validation always runs against RECORD_SCHEMA.
    """
    if compact:
        return compact_model_output_schema(transport=transport)
    schema = extract_output_schema()
    for variant in schema['properties']['records']['items']['anyOf']:
        for key in CODE_FIELDS:
            variant['properties'].pop(key)
            variant['required'].remove(key)
    if transport:
        unsupported = {'$schema', 'title', 'format', 'pattern', 'minLength',
                       'minimum', 'maximum', 'uniqueItems', 'minItems', 'maxItems'}
        def strip(value):
            if isinstance(value, dict):
                return {key: strip(item) for key, item in value.items() if key not in unsupported}
            if isinstance(value, list):
                return [strip(item) for item in value]
            return value
        schema = strip(schema)
    return schema


def field_guide():
    """Compact field/enum guide generated from the canonical definitions."""
    def describe(schema):
        if 'anyOf' in schema:
            return ' OR '.join(describe(x) for x in schema['anyOf'])
        if schema.get('type') == 'object':
            return '{' + ', '.join(key + ':' + describe(value)
                                  for key, value in schema['properties'].items()) + '}'
        if schema.get('type') == 'array':
            return '[' + describe(schema['items']) + ']'
        if 'enum' in schema:
            return '|'.join('null' if x is None else str(x) for x in schema['enum'])
        return '/'.join(schema['type']) if isinstance(schema.get('type'), list) else schema.get('type', '')
    fields = {key: value for key, value in COMMON.items() if key not in CODE_FIELDS}
    return ('共有字段（全部必填，未知用格式允许的null、空数组或空串）：\n' +
            '\n'.join(key + ': ' + describe(value) for key, value in fields.items()) +
            '\n类型专属detail：\n' + '\n'.join(key + ': ' + describe(value) for key, value in DETAILS.items()) +
            '\ncoverage：' + describe(COVERAGE_SCHEMA) + '\nschema_version=' + VERSION +
            '\n格式约束：numbers.value必须匹配' + NUMBER['properties']['value']['pattern'] +
            '；只写普通十进制字符串，不含千分位逗号、单位、括号或科学计数法。千分位和括号只在excerpts保留；负数value用半角负号。' +
            '\nmetric每条只对应一个指标、期间及范围维度；reported时numbers只放一个数值，多列指标、同比、比例及分项分别拆记录。' +
            'NA/NM时numbers为空且detail.value_note非空。事件、义务和陈述可含多个已明确label的数值。' +
            '\n所有日期是YYYY-MM-DD或null；未知日期的date_notes.field用完整路径（as_of、detail.starts_on等），reason逐项说明。' +
            '\ncoverage仅status=已抽时record_ids非空；本文档无和本块未覆盖的record_ids必须为空。部分相关事实可抽成记录，但不能挂在未覆盖项下。' +
            '\n定义、释义和脚注中有实质含义的术语、交易程序、法定名称、注册地及名称标记也须用相应类型记录，不因位置而省略。')


def accept_model_output(output, *, source_id, disclosed_on, extraction_version,
                        prefix, pages, checklist, context_pages=(), supplied_spans=None,
                        compact=False, validation_policy='s2', expansion_record_errors=None):
    """Validate envelope once, then accept/reject records independently.

    Top-level/coverage schema errors invalidate the block; per-record field and
    evidence failures never discard the other records in a parseable block.
    """
    if compact:
        expanded = expand_compact_output(output)
        result = accept_model_output(
            {key: expanded[key] for key in ('schema_version', 'records', 'coverage')},
            source_id=source_id, disclosed_on=disclosed_on,
            extraction_version=extraction_version, prefix=prefix, pages=pages,
            checklist=checklist, context_pages=context_pages,
            supplied_spans=supplied_spans, validation_policy='s2b',
            expansion_record_errors=expanded['expansion_record_errors'])
        issues = expanded['expansion_issues']
        by_id = {record['record_id']: record for record in result['records']}
        for issue in issues:
            for local_id in issue['record_ids']:
                record = by_id.get(prefix + '-' + local_id)
                if record is not None:
                    record['verification']['status'] = '待复核'
                    record['verification']['note'] += '；' + issue['rule']
        result.update(entities=expanded['entities'], outside_index=expanded['outside_index'],
                      expansion_issues=issues, blank_cells=expanded['blank_cells'])
        result['soft_queue'] = [dict(record_id=record['record_id'], issues=list(dict.fromkeys(
            soft_record_issues(record) + [issue['rule'] for issue in issues
            if record['record_id'].removeprefix(prefix + '-') in issue['record_ids']])))
            for record in result['records'] if record['verification']['status'] == '待复核']
        result['suspicious_missing_columns'] = sum(
            issue['rule'] == '疑似漏列' for issue in issues)
        return result
    projected = model_output_schema()
    model_record_schema = projected['properties']['records']['items']
    envelope = deepcopy(projected)
    envelope['properties']['records']['items'] = {'type': 'object'}
    errors = _fields(output, envelope, 'output')
    if errors:
        raise ValueError('Overall schema: ' + '; '.join(errors))
    expected = {item['id'] for item in checklist['items']}
    coverage_ids = [item['item_id'] for item in output['coverage']]
    if set(coverage_ids) != expected or len(set(coverage_ids)) != len(coverage_ids):
        raise ValueError('Overall schema: coverage must exactly match checklist')
    raw_ids = [record.get('record_id') for record in output['records']]
    valid_ids = [key for key in raw_ids if isinstance(key, str)]
    if len(set(valid_ids)) != len(valid_ids):
        raise ValueError('Overall schema: duplicate record ids make coverage ambiguous')
    rename = {key: prefix + '-' + key for key in valid_ids}
    accepted, rejected, discarded = [], [], []
    for index, raw in enumerate(output['records']):
        record = deepcopy(raw)
        record['record_id'] = (rename[raw['record_id']] if isinstance(raw.get('record_id'), str)
                               else prefix + '-invalid-id-' + str(index))
        if isinstance(raw.get('supersedes'), list) and all(isinstance(key, str) for key in raw['supersedes']):
            record['supersedes'] = [rename.get(key, key) for key in raw['supersedes']]
        # Source metadata is code owned; extra model fields are refused first.
        record_errors = _fields(raw, model_record_schema)
        record_errors += (expansion_record_errors or {}).get(raw.get('record_id'), [])
        record.update(source=dict(kind='pdf', source_id=source_id, choice_field=None, report_period=None),
                      disclosed_on=disclosed_on, extraction_version=extraction_version,
                      verification=dict(status='代码通过', note='S2字段、冻结页摘录与数值检查；未经语义验收'))
        record_errors += validate_record(record, pages, policy=validation_policy)
        if raw.get('supersedes'):
            record_errors.append('supersedes: no prior records supplied; must be empty')
        if supplied_spans is not None and not record_errors:
            for index, quote in enumerate(record['excerpts']):
                if not any(normalize_whitespace(quote['text']) in normalize_whitespace(span)
                           for span in supplied_spans.get(quote['page'], [])):
                    record_errors.append(f'excerpts[{index}]: outside supplied source spans')
        record_errors = list(dict.fromkeys(record_errors))
        if record_errors:
            record['verification'] = dict(status='拒收', note='；'.join(record_errors))
            rejected.append(dict(record=record, errors=record_errors))
        elif record['excerpts'] and all(q['page'] in context_pages for q in record['excerpts']):
            discarded.append(dict(record=record, reason='all excerpts lie on read-only context pages'))
        else:
            if validation_policy == 's2b':
                soft = soft_record_issues(record)
                if soft:
                    record['verification'] = dict(status='待复核', note='；'.join(soft))
            accepted.append(record)
    good_ids = {r['record_id'] for r in accepted}
    all_ids = set(rename.values())
    coverage = deepcopy(output['coverage'])
    for item in coverage:
        item['record_ids'] = [rename.get(key, prefix + '-' + key) for key in item['record_ids']]
        if set(item['record_ids']) - all_ids:
            raise ValueError('Overall schema: coverage references nonexistent records')
        if item['status'] == '已抽' and (not item['record_ids'] or any(key not in good_ids for key in item['record_ids'])):
            item['status'] = '未通过核验'
            item['basis'] += '；引用的记录被代码拒收或仅落在只读上下文。'
        if item['status'] == '已抽' and not item['checked_pages']:
            raise ValueError('Overall schema: extracted coverage requires page evidence')
        if item['status'] != '已抽' and item['status'] != '未通过核验' and item['record_ids']:
            raise ValueError('Overall schema: absence/uncovered coverage cites records')
        if item['status'] == '本文档无' and (not item['checked_pages'] or not item['basis'].strip()):
            raise ValueError('Overall schema: absence requires page evidence')
        if set(item['checked_pages']) - {p for sid, p in pages if sid == source_id}:
            raise ValueError('Overall schema: coverage cites an unread page')
    return dict(records=accepted, rejected=rejected, discarded=discarded, coverage=coverage,
                output_records=len(output['records']), rejection_rate=len(rejected)/len(raw_ids) if raw_ids else 0)


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
            errors.extend(_fields(item, schema.get('items', {}), f'{path}[{index}]'))
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
    """Ignore layout gaps except those separating numeric characters/tokens."""
    def numeric(index):
        char = value[index]
        return char.isdigit() or (char in '.,%' and
            ((index > 0 and value[index - 1].isdigit()) or
             (index + 1 < len(value) and value[index + 1].isdigit())))

    return re.sub(r'\s+', lambda m: ' ' if m.start() > 0 and m.end() < len(value)
                  and numeric(m.start() - 1) and numeric(m.end()) else '', value)


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


def formatted_number(item, allow_time_denominator=False):
    value = Decimal(item['value'])
    left, right = item['unit'], item['source_unit']
    if allow_time_denominator:
        left, right = _without_time_denominator(left), _without_time_denominator(right)
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


def number_in_excerpt(item, excerpt, *, allow_integer_words=False, allow_time_denominator=False):
    """Match a whole numeric token, including comma grouping and bracket negatives."""
    token = formatted_number(item, allow_time_denominator=allow_time_denominator)
    negative = token.startswith('-')
    token = token.lstrip('-')
    integer, dot, fraction = token.partition('.')
    grouped = format(int(integer), ',') + (dot + fraction if dot else '')
    alternatives = [token, grouped]
    escaped = '(?:' + '|'.join(re.escape(x) for x in dict.fromkeys(alternatives)) + ')'
    if negative:
        percent = r'(?:\s*[%％])?' if item['source_unit'] == '%' else ''
        escaped = ('(?:[-−－]\\s*' + escaped + r'|\(\s*' + escaped + percent +
                   r'\s*\)|（\s*' + escaped + percent + r'\s*）)')
    # A positive value must not match inside a negative, a larger integer or decimal.
    for match in re.finditer(r'(?<![\d.,\-−－(（])' + escaped + r'(?!\d|[.,]\d)', excerpt):
        if not negative and excerpt[:match.start()].rstrip().endswith(('-', '−', '－', '(', '（')):
            continue
        return True
    if allow_integer_words and not negative and Decimal(token) == Decimal(token).to_integral_value():
        value = int(Decimal(token))
        # English words need word boundaries; Chinese words need numeral boundaries
        # so 三 cannot satisfy 十三. Unit suffixes such as 年 remain allowed.
        for word in _integer_words(value):
            english = word.isascii()
            boundary = r'A-Za-z0-9\-' if english else r'0-9零〇一二三四五六七八九十百千万亿两'
            form = re.escape(word).replace(r'\ ', r'\s+')
            if re.search(r'(?<![' + boundary + '])' + form + r'(?![' + boundary + '])',
                         excerpt, re.IGNORECASE):
                return True
    return False


def _without_time_denominator(unit):
    return re.sub(r'/(?:年|月|日|天|小时|year|month|day|hour)s?$', '', unit, flags=re.IGNORECASE)


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


def validate_record(record, pages=None, *, policy='s2'):
    """Return errors. pages maps (source_id, PDF page sequence) to frozen text.

    Omit pages for field-only validation. Pass an empty mapping to require evidence
    and reject missing pages. Choice records are never tested against PDF excerpts.
    """
    if policy not in ('s2', 's2b'):
        raise ValueError('unknown validation policy: ' + str(policy))
    errors = _fields(record, RECORD_SCHEMA)
    if errors:
        return errors
    kind = record['record_type']
    definition = obj(dict(COMMON, detail=DETAILS[kind]))
    notes = {note['field']: note['reason'] for note in record['date_notes']}
    for path in _null_dates(record, definition):
        if not notes.get(path, '').strip() and policy != 's2b':
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
            if len(re.sub(r'\s+', '', quote['text'])) > MAX_EXCERPT_CHARS:
                errors.append(f'excerpts[{index}]: exceeds {MAX_EXCERPT_CHARS} non-whitespace characters')
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
                if not any(number_in_excerpt(item, quote['text'], allow_integer_words=policy == 's2b',
                                             allow_time_denominator=policy == 's2b') for quote in record['excerpts']):
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
    if policy != 's2b' and any(item['unit'] == '未知' or item['source_unit'] == '未知' for item in record['numbers']) and not record['limitations']:
        errors.append('unknown numeric unit requires an explicit limitation')
    if policy != 's2b' and record['nature'] == 'secondary' and (not record['origin']['original_source'] or
                                            not record['origin']['common_origin_id']):
        errors.append('secondary attribution requires original_source and common_origin_id')
    if policy != 's2b' and record['nature'] == 'institution_forecast' and kind != 'statement' and not (record['origin']['speaker'] or '').strip():
        errors.append('institution_forecast requires origin.speaker for non-statement records')
    if policy != 's2b' and kind == 'statement' and record['detail']['statement_type'] in ('market_rating', 'market_target') and not record['is_market']:
        errors.append('rating and target price must be marked as market content')
    if policy != 's2b' and record['additivity']['role'] in ('total', 'component') and not record['additivity']['group']:
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


# S2b's transport contract is separate from the immutable stored record contract.
COMPACT_VERSION = 'wiki-compact/1.0'
SPARSE = array(obj(dict(key=TEXT, value=string())))
QUOTES = array(obj(dict(page=dict(type='integer', minimum=1), text=TEXT)))
DIMENSIONS = COMMON['dimensions']
COMPACT_ENTITY = obj(dict(id=TEXT,
    type=enum(('company', 'transaction', 'product_or_industry', 'person', 'other')),
    names=array(TEXT, minItems=1, uniqueItems=True), canonical=TEXT,
    renamed_from=array(TEXT, uniqueItems=True)))
COMPACT_NUMBER = obj(dict(value=string(), unit=enum(UNITS), currency=enum(CURRENCIES, True),
                         source_unit=enum(UNITS)))
COMPACT_COLUMN = obj(dict(label=string(), period=TEXT, kind=enum(PERIODS),
    nature=enum(NATURES), as_of=DATE, unit=enum(UNITS, True),
    currency=enum(CURRENCIES, True), dimensions=DIMENSIONS, extras=SPARSE))
COMPACT_ROW = obj(dict(id=TEXT, name=TEXT, excerpts=QUOTES,
                      values=array(string()), extras=SPARSE))
COMPACT_TABLE = obj(dict(id=TEXT, name=TEXT, pages=array(dict(type='integer', minimum=1)),
    header=QUOTES, entity=TEXT,
    scope=DETAILS['metric']['anyOf'][0]['properties']['scope'],
    unit=enum(UNITS), currency=enum(CURRENCIES, True), columns=array(COMPACT_COLUMN, minItems=1),
    rows=array(COMPACT_ROW), extras=SPARSE))
COMPACT_RECORD = obj(dict(id=TEXT, type=enum(DETAILS), entity=TEXT, indicator=TEXT,
    period=TEXT, kind=enum(PERIODS), nature=enum(NATURES), as_of=DATE,
    excerpts=QUOTES, numbers=array(COMPACT_NUMBER), extras=SPARSE))
COMPACT_SCHEMA = obj(dict(schema_version=enum((COMPACT_VERSION,)),
    entities=array(COMPACT_ENTITY), tables=array(COMPACT_TABLE), records=array(COMPACT_RECORD),
    coverage=array(COVERAGE_SCHEMA), outside_index=array(obj(dict(page=dict(type='integer', minimum=1), title=TEXT)))))


def compact_model_output_schema(transport=True):
    """Small strict-schema transport. Full local rules remain authoritative."""
    schema = deepcopy(COMPACT_SCHEMA)
    if transport:
        unsupported = {'format', 'pattern', 'minLength', 'minimum', 'maximum',
                       'uniqueItems', 'minItems', 'maxItems'}
        def strip(value):
            if isinstance(value, dict):
                return {key: strip(item) for key, item in value.items() if key not in unsupported}
            if isinstance(value, list):
                return [strip(item) for item in value]
            return value
        schema = strip(schema)
    return schema


def compact_field_guide():
    """Human guide for sparse keys; the --output-schema carries core fields."""
    guide = '''schema_version=wiki-compact/1.0。所有Schema键必填，没内容用[]、null或允许的空串。
entities: id、type(company/transaction/product_or_industry/person/other)、names(本块全部原文名称写法)、canonical、renamed_from(原文明确的旧名)。canonical须在names中；公司法定全称可来自给定名称表。
tables: id/name/pages/header([{page,text}]列头连续摘录)/entity/scope/unit/currency/columns/rows/extras。
columns: label(列头名称，普通期间列可空串)/period/kind/nature/as_of/unit(null继承表)/currency(null继承表)/dimensions/extras。必须列全表头实际印出的历史、预测、同比、占比、变动等数值列。
rows: id/name/excerpts([{page,text}]行连续摘录)/values(严格按列序、原样字符串，数量必须等于列数，空白填"")/extras。每个非空格由代码展开一个metric。只在values保留千分位与括号。NA/NM保留原文。列头与行摘录分别连续，不自行拼回断句。
records: id/type/entity/indicator/period/kind/nature/as_of/excerpts/numbers/extras。只用于非表格事实；numbers每项value/unit/currency/source_unit，value用普通十进制。每条只承载一个事实。
extras是稀疏[{key,value}]，value必须为JSON编码的字符串（字符串要包含双引号）。只写用到的字段。可用key: components,additivity,origin,dimensions,limitations,date_notes,is_market,locator.table,locator.section,detail,detail.scope,detail.scope_note,detail.event_type,detail.occurred_on,detail.stage,detail.description,detail.obligor,detail.beneficiary,detail.action,detail.deadline,detail.conditions,detail.consequences,detail.payment_status,detail.subject,detail.object,detail.relationship_type,detail.starts_on,detail.ends_on,detail.status,detail.speaker,detail.content,detail.statement_type,detail.attribution_note。表/行还可用unit,currency,source_unit,as_of；数字可在extras用numbers整体覆盖以写qualifier或label。
未写的components/dimensions/limitations/date_notes为空，additivity是standalone，origin三项null，is_market=false。metric scope默认unspecified；event默认other/unspecified；relationship默认other/unspecified；statement默认explanation，speaker默认实体规范名。事件/关系/义务的实际细节必须在extras中填写，未知日期不猜。
表、行、列extras按此顺序覆盖；origin/additivity/日期说明等用完整对象或数组。表的metric范围默认来自scope，行名成为indicator；同比/增减/占比列的label加入indicator括号，真正拆分才放dimensions，本期数/上年同期不放dimensions。
coverage同清单逐项填item_id/status/record_ids/checked_pages/basis。record_ids可引用非表记录id、整表id、行id或展开格id(行id-c001起)；代码展开引用。outside_index每项只有page/title，只列清单外表格和小节原文标题，不生成记录。
所有日期YYYY-MM-DD或null；未写date_notes的null日期将标待复核。期间kind=instant/annual/half_year/quarter/range/event/unspecified；nature=actual/guidance/institution_forecast/judgment/secondary。A列可用actual并以origin说明转引，E列institution_forecast须给origin.speaker。scope=consolidated/parent/segment/subsidiary/company_model/unspecified。'''

    def describe(schema):
        if 'anyOf' in schema:
            return ' OR '.join(describe(item) for item in schema['anyOf'])
        if 'enum' in schema:
            return '|'.join('null' if item is None else str(item) for item in schema['enum'])
        if schema.get('type') == 'object':
            return '{' + ','.join(key + ':' + describe(item)
                for key, item in schema['properties'].items()) + '}'
        if schema.get('type') == 'array':
            return '[' + describe(schema['items']) + ']'
        if schema.get('format') == 'date':
            return 'YYYY-MM-DD|null'
        types = schema.get('type', '')
        return '|'.join(types) if isinstance(types, list) else types
    sparse_fields = ('components', 'additivity', 'origin', 'dimensions', 'limitations',
                     'date_notes', 'is_market')
    guide += '\n稀疏字段的精确展开类型（对象所有列出的键必填；不接受别名或多余键）：\n'
    guide += '\n'.join(key + '=' + describe(COMMON[key]) for key in sparse_fields)
    guide += '\nnumbers整体覆盖时每项的精确类型=' + describe(NUMBER)
    guide += '\n各record type专属detail的精确字段和枚举；仅该类型的字段可用，不允许跨类型字段：\n'
    guide += '\n'.join(kind + '.detail=' + describe(schema) for kind, schema in DETAILS.items())
    guide += ('\norigin只允许speaker/original_source/common_origin_id，不存在source键。'
              'date_notes必须是[{"field":"as_of","reason":"仅有月份"}]这类数组，不能写成对象。'
              'relationship_type持股用ownership，status现有用current；没有shareholding/existing。'
              'statement_type风险用risk；没有risk_assessment；statement.detail不能含scope。'
              '\n实体引用：record.entity和table.entity必须填实体ID；detail.subject/object/obligor/'
              'beneficiary/speaker以及origin.speaker指向已列实体时也填该ID，代码展开成canonical。'
              '不在实体表中的第三方名称或一般角色文字按原文保留，代码不猜测或替换。'
              '\n稀疏对象可整体写detail（必须该类型全部字段），也可逐个写detail.xxx以保留默认值。'
              '例：{"key":"origin","value":"{\\"speaker\\":null,\\"original_source\\":'
              '\\"合成年报\\",\\"common_origin_id\\":\\"synthetic-2025\\"}"}。')
    return guide


_SPARSE_KEYS = frozenset((
    'components', 'additivity', 'origin', 'dimensions', 'limitations', 'date_notes',
    'is_market', 'locator.table', 'locator.section', 'detail', 'numbers',
    'unit', 'currency', 'source_unit', 'as_of',
) + tuple('detail.' + key for detail in DETAILS.values()
          for key in (detail.get('properties') or detail['anyOf'][0]['properties'])))


def _sparse_values(items):
    result = {}
    for item in items:
        key = item['key']
        if key not in _SPARSE_KEYS:
            raise ValueError('unknown sparse field: ' + key)
        if key in result:
            raise ValueError('duplicate sparse field: ' + key)
        try:
            result[key] = json.loads(item['value'])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError('sparse field is not JSON: ' + key) from exc
    return result


def _default_detail(kind, subject, indicator):
    if kind == 'metric':
        return dict(scope='unspecified', scope_note='', value_status='reported', value_note='')
    if kind == 'event':
        return dict(event_type='other', occurred_on=None, stage='unspecified', description=indicator)
    if kind == 'obligation':
        return dict(obligor=subject, beneficiary=None, action=indicator,
                    deadline=dict(amount=None, unit='none', from_event='', from_date=None, deadline_on=None),
                    conditions=[], consequences=[], payment_status='unspecified')
    if kind == 'relationship':
        return dict(subject=subject, object=indicator, relationship_type='other', starts_on=None,
                    ends_on=None, status='unspecified', description=indicator)
    return dict(speaker=subject, content=indicator, statement_type='explanation', attribution_note='')


def _apply_sparse(record, values):
    for key, value in values.items():
        if key in ('unit', 'currency', 'source_unit'):
            if isinstance(record['numbers'], list):
                for item in record['numbers']:
                    if isinstance(item, dict):
                        item[key] = deepcopy(value)
        elif key.startswith(('detail.', 'locator.')):
            parent, leaf = key.split('.', 1)
            # Preserve an invalid sparse parent verbatim for field rejection;
            # a later nested override must not crash or silently repair it.
            if isinstance(record[parent], dict):
                record[parent][leaf] = deepcopy(value)
        else:
            record[key] = deepcopy(value)
    return record



def _resolve_entity_fields(record, entities):
    """Resolve explicit entity references only; never fuzzy-match source names."""
    paths = (('detail', ('subject', 'object', 'obligor', 'beneficiary', 'speaker')),
             ('origin', ('speaker',)))
    for container, fields in paths:
        value = record.get(container)
        if not isinstance(value, dict):
            continue
        for field in fields:
            reference = value.get(field)
            if isinstance(reference, str) and reference in entities:
                value[field] = entities[reference]['canonical']
    return record


def _compact_number(item):
    # Preserve invalid model types verbatim for independent record rejection.
    if not isinstance(item, dict):
        return deepcopy(item)
    result = deepcopy(item)
    value = result.get('value')
    result.update(decimals=len(value.partition('.')[2]) if isinstance(value, str) else 0,
                  qualifier='exact', label='')
    return result


def _cell_number(raw, unit, currency, source_unit):
    value = raw.strip()
    if value.upper() in ('NA', 'N/A', 'N.A.', 'N.A', 'NOT AVAILABLE') or value in ('—', '–', '-'):
        return [], 'not_available', '原表印作' + value
    if value.upper() in ('NM', 'N/M', 'N.M.', 'N.M', 'NOT MEANINGFUL'):
        return [], 'not_meaningful', '原表印作' + value
    numeric = value.replace(',', '').replace('，', '').replace('−', '-').replace('－', '-')
    if numeric.startswith('增加'):
        numeric = numeric[len('增加'):].strip()

    def printed_suffix(text):
        match = re.search(r'\s*(个百分点|百分点|ppts?|倍|x|[%％])$', text, re.IGNORECASE)
        if not match:
            return text, None
        label = match[1].lower()
        suffix_unit = ('百分点' if label in ('个百分点', '百分点', 'ppt', 'ppts')
                       else '倍' if label in ('x', '倍') else '%')
        return text[:match.start()].strip(), suffix_unit

    numeric, printed_unit = printed_suffix(numeric)
    if numeric.startswith(('(', '（')) and numeric.endswith((')', '）')):
        numeric = '-' + numeric[1:-1].strip()
    # Both (5%) and (5)% are printed forms. A second contradictory suffix
    # stays invalid rather than being guessed into a numeric value.
    if printed_unit is None:
        numeric, printed_unit = printed_suffix(numeric)
    if numeric.startswith('+'):
        numeric = numeric[1:].strip()
    if printed_unit is not None:
        unit, source_unit, currency = printed_unit, printed_unit, None
    # A percentage/share/physical column must not inherit a monetary table currency.
    if unit not in ('元', '千元', '万元', '百万元', '十亿元', '亿元', '元/股', '元/芯公里'):
        currency = None
    # JSON decimal strings deliberately retain printed decimal precision.
    # Keep malformed residual text for the per-record field validator rather than
    # dropping every other valid cell in this block.
    return [number(numeric, unit, currency, source_unit)], 'reported', ''


def expand_compact_output(output):
    """Expand compact cells into the existing projection; no evidence is invented.

    IDs table/row expand into all corresponding cell IDs for coverage references.
    Structural compact errors reject the block before independent record checks.
    """
    # A malformed independent record must not discard its valid neighbours.
    # Keep the envelope, identity and shared tables strict; record content is
    # checked separately and carried through to that record's hard rejection.
    envelope = deepcopy(COMPACT_SCHEMA)
    record_envelope = envelope['properties']['records']['items']
    record_envelope['properties'] = {key: (deepcopy(value) if key in ('id', 'entity') else {})
        for key, value in COMPACT_RECORD['properties'].items()}
    # An empty period is an invalid cell field, not an ambiguous table layout.
    # Expand it unchanged so only the cells in this column are hard-rejected.
    envelope['properties']['tables']['items']['properties']['columns']['items'][
        'properties']['period'] = string()
    errors = _fields(output, envelope, 'compact')
    if errors:
        raise ValueError('Overall compact schema: ' + '; '.join(errors))
    entities = {}
    for entity in output['entities']:
        if entity['id'] in entities:
            raise ValueError('duplicate entity id: ' + entity['id'])
        if entity['canonical'] not in entity['names']:
            raise ValueError('entity canonical must occur in names: ' + entity['id'])
        entities[entity['id']] = entity
    records, aliases, expansion_issues, blank_cells = [], {}, [], 0
    expansion_record_errors = {}
    all_ids = set()

    def base(kind, record_id, entity_id, indicator, period_label, period_kind, nature, as_of, excerpts):
        if entity_id not in entities:
            raise ValueError('unknown entity: ' + entity_id)
        if record_id in all_ids:
            raise ValueError('duplicate compact record/row/table id: ' + record_id)
        all_ids.add(record_id)
        subject = entities[entity_id]['canonical']
        result = make_record(kind, record_id, 'code-owned', subject, indicator,
                             period_label, None, as_of, [],
                             _default_detail(kind, subject, indicator),
                             period=dict(kind=period_kind, label=period_label), nature=nature)
        result['excerpts'] = deepcopy(excerpts)
        if isinstance(excerpts, list):
            result['locator']['pdf_pages'] = sorted({quote['page'] for quote in excerpts
                if isinstance(quote, dict) and isinstance(quote.get('page'), int)
                and not isinstance(quote.get('page'), bool)})
        return result

    def finish(record):
        _resolve_entity_fields(record, entities)
        for key in CODE_FIELDS:
            record.pop(key)
        records.append(record)

    for raw in output['records']:
        individual_errors = _fields(raw, COMPACT_RECORD, 'compact_record')
        record = base(raw['type'], raw['id'], raw['entity'], raw['indicator'], raw['period'],
                      raw['kind'], raw['nature'], raw['as_of'], raw['excerpts'])
        record['numbers'] = ([_compact_number(item) for item in raw['numbers']]
                             if isinstance(raw['numbers'], list) else deepcopy(raw['numbers']))
        if not _fields(raw['extras'], SPARSE, 'extras'):
            try:
                _apply_sparse(record, _sparse_values(raw['extras']))
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                individual_errors.append('compact_record.extras: ' + str(exc))
        if individual_errors:
            expansion_record_errors[raw['id']] = individual_errors
        finish(record)
        aliases[raw['id']] = [raw['id']]
    for table in output['tables']:
        if table['id'] in all_ids:
            raise ValueError('duplicate compact record/row/table id: ' + table['id'])
        all_ids.add(table['id'])
        table_values = _sparse_values(table['extras'])
        aliases[table['id']] = []
        for row in table['rows']:
            if row['id'] in all_ids:
                raise ValueError('duplicate compact record/row/table id: ' + row['id'])
            all_ids.add(row['id'])
            if len(row['values']) != len(table['columns']):
                raise ValueError('table row value count differs from columns: ' + row['id'])
            aliases[row['id']] = []
            row_values = _sparse_values(row['extras'])
            for index, (raw, column) in enumerate(zip(row['values'], table['columns']), 1):
                if not raw.strip():
                    blank_cells += 1
                    continue
                sparse = dict(table_values, **row_values)
                sparse.update(_sparse_values(column['extras']))
                unit = sparse.get('unit', column['unit'] or table['unit'])
                currency = sparse.get('currency', column['currency'] or table['currency'])
                source_unit = sparse.get('source_unit', unit)
                values, status, note = _cell_number(raw, unit, currency, source_unit)
                indicator = row['name']
                # A column may select a different metric, not just a period.
                # Keeping only the row name would merge e.g. gross receivables,
                # loss allowances and their percentages into the same key.
                label = column['label'].strip()
                temporal = re.fullmatch(r'(?:本报告期|本期|本期数|本期金额|本期发生额|报告期|'
                    r'上期|上期数|上期金额|上期发生额|上年同期|上年度末|本报告期末|本期末|上期末|期初|期末|'
                    r'\d{4}(?:[AE]|H[12]|Q[1-4]|年)?|\d{4}-\d{2}-\d{2}|[1-4]Q\d{2})', label)
                represented = any(label == d['value'] for d in column['dimensions'])
                if label and not temporal and not represented:
                    indicator += '（' + label + '）'
                record_id = row['id'] + f'-c{index:03}'
                quotes = deepcopy(row['excerpts'] + table['header'])
                record = base('metric', record_id, table['entity'], indicator,
                              column['period'], column['kind'], column['nature'],
                              column['as_of'], quotes)
                record['locator']['table'] = table['name']
                record['detail'].update(scope=table['scope'], value_status=status, value_note=note)
                record['dimensions'] = deepcopy(column['dimensions'])
                record['numbers'] = values
                _apply_sparse(record, sparse)
                # Per-row semantic extras must not turn NA/NM into numbers.
                # Invalid detail types survive for that cell's field rejection.
                if isinstance(record['detail'], dict):
                    record['detail'].update(value_status=status, value_note=note)
                finish(record)
                aliases[row['id']].append(record_id)
                aliases[table['id']].append(record_id)
                aliases[record_id] = [record_id]
            printed = sum(len(re.findall(r'(?<![A-Za-z\d])[-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?',
                                        quote['text'])) for quote in row['excerpts'])
            nonempty = sum(bool(value.strip()) for value in row['values'])
            if printed > nonempty:
                expansion_issues.append(dict(rule='疑似漏列', table_id=table['id'], row_id=row['id'],
                    excerpt_numeric_tokens=printed, nonempty_cells=nonempty,
                    record_ids=aliases[row['id']]))
    coverage = deepcopy(output['coverage'])
    for item in coverage:
        resolved = []
        for key in item['record_ids']:
            if key not in aliases:
                raise ValueError('coverage references unknown compact id: ' + key)
            resolved.extend(aliases[key])
        item['record_ids'] = list(dict.fromkeys(resolved))
    return dict(schema_version=VERSION, records=records, coverage=coverage,
                outside_index=deepcopy(output['outside_index']), entities=deepcopy(output['entities']),
                expansion_issues=expansion_issues, blank_cells=blank_cells,
                expansion_record_errors=expansion_record_errors)


def soft_record_issues(record):
    """Semantic/coherence checks that enqueue review without rejecting evidence."""
    result = []
    if record['additivity']['role'] in ('total', 'component') and not record['additivity']['group']:
        result.append('total/component缺组')
    if any(item['unit'] == '未知' or item['source_unit'] == '未知'
           for item in record['numbers']) and not record['limitations']:
        result.append('未知单位缺口径说明')
    if (record['nature'] == 'institution_forecast' and record['record_type'] != 'statement'
            and not (record['origin']['speaker'] or '').strip()):
        result.append('机构预测缺origin.speaker')
    if (record['record_type'] == 'statement' and record['detail']['statement_type'] in
            ('market_rating', 'market_target') and not record['is_market']):
        result.append('评级或目标价缺is_market标记')
    origin = record['origin']
    # Historical A columns may remain actual while explicitly attributed.
    if (record['nature'] == 'secondary' or origin['original_source'] or origin['common_origin_id']) and (
            not origin['original_source'] or not origin['common_origin_id']):
        result.append('转引缺original_source或common_origin_id')
    definition = obj(dict(COMMON, detail=DETAILS[record['record_type']]))
    notes = {item['field']: item['reason'] for item in record['date_notes']}
    for path in _null_dates(record, definition):
        if not notes.get(path, '').strip():
            result.append(path + ': null日期缺说明')
    if record['record_type'] == 'statement' and (len(record['numbers']) > 1 or
            len({item['label'] for item in record['numbers'] if item['label']}) > 1):
        result.append('statement疑似打包多个指标或多期数值')
    return result

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
