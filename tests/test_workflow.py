import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import extract_text as extract
import parallel_check as check
import generate_report as report


def response(claims=None, finish='stop', **kwargs):
    if claims is None:
        claims = [{'id': 'C001', 'assessment': 'uncertain', 'reason': '需要来源'}]
    value = {'model': 'returned-model', 'choices': [{'finish_reason': finish, 'message': {'content': json.dumps({'claims': claims})}}]}
    value.update(kwargs)
    return io.BytesIO(json.dumps(value).encode())


class FakeClient:
    def __init__(self, items):
        self.items = iter(items)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        value = next(self.items)
        if isinstance(value, Exception):
            raise value
        return value


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.model = {'name': 'test', 'model': 'test-model', 'endpoint': 'https://example.com/v1/chat/completions', 'api_key_env': 'FACTCHECK_TEST_KEY'}
        self.config = {'models': [self.model], 'timeout_seconds': 1, 'max_tokens': 512, 'retries': 1, 'batch_size': 1, 'max_workers': 2}
        self.claims = [{'id': 'C001', 'text': '待核查', 'context': ''}]
        self.environment = patch.dict(os.environ, {'FACTCHECK_TEST_KEY': 'test-only-not-a-real-credential'})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write(self, name, data):
        path = self.path / name
        path.write_text(json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data)
        return path

    def test_extract_inline_entities_skip_and_alt(self):
        text = extract.extract('<h1>梅<span>西</span></h1><p>A &amp; B</p><script>secret</script><style>css</style><img alt="球员介绍">')
        self.assertIn('梅西', text)
        self.assertIn('A & B', text)
        self.assertIn('球员介绍', text)
        self.assertNotIn('secret', text)
        self.assertNotIn('css', text)

    def test_claim_ids_and_date_preserved(self):
        data = check.read_claims(self.write('in.json', {'as_of': '2024-01-01', 'claims': self.claims}))
        self.assertEqual(data['as_of'], '2024-01-01')
        self.assertEqual(data['claims'], self.claims)

    def test_text_input_ignores_blank_lines(self):
        data = check.read_claims(self.write('in.txt', '第一条\n\n第二条'), '2024-01-01')
        self.assertEqual([c['id'] for c in data['claims']], ['C001', 'C002'])

    def test_duplicate_ids_rejected(self):
        with self.assertRaises(ValueError):
            check.read_claims(self.write('in.json', {'claims': self.claims * 2}))

    def test_overlong_input_rejected_not_truncated(self):
        with self.assertRaises(ValueError):
            check.read_claims(self.write('in.txt', '字' * 6001))

    def test_template_rejected_before_network(self):
        with self.assertRaises(ValueError):
            check.read_config(ROOT / 'config.example.json')

    def test_http_and_credentials_in_url_rejected(self):
        for endpoint in ['http://example.com/v1', 'https://user:pass@example.com/v1', 'https://example.com/v1?key=abc']:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                check.read_config(self.write('config.json', {'models': [dict(self.model, endpoint=endpoint)]}))

    def test_duplicate_models_rejected(self):
        with self.assertRaises(ValueError):
            check.read_config(self.write('config.json', {'models': [self.model, dict(self.model, name='other')]}))

    def test_missing_key_returns_failure(self):
        with patch.dict(os.environ, {}, clear=True):
            result = check.review_batch(self.model, self.claims, '2024-01-01', self.config, FakeClient([]))
        self.assertEqual(result['error'], 'missing_api_key')
        self.assertEqual(result['attempts'], 0)

    def test_valid_response_preserves_requested_and_returned_models(self):
        client = FakeClient([response()])
        result = check.review_batch(self.model, self.claims, '2024-01-01', self.config, client)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['returned_model'], 'returned-model')
        sent = json.loads(client.requests[0].data)
        self.assertEqual(json.loads(sent['messages'][1]['content'])['claims'], self.claims)

    def test_missing_or_extra_claims_fail(self):
        for claims in [[], [{'id': 'C002', 'assessment': 'uncertain', 'reason': 'x'}]]:
            with self.subTest(claims=claims):
                result = check.review_batch(self.model, self.claims, '2024-01-01', self.config, FakeClient([response(claims)]))
                self.assertEqual(result['status'], 'failed')

    def test_truncated_response_is_failure_even_with_valid_json(self):
        result = check.review_batch(self.model, self.claims, '2024-01-01', self.config, FakeClient([response(finish='length')]))
        self.assertEqual(result['error'], 'incomplete_response')

    def test_invalid_json_is_failure(self):
        client = FakeClient([io.BytesIO(b'{bad response')])
        result = check.review_batch(self.model, self.claims, '2024-01-01', self.config, client)
        self.assertEqual(result['error'], 'invalid_response_or_claim_coverage')

    def test_auth_failure_not_retried_or_leaked(self):
        error = urllib.error.HTTPError('https://example.com', 401, 'secret upstream', {}, io.BytesIO(b'private'))
        client = FakeClient([error])
        result = check.review_batch(self.model, self.claims, '2024-01-01', self.config, client)
        self.assertEqual(result['error'], 'http_401')
        self.assertEqual(len(client.requests), 1)
        self.assertNotIn('private', json.dumps(result))

    def test_transient_failure_retried(self):
        error = urllib.error.HTTPError('https://example.com', 429, 'rate limit', {}, io.BytesIO())
        result = check.review_batch(self.model, self.claims, '2024-01-01', self.config, FakeClient([error, response()]), sleeper=lambda _: None)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['attempts'], 2)

    def test_retries_are_bounded(self):
        result = check.review_batch(self.model, self.claims, '2024-01-01', self.config, FakeClient([TimeoutError(), TimeoutError()]), sleeper=lambda _: None)
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(result['error'], 'network_error')

    def test_redirect_blocked(self):
        self.assertIsNone(check.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example'))

    def test_secret_echo_is_redacted(self):
        value = [{'id': 'C001', 'assessment': 'uncertain', 'reason': os.environ['FACTCHECK_TEST_KEY']}]
        result = check.review_batch(self.model, self.claims, '2024-01-01', self.config, FakeClient([response(value)]))
        self.assertNotIn(os.environ['FACTCHECK_TEST_KEY'], json.dumps(result))

    def test_partial_failure_does_not_drop_other_models(self):
        config = dict(self.config, models=[self.model, dict(self.model, name='second', model='second-model')])
        claims = self.claims + [{'id': 'C002', 'text': '另一条', 'context': ''}]
        def fake(model, batch, as_of, config):
            return {'name': model['name'], 'status': 'failed' if model['name'] == 'second' else 'ok', 'claim_ids': [batch[0]['id']]}
        with patch.object(check, 'review_batch', side_effect=fake):
            result = check.run_reviews({'as_of': '2024-01-01', 'claims': claims}, config)
        self.assertEqual(result['models_completed'], ['test'])
        self.assertEqual(result['total_requests'], 4)
        self.assertEqual(result['successful_requests'], 2)
        self.assertFalse(result['cross_review_completed'])
        self.assertEqual(len(result['results']), 4)

    def test_env_does_not_override_existing_or_execute_shell(self):
        check.load_env(self.write('.env', 'FACTCHECK_TEST_KEY=changed\nFACTCHECK_LITERAL="$(echo surprise)"\n'))
        self.assertEqual(os.environ['FACTCHECK_TEST_KEY'], 'test-only-not-a-real-credential')
        self.assertEqual(os.environ['FACTCHECK_LITERAL'], '$(echo surprise)')

    def sample_report(self):
        return {'title': '测试报告', 'checked_at': '2026-09-21', 'as_of': '2026-09-21', 'model_summary': '未调用真实接口',
                'claims': [{'id': 'C001', 'text': '=1+1', 'status': '证据不足', 'reason': '测试 | <script>', 'sources': []}]}

    def test_unverified_cannot_be_green_red_or_yellow(self):
        for status in ('正确', '错误', '瑕疵'):
            with self.subTest(status=status), self.assertRaises(ValueError):
                data = self.sample_report()
                data['claims'][0]['status'] = status
                report.validate(data)

    def test_markdown_escapes_user_content(self):
        text = report.markdown(report.validate(self.sample_report()))
        self.assertIn('&#124;', text)
        self.assertIn('&lt;script&gt;', text)

    @unittest.skipUnless(importlib.util.find_spec('openpyxl'), 'optional openpyxl is not installed')
    def test_excel_text_not_formula_and_classification_color(self):
        from openpyxl import load_workbook
        output = self.path / 'report.xlsx'
        report.excel(report.validate(self.sample_report()), output)
        book = load_workbook(output)
        sheet = book['逐项核查']
        self.assertEqual(sheet['B2'].value, '=1+1')
        self.assertEqual(sheet['B2'].data_type, 's')
        self.assertEqual(sheet['D2'].fill.fgColor.rgb, '00E7E6E6')
        self.assertEqual(sheet.freeze_panes, 'A2')
        book.close()


if __name__ == '__main__':
    unittest.main()
