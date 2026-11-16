# 跨项目片段桥（project-bridge）

在当前 Codex 会话中，按需引用另一个项目的聊天片段、结论和本地源码，保留来源，不导出整段历史。

Bring selected chat excerpts, conclusions, and local source code from another project into the current Codex session, with provenance and without exporting the full history.

仓库：[project-bridge](https://github.com/lazybones11/project-bridge)。项目包含一个 Codex 技能和一个 Python 只读助手。

## 适用环境与兼容性

本地后备读取依赖 Codex 桌面版未公开的记录格式：`state_*.sqlite` 的 `threads` 表、rollout JSONL，以及用于后备查询的 `session_index.jsonl`。这些格式可能随 Codex 更新改变。没有可用数据源或读取时遇到不兼容结构，会报告 `status: error` 并以退出码 2 结束；工具不会伪造读取结果。

已验证环境：Windows、Python 3.13，Codex 桌面版 2026 年 10 月使用的记录格式。当前版本的回归测试使用合成记录；未验证 Linux、macOS、其他 Codex 版本或远程聊天。Python 助手只使用标准库，无需通过 pip 安装依赖。

## 安装

将本仓库中的 `project-bridge` 文件夹复制到 Codex 技能目录（默认 `~/.codex/skills/`）。确认入口是 `<Codex 技能目录>/project-bridge/SKILL.md`。已有同名安装时先备份。

## 快速开始

在本项目根目录打开 PowerShell：

```powershell
python -X utf8 -m unittest discover -s .\tests -v
python -X utf8 .\project-bridge\scripts\bridge.py --help
python -X utf8 .\project-bridge\scripts\bridge.py threads --query '跨项目' --limit 5
```

返回 `no_match` 表示没有匹配结果；`error` 和退出码 2 表示需要处理错误。真实聊天内容、片段包、缓存和本机备份不进入 Git。

## 在 Codex 中使用

在能发现该技能的聊天中说：

> 用 $project-bridge，从“目标聊天标题”里，只提取关于“目标关键词”的片段，保留来源。

只有明确要求发送任务时，才使用宿主提供的聊天协作工具。读取或保存片段不会自动唤醒另一个聊天。

更多例子见 [使用方法](使用方法.md)，数据流和排障见 [项目说明](项目说明.md)。

## 当前边界

- 优先使用宿主的项目和聊天工具；本地记录解析是兼容性后备方式，可能随 Codex 格式变化而需要维护。
- 只提取选定来源的可见用户与助手文本，不导出隐藏推理、系统消息或整段历史。
- 源码引用包含路径、行号、内容哈希，以及可获取的 Git 信息；这些信息不保证内容结论正确。
- 本地路径与本地记录不能代表远程、云端、其他账号或其他主机的可访问状态。
- 当前不新增侧栏原生按钮，也没有常驻服务。

WorkBuddy 适配、跨工具自动派任务和插件市场发布尚未实现。

## 许可证

本项目以 [MIT 许可证](LICENSE)开源。
