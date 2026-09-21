#!/usr/bin/env python3
"""Inspect setup, create a non-secret configuration, and test configured models."""
import argparse
import getpass
import warnings
import re
import sys
from datetime import date
import json
import os
from pathlib import Path
import tempfile

import parallel_check as check

ROOT = Path(__file__).resolve().parents[1]
HINTS = {
    'missing_api_key': '请在本地环境变量或 .env 中填写对应 Key，再测试。不要把 Key 发到对话里。',
    'http_400': '请求参数可能不兼容。核对服务商文档、模型 ID 和 max_tokens 参数。',
    'http_401': '认证失败。检查 Key 是否有效、是否属于此服务，以及环境变量是否覆盖了 .env。',
    'http_403': '权限不足。检查模型访问权限、账户或地区限制。',
    'http_404': '接口或模型可能不存在。核对完整 Chat Completions 地址和实际模型 ID。',
    'http_429': '可能触发限流或额度不足。检查账户额度，稍后再试，勿循环重试。',
    'unexpected_request_error': '请求出现未预期错误；其他模型结果已保留。请检查服务协议或联系维护者，不提供密钥。',
    'network_error': '网络、DNS、TLS 或超时问题。核对网络和地址；不要关闭证书验证。',
    'invalid_response_or_claim_coverage': '接口返回格式不符合审阅要求。检查协议、模型 JSON 输出能力与断言覆盖。',
    'incomplete_response': '回答被截断或未正常完成。核对输出上限及模型能力后再试。',
    'response_too_large': '返回过大，检查接口是否返回了预期的模型响应。',
}


def hint(error):
    if error.startswith('http_5'):
        return '服务端暂时故障；稍后重试或改用用户已配置的其他模型。'
    if error.startswith('http_3'):
        return '接口发生重定向，已阻止凭证转发。请从官方文档确认最终接口地址。'
    return HINTS.get(error, '检查服务商文档和本地配置。')


def env_path(explicit, config_path=None):
    return check.resolve_env(config_path or ROOT / 'config.json', explicit)


def inspect_config(path, env_file=None):
    if not path.exists():
        return {'state': 'needs_configuration', 'message': '尚未配置服务商和模型。先询问用户使用的服务，再核实官方接口文档。'}
    config = check.read_config(path)
    check.load_env(env_file)
    models = [{'name': m['name'], 'model': m['model'], 'endpoint': m['endpoint'],
               'key_env': m['api_key_env'], 'key_present': bool(os.environ.get(m['api_key_env'], '').strip())}
              for m in config['models']]
    missing = sorted({m['key_env'] for m in models if not m['key_present']})
    state = 'needs_credentials' if missing else ('single_model_only' if len(models) < 2 else 'ready_to_test')
    return {'state': state, 'models': models, 'missing_key_envs': missing,
            'network_tested': False, 'message': '配置检查不代表接口可用；连接测试通过也不代表事实正确。'}


