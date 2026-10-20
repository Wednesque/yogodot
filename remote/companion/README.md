# Coding 伴侣适配器

把 Claude Code / Codex / TRAE / OpenCode 的任务状态接到 YogoDot 的 **Coding 伴侣** 模式。
点阵屏上 **形状认工具，颜色认状态**；键盘保持当前主题，只有 6×6 小屏动。

```
Claude Code ─┐
Codex ───────┼─ 原生 hook ─→ companion-hook.py ─→ POST /events ─→ YogoDot
TRAE ────────┘                                      (127.0.0.1:8787)
OpenCode ───── 原生插件 ────────────────────────────┘
```

## 文件

| 文件 | 用途 |
|---|---|
| `remote/companion-hook.py` | Claude / Codex / TRAE 共用的事件翻译器 |
| `remote/companion-hook.cmd` | Windows 包装器（自动找 `py -3` 或 `python`） |
| `remote/companion-hook.sh` | macOS / Linux 包装器 |
| `remote/opencode-companion.js` | OpenCode 原生插件 |
| `remote/companion/claude-settings.json` | Claude Code 用户级 hooks 模板 |
| `remote/companion/codex-hooks.json` | Codex 用户级 hooks 模板 |
| `remote/companion/trae-hooks.json` | TRAE 用户级 hooks 模板 |

## 先决条件

1. YogoDot 正在运行（本地服务默认 `127.0.0.1:8787`）。
2. 控制台里选择 **Coding 伴侣** 模式。
3. 本机有 Python 3。`companion-hook.cmd` 会优先用 `py -3`，没有再退回 `python`。
4. 模板里的路径假设项目在 `D:/Projects/yogodot`。项目挪了位置就把模板里的
   `D:/Projects/yogodot` 替换成实际路径；路径含空格时给整个 `.cmd` 路径加双引号。

## 安装

### Claude Code

把 `remote/companion/claude-settings.json` 里的 `hooks` **增量合并**到
`%USERPROFILE%\.claude\settings.json`，不要覆盖整个文件。保存后重启 Claude Code，
在 `/hooks` 里确认来源为 `User` 的条目。

包含的事件：`SessionStart` / `SessionEnd` / `UserPromptSubmit` / `PermissionRequest` /
`PermissionDenied` / `PreToolUse` / `PostToolUse` / `PostToolUseFailure` / `Stop` /
`StopFailure` / `Notification`。

### Codex

把 `remote/companion/codex-hooks.json` 合并到 `%USERPROFILE%\.codex\hooks.json`。
Codex 的命令信任哈希会变化，重启后在 `/hooks` 里重新信任新增条目。

包含的事件：`UserPromptSubmit` / `PermissionRequest` / `PreToolUse` / `PostToolUse` / `Stop`。
Codex 没有 `SessionEnd`，会话靠手动「清空会话」或 15 分钟空闲兜底收起。

### TRAE

把 `remote/companion/trae-hooks.json` 合并到 `%USERPROFILE%\.trae-cn\hooks.json`。
在 TRAE 的 **Agent Hooks** 页面启用用户级配置，运行方式选 **本地自动运行**，然后重启或
重新载入 TRAE。

包含的事件：`SessionStart` / `UserPromptSubmit` / `PreToolUse` / `PostToolUse` /
`Stop` / `Notification`。`Notification(permission_prompt)` 进入「等你授权」，
`Notification(ask_user_question / document_review / browser_interaction)` 进入「等你补充」，
`Notification(idle_prompt)` 和 `Stop` 都算本轮完成（服务端会去重）。

### OpenCode

复制 `remote/opencode-companion.js` 到下面**其中一个**位置，然后重启 OpenCode：

- 用户级：`%USERPROFILE%\.config\opencode\plugins\yogodot-companion.js`
- 项目级：`<PROJECT>\.opencode\plugins\yogodot-companion.js`

不要同时装用户级和项目级两份，否则同一个事件会上报两次。

