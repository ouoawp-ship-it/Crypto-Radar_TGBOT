# OAR DeepSeek V4 Pro 运维说明

OAR 继续复用现有 OpenAI-compatible 客户端。DeepSeek 是一个受约束的 Provider Profile，不是第二套 AI 客户端；AI 默认关闭，规则摘要始终可以在没有 AI 时独立生成。

## 安全默认配置

```dotenv
OAR_AI_ENABLE=false
OAR_AI_PROVIDER=deepseek
OAR_AI_BASE_URL=https://api.deepseek.com
OAR_AI_API_KEY=
OAR_AI_MODEL=deepseek-v4-pro
OAR_AI_THINKING_MODE=enabled
OAR_AI_REASONING_EFFORT=high
OAR_AI_MAX_TOKENS=8192
OAR_AI_OPERATOR_PROMPT_FILE=data/onchain/config/oar_ai_operator_prompt.txt
OAR_AI_TIMEOUT_SEC=20
OAR_AI_MAX_RETRIES=1
```

DeepSeek Profile 只接受 `deepseek-v4-pro` 和 `deepseek-v4-flash`，默认推荐 `deepseek-v4-pro`。`OAR_AI_MAX_TOKENS` 范围为 512～32768，timeout 为 1～120 秒，重试为 0～3 次。远程 Base URL 必须使用 HTTPS；HTTP 仅允许本机回环地址。URL 不能包含 username、password、query 或 fragment。

可离线、原子应用六项推荐值：

```bash
python scripts/paopao_config.py profile deepseek-v4-pro
```

Profile 不设置 API Key、不启用 AI，也不执行 `/models` 或生成请求。输出只显示脱敏后的 configured 状态和安全枚举。

## Thinking 与 Reasoning Effort

`OAR_AI_THINKING_MODE=enabled` 时，请求包含：

```json
{
  "thinking": {"type": "enabled"},
  "reasoning_effort": "high",
  "max_tokens": 8192
}
```

该模式不发送 `temperature`。`disabled` 模式发送 `thinking.type=disabled` 和 `temperature=0`，不发送 `reasoning_effort`。两种模式都继续使用 `response_format={"type":"json_object"}`。

客户端只读取最终 `message.content`。Provider 返回的 `reasoning_content` 不写日志、数据库、Cache 或 Telegram，不返回给操作者，也不参与下一轮。

## 提示词分层

核心 System Prompt、九字段 JSON 输出契约、Restricted Input 和禁止项由代码管理，Operator Prompt 不能覆盖它们。

- 公共模板：`config/onchain/oar_ai_operator_prompt.default.txt`
- 私有运行文件：`data/onchain/config/oar_ai_operator_prompt.txt`
- 私有历史：`data/onchain/config/oar_ai_operator_prompt.history/`

第一次实际需要 AI 时，运行文件会从公共模板安装。运行文件和历史权限为 600，父目录不高于 700；文本必须为 UTF-8、不得包含 NUL，最长 12000 字符，最近保留 20 个版本。

公共模板包含数据质量、交易所流向、行为候选、钱包关联、市场关联信号、主/备假设、下一步链上动作、观察条件、失效条件、Bias、Confidence 和输出风格十二部分。已有私有运行文件不会因模板升级被自动覆盖；只有显式 `restore-default` 才应用新版模板。

离线检查不会显示完整私有提示词：

```bash
python onchain_main.py ai-prompt-check
python onchain_main.py ai-prompt status
python onchain_main.py ai-prompt validate
python onchain_main.py ai-prompt history
```

显式查看、安装、保存和恢复：

```bash
python onchain_main.py ai-prompt show
python onchain_main.py ai-prompt install-default
cat operator.txt | python onchain_main.py ai-prompt save --stdin
python onchain_main.py ai-prompt restore-default
python onchain_main.py ai-prompt rollback --version <版本或Hash前缀>
```

保存使用文件锁、原子替换和有界历史。菜单中的恢复操作还要求完整中文确认短语。

## Prompt Hash 与 Cache

AI Request Control 包含：

- `core_prompt_version`
- `operator_prompt_hash`
- `operator_prompt_present`
- `thinking_mode`
- `reasoning_effort`
- `restricted_input`

Cache 身份包含 Provider、model、核心 Prompt Version、Operator Prompt Hash、Context Hash、Thinking Mode、Reasoning Effort 和 Max Tokens。修改业务提示词、模型或思考配置会自动 miss；旧条目正常过期，不会被误用。

Cache 只保存验证后的九字段结果和非敏感身份字段，不保存 API Key、Authorization、完整 System Prompt、完整 Operator Prompt 或 `reasoning_content`。

Cache 结果和小时调用预算共用同一原子 JSON，但清理操作严格隔离：

```bash
python onchain_main.py ai-cache status
python onchain_main.py ai-cache clear-results
```

`status` 只显示文件状态、有效/过期条目数、最近一小时调用数和文件大小。`clear-results` 只清空结果条目，保留最近一小时的 `call_timestamps`，因此不能通过清理 Cache 绕过 `OAR_AI_MAX_CALLS_PER_HOUR`。

## Provider Check

以下命令需要显式联网授权：

```bash
python onchain_main.py ai-provider-check --allow-network
```

它只调用 OpenAI-compatible `GET /models`，验证鉴权和配置模型是否存在，不执行生成，不调用 Base RPC，不创建 TelegramGateway。输出只含 Provider、模型、状态和延迟，不显示 API Key 或完整 Base URL。

没有 `--allow-network` 时会以 `allow_network_required` 安全拒绝，网络调用为 0。

## AI Smoke

```bash
python onchain_main.py ai-smoke --allow-network
```

Smoke 使用固定、合成、脱敏且受限的 OAR Context，只执行一次生成，不调用 Base RPC 或 Telegram。返回只显示 status、model、latency 和 Schema 是否有效，不显示模型正文或思考内容。

## 真实启用顺序

1. 通过 `paopao` 菜单或 `scripts/paopao_config.py` 设置 Provider、HTTPS Base URL、API Key、模型与思考配置。
2. 运行 `ai-prompt-check` 并校验 Operator Prompt。
3. 运行 `ai-provider-check --allow-network`。
4. 在受控环境运行一次 `ai-smoke --allow-network`。
5. 最后才设置 `OAR_AI_ENABLE=true`。

启用 AI 不会自动启用 OAR Automation，也不会打开真实 Telegram。真实 Telegram 仍保留独立双命令门禁和环境门禁。

## 安全边界

- 不调用 Arkham，不新增 AI SDK，不新增数据库 Migration。
- AI 不能修改 OAR-P1/P2 链上事实或规则结果。
- 非法、缺字段、多字段或 Markdown JSON 继续由本地严格验证器拒绝。
- AI 超时、限流或非法输出不阻断确定性规则摘要。
- 普通测试只使用 Fake Session，不访问真实 DeepSeek、RPC 或 Telegram。