def initialize(path, endpoint, entries, key_env):
    models = []
    for entry in entries:
        name, sep, model = entry.partition('=')
        if not sep or not name.strip() or not model.strip():
            raise ValueError('模型使用 名称=实际模型ID 格式')
        models.append({'name': name.strip(), 'model': model.strip(), 'endpoint': endpoint,
                       'api_key_env': key_env, 'enabled': True})
    config = {'timeout_seconds': 90, 'retries': 1, 'batch_size': 8, 'max_workers': 4, 'max_tokens': 4096, 'models': models}
    # Validate before creating the user's configuration. Never overwrite existing settings.
    with tempfile.TemporaryDirectory() as temp:
        staged = Path(temp) / 'config.json'
        staged.write_text(json.dumps(config), encoding='utf-8')
        check.read_config(staged)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as output:
        output.write(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    return {'state': 'configuration_created', 'models': len(models), 'message': '已生成无密钥配置。下一步由用户在本地填写 Key。'}


def test_connections(config):
    # One harmless synthetic claim per model. No real user document is transmitted.
    settings = dict(config, retries=0)
    result = check.run_reviews({'as_of': date.today().isoformat(), 'claims': [
        {'id': 'SETUP001', 'text': '某球员下一场比赛一定进球。', 'context': '连接测试；这是预测性断言，仅测试响应格式。'}]}, settings)
    statuses = []
    for r in result['results']:
        item = {'name': r['name'], 'requested_model': r['requested_model'], 'status': r['status']}
        if r['status'] == 'ok':
            item['message'] = '接口响应和断言格式检查通过；不代表事实准确率。'
        else:
            item['error'] = r.get('error', 'unknown_error')
            item['next_step'] = hint(item['error'])
        statuses.append(item)
    count = len(result['models_completed'])
    independent_count, warnings = check.independent_models(config, result['results'], result['models_completed'])
    state = 'ready' if count == len(config['models']) and independent_count >= 2 else ('partial' if independent_count >= 2 else ('single_model_only' if independent_count == 1 else 'not_ready'))
    return {'state': state, 'models_passed': count, 'models_requested': len(config['models']),
            'independent_models_passed': independent_count, 'model_identity_warnings': warnings,
            'cross_review_available': independent_count >= 2, 'results': statuses}



def save_key(path, name, value):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
        raise ValueError('无效变量名')
    if not value or any(c.isspace() for c in value) or value[0] in "\"'":
        raise ValueError('Key必须是非空且不含空白的单行值')
    if path.is_symlink():
        raise ValueError('密钥文件不能是符号链接')
    lines = path.read_text(encoding='utf-8-sig').splitlines() if path.exists() else []
    kept = [line for line in lines if line.partition('=')[0].strip() != name]
    kept.append(name + '=' + value)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.credential-', dir=str(path.parent))
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write('\n'.join(kept) + '\n')
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for action in ('status', 'init', 'test'):
        command = sub.add_parser(action)
        command.add_argument('--config', type=Path, default=ROOT / 'config.json')
        if action == 'init':
            command.add_argument('--endpoint', required=True)
            command.add_argument('--model', action='append', required=True, help='名称=实际模型ID，可重复')
            command.add_argument('--key-env', default='FOOTBALL_API_KEY')
        else:
            command.add_argument('--env-file', type=Path)
    key_command = sub.add_parser('key', help='由用户在自己的交互终端隐藏输入Key')
    key_command.add_argument('--name', default='FOOTBALL_API_KEY')
    key_command.add_argument('--env-file', type=Path, default=ROOT / '.env')
    args = parser.parse_args()
    try:
        if args.command == 'key':
            if not sys.stdin.isatty():
                parser.exit(2, '请由用户在自己的交互终端运行此命令，或在本地编辑 .env；不接受管道或命令参数传入Key。\n')
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                secret = getpass.getpass('输入 API Key（不会显示）：')
            save_key(args.env_file, args.name, secret)
            result = {'state': 'credential_saved', 'message': '已保存到本地文件；未显示 Key。现有环境变量仍优先，请运行 status/test。'}
            code = 0
        elif args.command == 'init':
            result = initialize(args.config, args.endpoint, args.model, args.key_env)
            code = 0
        elif args.command == 'status':
            result = inspect_config(args.config, env_path(args.env_file, args.config))
            code = 0
        else:
            config = check.read_config(args.config)
            check.load_env(env_path(args.env_file, args.config))
            result = test_connections(config)
            code = 0 if result['state'] == 'ready' else 3
        # No response bodies, credentials or raw upstream messages are printed.
        text = json.dumps(result, ensure_ascii=False, indent=2)
        for m in (config['models'] if args.command == 'test' else []):
            secret = os.environ.get(m['api_key_env'], '').strip()
            if secret:
                text = text.replace(json.dumps(secret, ensure_ascii=False)[1:-1], '[REDACTED]')
        print(text)
        return code
    except (getpass.GetPassWarning, EOFError):
        parser.exit(2, '此终端不支持隐藏输入，未读取/保存Key。请改用本地编辑器填写 .env。\n')
    except FileExistsError:
        parser.exit(2, '配置已存在，未覆盖。请先读取现有非密钥配置，只修改用户要求的部分。\n')
    except (ValueError, TypeError, OSError):
        parser.exit(2, '配置或环境文件无效/不可读取。检查JSON结构、HTTPS接口、真实模型ID及文件路径；不要打印密钥文件。\n')


if __name__ == '__main__':
    raise SystemExit(main())
