# 本地 Codex 记录格式

这是 [读取器](../project-bridge/scripts/bridge.py) 实际使用的字段和搜索规则，不是 OpenAI 的公开规范。宿主的聊天读取工具可用时，应优先用宿主工具。

## 聊天目录

- 默认使用 `~/.codex`；可以通过 `CODEX_HOME` 或全局参数 `--codex-home` 指定目录。
- SQLite：只选文件名完整匹配 `state_<数字>.sqlite` 的普通文件，取编号最大者，只读打开。`threads` 表需要 `id`、`title`、`cwd`、`updated_at`、`archived`、`rollout_path`；可选的非空 `name` 优先作为显示标题。`threads` 命令的结果不返回内部 `rollout_path`。
- JSONL 索引：没有匹配数据库，或最新数据库没有 `threads` 表时，回退到 `session_index.jsonl`。使用 `id`、`thread_name`、`updated_at`；标题和时间缺失或非字符串时按空字符串处理。这个索引没有 `cwd`，不能按项目目录筛选。表存在但缺必需列、数据库损坏等情况不会静默用索引代替。

## 单个聊天正文

- 传入完整 UUID。优先从数据库获取记录路径；没有可用路径时，在 `sessions/` 和 `archived_sessions/` 查找文件名以该 UUID 加 `.jsonl` 结尾的记录。
- 解析后的路径必须在所选 Codex 目录以内。首个可解析对象必须是 `session_meta`，其 `payload.id` 或缺省时的 `payload.session_id` 要与所请求 UUID 一致；依据 `payload.cwd` 可补充来源。路径越界或身份不匹配的文件不会作为已核实来源。
- 首选 `type=response_item` 且 `payload.type=message` 的记录；只取 `payload.role` 为 `user` 或 `assistant` 的消息。若 `payload.channel` 或 `payload.phase` 为 `analysis`，跳过。正文来自 `payload.content` 中 `input_text`、`output_text` 和 `text` 类型块的字符串 `text`；保留顶层 `timestamp`、原始行号和消息 ID，并按 ID 去重。
- 旧记录：同一角色没有首选格式时，回退到 `type=event_msg` 中 `payload.type` 为 `user_message` / `agent_message` 的 `payload.message` 字符串。
- 系统/开发者消息、工具输出和推理记录不作为正文。移除可见文本里的 `environment_context` 和 `external_codex_apps_open_page` 成对块。输出不会自动把可见文本里的个人信息或凭据替换掉。

## 结果及兼容性

无关键词命中返回 `no_match`，不改读不相关的最近消息。读取聊天时，无法解析的 JSONL 行会跳过并记录到 `diagnostics.skipped_jsonl_records`；这只能代表局部快照。缺少可用数据源、表结构不兼容、聊天身份无法确认或未读到支持的可见消息时，CLI 返回 JSON `error` 和退出码 2。

历史记录格式曾在 2026 年 10 月的 Windows Codex 桌面环境验证，具体应用版本号未记录。本版于 2026-11-16 在 Windows / Python 3.13 上通过合成数据回归；未用本轮测试重新读取真实聊天，也未验证 Linux/macOS 或 Python 3.9 运行结果。
