# 配置与脚本

首次使用优先按 [AI 引导流程](onboarding.md) 完成配置、密钥本地输入和连接测试。下面是脚本参数参考。

## 配置多模型

Python 3.9+。提取与并行调用只使用标准库；Excel 导出需要 `pip install -r requirements.txt`。

AI 可通过 `setup.py init` 生成配置，或复制 `config.example.json` 为 `config.json`；根据所选服务商官方资料确认并填入完整 HTTPS Chat Completions 地址、真实可用模型 ID 和对应的密钥环境变量名。默认文件中的 `YOUR_...` 是占位符，脚本会拒绝直接发送。网关可以同时路由多个模型家族；也可给每个条目配置不同兼容服务。使用 HTTPS，脚本拒绝重定向，避免凭证被转发到其他地址。

设定 `FOOTBALL_API_KEY`，或复制 `.env.example` 为 `.env` 并填值。默认自动加载所选 `config.json` 同目录中已存在的 `.env`；其他路径通过 `--env-file` 指定，不执行 shell 语句、不支持变量展开；进程环境变量优先。多个服务可各用一个环境变量。不要在 config.json 中写密钥，不向对话粘贴密钥。

```
python3 scripts/parallel_check.py examples/claims.json outputs/model-results.json --config config.json --env-file .env
```

配置字段：
- `models[]`: `name`（唯一显示名）、`model`（真实 ID）、`endpoint`（完整兼容接口 URL）、`api_key_env`、`enabled`；可选 `token_limit_parameter` 为 `max_tokens`（默认）或 `max_completion_tokens`，依据服务文档填写。
- `timeout_seconds`: 每次请求超时（默认 90）。
- `retries`: 429、5xx、网络错误最多重试次数（默认 1，最多 2）；401/403 等不重试。
- `batch_size`: 每批断言数（默认 8）。
- `max_workers`: 并发任务数（默认 4，最多 8）。
- `max_tokens`: 每次返回上限（默认 4096），通过该模型的 `token_limit_parameter` 传给兼容接口；其他不兼容协议仍需单独适配。

```
# 不发送请求，不需要密钥；检查配置、断言和预计请求数
python3 scripts/parallel_check.py examples/claims.json outputs/check.json --config config.json --dry-run
# 只调用指定模型（可重复 --model）
python3 scripts/parallel_check.py outputs/claims.json outputs/models.json --config config.json --model deepseek --model gpt
# 也可直接输入 UTF-8 文本（每非空行一个断言）
python3 scripts/parallel_check.py article.txt outputs/models.json --config config.json --as-of 2026-09-21
```

输入最多 200 条、共 60000 字符，单条最多 6000 字符；超过则明确报错，先由助手拆成多个任务，并在总报告记录覆盖范围。输出保留每模型每批状态和响应、实际返回模型名及用量（服务支持时）。内容截断、无效 JSON、缺失/重复 ID 均作为失败记录；不因部分失败丢弃其余模型结果。退出码：0=全部请求合格，2=配置/输入问题，3=部分/全部 API 审阅失败（已保存结果）。`--dry-run` 不证明接口可用。

## 导出

由宿主助手完成网页核验并按 methodology.md 创建 `report.json` 后：

```
python3 scripts/generate_report.py outputs/report.json --claims outputs/claims.json --markdown outputs/report.md
python3 scripts/generate_report.py outputs/report.json --claims outputs/claims.json --markdown outputs/report.md --xlsx outputs/report.xlsx
```

Excel 使用红/黄/绿/灰分类、冻结标题行、筛选及换行，模型意见与真实来源分列。文本作为单元格字符串写入，防止原文被当作 Excel 公式。脚本拒绝无证据却标为正确/错误/瑕疵的记录，并通过 `--claims` 核对原始ID、原文、日期，保留语境。它不会替代人工浏览判断。

## 能力边界

- 多模型脚本联网仅用于请求已配置的模型接口，不会自动搜索互联网。
- 完整事实核查依赖宿主助手可用的搜索/浏览器工具。纯命令行只能取得模型审阅，不能宣称完成证据核查。
- 不内置任何服务商密钥、专用公司网关或可用性承诺；示例模型名是用户需填写的占位符。
- 调用会把输入内容发送给配置中的服务，费用与数据处理规则由相应服务决定。共享仓库只包含源码、配置模板和虚构/公开测试数据。

协议参考：[DeepSeek Chat Completions 文档](https://api-docs.deepseek.com/api/create-chat-completion/)。具体网关支持哪些模型、限额及参数以其文档为准。

`setup.py status` 仅做本地检查；`setup.py key` 供用户在自己的终端隐藏输入密钥；`setup.py test` 每模型发送一条测试请求（可能计费），输出逐模型诊断。`test` 退出码为 0 时全部通过且至少两模型；3 表示部分失败/单模型/不可用；2 表示配置或文件错误。
