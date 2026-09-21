import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('football_setup', ROOT / 'scripts/setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'config.json'
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def create(self, models=None):
        setup.initialize(self.path, 'https://example.com/v1/chat/completions', models or ['a=first-model', 'b=second-model'], 'TEST_KEY')

    def test_fresh_install_requires_configuration(self):
        self.assertEqual(setup.inspect_config(self.path)['state'], 'needs_configuration')

    def test_initialization_creates_valid_nonsecret_config(self):
        self.create()
        self.assertEqual(len(setup.check.read_config(self.path)['models']), 2)
        self.assertFalse((self.root / '.env').exists())
        self.assertEqual(setup.inspect_config(self.path)['state'], 'needs_credentials')

    def test_initialization_preserves_existing_config(self):
        self.create()
        original = self.path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.create(['other=other-model'])
        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_configuration_does_not_create_file(self):
        with self.assertRaises(ValueError):
            setup.initialize(self.path, 'http://example.com/v1', ['a=first-model'], 'TEST_KEY')
        self.assertFalse(self.path.exists())

    def test_status_reports_presence_not_secret(self):
        self.create()
        os.environ['TEST_KEY'] = 'private-test-value'
        result = setup.inspect_config(self.path)
        self.assertEqual(result['state'], 'ready_to_test')
        self.assertNotIn('private-test-value', json.dumps(result))
        self.assertFalse(result['network_tested'])

    def test_single_model_not_claimed_as_cross_review(self):
        self.create(['one=one-model'])
        os.environ['TEST_KEY'] = 'test-value'
        self.assertEqual(setup.inspect_config(self.path)['state'], 'single_model_only')

    def test_local_key_preserves_other_entries_and_permissions(self):
        path = self.root / '.env'
        path.write_text('# retain comment\nOTHER_KEY=old-value\nTEST_KEY=before\nTEST_KEY=duplicate\n')
        setup.save_key(path, 'TEST_KEY', 'after-value')
        self.assertEqual(path.read_text().count('TEST_KEY='), 1)
        self.assertIn('OTHER_KEY=old-value', path.read_text())
        self.assertIn('# retain comment', path.read_text())
        if os.name != 'nt':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        setup.check.load_env(path)
        self.assertEqual(os.environ['TEST_KEY'], 'after-value')

    def test_key_rejects_multiline_and_preserves_file(self):
        path = self.root / '.env'
        path.write_text('OTHER=unchanged\n')
        with self.assertRaises(ValueError):
            setup.save_key(path, 'TEST_KEY', 'bad\nINJECT=value')
        self.assertEqual(path.read_text(), 'OTHER=unchanged\n')

    def test_key_rejects_symlink(self):
        target = self.root / 'target'
        target.write_text('unchanged')
        link = self.root / '.env'
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest('symlink unavailable')
        with self.assertRaises(ValueError):
            setup.save_key(link, 'TEST_KEY', 'new-value')
        self.assertEqual(target.read_text(), 'unchanged')

    def test_environment_takes_precedence_over_saved_key(self):
        self.create()
        local = self.root / '.env'
        setup.save_key(local, 'TEST_KEY', 'local-value')
        os.environ['TEST_KEY'] = 'environment-value'
        setup.inspect_config(self.path, local)
        self.assertEqual(os.environ['TEST_KEY'], 'environment-value')

    def test_probe_uses_synthetic_content_and_no_retries(self):
        self.create()
        config = setup.check.read_config(self.path)
        results = {'models_completed': ['a'], 'results': [
            {'name': 'a', 'requested_model': 'first-model', 'status': 'ok', 'raw_response': 'not for display'},
            {'name': 'b', 'requested_model': 'second-model', 'status': 'failed', 'error': 'http_401'}]}
        with patch.object(setup.check, 'run_reviews', return_value=results) as run:
            value = setup.test_connections(config)
        self.assertEqual(run.call_args.args[1]['retries'], 0)
        self.assertEqual(len(run.call_args.args[0]['claims']), 1)
        self.assertEqual(value['state'], 'single_model_only')
        self.assertFalse(value['cross_review_available'])
        self.assertNotIn('not for display', json.dumps(value))
        self.assertIn('Key', value['results'][1]['next_step'])

    def test_probe_partial_and_full_success_states(self):
        self.create(['a=first', 'b=second', 'c=third'])
        config = setup.check.read_config(self.path)
        for passed, expected in [(0, 'not_ready'), (2, 'partial'), (3, 'ready')]:
            results = {'models_completed': ['a', 'b', 'c'][:passed], 'results': []}
            with self.subTest(passed=passed), patch.object(setup.check, 'run_reviews', return_value=results):
                self.assertEqual(setup.test_connections(config)['state'], expected)

    def test_cli_rejects_piped_credentials_without_saving(self):
        path = self.root / '.env'
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/setup.py'), 'key', '--env-file', str(path)],
                                input='private-value', capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('private-value', result.stderr + result.stdout)
        self.assertFalse(path.exists())

    def test_review_reuses_default_config_and_env(self):
        self.create()
        setup.save_key(self.root / '.env', 'TEST_KEY', 'saved-local-test-value')
        claims = self.root / 'claims.json'
        claims.write_text(json.dumps({'claims': [{'id': 'C001', 'text': '测试'}]}))
        output = self.root / 'result.json'
        script_path = self.root / 'scripts' / 'parallel_check.py'
        seen = []
        def fake_run(payload, config):
            seen.append(os.environ.get('TEST_KEY'))
            return {'successful_requests': 2, 'total_requests': 2, 'models_completed': ['a', 'b'], 'models_requested': 2}
        import io
        with patch.object(setup.check, '__file__', str(script_path)), patch.object(sys, 'argv', ['parallel_check.py', str(claims), str(output)]), patch.object(setup.check, 'run_reviews', side_effect=fake_run), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(setup.check.main(), 0)
        self.assertEqual(seen, ['saved-local-test-value'])
        self.assertTrue(output.exists())

    def test_cli_missing_credentials_no_network(self):
        self.create()
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/setup.py'), 'test', '--config', str(self.path)],
                                capture_output=True, text=True, env=dict(os.environ))
        self.assertEqual(result.returncode, 3)
        data = json.loads(result.stdout)
        self.assertEqual(data['state'], 'not_ready')
        self.assertTrue(all(r['error'] == 'missing_api_key' for r in data['results']))


if __name__ == '__main__':
    unittest.main()