插件监听 `session.created` / `session.deleted` / `message.updated`（user）/
`permission.asked` / `permission.replied` / `question.asked` / `question.replied` /
`session.idle` / `session.error`，并在 `tool.execute.before` 时精确解除对应授权等待。

## 测试

先不连服务，只看翻译结果：

```bash
echo '{"session_id":"demo","cwd":"D:/Projects/yogodot","hook_event_name":"UserPromptSubmit"}' | python remote/companion-hook.py --source claude --dry-run
```

正常会输出一段 `schema=yogo.companion.v1` 的 JSON，其中不应出现 prompt、模型回答或
工具输入输出。再在控制台的 Coding 伴侣页发一条消息，会话会出现在列表里。

服务端也可以直接看：

```bash
curl http://127.0.0.1:8787/snapshot
```

## 原生事件 → 标准事件

| 原生事件 | 标准事件 | 小屏状态 |
|---|---|---|
| `SessionStart` | `session_started` | 白色空闲 |
| `SessionEnd` | `session_ended` | 立即移除 |
| `UserPromptSubmit` | `user_message_submitted` | 蓝色执行中 |
| `PermissionRequest` / `Notification(permission_prompt)` | `approval_requested` | 琥珀等你 |
| `Notification(elicitation_dialog / question / ask_user_question / document_review / browser_interaction)` | `supplement_requested` | 紫色补充 |
| `PermissionDenied` / `PermissionResolved` / `PreToolUse` / `PostToolUse` | `processing_resumed` | 回到蓝色执行中 |
| `PostToolUseFailure` | `processing_resumed` | 单次工具失败，不算整轮失败 |
| `Stop` / `Notification(idle_prompt)` | `turn_completed` | 绿色完成 |
| `StopFailure` | `turn_completed` + `outcome=failed` | 红色出错 |
| `SubagentStart` / `SubagentStop` | 忽略 | 不改变主任务 |

## 状态与隐私

- 适配器在 `~/.yogo/companion/`（Windows 是 `C:\Users\<你>\.yogo\companion`）保存每个会话的
  turn 计数和待处理 requestId；删掉对应文件即可重置。
- 只读取白名单元数据：事件名、session/turn/request 身份、cwd、项目名、工具名、通知类型。
- **不提取、不发送** `prompt`、`tool_input`、`tool_response`、`last_assistant_message`、消息正文、
  `additionalContext`。`data.message` 是适配器生成的短状态，不是原文。
- 只向 `127.0.0.1` / `localhost` / `::1` 发 HTTP。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `YOGO_PORT` | `8787` | YogoDot 本地服务端口 |
| `YOGO_COMPANION_URL` | `http://127.0.0.1:<port>/events` | 完整事件地址（仍限 loopback） |
| `YOGO_STATE_DIR` | `~/.yogo/companion` | 适配器状态目录 |
| `YOGO_COMPANION_HOST` | 空 | 可选，写入 `data.host`，区分多台机器 |

## 排查

- **控制台没有会话**：确认 YogoDot 在运行、选的是 Coding 伴侣模式；在工具里重新触发一次
  消息；检查 `/hooks` 是否已信任；用 `--dry-run` 确认命令能拿到 stdin。
- **一直停在「等你」**：某些原生通知没有稳定的 requestId。下一次用户提交、`Stop` 或手动
  「清空会话」会清掉；Adapter 不会凭文本猜授权结果。
- **重复出现两条**：同时装了两份 OpenCode 插件，或同一份 hooks 同时存在于用户级和项目级。
- **Hook 拖慢工具**：适配器对 HTTP 只等 0.35 秒并重试一次，失败就静默退出；正常情况下
  本机服务是毫秒级。
- **路径含空格**：把模板里的命令改成带双引号的完整路径，例如
  `"C:/Program Files/yogodot/remote/companion-hook.cmd" --source claude`。

## 卸载

删除对应工具的 hook 条目（只删 YogoDot 这几条，保留其他 hooks），删除 OpenCode 插件文件，
最后可删 `~/.yogo/companion`。不会影响键盘主题、胶囊或旧版 SSH 桥接。
