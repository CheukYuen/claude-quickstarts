"""Synthetic interface interpretation only; no network or SDK login."""
import unittest

from fetch import pack
from s1 import inspect_fx_query


class FXInterpretationTests(unittest.TestCase):
    def query(self, close):
        return {'query': {'function': 'csd', 'args': ['SYNTH.EX', 'CLOSE,SNAME', '2026-01-01', '2026-01-02', 'Ispandas=0']},
                'result': {'ErrorCode': 0, 'ErrorMsg': 'synthetic success',
                           'Codes': pack(['SYNTH.EX']), 'Indicators': pack(['CLOSE', 'SNAME']),
                           'Dates': pack(['2026/01/01', '2026/01/02']),
                           'Data': pack({'SYNTH.EX': [close, ['模拟币中间价', '模拟币中间价']]})}}

    def test_metadata_success_is_not_numeric_quote_success(self):
        result = inspect_fx_query(self.query([None, None]), 'choice-synthetic', 'synthetic-hash')
        self.assertEqual(result['outcome'], 'no_numeric_quote')
        self.assertEqual(result['numeric_quote_count'], 0)
        self.assertEqual(len(result['quotes']), 2)
        self.assertIsNone(result['quotes'][0]['raw_value'])

    def test_numeric_values_keep_original_basis(self):
        result = inspect_fx_query(self.query([1.25, None]), 'choice-synthetic', 'synthetic-hash')
        self.assertEqual(result['numeric_quote_count'], 1)
        self.assertEqual(result['outcome'], 'numeric_quotes')
        self.assertEqual(result['quotes'][0]['raw_value'], 1.25)
        self.assertEqual(result['quotes'][0]['basis'], 'midpoint')


if __name__ == '__main__':
    unittest.main()
