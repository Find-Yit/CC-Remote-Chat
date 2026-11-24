# HTTP API

后端只监听 `127.0.0.1`。除 `POST /api/login` 外，所有 `/api/*`（含 GET 与 SSE）都必须带 `X-CCR-Token: <会话token>` 请求头（token 由一次性登录码换取，见 README「安全」）。更完整的约定见 [`SPEC_V2_TUI.md`](SPEC_V2_TUI.md) §4.3。

| 方法 | 路径 | 请求体 | 说明 |
|---|---|---|---|
| POST | `/api/sessions` | `{jobid?, cwd?, title?, model?, permission_mode?, skip_permissions?, effort?, resume?}` | 新建（立即返回 meta，后台启动；失败时 meta.state=error） |
| POST | `/api/sessions/<sid>/message` | `{text, clear_input?}` | 粘贴 + Enter。有对话框 → 409 `dialog_open`；输入框有残留 → 409 `input_not_empty`（带 `clear_input:true` 重试） |
| POST | `/api/sessions/<sid>/keys` | `{keys:[...]}` | 白名单：`Enter Escape Up Down Left Right Tab BTab BSpace Space C-c C-u C-b C-x C-s 1-9 s y n`，最多 40 个 |
| POST | `/api/sessions/<sid>/background` | – | 屏幕上有 `ctrl+b … to run in background` 提示且无对话框时发一次 `C-b`（带 guard）；否则 409 `no_background_hint`/`dialog_open` |
| POST | `/api/sessions/<sid>/send_now` | – | 有 `Queued · ctrl+x ctrl+s to send now` 提示且无对话框时发 `C-x C-s`；否则 409 `no_send_now_hint`/`dialog_open` |
| GET | `/api/notify` | – | 提示音：可选系统声音、默认值、本机 Stop hook 是否已 afplay |
| POST | `/api/notify/test` | `{sound?}` | 试听一次（本机 afplay）；设置经 `POST /api/config` 的 `notify_*` 字段保存 |
| POST | `/api/sessions/<sid>/choose` | `{index, text?}` | 当前对话框选第 index 项（↑/↓ 逐步 + Enter）；`text` 仅用于 `free_text` 选项（Type something.） |
| POST | `/api/sessions/<sid>/cancel_dialog` | – | Esc |
| POST | `/api/sessions/<sid>/interrupt` | – | Esc |
| POST | `/api/sessions/<sid>/mode/cycle` | – | Shift+Tab，返回 `{mode, mode_line}` |
| POST | `/api/sessions/<sid>/mode` | `{mode}` | 切到指定权限模式：逐个 Shift+Tab 并确认（最多 6 次）；不在本次启动的循环里 → 409 `mode_needs_restart`（改用 `restart {mode}`） |
| POST | `/api/local/choose_folder` | `{default?}` | 本机：弹出 macOS 自带的选择文件夹对话框（osascript），返回 `{path}` 或 `{cancelled:true}` |
| POST | `/api/sessions/<sid>/model_picker` | `{clear_input?}` | 打开 `/model` 并滚动收集完整列表，返回 `{rows:[{index,n,name,desc,current}], cursor, effort}`；面板保持打开 |
| POST | `/api/sessions/<sid>/model` | `{index, effort?, scope:"session"\|"save"}` | 面板内导航 + 调 effort + `s`/Enter，返回 `{ok, result, state}` |
| POST | `/api/sessions/<sid>/effort` | `{level, scope, clear_input?}` | 打开 `/effort`、调档、`s`/Enter |
| GET | `/api/sessions/<sid>/screen` | – | 最新一帧 `{text, state, ts}` |
| POST | `/api/sessions/<sid>/restart` | `{jobid?}` | 关闭旧的并 `--resume` 续接 |
| POST | `/api/sessions/<sid>/close` | – | 关闭远端 claude（保留记录） |
| DELETE | `/api/sessions/<sid>[?force=1]` | `{force?}` | 关闭并删除记录；连接不可用时需 force（远端可能残留） |
| PATCH | `/api/sessions/<sid>` | `{title}` | 改名；空字符串 = 改回使用 Claude Code 里的标题 |
| POST | `/api/sessions/order` | `{sids:[…]}` | 侧栏手动排序（从上到下） |
| POST | `/api/sessions/<sid>/seen` | `{done_at}` | 标为已看（「新回复」清除；只往前推，不超过当前 done_at） |
| POST | `/api/sessions/<sid>/unseen` | – | 标为未读 |

