from __future__ import annotations

# Telegram's command menu only supports top-level slash commands; subcommands stay in /help.
V2_BOT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "开始使用并显示帮助"),
    ("help", "查看所有可用命令"),
    ("status", "查看运行状态、模型与最近延迟"),
    ("provider", "查看或切换 LLM 与 Web Search 上游"),
    ("memory", "查看或开关长期记忆"),
    ("remember", "手动保存一条长期记忆"),
    ("forget", "删除匹配的长期记忆"),
    ("profile", "查看已保存的事实与偏好"),
    ("history", "查看最近对话"),
    ("autonomy", "管理主动消息与短期续话"),
    ("new", "开始新的对话上下文"),
    ("cancel", "取消正在生成的回复"),
)
