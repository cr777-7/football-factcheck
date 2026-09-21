import http.client
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import parallel_check as check
import generate_report as report
from test_workflow import FakeClient, response


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.model = {'name': 'first', 'model': 'one-model', 'endpoint': 'https://example.com/v1/chat/completions', 'api_key_env': 'TEST_KEY'}
        self.config = {'models': [self.model], 'timeout_seconds': 1, 'max_tokens': 128, 'retries': 0, 'batch_size': 1, 'max_workers': 2}
        self.claims = [{'id': 'C001', 'text': '原话', 'context': '国家队；第1句'}]
        self.original = {'as_of': '2026-09-21', 'claims': self.claims}
        self.document = {'title': '核查', 'checked_at': '2026-09-21', 'as_of': '2026-09-21', 'model_summary': '未联网', 'claims': [
            {'id': 'C001', 'text': '原话', 'status': '证据不足', 'reason': '待查', 'sources': []}]}
        env = patch.dict(os.environ, {'TEST_KEY': 'fake-credential'})
        env.start()
        self.addCleanup(env.stop)

    def test_nested_template_text_stays_excluded(self):
        from extract_text import extract
        result = extract('<p>可见A</p><template><template>隐藏1</template>隐藏2</template><p>可见B</p>')
        self.assertIn('可见A', result)
        self.assertIn('可见B', result)
        self.assertNotIn('隐藏', result)

    def test_external_config_resolves_only_adjacent_env(self):
        external = self.root / 'external' / 'config.json'
        external.parent.mkdir()
        (self.root / '.env').write_text('TEST_KEY=other-value')
        self.assertIsNone(check.resolve_env(external))
        adjacent = external.parent / '.env'
        adjacent.write_text('TEST_KEY=correct-value')
        self.assertEqual(check.resolve_env(external), adjacent.resolve())

    def test_same_model_across_gateways_rejected(self):
        path = self.root / 'config.json'
        path.write_text(json.dumps({'models': [self.model, dict(self.model, name='other', endpoint='https://other.example/v1/chat/completions')]}))
        with self.assertRaises(ValueError):
            check.read_config(path)

    def test_same_returned_model_not_counted_twice(self):
        config = dict(self.config, models=[self.model, dict(self.model, name='second', model='second-model')])
        results = [{'name': name, 'returned_model': 'same-upstream'} for name in ['first', 'second']]
        count, warnings = check.independent_models(config, results, ['first', 'second'])
        self.assertEqual(count, 1)
        self.assertTrue(warnings)

    def test_transport_incomplete_read_is_recorded(self):
        client = FakeClient([http.client.IncompleteRead(b'partial')])
        result = check.review_batch(self.model, self.claims, '2026-09-21', self.config, client)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error'], 'network_error')

    def test_unexpected_failure_does_not_erase_success(self):
        config = dict(self.config, models=[self.model, dict(self.model, name='second', model='second-model')])
        def fake(model, *args):
            if model['name'] == 'first':
                raise RuntimeError('private transport error')
            return {'name': model['name'], 'status': 'ok', 'claim_ids': ['C001']}
        with patch.object(check, 'review_batch', side_effect=fake):
            result = check.run_reviews(self.original, config)
        self.assertEqual(result['successful_requests'], 1)
        self.assertEqual(result['results'][0]['error'], 'unexpected_request_error')
        self.assertNotIn('private transport error', json.dumps(result))

    def test_configurable_output_limit_parameter(self):
        client = FakeClient([response()])
        check.review_batch(dict(self.model, token_limit_parameter='max_completion_tokens'), self.claims, '2026-09-21', self.config, client)
        sent = json.loads(client.requests[0].data)
        self.assertEqual(sent['max_completion_tokens'], 128)
        self.assertNotIn('max_tokens', sent)

    def test_report_missing_claim_is_rejected(self):
        original = dict(self.original, claims=self.claims + [{'id': 'C002', 'text': '另一条', 'context': ''}])
        with self.assertRaises(ValueError):
            report.validate(self.document, original)

    def test_report_reworded_original_is_rejected(self):
        self.document['claims'][0]['text'] = '偷偷改成正确说法'
        with self.assertRaises(ValueError):
            report.validate(self.document, self.original)

    def test_report_changed_date_is_rejected(self):
        self.document['as_of'] = '2025-09-21'
        with self.assertRaises(ValueError):
            report.validate(self.document, self.original)

    def test_context_retained_in_export(self):
        data = report.validate(self.document, self.original)
        self.assertIn('国家队；第1句', report.markdown(data))

    def test_excel_summary_does_not_silently_truncate(self):
        try:
            import openpyxl
        except ImportError:
            self.skipTest('optional openpyxl is unavailable')
        self.document['model_summary'] = 'x' * 32768
        with self.assertRaises(ValueError):
            report.excel(self.document, self.root / 'report.xlsx')


if __name__ == '__main__':
    unittest.main()