本机会话：`POST /api/sessions` 带 `jobid:"local"`；`GET /api/history`、`/api/history/<uuid>`、`/api/dirs` 加 `?target=local` 读本机（缺省 = 服务器）；跨运行位置 `restart` → 409 `target_mismatch`。

错误一律 `{"error":"中文说明","code"?}`：400 参数错误、404 会话不存在、409 当前状态不允许（`code` 如 `dialog_open`、
`no_dialog`、`busy`、`input_not_empty`、`no_model_panel`、`wrong_dialog`、`nav_timeout`、`effort_unsupported`、`exited`、
`op_in_progress`、`already_running`、`not_busy`）、502/504 远端操作失败/超时、503 `link_not_ready`。


事件（SSE `data` = `{gseq,sid,ts,kind,data}`）：`kind="transcript"`（`{type:"transcript",entries,reset,session_id}`，
`reset` 时整体重建）；`kind="screen"`（`{type:"screen",state:<解析结果>,text:<原始屏幕>}`，每会话 ≤4 次/秒，回放只给最新一帧）；
`kind="app"`：`meta`、`notice`、`exit`、`model_list`。屏幕解析结果结构见 SPEC §4.1（另附 `echo`、`turn_done`、`interrupted`、
`popup`、`mode_line`、`last_result`、`model_hint` 辅助字段；对话框选项 `free_text`；权限框 `description`/`tip`）。
`GET /api/bootstrap` 额外返回 `permission_modes`、`effort_levels`、`keys`。SSE 的 `reset`/`config` 事件、`/fixtures/*`（仅 `--dev`）。

## 其它接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/login` | `{code}` → `{token}`：一次性登录码（120 秒内有效、只能用一次）换会话 token；唯一不需要 token 的接口 |
| GET | `/api/bootstrap` | 配置、会话列表、可选权限模式/effort/按键 |
| GET | `/api/stream?since=N` | SSE 事件流（见上） |
| GET/POST | `/api/config` | 读取 / 部分更新配置（字段白名单校验，见 README「配置」） |
| GET | `/api/sessions`、`/api/sessions/<sid>` | 会话列表 / 单个会话 meta |
| GET | `/api/sessions/<sid>/events` | 该会话的事件回放 |
| GET | `/api/sessions/<sid>/blob/<ref>` | transcript 里图片的原始数据（服务器会话经 agent 读取） |
| GET | `/api/sessions/<sid>/subagents[/<id>]` | 子代理摘要 / 条目 |
| GET | `/api/sessions/<sid>/local/info` | 本机会话：文件信息（只读） |
| GET | `/api/jobs` | 服务器：`squeue` 里你自己的作业 |
| GET | `/api/dirs?path=…[&target=local]` | 列目录（登录节点 / 本机），只读 |
| GET | `/api/history[?target=local]`、`/api/history/<uuid>` | 历史会话列表 / 内容（只读 `~/.claude/projects/`） |
| GET/POST/PATCH/DELETE | `/api/api_keys…` | API key 管理（明文 key 只进不出，只返回掩码） |
| GET | `/api/api_keys/usage[?refresh=1]` | 可选：查询网关用量（兼容 sub2api 的 `/v1/usage`） |
| GET | `/preview/<cap>/<绝对路径>` | 仅本机会话：文件/网页预览（一次性能力路径 + CSP sandbox） |
