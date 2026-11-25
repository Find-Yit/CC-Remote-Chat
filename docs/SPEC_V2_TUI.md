# cc-remote-chat v2 规格：驱动**交互式** Claude Code CLI（唯一权威约定）

用户要求：本程序只是"本地可视化 + 远程聊天"，**在官方看来就是用户在使用交互式 Claude Code CLI**，没有任何修改和区别。
（另有"本机"运行位置：驱动这台 Mac 上的交互式 claude，规则见 §9，红线同样适用。）
因此 v2 **彻底放弃** headless（`claude -p` / stream-json / control_request / SDK 协议），改为：在计算节点的 tmux（真实 PTY）里运行**交互式** `claude`，本地只做三件事：**发按键/粘贴文本、读屏幕、只读 Claude Code 自己写的文件**（`~/.claude/sessions/<pid>.json` 与会话 transcript jsonl）。

说明：本文档是开发过程中的设计规格（中文），记录各组件之间的约定与实测到的 CLI 行为，代码注释里的「§x」指本文档章节。v1（headless / stream-json）代码已全部删除。本地 Python **只用标准库**（3.9+）；远端 python3 需 3.8+。前端原生 JS，无构建、无外部资源。

---

## 0. 红线（最高优先级）

1. **绝不**运行 `claude -p`、`--print`、`--input-format`、`--output-format`，绝不发送任何 `control_request`/stream-json，绝不使用 SDK。
2. 启动 claude 的命令行 = `claude` + 用户在界面上**明确选择**的标准交互式参数，白名单仅限：
   `--resume <uuid>`（续接）、`--model <value>`、`--permission-mode <acceptEdits|auto|bypassPermissions|manual|dontAsk|plan>`、`--dangerously-skip-permissions`、`--effort <low|medium|high|xhigh|max>`，以及**仅当用户选择了某个已保存的 API key** 时的 `--settings <HOME>/.cc-remote-chat/keys/<16 位 hex>.json`（路径只能由 node_agent 自己生成并校验 `^<HOME>/\.cc-remote-chat/keys/[0-9a-f]{16}\.json$`；本地/前端传入的 `--settings` 一律拒绝；不用 `--settings '<json>'`）。不选则什么都不加（= 用户平时直接敲 `claude`；选"服务器默认"= 不加 `--settings`，与以前完全一致）。**禁止**任何其它参数（尤其 `--plugin-dir`、`--append-system-prompt`、`--system-prompt`、`--mcp-config`、`--session-id`、`-p`…）。
3. **不注入任何环境变量**：tmux server 由 srun step 启动，环境 = 该 step 的环境（等同用户 `srun --pty bash` 后再开 tmux）。不设置 `CLAUDE_*`、`ANTHROPIC_*`、`HOME` 等（fake 测试模式除外，见 §5）。实测（v2.1.330）启动时设 `ANTHROPIC_AUTH_TOKEN` 环境变量会被 settings.json 的 `env` 覆盖、无效；选 key 一律走上面的 `--settings` 文件（只覆盖 `env.ANTHROPIC_AUTH_TOKEN` / 可选 `env.ANTHROPIC_BASE_URL`，其它设置仍取 settings.json；主会话与子代理都走该 key）。**key 只在 claude 启动时读取**（实测运行中原子替换 `--settings` 文件不生效），所以"换 API" = 以 `--resume <当前 claude_session_id>` + 新的 `--settings` 续接重启（§4.2 restart）。明文 key 绝不进 argv（计算节点共享，`ps` 可见）、env、日志。
4. **不写**本地或远端的 `~/.claude*`、`~/.codex`、任何其它 dotfile。远端运行时痕迹 = tmux 套接字 `/tmp/tmux-<uid>/ccr`、Slurm step，以及（仅当用户选择了 API key 时）node_agent 写的 `~/.cc-remote-chat/keys/<sha256(key+"\n"+base_url)[:16]>.json`：目录 0700（不存在则创建；已存在须属主=本人且权限正好 0700，上级 `~/.cc-remote-chat` 须属主=本人且他人不可写，逐级 `O_NOFOLLOW` 拒绝软链接），文件 `O_CREAT|O_WRONLY|O_TRUNC|O_NOFOLLOW` 0600、拒绝软链接/硬链接，内容**严格**为 `{"env":{"ANTHROPIC_AUTH_TOKEN":<key>[,"ANTHROPIC_BASE_URL":<url>]}}`（写后复核；同一 key+地址复用同一文件，内容一致不重写）。这是 node_agent 唯一允许写的文件。本地数据只在 `~/.cc-remote-chat/`（`api_keys.json`、`session_keys.json` 均 0600）。
5. **按键只来自用户操作**：程序**绝不自动**回答权限审批、目录信任、AskUserQuestion 等对话框；只有用户在界面上点击后才发送对应按键。例外（都是用户点击某个按钮触发的固定序列）：关闭会话时发送 `/exit`；用户在"切换模型/effort"界面确认后，程序在 `/model`、`/effort` 面板里按方向键导航并按 `s`/Enter；"中断"=Esc；**本机会话里用户在输入框按 ⌘V 粘贴的内容里有图片时按一次 `C-v`**（= 用户在终端里按 Ctrl+V，由 claude 自己读剪贴板，§4.2「图片」）。用户点图片条上某张图片的 × 后，用 ← / → 把光标挪到那张 `[Image #N]` 后面按退格、再整理空格并把光标挪回末尾（= 用户在终端里删掉这一张；每一步都带 guard 就地核对光标位置与图片编号，§4.2 `remove_image`）。本程序与测试**绝不读写系统剪贴板**（浏览器 paste 事件里的 clipboardData 是用户自己按 ⌘V 交给网页的，只用来判断"有没有图片"和显示缩略图，不上传、不落盘）。
   **任何"确认类"按键（提交输入框的 Enter、对话框选项的 Enter、面板的 s/Enter、Shift+Tab）都先核对最新屏幕，并带 `guard` 让 agent 在 send-keys 前就地重抓屏复核**（§2 `keys`）；核对不通过就一个键都不按、返回 409，绝不把 Enter 打进刚弹出的对话框。
6. `/model`、`/effort` 的确认**按面板提示行决定**（绝不盲按）：提示行含 `s to use this session only` → 仅本会话按 `s`（默认）；只有 `Enter to use this session only`（实测 `/effort` 面板在 max 档，无 `Enter to save`）→ 仅本会话按 Enter（CLI 明示的"仅本会话"）；用户明确点"保存为默认"→ 提示行必须含 `Enter to save` 才按 Enter，否则 409 `save_unsupported`；其它提示行一律 409 `unexpected_panel`。**绝不发 Tab**（`/effort` 面板的 Tab = 切换 Ultracode）。

---

## 1. 已实测的远端事实（fixtures 见 §6）

- 远端拓扑：`ssh <host>`（`~/.ssh/config` 里的主机别名，在「设置」里填写）→ `srun --jobid=J --overlap` 进入作业所在的计算节点；登录节点需要 `srun`/`squeue`/python3，计算节点需要 `claude` 与 `tmux`。claude 只在计算节点运行。
- 计算节点 tmux：同一作业的所有 step 必须用**同一个 tmux 二进制**（不同版本连不上同一 server，报 `server exited unexpectedly`）。解析顺序：`$HOME/.local/x86_64/bin/tmux`（自己编译的 tmux 放这里）存在则用之，否则 `bash -lc 'command -v tmux'`。所有 tmux 调用一律 `-L ccr -f /dev/null`。
- `/tmp` 在同一作业的不同 step 之间共享（套接字可见）。
- **keeper 模式已实测可行**：从登录节点 `setsid nohup srun --jobid=J --overlap -n1 -N1 -J ccr-keeper bash -c '<T> new-session -d -s _ccr_keeper "sleep 2147483647"; while <T> has-session -t _ccr_keeper 2>/dev/null; do sleep 15; done' </dev/null >/dev/null 2>&1 &` 启动后，ssh 断开 step 仍在；另一个 step（agent）用同一 tmux 能 `ls`/`new-session`，**新会话的进程属于 keeper step 的 cgroup**，agent/ssh 断开不影响 claude；`kill-server` 或杀掉 `_ccr_keeper` 会话后 keeper step 自行结束（无需 scancel）。
- 这个集群的 `scancel <step>` 即使成功也可能打印 `Invalid job id specified`：一律以 `squeue -h -s -j J -o '%i|%j'` 的结果为准。
- 交互式 claude 进程：tmux `pane_pid` 是包装进程，真正的 claude 是其**子孙进程**；`~/.claude/sessions/<claude_pid>.json` 形如
  `{"pid":3452243,"sessionId":"59dd443a-…","cwd":"/home/demouser","kind":"interactive","entrypoint":"cli","status":"idle|busy|waiting",…}`（waiting = 正在等审批/对话框）。按 pane 进程树（`pgrep -P` 递归或 /proc）找。
- transcript：`~/.claude/projects/*/<sessionId>.jsonl`；条目类型很多（user/assistant/system/attachment/mode/permission-mode/file-history-snapshot/api-request*…），渲染只需 `user`、`assistant`、`system`（且非 `isSidechain`、非 `isMeta`）。**只在消息完成后写入**（无逐字）。
- **逐字回显**：交互界面会把正在生成的文字逐行刷到屏幕上（实测 0.3–0.5s 一行），状态行形如 `✢ Schlepping… (5s · ↓ 175 tokens · thinking with low effort)`，底部提示行含 `esc to interrupt` 表示忙。以 ~250ms 频率 `capture-pane` 即可实现近似逐字的"实时草稿"。
- 启动时在 home 目录会出现**信任目录**对话框（默认光标在 `No, exit`；home 目录选 Yes 仅本次有效）。程序必须原样展示给用户选择。
- 屏幕布局（宽 200 高 50）：上方是会话内容（`❯ <用户消息>` 回显、`● ` 开头的助手/工具行、两空格缩进续行、`⎿` 工具结果行、`✻ Baked for 9s · done …` 每轮结束），下方是输入框（两条 `────` 横线之间以 `❯` 开头），再下是状态栏（用户自定义 statusline，如 `[Haiku 5.5 · 1M · effort:low]`）和模式行（`⏵⏵ auto mode on (shift+tab to cycle)`，忙时追加 ` · esc to interrupt`）。对话框出现时，输入框位置被对话框（`────` 或 `▔▔▔▔` 开头的区域）取代。
- 对话框样例（全部在 fixtures 里，务必逐个对照）：
  - 权限（Bash）：`Bash command` / 描述 / `╌╌╌` 包围的预览 `touch /tmp/…` / `Do you want to proceed?` / `❯ 1. Yes` `2. Yes, and always allow access to /tmp from this project` `3. Yes, and switch to auto mode · …` `4. No` / `Esc to cancel · Tab to amend`。
  - 权限（Write）：`Create file` / 文件名 / 预览 / `Do you want to create X?` / `1. Yes` `2. Yes, and switch to accept edits …`（可能折行）`3. No`。按数字键立即选择（实测按 `3` = No 生效）。
  - AskUserQuestion：`☐ 颜色偏好` / 问题 / `❯ 1. 红色`（下一行缩进描述）`2. 蓝色` `3. Type something.` / `────` / `4. Chat about this` / `Enter to select · ↑/↓ to navigate · Esc to cancel`。
  - 信任目录：无编号选项 `❯ No, exit` / `Yes, I trust this folder` / `Enter to confirm · Esc to cancel`。
  - `/model`：`▔▔▔` 下 `Select model` / `Your pick is the default for new sessions…` / `↑ 2 more` / `3. Fable 5.1   Custom Fable model (claude-fable-5-1[1m])` / `4. Sonnet   Sonnet 5.5 · …`（描述可折行）/ `❯ 5. Haiku ✔   Haiku 5.5 · …` / `↓ 1 more` / `○ Low effort ←/→ to adjust`（按 → 变 `◐ Medium effort (default)`）/ `s to use this session only · Enter to save · Esc to cancel`。列表**只显示可视窗口**，需用 ↑/↓ 滚动收集全部行（完整列表实测为：Default (recommended)、Opus 5.5、Fable 5.1、Sonnet、Haiku、Fable 5.2 (1M)）。
  - `/effort`：`Effort` / `Faster … Smarter` / 滑杆 `─▲───…` / `low  medium  high  xhigh  max` / `Ultracode  off  Tab to toggle` / `←/→ to adjust · s to use this session only · Enter to save · Esc to cancel`。▲ 的列位置对齐到下方档位名。**移到 max 时（2026-11-22 Opus 5.5 实测）**：上方多一行 `May use excessive tokens resulting in long response times or overthinking. Use sparingly for the hardest tasks.`，提示行变成 `←/→ to adjust · Enter to use this session only · Esc to cancel`（没有 s，也不能保存）。
  - `/model` 面板的 effort 行（实测）：`○ Low effort` / `◐ Medium effort` / `● High effort` / `◉ xHigh effort` / `◈ Max effort`（后接 `←/→ to adjust`；图标会变，解析不依赖图标；`xHigh` 大小写混合，整体识别，不能只取最后一个词当成 high；识别不了 level=null）；Max 时同样多一行警告，但**提示行不变**（仍可 `s` / `Enter to save`）。
  - slash 补全（实测）：输入框里是 `/model` 时补全列表在上横线之上，第一项高亮 `  ❯ /model   Switch model (currently …)`，其余项四个空格缩进（`/auto-mode-setup`、`/code-review`、`/effort`…）；`/exit` → 高亮 `❯ /exit   Exit the CLI`；`/effort` → 高亮 `❯ /effort   Set the model's effort level`。此时按 Enter 执行的是**高亮项**。
  - 作业结束后：`srun: error: Slurm job 276403 has expired`、`srun: Check SLURM_JOB_ID environment variable. Expired or invalid job 276403`；`squeue -h -j J` 报 `Invalid job id specified` 或无输出。
  - 中断后：`⎿  Interrupted · What should Claude do instead?`；Esc 取消对话框后会出现 `⎿  Cancelled` 或 `Declined to answer…`。
  - 粘贴：`tmux load-buffer` + `paste-buffer -p`（bracketed paste）多行进入输入框且不提交；之后单独发 Enter 提交。
  - **图片（CLI 2.1.332 macOS 真机，2026-11-25，`fixtures/image_paste/`、`fixtures/tui_screens_images.ndjson`）**：用与 node_agent 完全相同的粘贴方式（`load-buffer` + `paste-buffer -p -d`）把**图片文件路径**单独粘贴一次，输入框立即显示 `[Image #N]`（png / jpg / gif / webp、大写 `.PNG`、cwd 之外的文件、隐藏目录里的文件、`$TMPDIR/TemporaryItems/NSIRD_screencaptureui_*/截屏… .png` 这类截屏缩略图路径都可以；不需要读文件的权限框）。`N` 在整个 claude 进程里递增、不随消息重置；连续两张之间 CLI 自己补一个空格（`[Image #2] [Image #3]`），跟在文字后面不补（`比较两张图：[Image #2]`）；退格整块删掉占位。**识别条件**：整段粘贴就是一个存在的图片文件（路径里有未转义的空格也行），或多个用空格分隔、按终端拖放写法反斜杠转义（`my\ shot.png`、`a\ \(1\)\&b.png`、`截屏2026-11-25\ 21.00.00.png`）或加引号的路径；**路径 + 文字放在同一次粘贴里、换行分隔的多个路径、不存在的文件、非图片扩展名（`.txt`）都只是普通文字**。所以发图片必须"每个路径单独粘贴一次，文字另外粘贴"。提交后 transcript 的用户条目 `content = [{type:text, text:"[Image #1] 文字"}, {type:image, source:{type:base64, media_type:"image/png", data}}…]` + `imagePasteIds:[1]`，紧接一条 `isMeta` 的 `[Image: source: <路径>]`；屏幕回显 `❯ [Image #1] 文字`，下面每张一行 `  ⎿  [Image #1]`。只有图片的消息文字块就是 `[Image #20]`。忙时同样可以粘贴，按 Enter 进入排队（`❯ [Image #18] 文字` + `⎿  Queued · ctrl+x ctrl+s to send now`），之后照常带图片发出。haiku 能正确说出测试图里的颜色与单词。**光标与删除单张（2026-11-25 真机，`fixtures/image_paste/caret_probe.json`）**：CLI 把（隐藏的，`cursor_flag=0`）终端硬件光标停在输入框插入点上，tmux `#{cursor_x} #{cursor_y}` 准确（中文两列宽、多行都对得上；代码里是 Ink 的 cursor declaration，经典渲染器同样停放，但只实测了本机会话实际用的 fullscreen 渲染器）；← / → 在 `[Image #N]` 上**整块跳**、空格和其它字符一步一个（`[Image #1] [Image #2] [Image #3]` 光标在末尾 34 列，连按 ← 依次到 24/23/13/12/2/2，→ 反之）；Home / End / Ctrl+A / Ctrl+E 到本行行首 / 行尾（多行时只到本行），本行行首按 ← 到上一行行尾；光标紧跟在 `[Image #N]` 后按退格 = 整块删掉这一张，**两边的空格都留着**（`[Image #1]  [Image #3]`，再退格一次删掉多出来的空格），**编号不重排**（之后新图片是 #4）；光标被挪到中间时粘贴图片路径会插在光标处且不补空格（`[Image #1][Image #4] [Image #3]`），所以删完要把光标挪回末尾；删掉中间那张后再发送，transcript 里只有剩下的 image 块、`imagePasteIds:[5,7]`，haiku 只说出剩下两张的颜色。状态栏提示 `Tip: Use ctrl+v to paste images from your clipboard`（Ctrl+V 读剪贴板图片；为了不碰剪贴板，真机未测 Ctrl+V，由 fake 仿真）。

---

## 2. 远端组件：node agent

`ccr/node_agent.py`：**独立脚本**（不 import ccr 包），源码作为 `python3 -I -u -c <源码>` 的参数传给计算节点执行；本地 fake 模式直接 `python3 -I -u ccr/node_agent.py`。只用标准库、兼容 3.8+。除 `start` 带 `api_key` 时写的 `~/.cc-remote-chat/keys/<指纹>.json`（§0.4）外**不写任何文件**（源码测试：只有 `_open_private_dir`/`write_key_file` 可以出现 mkdir/O_CREAT/O_WRONLY/fchmod 等写操作）。stdin EOF 或 90s 未收到任何请求（含 `ping`）时退出。

协议：stdin/stdout 每行一个 JSON。
- 请求 `{"id":int,"op":str,...}` → 响应 `{"id":int,"ok":true,"result":...}` 或 `{"id":int,"ok":false,"error":"中文说明"[,"code":"guard|tmux_busy"]}`
- 事件（无 id）`{"ev":str,...}`

ops：
- `hello` → `{"node","slurm_node","tmux_path","tmux_version","server_alive":bool,"home","sessions":[名称…]}`（`slurm_node` = 只读的 `SLURMD_NODENAME`，多节点作业钉节点用）
- `ping` → `{}`
- `list` → `[{"sid","name","pane_pid","pane_dead":bool,"exit_status":int|null,"claude_pid":int|null,"session":{sessionId,status,cwd,kind,entrypoint}|null}]`（只列 `ccr-` 前缀会话）。只有 tmux **确定**报 `no server running` / `error connecting to …(No such file|Connection refused)` 才返回 `[]`；超时等暂时失败报错（code `tmux_busy`），不能让本地把仍在运行的会话误判为已退出。`stop_keeper` 同理。
- `start` `{"sid","cwd","args":[...],"api_key"?:str,"base_url"?:str}`：校验 sid `^s[0-9a-f]{12}$`、cwd（`~` 展开为 home，必须是已存在目录）、args 白名单（§0.2，逐项精确校验，值用正则；**args 里的 `--settings` 一律拒绝**）；会话名 `ccr-<sid>` 已存在则报错。
  可选 `api_key`（`^[A-Za-z0-9._-]{8,256}$`）/ `base_url`（`^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$`，≤200，只能与 api_key 同时给）：明文只在这条 stdin JSON 请求里，绝不进 argv/env/日志/错误信息。agent 按 §0.4 写 key 文件（内容已一致则复用），再追加 `--settings <该绝对路径>` 并以 `allow_settings` 重新逐项校验（值必须匹配 `^<HOME>/\.cc-remote-chat/keys/[0-9a-f]{16}\.json$`）。不给 api_key = 与以前完全一致（不碰任何文件、不加 `--settings`）。执行
  `tmux -L ccr -f /dev/null new-session -d -s ccr-<sid> -x 200 -y 50 -c <cwd> bash -l -i -c 'exec claude "$@"' claude <args…>`（多参数形式，不经 shell 拼接；**登录 + 交互式** bash = 用户在终端里登录后敲 claude：~/.profile → ~/.bashrc 完整执行。审计实测：用户的 ~/.bashrc 对非交互 shell 提前 return，conda 初始化在其后，以前的 `bash -lc` 让 Bash 工具拿到 /usr/bin/python 而非平时的 conda python）；随后 `set-option -t ccr-<sid> remain-on-exit on`、`history-limit 10000`。返回 `{"pane_pid"}`。前提：server 已由 keeper 启动（`hello.server_alive`），否则报错"tmux 常驻进程未启动"。`-c` 的值会被 tmux 做 format 展开、以 `;` 结尾会被当成命令分隔符：传入前 `#`→`##`、结尾 `;`→`\;`（实测本机 tmux 3.6a 可得到字面路径）。start 前先丢掉该 sid 旧的监视状态（同 sid 重启时不会沿用旧屏幕缓存、也不会再发旧代际的 exited）。
- `paste` `{"sid","text","enter":bool}`：`load-buffer -b ccrp-<sid> -`（text 走 stdin）→ `paste-buffer -p -d -b ccrp-<sid> -t ccr-<sid>`；enter 为真时 sleep 0.08 再 `send-keys Enter`（本地已不再使用 enter=true，见 §4.2 send_message）。text ≤ 200000 字符，禁止 NUL。
- `keys` `{"sid","keys":[...],"guard"?:{...}}`：每个键必须在白名单：`Enter Escape Up Down Left Right Tab BTab BSpace Space C-c C-u C-b C-x C-s 1 2 3 4 5 6 7 8 9 s y n`（`C-b`/`C-x`/`C-s` 见 §11）；逐个 `send-keys`，间隔 40ms。最多 40 个。
  给了 `guard` 时，send-keys **之前**先 `capture-pane` 就地复核，不通过就一个键都不按、返回 `code:"guard"`：`lines:[行…]`（这些行仍在屏幕上）；`input_box:true`（输入框在屏幕底部：两条顶格 `────` 之间 `❯` 开头，下横线之后只有 ≤4 行状态栏/模式行（子代理列表行不计），且没有编号选项/`Esc to cancel`/`Do you want to` 等对话框内容）；`input_nonempty:true`；`popup:"/cmd"|null`（输入框上方 slash 补全列表没出现，或高亮项正好是它）；`tail:{n,sha1}`（屏幕最后 n 行 = 对话框区域的 sha1 没变，即对话框没被换成另一个）；`hint:"background"|"send_now"`（输入框在底部、且其上方此刻仍有该 CLI 提示行；**白名单**：只接受这两个固定 id，整行匹配固定写法，不接受任意正则，见 §11）；删除单张图片用（都要求 `input_box`）：`input_images:[N…]`（输入框里的 `[Image #N]` 编号按顺序正好是它）、`cursor_image:N`（终端光标在输入框文字区里、紧跟在 `[Image #N]` 后面）、`cursor_space:true`（光标前一个字符是空格）——带 `cursor_*` 时 agent 用**同一条 tmux 命令**先 `display-message -p '#{cursor_x} #{cursor_y}'` 再 `capture-pane -p`（不带 `-J`：行号 = 终端行号）复核，规则与 `tui_parse.input_caret` 逐字一致。复核前 agent 先按 `screen_lines` 规范化（rstrip、NBSP→空格、去掉末尾空行、裁掉右侧 diff 面板，见 §4.1），与 `tui_parse.screen_lines` 逐字一致（session 的 tail sha1 与 `guard.lines` 都按它计算；单测逐帧比对两份实现）。
- `capture` `{"sid","history":int=0,"cursor":bool=false}` → `{"text"}`（`capture-pane -p -J`，history>0 时加 `-S -<n>`）；`cursor:true`（不能与 history 同用）→ `{"text","cursor":{"x","y"}}`：同一条 tmux 命令里读光标 + 不带 `-J` 抓屏（删除单张图片时定位输入框里的光标）
- `watch` `{"sid","on":bool}`：开/关该会话的监视循环（一个后台线程统一轮询所有被监视会话）；`on:true` 总是换成全新的监视状态（立即发首帧、transcript reset）：
  - 屏幕：忙（屏幕含 `esc to interrupt` 或 session.status==busy）或最近 3s 有变化时每 250ms 抓一次，否则每 1s；内容变化才发 `{"ev":"screen","sid","text","ts"}`。
  - session json：变化时发 `{"ev":"session","sid","data":{pid,sessionId,status,cwd,kind,entrypoint}}`。
  - transcript：根据 sessionId 定位文件（sessionId 变化时切换，如 `/clear`），只发**完整行**，过滤为 `type∈{user,assistant,system}` 且非 `isSidechain`；**先把大图片换成引用（§10.1）**，再把超过 20000 字符的字符串截断并加 `"truncated":true`；`{"ev":"transcript","sid","session_id","entries":[...],"reset":bool}`（首次/切换文件时 reset=true 并从头发送，每批 ≤ 300 条）。
  - 子代理（subagent）：主 transcript 找到后，监视同目录下由已知 sessionId 推出的 `<sessionId>/subagents/`（实测 CLI v2.1.330：`agent-<agentId>.jsonl` 条目带 `isSidechain:true`、`agentId`；旁边 `agent-<agentId>.meta.json` 形如 `{"agentType","description","toolUseId","model","requestShape","finished",…}`）。
    - 路径约束：`<sessionId>` 与 `subagents` 两级都必须是真实目录（lstat，软链接即放弃）；只列名字匹配 `^agent-[A-Za-z0-9_-]{1,64}\.(jsonl|meta\.json)$` 的普通文件（不跟随软链接，读取用 `O_NOFOLLOW` + fstat 普通文件校验）；不读其它任何路径，**不写任何文件**。
    - 跟踪最近活动（mtime）的最多 50 个；目录每 2s 扫描一次；未结束的子代理随会话 tick（0.25s/1s）检查 lstat 签名变化后增量读取，已结束（meta.finished=true 且已读完）的只在扫描时检查。
    - jsonl 只转发**完整行**、`type∈{user,assistant}`、非 `isMeta`（attachment / api-request-shape / api-request-blob / api-request 等一律不传）；字符串 > 20000 截断并 `"truncated":true`，整条序列化仍 > 256KB 时字符串再截到 2000/200；每次每个子代理最多读 4MB、每批 ≤ 300 条。meta.json ≤ 64KB，只转发标量字段（字符串截 2000），内容变化才随事件重发。
    - 事件 `{"ev":"subagent","sid","session_id","agent_id","meta":{…}|null,"entries":[...],"reset":bool}`：每个子代理首次（及 jsonl 被截断重写）时 reset=true 并从头发送；`meta` 只在首次/变化时给出，否则为 null。主 transcript reset（首次 / 换 sessionId / 重写）时子代理跟踪一并清空并从头重发（因此断线重连、重新 watch 后都会完整重发）。
  - pane 死亡或会话消失：`{"ev":"exited","sid","exit_status","pane_pid"}`（`pane_pid` = 代际信息）并停止监视该会话。`list-panes` 只有确定 server 不在才算"会话消失"；超时等暂时失败连续两次才算（本地还会再 list 核实）。期间已被 kill/start/watch 换掉的旧监视状态不再发 exited。
- `kill` `{"sid"}` → 先停监视（自己 kill 的不再发 exited），再 `kill-session -t ccr-<sid>`（不存在不报错）
- `stop_keeper` → 若已无 `ccr-` 会话则 `kill-session -t _ccr_keeper`（server 随之退出，keeper step 自行结束），返回 `{"stopped":bool,"remaining":n}`
- `blob` `{"sid","ref","session_id"?}` → `{"media_type","data"}`：按图片引用重读 transcript 的那一行、校验 hash 后返回原始 base64（只读，见 §10.1）。

## 3. 本地：AgentLink

`ccr/agent_link.py`：
```python
class AgentLink:
    def __init__(self, cfg: dict, jobid: str, on_event: Callable[[dict], None],
                 on_state: Callable[[str, str | None], None]): ...
    state: str            # "connecting" | "ready" | "down" | "ended"（作业已结束：不再重连）
    def start(self) -> None                       # 后台线程连接；断开后指数退避重连（2,4,8,…,60s），重连成功 on_state("ready")
    def request(self, op: str, timeout: float = 30, **params): ...   # 返回 result；失败抛 AgentError(中文，.code 可为 "guard")；未连接抛 AgentDown
    def ensure_keeper(self, timeout: float = 120) -> None            # hello；server 未启动则经登录节点用 setsid nohup srun 启动 keeper，轮询 hello 直到 server_alive
    def check_job(self) -> bool | None                               # 同步 squeue：True=作业已结束（并进入 ended）/False=还在/None=查不到
    def close(self) -> None
```
- agent 进程：`ssh -T <复用参数同 v1 remote.ssh_base_argv> <host> "bash -lc 'exec srun --jobid=J --overlap -n1 -N1 [-w <节点>] -u -J ccr-agent python3 -I -u -c <引用后的源码>'"`；远端字符串逐层 `shlex.quote`；本地 `shell=False`；不传 env。每 30s 发 `ping`。
- **作业状态（以 squeue 为准）**：首次连接前、以及每次连不上/断开后，执行 `squeue -h -j J -o '%T|%N'`：无输出或报 `Invalid job id` → 作业已结束；状态为 COMPLETED/COMPLETING/CANCELLED/FAILED/TIMEOUT/NODE_FAIL/PREEMPTED/… 也算结束；squeue 本身查不了（ssh 失败）时退而看 srun 报错里是否有 `has expired` / `Expired or invalid job`。已结束 → `on_state("ended", "作业 J 已结束…")`，停止重连循环（不再每 60s 白白 ssh+srun）。fake 模式不查。
- **多节点作业**：`%N` 是节点列表表达式（`gpu[0001-0002]`）时，agent 用 `-w <列表第一个节点>` 钉住；连上后以 hello 的 `slurm_node` 为准记住节点，之后重连与 keeper 都 `-w` 到同一节点（tmux 套接字在节点本地 /tmp）。先有 keeper 后起 agent（app 重启）时同样按"列表第一个节点"规则，与当初 keeper 所在节点一致。单节点作业不加 `-w`（命令与以前完全相同）。
- keeper 启动命令见 §1（tmux 路径用 agent `hello` 报告的 `tmux_path`，`shlex.quote`）；step 名固定 `ccr-keeper`；同一作业已有 `ccr-keeper` step 且 `server_alive` 为真时不重复启动。keeper 脚本：`T new-session -d -s _ccr_keeper "sleep 2147483647" || exit 0`（已有 keeper → 立即退出，不留第二个空转 step）；循环里 `list-sessions` **成功**且连续 600s 没有任何 `ccr-` 会话 → 自己 `kill-session -t _ccr_keeper`（keeper step 随之结束）。兜底"ensure_keeper 超时后 keeper step 才姗姗启动""app 崩溃没 stop_keeper"留下的空闲 keeper。
- fake 模式（`cfg["fake"]`）：本地 `python3 -I -u ccr/node_agent.py`，环境变量 `CCR_FAKE=1`、`HOME=<fake_home>`、`TMUX_TMPDIR=<tmpdir>`、`CCR_CLAUDE_CMD=<python3 ccr/fake_tui.py 的绝对命令>`（agent 在 fake 下用它代替 `claude`，tmux 用本机 tmux），keeper = 本机 `tmux new-session -d -s _ccr_keeper sleep …`（同样 `-L ccr -f /dev/null`）。**真实模式绝不读取这些环境变量**（agent 源码里 fake 分支只在 `CCR_FAKE=1` 时启用；真实模式通过 ssh 启动时不会有这些变量）。
- `remote.py` 继续提供 `validate_*`、`ssh_base_argv`、`close_control_master`、`resolve_remote_path`、`run_remote`；删除 headless 相关（`build_claude_argv` 改为 `build_claude_args(model, permission_mode, skip_permissions, effort, resume) -> list[str]` 只产出 §0.2 白名单参数）。

## 4. 本地：屏幕解析 + 会话管理 + API

### 4.1 `ccr/tui_parse.py`（纯函数，fixtures 单测）
`parse_screen(text: str) -> dict`：
```json
{
 "busy": bool,                      // 模式行含 "esc to interrupt"
 "spinner": "Schlepping… (5s · ↓ 175 tokens · thinking with low effort)" | null,
 "mode": "auto|acceptEdits|plan|bypassPermissions|manual",   // 由模式行判断，无模式行 = manual
 "status_line": "原始状态栏文本" | null,
 "input": {"visible": bool, "text": "输入框里的文字"},
 "live": "当前这一轮在屏幕上的实时内容（最后一个 '❯ 用户消息' 回显之后到输入框/对话框之前；去掉 spinner、Tip 行）" | null,
 "dialog": null | {
    "kind": "permission|question|trust|model|effort|generic",
    "title": str, "lines": [对话框原文行…],
    "options": [{"index":0起,"n":编号或null,"label":str,"desc":str,"selected":bool}],
    "hints": "Esc to cancel · Tab to amend",
    // kind=permission: "tool":"Bash command|Create file|…","question":"Do you want to proceed?","preview":[行…]
    // kind=question: "header":"颜色偏好","question":"你更喜欢红色还是蓝色？"
    // kind=trust: "path":"/home/demouser"
    // kind=model: "rows":[{"n":3,"name":"Fable 5.1","desc":"…","current":bool,"selected":bool}],"more_above":2,"more_below":1,
    //             "effort":{"level":"low|medium|high|xhigh|max"|null,"label":"Low effort","is_default":bool}|null
    // kind=effort: "level":"low…max"|null（▲ 对齐的档位）,"ultracode":"off|on"|null
 },
 // 统计用的辅助字段（见下文"统计字段"）
 "spinner_info": {"verb","elapsed_s","tokens","token_dir":"down|up"|null,"note"} | null,
 "turn_done_s": 9.0 | null,                    // "✻ Baked for 9s · done …" 的用时（秒）
 "status_stats": {"context_used","context_limit","context_pct","cost_usd","cost_text"} | null
}
```
对话框的选项：编号行 `^\s*(❯\s*)?(\d+)\.\s+(.+)`，缩进续行并入上一项 desc（model 行的名字/描述按两段以上空格分隔）；信任框无编号，按 `❯` 判定选中。解析不出时 `kind="generic"`（保留 lines/options/hints）。**解析器必须对所有 fixtures 画面给出合理结果**（单测逐一断言）。
**输入框位置约束**：只有最后一组"横线 + `❯` 开头 + 横线"且下横线之后只有 ≤4 行状态栏/模式行（子代理列表行不计）、没有编号选项/`Esc to cancel`/`Enter to select`/`Enter to confirm`/`Do you want to`/横线时才算输入框；否则（例如权限框上方用户回显的正上方恰好有一条顶格横线）一律按对话框处理。
**统计字段**（纯函数 `parse_spinner` / `parse_status_stats` / `parse_duration` / `has_1m_hint`，解析不到返回 None、绝不抛异常）：
- `spinner_info`：实测写法 `Schlepping… (5s · ↓ 175 tokens · thinking with low effort)`、`Shimmying… (6s · ↓ 277 tokens · thought for 1s)`、`Cogitating… (3s · ↓ 156 tokens)`、`Wandering…`（无括号 = 只有 verb）；另兼容 `↑`、`1.2k tokens`、`3,456 tokens`、`1m 5s`、括号被截断。用时只认括号里第一段时长；tokens 只认 `N tokens`（子代理列表行的 `↑ 14.3k tokens` 不是 spinner，不解析）。
- `status_stats`：状态栏是**用户自己的 statusline**，格式因人而异，宽松解析：花费 = 第一个 `$金额`（保留原文 `cost_text`，如 `$0.0037`）；上下文 = 第一个 `已用/上限`（`25k/1M`、`0/1M`、`120.5k / 200k`；上限无 k/M 单位时须 ≥ 10000，前后不能紧挨字母/`/`，排除路径、日期、`cache r:2551/w:9564`）；百分比取紧挨 `已用/上限` 的那个（24 字符内），否则取前面带 ctx/context/上下文/进度条字符的，再否则（无 `已用/上限` 时）取第一个；只有 `已用/上限` 时按比值算。对话框遮住状态栏的画面 = None。
- `has_1m_hint`：`[1m]`（不分大小写）或大写 `1M` 独立词（`· 1M ·`、`(1M)`、`1M context`）；`1m 5s` 不算。
- `context_usage`：屏幕上的 /context 输出（CLI 2.1.331：fullscreen 与经典渲染器下都内联在消息区，不是需要 Esc 的面板；"Context usage" 标题后 14 行内，右列依次是 显示名 / 模型 id / `16k/1m tokens (2%)`，窄终端时右列折到网格下方）→ `{used, limit, pct, model, display}`，有多份时取最后一份，标题已滚出屏幕时为 None。同一函数 `parse_context_usage` 也解析 transcript 的 `<local-command-stdout>`（带 ANSI）和 isMeta 的 Markdown 版（`**Tokens:** 16k / 1m (2%)`）。

子代理运行时（`fixtures/subagent_sample/screen_frames.txt`）模式行下方会多出子代理列表 `● main` / `◯ general-purpose  前台计算 1+1      2s · ↑ 14.3k tokens`：解析为 `agents_panel:[{name,desc,info,current}]`，不进 `status_line`、不当成对话框/输入框；`✻ Waiting for N background agent(s) to finish` 不是本轮结束行（`turn_done` 只认 `✻ <动词> for <数字>s|m|h…`）；输入框正上方右对齐的提示行（`○ low · /effort`、`tmux focus-events off · …`）不进 `live`。
**输入框里的图片**（CLI 2.1.332，§1「图片」）：`input.text` 里有 `[Image #N]` 时 `input.images = [N…]`（按出现顺序；没有图片时没有这个键，旧画面的结果不变）；纯函数 `split_input_images(text) → ([N…], 其余文字)`。回显下紧跟的 `  ⎿  [Image #N]` 行属于回显，不进 `live`。
**输入框里的光标**（删除单张图片，§1「图片」实测）：纯函数 `input_caret(lines, cx, cy)`（屏幕行 + 终端光标 → `{box,row,rows,col,before,after}`：光标所在输入框行里光标前 / 后的输入文字，去掉每行开头 2 列的 `❯ ` / `  `；光标在 rstrip 掉的行尾空格里 = 真的空格，before 末尾补上；落在宽字符中间或不在输入框文字区 → None）、`input_image_cells(lines, n)`（每个 `[Image #n]` 后面那一格的 `(cx, cy)`）、`input_end_cell(lines)`、`caret_moves(lines, cur, tgt, limit=40)`（← / → 的一批按键：同一行按步数精确走，`[Image #N]` 与 `[Pasted text #N …]` 整块算 1 步、其余字符 1 步；跨行先走到本行行首 / 行尾再多 1 步；**宁少勿多**，→ 绝不越过目标）。`lines` 必须来自不带 `-J` 的抓屏（行号 = `cursor_y`）。与 node_agent `caret_before` 逐格一致（单测对全部真机画面的每一格比对）。

**CLI 2.1.332**（`fixtures/cli_2_1_332/`）：子代理行圆点还有 `⏺`（`_AGENT_ROW_RE`、`_FOOTER_AGENT_ROW_RE` 与 node_agent `G_AGENT_ROW_RE` 一致），选中行前缀 `❯ `（`selected`）；info 可带状态词（`stopped · 22s · ↑ …`，对应 `state:"stopped"`）。焦点在子代理列表上时，模式行被 `↑/↓ to select`、`enter to view · x to stop · ctrl+x ctrl+k to stop all agents`、`enter to view · x to clear` 顶替：解析为 `agents_focus:true`、`agents_hint`、`mode:null`（不误判成 manual，会话沿用之前看到的模式），此时 `cycle_mode` / `set_mode` 返回 409 `agents_focus`、不发 Shift+Tab。模式行新后缀 `· ← for agents`、`· ↓ to resume subagent`、`· /tasks to see subagents` 照常解析。

**右侧 diff 面板（CLI 2.1.331，内置插件 cc-plugin-diff）**：fullscreen 渲染器（如用户 `.zshrc` 里的 `CLAUDE_CODE_NO_FLICKER=1`）下，当前目录是 git 仓库、本会话第一次用工具改文件后，宽度 ≥144（用户用 `/diff` 打开过则 ≥110）时屏幕右侧自动出现 diff 面板：左栏聊天区 + `│` + 面板，面板从第 0 行一直到整行宽的输入框上横线之前；对话框画在左栏时一直到屏幕底部，对话框上横线变成 `───…│───…`（不处理时权限框整个识别不出、echo/live/spinner 混进面板文字）。`parse_screen` 在任何规则之前先 `strip_side_panel`：前 3 行内某行在显示列 c（≥40，按东亚宽度计）有 `│`，自该行起连续 ≥3 行在 c（±2 列）都有 `│`，且面板前 4 行有面板头（`│` 紧跟 `N file(s) changed`，或含 `/diff to close`、`source: … ▾`，或首行只有右对齐的 `✕`）、竖线右侧不再有 `│` 时，把这些行裁到 `│` 之前；其余规则都按裁后的行工作。没有面板时是恒等变换（单测对全部旧 fixtures 锁定）。结果附 `side_panel: {files, add, del} | null`（前端暂不用）。<110 列时面板隐藏，输入框上方的 "Resize your terminal to at least 110 columns…" 提示框不进 `live`。**红线**：只能在读屏端适配，不能用发 `/diff`、改 pane 宽度、改设置或环境变量等方式规避面板。另：没设 `tui`、也没有 NO_FLICKER 的机器上 CLI 可能弹出「Try the new fullscreen renderer?」，接受会写用户设置——它和其它对话框一样绝不自动回答。

### 4.2 `ccr/session.py`（重写）
- 每个 jobid 一个 `AgentLink`；会话 sid ↔ `ccr-<sid>` tmux 会话。会话 meta：
  `{"sid","title","jobid","cwd","launch":{model,permission_mode,skip_permissions,effort,api_key_id},"claude_session_id","claude_pid","state","status","mode","busy","dialog_kind","created_at","last_activity","error","link":"connecting|ready|down|ended","stats":{…}|null,"api_key_name","api_key_missing"}`（`ended` = 作业已结束，只能在新作业上续接；`stats` 见下；`launch.api_key_id` = 本地 `api_keys.json` 条目 id，null = 服务器默认；`api_key_name` 仅显示用，key 被删后保留最后已知名称并 `api_key_missing=true`；meta/sessions.json/SSE/日志里**从不出现明文 key**）；
  state ∈ `starting|trust|idle|busy|waiting|exited|error`（由 session.status、屏幕解析、pane 存活综合得出：有 dialog 且 kind=trust → trust；有 dialog → waiting；**session.status == waiting（即使屏幕没解析出对话框）→ waiting**；busy → busy；否则 idle）。
- **`meta.stats`（上下文 / 会话总花费 / token/s，`ccr/stats.py` 的 `StatsTracker`，只被动读取屏幕与 transcript，不发任何按键）**：
  `{"context_used","context_limit","context_pct","context_source":"status|transcript"|null,"context_approx":bool,"cost_usd","cost_text":"$0.0037"|null,"cost_source":"status"|null,"tok_s_live","tok_s_last","last_turn_tokens","last_turn_elapsed_s","updated_at"}`；什么都不知道时整个为 null。
  - 上下文：优先状态栏的 `已用/上限 (百分比)`（某帧解析不到——如对话框遮住状态栏——沿用最后一次的值）；后备 = 最近一条主链 assistant 的 `usage.input_tokens + cache_creation_input_tokens + cache_read_input_tokens`（跳过 isSidechain、`<synthetic>`；transcript reset 时清空），上限按模型推断：状态栏原文 / 当前模型名 / `/model` 面板里当前模型的描述 / 启动参数 `--model` / transcript 的 `message.model` 任一 `has_1m_hint` → 1,000,000，否则 200,000，`context_approx=true`（界面标"约"）。
  - 上下文窗口（transcript 后备时；用户报告 Opus 5.5 显示 107% = 214,632 / 200k，CLI 自己按 1M 算）按优先级取，来源写在 `context_limit_source`、模型显示名写在 `context_model`：① `context_cmd`：/context 的输出（transcript 或屏幕），按模型记下，同一 claude 进程里不随 /clear 重置，transcript 的模型名不在表里（网关）时按"运行 /context 时的模型"对上；② `model`：最新主链 assistant 的 `message.model` 查 `stats.MODEL_CONTEXT`（CLI 2.1.331 内置模型目录：Haiku 5.5、Sonnet 5/5.5、Opus 4.7 及以后、Fable、Mythos 原生 1M；Opus 4.6 及以前、Sonnet 4.x、Haiku 4.5、3.x 为 200k，带 `[1m]` 才是 1M）；`hint`：模型名 / 状态栏 / /model 面板 / 启动参数里有 `[1m]` 或大写 1M → 1M；`unknown_model`：模型名不在表里 → 1M，与 CLI 一致（`ik()`：已知但目录没给窗口的模型 200k，不认识的模型默认 1M）；③ `observed`：同一模型观测到的上下文超过推断的窗口 → 至少 1M，只升不降，换模型重新判断；④ `default`：200k，只在还没有任何带模型名的主链 assistant 条目时出现。推断的分母不让百分比超过 100%；状态栏里明确的"已用/上限"仍然最优先。**已知限制**：CLI 的 `CLAUDE_CODE_DISABLE_1M_CONTEXT`、`CLAUDE_CODE_MAX_CONTEXT_TOKENS`、账号 1M 额度被锁、context-1m beta 头我们都读不到（红线：不读、不改 claude 的环境），只能靠用户运行一次 /context 或状态栏里明确的上限纠正。
  - 总花费：只用状态栏 `$x`（Claude Code 自己算好交给 statusline 的会话花费，最权威）；没有就是 null，**绝不按价格自行估算**。
  - token/s：一轮 = 屏幕 busy 起到"不忙且输入框可见或出现 `✻ … for Ns`"为止；对话框画面（权限/提问/面板）只暂停、不结束本轮。spinner 每帧给 `(elapsed, tokens)`，elapsed 变小或箭头方向变化 = 新的一段（一次请求）。
    `tok_s_live`（生成中）= 当前单调段内 tokens 增量 ÷ 本地收到帧的单调时钟时间差，窗口约 4s（跨度 ≥ 0.5s 才出数）；计数回落（真机实测：新请求开始时 spinner 计数会先从上次的值往下动画到 0 再上升）或换段时记为 0；忙但 spinner 暂时不在屏幕上（实测长输出时会消失）最多保留 3s；不忙为 null。
    `tok_s_last`（上一轮平均）= 本轮各段最后看到的 tokens 之和 ÷ 对应 elapsed 之和（= spinner 显示的"该轮最终 tokens / 该轮用时"，含 thinking token 与首 token 等待；真机 stream_frames：211 tokens / 6s ≈ 35.2）；本轮 spinner 从没显示 tokens（很短的回复）时保留之前的值。spinner 的 tokens 按屏幕实际显示计（可能含 thinking）。
  - 推送：每帧更新 tracker，但只有值变化（忽略 updated_at）且距上次更新 ≥ `stats_interval`（1s）才写进 meta；不足 1s 时排一个定时器补发，保证最终值一定推出去。会话退出 / 重启时丢弃进行中的一轮（`tok_s_live`=null），上下文/花费保留最后已知值。
  - 恢复：`stats` 随 meta 写入 sessions.json；app 重启时校验后恢复最后已知值（`tok_s_live` 不恢复），存活会话随后由重新 watch 的首帧屏幕与 reset 的 transcript 重新计算；已退出会话显示最后已知值。
- 事件信封沿用 v1：`{"gseq","sid","ts","kind","data"}`：
  - `kind="transcript"`：`data={"type":"transcript","entries":[…],"reset":bool,"session_id"}`
  - `kind="screen"`：`data={"type":"screen","state":<parse_screen 结果>,"text":<原始屏幕>}`（每会话最多 4 次/秒；只保留最新一帧在缓冲里，回放时不重复旧帧）
  - `kind="subagent"`：`data={"type":"subagent","agent_id","gen","summary":{agent_id,tool_use_id,description,agent_type,model,resolved_model,background,prompt,status:"running|completed|failed|killed",started_at,last_activity,ended_at,duration_ms,tool_uses,last_action,result,summary,usage,entries,…},"entries":[本批新条目],"from":首条序号,"total":已缓存条目总数,"reset":bool}`（`gen` 变化 = 该子代理条目从头重建）。摘要由 `ccr/subagents.py` 汇总：meta.json + 主 transcript 里的 Agent/Task 调用、启动结果（`toolUseResult.agentId`）、`<task-notification>`（按 task-id / tool-use-id 关联；有通知以其 status 为准，之后又有新活动视为续跑；否则 meta.finished → completed；会话退出时仍 running 的记为 killed；**判为 running 时，若子代理 jsonl 最后一条是 `[Request interrupted by user…]`，或 meta `stoppedByUser:true` 且停止点之后没有新活动（assistant 条目或发给它的消息），则为 killed，被续跑恢复 running**——CLI 2.1.332 实测：子代理列表里按 x 停止写 `stoppedByUser` + `<status>killed</status>` 通知；Ctrl+X Ctrl+K / 空闲时 Ctrl+C 只写 `stoppedByUser`、`system agents_killed` 与一行纯文本「Background agent "X" was stopped by the user.」；Claude 调 TaskStop 时子代理末尾是中断标记，主会话正在回复时通知只以 `type:"attachment"`（`queued_command`、`commandMode:"task-notification"`）写入、没有 user 条目；Esc 中断主回复**不会**停止后台子代理）。主 transcript 另外转发 `attachment` 中 `queued_command` 且 `commandMode` 或 `origin.kind` 为 task-notification 的条目（node_agent `note_attachment`，精简为 type/uuid/parentUuid/timestamp/isSidechain 与 attachment 的 type/commandMode/origin.kind/prompt；排队的用户消息见下），前端渲染成「子代理「…」已终止」那一行。**回复中途被吸收的用户消息**：另外转发 `commandMode=prompt`、`origin.kind` 为 human / 缺省（isMeta 的不转发）/ peer 的 queued_command——回复进行中发出、在工具结果之后被这一轮中途吸收的消息（CLI 2.1.330–2.1.332 实测**只**写 attachment、不另写 user 条目，`source_uuid`/`delivery_id` 对应 `queue-operation remove absorbed_mid_turn` 的 commandUuid/deliveryId；命令运行中按 Ctrl+X Ctrl+S 时命令转后台、消息同样被吸收）。字段多带 `source_uuid`，peer 多带 `origin.name`；prompt 为字符串或 `[文字块, 图片块…]`（图片引用块路径为 `attachment.prompt[i]`，op_blob 可取回）。没被吸收的在本轮结束后写成普通 user 条目（`promptSource:"queued"`）；纯流式输出时按 Ctrl+X Ctrl+S 会截断回复（assistant 条目 stop_reason 为 null、不写中断标记）后同样写成这种 user 条目。前端：渲染成用户气泡、顶部标「回复中发送」，属于当前这一轮（不出「本轮改动」卡片、「改动」栏不另起一轮；之前那段助手文字不显示一轮结束的时间/复制），↑/↓ 历史、小格子、待出现对账照常算它；按 uuid、再按 source_uuid 去重（压缩后 CLI 会把 peer 消息再写一遍，uuid 不同、source_uuid 相同）；peer 消息（isMeta、正文是 `<agent-message from=…>`）显示为系统行「来自其它会话「名字」：…」、不进 ↑/↓ 历史。history.py 的历史预览同一口径（大图片换成「已省略」）。fake_tui：`/tool`、`/bg` 的工具结果之后吸收排队消息（屏幕 `❯ …`、transcript 只写 attachment，`/` 开头的不吸收）；Ctrl+X Ctrl+S 命令在跑 → 转后台并吸收，否则截断并作为新一轮发出；本轮结束后发出的写 `promptSource:"queued"`。测试 `tests/test_queued_prompt.py`、回放页第 14h 节。fixtures `subagent_stop/`；测试 `tests/test_subagents.py`；回放页第 14e 节。**回放**（events_since / `GET /events`）每个子代理只保留最新一条摘要事件且不带条目（`entries:[]`、`from=total`），客户端发现本地条目不全时 `GET /subagents/<id>` 补齐；主 transcript reset 时子代理状态一并清空重建。session meta 另有 `"subagents":{"running","total"}`。
  - `kind="app"`：`meta`、`notice`、`exit`、`model_list`（见下）
- 操作（全部线程安全，远端调用不持全局锁）：
  - `create(jobid, cwd, title=None, model=None, permission_mode=None, skip_permissions=False, effort=None, resume=None, api_key_id=None)`：确保 link ready → `ensure_keeper` → `start`（args 由 `build_claude_args`；选了 API key 时 start 另带 `api_key`/`base_url`，明文只在内存里直接交给 start 请求，不存到会话对象/meta）→ `watch on`。`api_key_id` 不存在 → 409 `api_key_missing`；指向"服务器默认"条目（`is_server_default`，只用于显示名称/查询用量）视同 null。
  - **API key 存储**（`ccr/api_keys.py`）：`~/.cc-remote-chat/api_keys.json`（目录 0700、文件 0600、原子写、`O_NOFOLLOW` 读）条目 `{id, name, key, base_url|null, created_at[, is_server_default:true]}`；id `^[a-z0-9][a-z0-9_-]{0,31}$`（`usage` 保留）；key `^[A-Za-z0-9._-]{8,256}$`；base_url 为空 = 用设置里的 `default_base_url`（config.json，空 = 没设置），也没有则沿用 settings.json 的 `ANTHROPIC_BASE_URL`（与以前一致）；名称唯一，(key, base_url) 唯一；`is_server_default` 最多一条（代表服务器 settings.json 里那个 key：只用于显示名称与查询用量，选"服务器默认"启动时依然不加 `--settings`）。对外只给 `{id,name,masked:"sk-ab12…cd34",base_url,is_server_default,created_at}`。
  - **claude 会话 → 上次用的 key**：`~/.cc-remote-chat/session_keys.json`（0600）`{claude_session_id: {api_key_id|null, api_key_name, updated_at}}`；每次 session/transcript 事件拿到（或换了，如 `/clear`）claude_session_id 时记录当前 `launch.api_key_id`（null = 服务器默认也要记；没有记录 ≠ 服务器默认）。"从历史续接"据此默认选中同一个 key（§4.3 history 的 `api_key` 字段）。
  - **用量**（`ccr/usage.py`）：本地后端直接 `GET <base_url>/v1/usage`（`Authorization: Bearer <key>`，`User-Agent: claude-cli/<ver> (external, cli)`；base_url 为空的条目依次用设置里的 `default_base_url`、服务器默认条目的地址，都没有则不查询；`default_base_url` 变了缓存失效），超时 10s；7 天用量 = `daily_usage` 中 date ≥ 今天−6 天的 `actual_cost`（缺失用 `cost`）之和，"今天" = daily_usage 最大日期与本地日期的较大者；另给今日用量与 `remaining`。有的网关的版本守卫：403 `claude_code_version_mismatch` → 用 `required_version` 重试一次并缓存版本。每个 id 缓存 5 分钟（key/地址变了失效）；失败只影响显示；错误信息里出现明文 key 会被换成掩码。
  - `send_message(sid, text)`：有对话框、state=trust 或 session.status==waiting 时拒绝（409 `dialog_open`；屏幕无对话框而 status 仍是 waiting 时先用 `list` 读一次最新 status，排除事件滞后）。否则：`paste(text, enter=False)` → 抓屏确认文字已进输入框（忽略空白/折行；大段粘贴被折叠成 `[Pasted text #…]` 也算）、没有对话框、status 不是 waiting；以 `/` 开头时补全列表**没出现或高亮项正好是第一个词**（否则 409 `slash_mismatch`）；连续两帧一致后，单独发 `Enter` 并带 guard `{input_box, input_nonempty, popup}`（agent 发之前再复核，不通过 409 `screen_changed`）。任何一步不满足都**不按 Enter**，文字留在输入框里（粘贴后对话框弹出 → 409 `dialog_open`；超时未确认 → 504 `paste_unconfirmed`）。`open_model_picker` 的 `/model`、`apply_effort` 的 `/effort`、`close` 的 `/exit` 走同一流程。
    **带图片**（`send_message(sid, text, clear_input=False, expect_images=None)`）：`expect_images` = 前端此刻图片条上显示的 `[Image #N]` 编号；给了就必须与最新屏幕的 `input.images` 完全一致，否则 409 `images_changed`（不发用户没看到的图片，也不漏发）。claude 输入框里**只有**图片占位（用户 ⌘V / 拖入的）时不清空：文字前补一个空格粘贴，确认时要求图片占位原样还在、其余部分是刚粘贴的文字，再带同样的 guard 按 Enter；`text` 为空 = 只发图片（连续两帧"输入框里正好是这些图片"后，Enter 带 guard `{input_box, input_images_only}`）；有图片时发 `/` 命令 → 409 `images_with_command`（会被当成普通文字）。输入框里图片 + 其它文字 → 照旧 409 `input_not_empty`，说明里写明"含 N 张图片，确认后会连同图片一起清空"，只有用户确认才按 Ctrl+U。纯文字消息（没有图片）的流程与以前逐字相同。
  - **图片**（只对本机会话，`jobid=local`；服务器会话 409 `not_local`：远端 claude 读不到这台 Mac 的剪贴板 / 文件）：与终端里的操作完全等价，**不另存文件、不读写剪贴板**。都持会话操作锁、先核对最新屏幕（没有对话框 / 信任框、输入框可见、焦点不在子代理列表上，否则 409 `dialog_open` / `no_input` / `agents_focus`）。
    - `paste_image(sid)`：用户在输入框按 ⌘V 且粘贴内容里有图片 → 发 `C-v`（guard `{input_box}`）= 用户在终端里按 Ctrl+V，claude 自己读剪贴板；等输入框里出现新的 `[Image #N]`（`image_timeout` 8 秒），返回 `{images, added}`；没出现 → 504 `image_not_found`（剪贴板里可能已经不是图片）。
    - `attach_paths(sid, paths)`：原生窗口拖入的本机文件路径（最多 10 个）。逐个 `ccr.attach.check_image_path`：绝对路径、无控制字符（换行会把一次粘贴拆开）、扩展名 png/jpg/jpeg/gif/webp、普通文件（跟随软链接）、非空且 ≤100MB、开头 16 字节魔数是 png/jpeg/gif/webp（不合格 400，什么都不发）；然后每个路径按终端拖放的写法转义（`escape_drop_path`：安全字符原样，其余 ASCII 前加 `\`，非 ASCII 不转义）**单独粘贴一次**（agent 的 `paste` 带 guard `{input_box}`：粘贴前就地重抓屏复核，对话框弹出就一个字都不粘），等 claude 把它变成新的 `[Image #N]` 再粘下一个；没变 → 409 `path_not_image`（路径文字留在输入框里，与终端一致）。返回 `{images, added, files:{"N": token}}`：每个识别成图片的路径登记进**会话级白名单** `drop_files`（token = 24 字符随机串 → `{n, path, (st_dev, st_ino)}`，身份取自校验时打开的那个文件描述符；只在内存里，每个会话最多 32 个、先进先出），供缩略图 / 灯箱只读（见 `read_drop_image`）。
    - `clear_images(sid)`：用户点图片条的「清除」：输入框里只有图片占位时发 `C-u`（guard `{input_box, input_images_only}`），直到没有图片；还有别的文字 → 409 `input_not_only_images`，不动。
    - `remove_image(sid, n, expect)`：用户点图片条上某张的 ×，**只删这一张**（其它图片和输入框里的文字不动）。`expect` = 前端此刻显示的编号列表，与最新屏幕不一致 / `n` 不在里面 → 409 `images_changed`，同一编号出现两次 → 409 `image_ambiguous`，都是一个键都不按；对话框 / 输入框不可见 / 焦点在子代理列表 → 同 `paste_image`。步骤（每步都先读光标 + 屏幕 `capture cursor:true`，任何一步对不上就停下返回错误，不连续乱按）：① 只用 ← / →（`caret_moves`，每批最多 40 个，guard `{input_box, input_images:expect}`）把光标挪到 `[Image #n]` 后面，每批之后等光标连续两次读到同一位置再算下一批，按了没动 / 12 轮还没到 → 409 `cursor_unknown`（终端没报告光标、光标不在输入框、占位被折行拆开都是这个）；② 退格一次，guard `{input_box, input_images:expect, cursor_image:n}`；③ 等屏幕上的图片变化，必须正好少了 n（否则 409 `remove_unconfirmed`，停下）；④ 整理（尽力而为，失败只在返回里带 `warning`）：光标前后都是空格 / 光标前是空格且在末尾 → 再退格一次（guard `cursor_space`）；在最开头且后面是空格 → → 一次再退格；然后只用 → 把光标挪回输入框末尾（之后的拖入 / 发送都接在末尾）。删完输入框就空了 → 不做④（空输入框里 CLI 可能画灰色建议文字，→ 会把它接受进来）。按键白名单不变（← → 退格本来就有，没有加 End/Home）。返回 `{images, removed, warning?}`，并作废 n 的 `drop_files`。
    - `read_drop_image(sid, token)`：缩略图 / 灯箱读拖入的图片。只认本会话 `drop_files` 里的 token（格式不对 / 不存在 / 已作废 → 404），`ccr.attach.read_image_file`：打开（跟随软链接，与拖入时一致）后 `fstat` 必须是普通文件且 `(st_dev, st_ino)` 与登记时相同（被删、被换成别的文件或指向别处的软链接 → 404）、≤50MB（413）、开头魔数是 png/jpeg/gif/webp（415），Content-Type 按魔数给。白名单作废：claude 退出（编号随进程重来）、`clear_images`、`remove_image`（那一张）、`send_message` 成功之后；会话删除时随会话对象消失。**不是**任意文件读取：只能读本程序刚经 `attach_paths` 校验并粘进 claude 输入框的那几个文件。
  - `send_keys(sid, keys)`：白名单透传（终端面板/对话框按钮用）。
  - `choose(sid, index, text=None, expect=None)`：针对当前对话框（重新解析最新屏幕）把光标从当前选中项移到 index（Up/Down）再 Enter；信任框同理。**只由用户点击触发**。`expect` = 前端渲染卡片时的对话框指纹 `{kind,title,question,preview,labels}`（空白差异忽略，"Type something." 自由输入项的 label 不比）：导航前、导航每一步、按 Enter 前都与最新屏幕比对，不一致 → 409 `dialog_changed`（一个键都不按）；Enter 还带 guard `tail`（对话框区域 sha1），对话框在最后一刻被换掉也不会按下去。
  - `interrupt(sid)`：Escape。
  - `cycle_mode(sid)`：BTab（带 guard `input_box`：Shift+Tab 在权限框里另有含义）。
  - `open_model_picker(sid)`：空闲且无对话框时输入 `/model` 再 Enter（与实测一致：先出现 slash 补全，Enter 打开面板），等待 dialog.kind=model；然后用 ↑ 滚到顶、↓ 逐行收集完整列表，最后回到原光标行；发 `app/model_list` 事件 `{"rows":[{index,n,name,desc,current}],"cursor":index,"effort":{...}}`。面板保持打开。
  - `apply_model(sid, index, effort=None, scope="session"|"save")`：面板须打开；导航到 index；若给定 effort，用 ←/→ 调整并每步重新解析直到 level 匹配（最多 8 步）；最后按**面板提示行**决定确认键（§0.6，按键带 guard `lines:[提示行]`）。返回解析到的结果（屏幕出现 `Set model to …`/`Kept model …`）；按下后面板没在 5s 内关闭 → 504 `panel_timeout`（不再报成功）。
  - `apply_effort(sid, level, scope)`：输入 `/effort` Enter，等 kind=effort，←/→ 调到 level，按提示行决定 `s`/Enter（max 档：仅本会话 = Enter；scope=save → 409 `save_unsupported`，前端在 max 档提前置灰「保存为默认」）；超时 504。
  - **以 CLI 回报为准**（CLI 2.1.332 实测）：`last_result` 取 `/model`、`/effort` 回显后第一条提到 effort 或 model 的 `⎿` 行，`cmd_result = tui_parse.parse_cmd_result(last_result)` = `{text, model, effort, effort_locked{level, source, wanted}}`。屏幕上的结果文案没有反引号（transcript 里有）：`Set model to Haiku 5.5 for this session only[ with high effort]`；effort 被环境变量锁定时 `/effort` 按 s 得到 `CLAUDE_CODE_EFFORT_LEVEL=max overrides this session — clear it and xhigh takes over`，`/model` 得到 `Set model to … for this session only · effort stays max (set by CLAUDE_CODE_EFFORT_LEVEL); xhigh isn't saved`（锁定时 /effort 面板和正常 max 档外观相同）。模型名只取名字本身（`clean_model_name`，恢复 sessions.json 时也清洗旧版被整句污染的 `model` / `seen_base.model`，effort 不是五档之一则置空）。`apply_effort`：锁定 → 409 `effort_env_locked` + 中文说明（选的正是锁定档位 → ok + warning），`s.effort` = 实际档位；结果认不出 → ok + `code:"result_unknown"`、「结果未知，请查看终端面板」，**不改** `s.effort`。`apply_model`：换成功但 effort 被锁 → ok + `code/warning`。meta `effort_locked {level, source}` 只从 CLI 文案推断（红线：不读 claude 的环境），重启时清空、不持久化；用户自己在终端里输 `/effort` 不设锁定标记。面板关了而结果行晚一帧出现时 `_confirm_panel` 再等 `result_settle`（1.5s）。
  - `cancel_dialog(sid)`：Escape。
  - `close(sid, force=False)`：忙则先 Escape；对话框打开则 Escape；然后提交 `/exit`（同 send_message 流程，补全高亮不是 `/exit` 就不按 Enter、直接 kill）；等 `exited` 最多 15s，否则 `kill`；该作业无剩余会话时 `stop_keeper` 并**关闭该作业的 link**（ccr-agent step 随之结束；之后再操作会新建 link）。
    与计算节点连不上时：先同步 `check_job()`（squeue）——作业已结束 → 本地标为 exited 即可；作业还在/查不到 → 409 `link_down`（`force=True` 时只在本地标记，远端可能残留）。
  - `restart(sid, jobid=None, force=False, api_key_id=<沿用>)`：关闭旧会话（同 close：`/exit` 优雅退出，失败再 kill；不删除记录）→ 在（新）作业 `start` 并带 `--resume <claude_session_id>`（无 claude_session_id 时普通启动），沿用 launch 参数。旧作业已结束时不需要连上旧作业；不给新 jobid → 409 `job_ended`。同一作业上重启保留 link/keeper；换到新作业后旧作业若已无会话则停 keeper、关 link。
  - **阶段进度 `meta.op`**（`ccr/opprogress.py` / `static/opprogress.js`，用户需求：点重启 / 续接后不知道后台有没有在做、连点还报错）：只在内存、不落盘，随 meta 推送。`{id, kind: create|restart|reconnect|resume_new_job|switch_api|mode, state: running|done|failed|trust|stalled, phase, phases:[{id,label,slow_after,slow_hint,state,t0,t1}], started_at, phase_started_at, finished_at, detail, error, error_code, jobid, local, retries}`。阶段：stop（/exit，最多 15 秒）→ probe（服务器、新建连接时：ssh squeue）→ connect（srun --overlap 起 agent / 本机 agent；连不上时 detail 写第 n 次失败与原因）→ keeper → start（参数不变）→ ready（出现输入框或对话框 = done；信任框 = trust，结束进度并提示用户确认，**绝不代答**；90 秒内未就绪 = stalled，建议打开终端面板）。同一作业上重启、连接已就绪时跳过 probe / connect；本机会话没有 probe，文字里不出现 ssh / srun。失败时 `error` 是一句中文原因（例如「srun 无法连接作业 123456：作业已结束（请在新作业上续接）」「连接作业 J 超时（已等 90 秒）：ssh 连接 gpu-cluster 超时」，不含原始 stderr），作业已结束时 `error_code=job_ended`。done 显示 3 秒后清除；failed / stalled 保留到用户关闭或下一次操作。`job_ended` / `api_key_missing` / `link_down` / `target_mismatch` 这些由前端接着弹选择框的情况直接收起进度。
  - **幂等**：同一会话有 running 的 op 时，restart 不再执行，直接返回当前 meta 并带 `op_reused:true`（HTTP 200）。以前前端的 restarting 标记在 HTTP 返回时就清除，而后台启动线程仍持有会话操作锁（连接最多 90 秒 + keeper 最多 120 秒 + 启动 60 秒），第二次点击会在 `oplock.acquire(timeout=60)` 上等 60 秒后 409 `op_in_progress`（或把刚启动的 claude 再重启一遍）。前端 OpGuard 在请求未返回或 op 进行中时不发请求，并禁用重启、换 API、改 jobid、关闭按钮。
  - **换作业续接**：旧作业的 stop_keeper / 关闭 link 放到后台线程，不阻塞续接；`_wait_link` 发现作业在已结束集合里立即报 job_ended（以前要等 90 秒超时）。**用户报告「点在新作业上续接直接卡住」的根因在前端**：jobid 输入框预填的是会话自己已结束的旧作业，用户直接确认时 `if (!j || j === s.meta.jobid) return;` 静默返回、不发请求。现在作业已结束时预填设置里的当前作业，填回已结束的作业在框里说明原因；已退出的会话一步即可续接。
    - `api_key_id`：不传 = 沿用 `launch.api_key_id`；null/"" = 改用服务器默认；id = 改用该 key。**key 只在 claude 启动时读取，"换 API" 就是这个续接重启**（本地 sid、tmux 会话名、claude_session_id 都不变，`--resume` 续写同一个 transcript jsonl）。所用 key 已被删除 → 在关闭旧会话**之前**就 409 `api_key_missing`（"该会话使用的 API key「…」已被删除，请改选…"），旧会话不受影响。
    - **保留会话里改过的模型/effort/权限模式**（所有 restart 都适用，含换作业、换 API、已退出后重连）：记录本次启动后屏幕上第一次看到的模型/effort/模式作为基线（横幅/状态栏/模式行；随 sessions.json 持久化），重启时当前值与基线不同（或没有基线）就把当前值作为 `--model`（屏幕名 → `/model` 面板描述里的 `(claude-…)` 完整 id，或 opus/sonnet/haiku[1m] 别名；识别不了就沿用 launch.model）、`--effort`、`--permission-mode`（`--dangerously-skip-permissions` 启动的会话不动模式）传入，都在 §0.2 白名单内；没改过则完全沿用原 launch。新 launch 写回 `meta.launch`。
  - `remove(sid, force=False)`：close + 删除记录（连不上且作业仍在 → 409 `link_down`，前端提供"强制删除"）。
  - **作业结束**：link 报 `ended` → 该作业所有存活会话标 exited（notice"作业 J 已结束…可在新作业上用 --resume 续接"），meta.link=`ended`，link 关闭并移除（不再重连）；之后同一 Claude 会话可在新作业上续接（`_check_unique_resume` 不再拦）。
  - **exited 事件**：启动中忽略；`pane_pid` 与当前代际不符丢弃；其余先持操作锁用 `list` 核实 pane 确实已死/会话已不在，才标 exited、kill 空壳、必要时 stop_keeper；pane 还活着 = 误报，恢复 watch（绝不 kill 活着的 pane）；list 失败则保持会话、稍后恢复 watch。另：link 回调带 link 身份，已关闭/替换的旧 link 的迟到回调一律丢弃。
  - 持久化 `~/.cc-remote-chat/sessions.json`（原子写）；app 重启后：按作业建 link，`list` 匹配 `ccr-<sid>`：存活的直接重新 `watch`（claude 从未中断），不存在的标 exited（可"重连"= `--resume`）。
  - app 退出（`shutdown`）：**不关闭**远端 claude（keeper 保活），只关闭 link。
- 已删除：所有 headless/stream-json/control_request/permission_resolved 逻辑与 v1 的 cost 计算（v2 的花费只读 Claude Code 状态栏，见 `meta.stats`）。

### 4.3 API 变化（`ccr/server.py`，认证/安全不变）
保留：bootstrap（返回 meta 列表、gseq、instance_id、config）、stream(SSE)、`GET /events`、create（字段见上）、message、interrupt、restart、close、DELETE、PATCH 改名、config、jobs、dirs、history、login。
新增：
- `POST /api/sessions/<sid>/keys` `{"keys":[...]}`
- `POST /api/sessions/<sid>/choose` `{"index":int,"text"?:str,"expect"?:{kind,title,question,preview,labels}}`（前端总是带 `expect`；不一致 409 `dialog_changed`）
- `POST /api/sessions/<sid>/close`、`/restart` 可带 `"force":true`（409 `link_down` 后用户确认）；`DELETE ?force=1` 同理。作业已结束时 restart 不带新 jobid → 409 `job_ended`。
- `POST /api/sessions/<sid>/cancel_dialog`
- `POST /api/sessions/<sid>/mode/cycle`
- `POST /api/sessions/<sid>/model_picker` → 打开并返回 `{"rows","cursor","effort"}`
- `POST /api/sessions/<sid>/model` `{"index":int,"effort":str|null,"scope":"session|save"}`
- `POST /api/sessions/<sid>/effort` `{"level":str,"scope":"session|save"}`
- `GET /api/sessions/<sid>/screen` → `{"text","state"}`（最新一帧）
- 图片（§4.2「图片」，只对本机会话）：`POST /api/sessions/<sid>/image/paste`（⌘V → Ctrl+V）→ `{ok, images, added}`；`POST /api/sessions/<sid>/image/paths` `{"paths":[绝对路径…]}`（原生窗口拖入）→ `{ok, images, added}`；`POST /api/sessions/<sid>/image/clear` → `{ok, images:[]}`；`POST /api/sessions/<sid>/image/remove` `{"n":N,"expect":[N…]}`（图片条上的 ×，只删这一张）→ `{ok, images, removed[, warning]}`；`GET /api/sessions/<sid>/image/file/<token>`（拖入图片的缩略图 / 灯箱原图，token 来自 `image/paths` 返回的 `files`）→ 图片字节，`Content-Type` 按文件头、`Cache-Control: no-store`、`nosniff`、CSP `default-src 'none'; sandbox`，404/413/415 见 §4.2 `read_drop_image`。`POST /message` 可带 `"expect_images":[N…]`（有它时 `text` 可以为空）；v1 的 `images`（随消息上传图片）仍 400。手机远程口（私有版）禁止 `image/paste`、`image/paths`（手机上的图片到不了 Mac）与 `image/file/*`（读的是 Mac 上的本机文件）；`image/remove` 允许。
- 可视化（§10）：`GET /api/sessions/<sid>/blob/<ref>`（图片字节）、`GET /api/sessions/<sid>/local/info`、`POST /api/sessions/<sid>/local/stat`、`POST /api/sessions/<sid>/local/open`；不需要 token 的只读预览地址 `GET|HEAD /preview/<cap>/<绝对路径>`
- `GET /api/sessions/<sid>/subagents` → `{"subagents":[摘要…]}`；`GET /api/sessions/<sid>/subagents/<agent_id>` → `{"agent_id","gen","summary","meta","entries":[该子代理全部已缓存条目],"from","total","truncated"}`（每个子代理最多缓存 2000 条 / 8MB，超出从前面丢弃，`from`>0）。会话或子代理不存在 → 404。
- **API key**（认证/错误格式同上；**任何响应都不含明文 key**，校验错误不回显 key）：
  - `GET /api/api_keys` → `{"keys":[{id,name,masked,base_url,is_server_default,created_at,in_use}]}`（`in_use` = 引用它的本地会话数，含已退出的）
  - `POST /api/api_keys` `{name,key,base_url?,is_server_default?}` → `{"api_key":{…}}`；重复 409 `duplicate`，第二条服务器默认 409 `server_default_exists`
  - `PATCH /api/api_keys/<id>` `{name?,base_url?}`（base_url null/"" = 改回默认地址；key 本身不可改）
  - `DELETE /api/api_keys/<id>`：有会话在用 → 409 `api_key_in_use`（列出会话；已在运行的 claude 不受影响，重启/续接时需改选）；`?force=1` 确认后删除
  - `GET /api/api_keys/usage[?refresh=1]` → `{"usage":{id:{week_usd,today_usd,remaining_usd,unit,fetched_at,error,base_url}}}`（缓存 5 分钟，过期的自动重查，refresh=1 强制）
  - `POST /api/sessions` 可带 `api_key_id`（缺省/null = 服务器默认，与以前完全一致）；`POST /api/sessions/<sid>/restart` 可带 `api_key_id`（不带 = 沿用；null/"" = 服务器默认；所用 key 已删 → 409 `api_key_missing`）。
  - `GET /api/history` 的每项若在 `session_keys.json` 有记录则附 `"api_key":{"id":id|null,"name","missing":bool}`（id=null = 上次用的是服务器默认；没有记录则无此字段）。
删除：permission、mode(set)、model(set_model) 旧接口。

## 5. fake 测试模式
一个**假的交互式 claude**（Python，用 ANSI/直接 print 画屏即可，不必 curses），在本机 tmux 里运行，读键盘（tty raw 模式），**画面文字尽量逐字复刻 fixtures**：启动时信任框（home 目录或 `CCR_FAKE_TRUST=1` 时出现）→ 输入框 + 状态栏 + 模式行；收到消息后显示 spinner 与 `esc to interrupt`，逐行"流式"输出回复（关键词：`/tool` 触发 Bash 权限框、`/write` 触发 Write 权限框、`/ask` 触发 AskUserQuestion、`/long` 慢速长输出可被 Esc 中断、`/md` 输出 markdown、`/agent` 仿真实格式派一个后台子代理：Agent tool_use →"Async agent launched…agentId: …"结果 → `<sessionId>/subagents/agent-<id>.meta.json` 与逐步写入的 `agent-<id>.jsonl`（3 次工具调用，夹杂 api-request*/attachment 行）→ meta.finished=true + `<task-notification>` 用户条目（`origin.kind=task-notification`、`promptSource=system`），屏幕底部显示子代理列表行；Esc 中断时子代理以 killed 结束）；`/model` 打开模型面板（6 个模型、可视窗口 4 行、↑/↓ 滚动、←/→ 调 effort、s/Enter/Esc）；`/effort` 面板；`/exit` 退出；Shift+Tab 循环模式行。写 `$HOME/.claude/sessions/<pid>.json`（status idle/busy/waiting）与 `$HOME/.claude/projects/<slug>/<sessionId>.jsonl`（完成消息后追加 user/assistant 条目，格式仿 `fixtures/transcript_interactive_59dd443a.jsonl`）。`--resume <uuid>` 时沿用该 id 并接着写。`HOME`/`TMUX_TMPDIR` 由测试/fake 模式提供的临时目录。
状态栏与真机同格式 `  user@host:~/proj ▱▱▱▱▱▱▱▱▱▱ 0% 0/1M $0.0000 [Haiku 5.5 · 1M · effort:low]`（每轮完成后上下文/花费增加），spinner 3s 后显示 `(Ns · ↓ N tokens · thinking with low effort)`（≥1000 写成 `1.2k`）；assistant 条目的 usage 与其上下文计数一致；`CCR_FAKE_STATUSLINE=0` 不画状态栏（测试统计的 transcript 后备，经 `cfg["fake_env"]` 传入）。
`--settings <文件>`：读取 `env.ANTHROPIC_AUTH_TOKEN`（及可选 `ANTHROPIC_BASE_URL`），在启动横幅下多显示一行 `API key: sk-test…1111 [@ <base_url>] (--settings)`（fake 专用，供端到端确认 claude 拿到了 `--settings`）；文件不存在/没有 token → 像真 CLI 一样报错退出。

- 子代理停止的模拟（仅 fake）：Ctrl+X Ctrl+K 两次 = 用户停止后台子代理；消息里含 `/agentstop` = Claude 调 TaskStop。
- 图片（仿 §1「图片」实测）：一次 bracketed paste 的整段是图片路径（单个原样路径，或 `shlex` 能拆开的多个转义 / 加引号路径，每个都是存在的图片文件）→ 输入框末尾加 `[Image #N]`（进程内递增、紧跟另一张图时补空格）；路径 + 文字、换行分隔 → 普通文字。`C-v` = 读剪贴板图片：**fake 绝不读真实剪贴板**，生成一张示意 PNG；`CCR_FAKE_CLIPBOARD=none` 时什么都不发生（模拟剪贴板里没有图片）。退格整块删占位。输入框有插入点（仿真机）：← / → 在 `[Image #N]` 上整块跳，退格删光标前的一个字符或整块占位（两边空格留着），打字 / 粘贴 / 新图片插在光标处（插在中间不补空格），Ctrl+U 删到本行行首；每次重画后把隐藏的硬件光标停在插入点（`ESC[行;列H`，tmux `#{cursor_x}/#{cursor_y}` 读得到）。直接给 `input` 赋值 = 光标放到末尾（与以前一样）。提交时回显下每张一行 `⎿  [Image #N]`，transcript 写 `[文字块, 图片块…]` + `imagePasteIds` 与 isMeta 的 `[Image: source: 路径]`（Ctrl+V 的没有）；回复首行带「（含 N 张图片）」。
- 进度测试钩子（仅 fake，真实模式不读）：`app.py --fake --fake-delays probe=…,connect=…,keeper=…,boot=…,unreachable=J1+J2`（隐藏参数，各阶段人为延迟或让某作业连不上）；`<数据目录>/fake/ended_jobs` 里列出的作业视为已结束；`CCR_FAKE_BOOT` 让假 claude 延迟出第一帧。测试：`tests/test_opprogress.py`（阶段顺序、本机与服务器、失败文字、并发只执行一次、信任框、stalled、旧作业已结束时续接不挂起）。

## 6. fixtures
- `fixtures/tui_screens_stream.ndjson`：`{"name","t","plain"[, "esc"]}` 每行一个画面；`stream_frames` 一行含 29 帧流式过程 `frames:[{t,s,status}]`；包含 trust、ready、after_stream、ask、ask_down、pasted、busy_count、interrupted 等（注意本次录制中 perm/model 画面因时序不准不可用）。
- `fixtures/tui_screens_dialogs.ndjson`：ready、slash_popup、model、model_right、model_down、model_after_esc、effort、effort_after_esc、perm（Bash）、perm_after_esc、perm_write（Write）、perm_write_after_3、final；含 `sess`（sessions json 实测）。
- `fixtures/transcript_interactive_59dd443a.jsonl`：交互模式 transcript。
- `fixtures/diff_panel/*.txt`：右侧 diff 面板真机画面（CLI v2.1.331，fullscreen，200×50 及 260/143/120/109 宽；路径已换成中性路径），含左栏权限框、流式输出、spinner、interrupted。
- `fixtures/image_paste/*.txt`：CLI 2.1.332 macOS 真机输入框里有图片的画面（只有图片、两张 + 文字、多行、忙时、只有图片的消息提交后、排队）与 `transcript_image.jsonl`（只保留 user / assistant 文本与图片块，路径换成 `/tmp/imgs/`、cwd `/tmp/proj`，状态栏用量数字换成中性值）；`fixtures/tui_screens_images.ndjson` 是其中 4 帧，供回放页第 1e 节使用。
- v1 的 stream-json fixtures 删除。

## 7. 前端—— Orca 风格保持不变，数据源改为 transcript + 屏幕
- 消息区：渲染 transcript entries（user/assistant/tool_use/tool_result/system，沿用 v1 历史渲染与 markdown 安全渲染），按 `uuid` 去重，`reset` 时整体重建。
- **实时草稿（逐字回显）**：当 `screen.state.busy` 或 `live` 非空时，在消息流底部显示"生成中"卡片：spinner 文本 + `live` 内容（等宽、弱化色、随每帧更新，自动滚动）；对应 assistant 条目到达后草稿隐藏（或空闲后隐藏）。
- **对话框卡片**（消息流底部、输入框上方）：
  - permission：工具名、描述、预览（等宽）、问题；每个选项一个按钮（`choose(index)`）；"Esc 取消"按钮。
  - question：header、问题、选项（label+desc）按钮；`Type something.` 选项点击后显示文本框，提交 = `choose(index)` 后 `paste(text, enter=true)`（后端提供 `POST /message` 即可，或前端先 choose 再 message）。
  - trust：路径 + 两个选项按钮（原文显示，含"home 目录仅本次有效"说明行）。
  - generic：原文行 + 选项按钮 + 键盘区（↑ ↓ ← → Enter Esc Tab）。
- **模型/effort**：输入框底栏的胶囊 `[✦ 模型 │ effort]` 显示当前模型与 effort（从 `status_line`/对话框解析，或来自 /model 面板的 current 行与 effort）；胶囊左半（切换模型）→ `POST /model_picker` → 弹窗：完整模型列表（name + desc，✔ 当前）、effort 分段选择（low/medium/high/xhigh/max）、按钮"仅本会话（s）"（主按钮）/"保存为默认（Enter）"/"取消（Esc）"→ `POST /model` 或 `cancel_dialog`。若屏幕出现 model 对话框而非由弹窗打开（用户自己输入 /model），也用同一弹窗呈现。胶囊右半（effort）→ 只调 effort 的弹窗 → `POST /effort`。两半各自 hover / title；已退出、有对话框、运行中、断线时都禁用（title 说明原因）。
  - effort 被锁定（meta `effort_locked`）：胶囊显示锁图标 + 实际档位、警示色，tooltip 写明原因与解决办法（清除环境变量后重启会话）；调整 effort 弹窗与切换模型弹窗都显示锁定提示；锁定失败显示「没有生效：…」并保持弹窗打开；effort 弹窗选 max（或模型弹窗提示行没有 `Enter to save`）时「保存为默认」置灰并说明原因。前端只接受 low/medium/high/xhigh/max 五档，提示（`appliedToast` / `resultText`）任何情况都不显示 null / undefined；`result_unknown` 显示「结果未知，请查看终端面板」。
- 权限模式：底栏模式 chip 显示 `mode`（中文名；英文 id 在 tooltip），点开模式列表（§13.3）；旁边「Shift+Tab」小按钮 → `mode/cycle`。
- **输入框底栏**（`static/composerbar.js` 的 `ComposerControls` + 统计条；参考 T3 Code 的 composer footer）：textarea 下方一行，固定 32px 高、`flex-wrap: nowrap`，从不换行（WebKit 没有滚动锚定，底栏高度一变时间线就跳）。左 = `[✦ 模型 │ effort] [🛡 模式 ▾] [Shift+Tab]`（统一 ghost 控件：28px 高、圆角 8px、透明底，hover / 菜单打开时填充，按下微缩，禁用变淡但仍显示 title）；右 = `◔ 3%  $0.04  35 tok/s` + 中断（busy 时）+ 发送。左侧控件组可收缩并裁切（模型名先省略号截断），右侧永远完整。模型/effort 部分与模式部分各有 key，内容不变不重建；模式列表打开时模式变了 → 在新 chip 上重新打开（内容刷新），换会话则关闭。运行中提示写进 textarea 的占位文字（先改占位文字再测高）。按底栏宽度用容器查询 `composer` 分级折叠：≤680px 中断只留图标、模型名最多 120px；≤600px 去 tok/s；≤520px 去花费、`Shift+Tab` 写成 `⇧Tab`、模式 chip 去下拉箭头；≤420px 只留上下文圆环、模式 chip 只留图标、模型名最多 72px。
- 会话头部：标题、状态、本机/job 徽章、cwd、API chip、后台 shell chip、错误 chip、中断/终端/重启/关闭按钮（头部按钮按容器查询 `chat-hd` ≤720px 只留图标）。
- **统计条**（`static/stats.js` 的 `StatsBar`，输入框底栏右侧、发送/中断按钮左边；只读，无任何按钮/请求）：数据 = `meta.stats`。上下文 = 16px SVG 圆环（viewBox 24、r=9.75、描边 3、旋转 -90°、`stroke-dasharray` = 周长、`stroke-dashoffset` 用 CSSOM 设置、0.5s ease-out 过渡，减弱动效时关闭）+ 百分比（>80% 警告色、≥95% 危险色，圆环与百分比同色；transcript 后备时前缀"约"；只有 token 数没有百分比时不画圆环、直接显示 `28.3k`），「上下文」与 `25k / 1M` 在 tooltip 第一行（`上下文已用 3% · 25k / 1M`，其后是来源说明）；transcript 后备时 tooltip 第 3 行写窗口来源（「窗口 1M：来自 /context（…）」「按模型 X 推断（Claude Code 2.1.331 内置模型目录）」「模型 X 不在内置模型目录里，按 Claude Code 的默认 1M 计」「模型名 / 状态栏 / /model 面板里有 [1m] 或 1M」「本会话 X 的上下文曾超过 200k，按 1M 计」「默认估算」，后几种附「运行一次 /context 可得到准确值」），百分比按 used / limit 的原始值显示；`$0.0037`（null → `—`，tooltip 说明"来自 Claude Code 状态栏、本程序不估价"）；忙时 `● 46 tok/s`（生成中；还没有 token 数时 `— tok/s`）/ 空闲时 `35 tok/s`（「上一轮」与口径 tokens ÷ 用时、含 thinking 在 tooltip）。数字等宽（tabular-nums + min-width），宽度不抖。该元素只建一次，boot 时挂进底栏的固定槽位 `#composer-stats`，之后原地更新、不随任何重建移动。折叠见上「输入框底栏」。颜色变量 `--stats-track/--stats-fill/--stats-live` 在浅色 `:root` 与深色 `:root[data-theme="dark"]` 都有定义。
- **终端面板**（可开关）：显示最新屏幕原文（等宽，`GET /screen` + screen 事件）+ 键盘区（↑ ↓ ← → Enter Esc Tab Shift+Tab 1–4 s y n Ctrl+C）→ `POST /keys`。用于任何未识别情况的兜底。
- 新建会话对话框：jobid、目录（浏览）、可选：模型（文本，带最近一次 model_list 的候选）、权限（默认=不加参数/各模式/跳过权限）、effort（默认/各档）、**API**、标题。
- **API key**（`static/apikeys.js`）：
  - 「API」下拉（新建、从历史续接、重启、换 API 共用）：`服务器默认（settings.json）`（有服务器默认条目时附其名称与用量）+ 已保存的 key（`名称 · 掩码 · base_url 或"默认地址" · 近7天 $x`）+「管理 API key…」。所选 key 已删除 → 显示"（已删除，请改选）"并拦住提交。
  - 管理弹窗：每行 名称、掩码、base_url（空 = "默认地址"）、「近 7 天 $12.34 · 今日 $0.01」（tooltip：余额、更新时间或失败原因）、在用会话数、编辑（名称 / base_url）、删除（在用时二次确认 force）；"刷新用量"按钮（`?refresh=1`）；新增 = 名称 + key（密码框，提交后清空）+ 可选 base_url + "这是服务器默认 key"勾选。打开时拉取用量（后端 5 分钟缓存，过期自动重查）。
  - （2.17）同一时刻只有一个真正的模态对话框：已有对话框时新对话框挂在最上层对话框里面、用非模态 `show()` 打开，下层内容 inert，Esc 只关最上层，下层关闭时先关嵌套的（`dialogs.js` `modal()`；朋友在 dmg 版 WKWebView 里叠放 showModal 时 base_url 等输入框打不进字，根因未能复现，按最可能原因处理）。管理弹窗的新增表单每个输入框一行加标签；key 输入框是文本框 + `-webkit-text-security: disc`（整个弹窗不再有 `type=password`，避开密码自动填充），base_url 的 placeholder 只是 `https://…`、「留空 = 用什么地址」写在框下；每条 key 有「修改地址」（打开编辑弹窗、焦点落在 base_url 并全选）。地址显示顺序：自己的 → `xxx（默认）`（设置里的默认 base_url）→ `默认地址（settings.json）`。设置弹窗有「默认 base_url（API key）」（保存按钮 / 回车 / 失焦保存，格式错误就地提示；`GET /api/api_keys` 返回 `default_base_url`，`POST /api/config` 接受 `default_base_url`，"" = 清除）。
  - 会话头 API chip：显示所用 key 名称（服务器默认 = 服务器默认条目名称或"默认 API"；已删除标出），tooltip 含 base_url 与近 7 天/今日/余额；点击 =「换 API 重启续接」：说明 claude 只在启动时读 key、会 `/exit` 后以 `--resume` 用新 key 续接同一段对话（几秒到半分钟，模型/effort/模式保留），改选后提示"换 key 后提示缓存可能失效，下一条消息会在新 key 上重新读取整段历史，可能更慢、更贵一点"；正在回复或有对话框时再弹确认"切换会中断当前这一轮回复/对话框，确定？"。重启遇 409 `api_key_missing` → 直接弹改选。
  - 左侧会话列表每项、历史列表每项（有记录时）显示小标签（key 名称；服务器默认 = "默认"或服务器默认条目名称；已删除用警告色）。
  - 从历史续接：API 下拉默认选中该 claude 会话上次用的 key（`history[].api_key`），旁边提示"该会话上次使用：xxx"；上次的 key 已删除 → "上次使用的 key「x」已删除，请改选"；没有记录才默认"服务器默认"。改选其它 key 允许，并显示上面的代价提示。
  - 颜色变量 `--api-chip-*` / `--api-tag-*`（浅色 `:root` 与深色 `:root[data-theme="dark"]` 都定义）。
- 输入框：Enter 发送（IME 组字不发送）、Shift+Enter 换行；有对话框时禁用并提示"请先处理上方对话框"；运行中占位文字为"运行中：新消息会进入 claude 的输入队列"；"中断（Esc）"按钮在 busy 时显示（底栏与头部各一个）。
- **输入框 ↑/↓ 历史**（`static/prompthist.js` + `AutoSize.caretEdges`；用户需求，行为参考终端里 claude 的 ↑/↓）：纯前端，只改输入框，不发请求、不按任何键。历史 = 当前会话时间线上的用户文字消息（`kind:'user'`，跳过被截断的、只有图片的；/ 命令、! shell、系统注入消息本来就不是 user 条目）+ 本页发出、transcript 里还没出现的消息（运行中排队的；发送前就记 `{text, n=发送前的消息条数}`、发送失败撤回，在 transcript 第 n 条之后对上即去掉，之后又来了 3 条仍没对上也去掉；/clear 后照样能翻到）；相邻重复只留一条。不带修饰键、不在输入法组字中、没有选中文字、slash 补全列表没打开时：光标在**第一个视觉行**（自动折行也算行，用 AutoSize 的镜像量“开头到光标所在词末尾”的高度；“词”只算拉丁字母这类，遇到中日韩文字就停——中文可在任意两字之间折行）按 ↑ → 上一条、光标放开头（连按继续往前；最早一条之后交给浏览器）；正在浏览且光标在**最后一个视觉行**按 ↓ → 下一条、光标放末尾，翻过最新一条恢复开始浏览前的草稿。开始浏览时对列表做快照；输入框里正好是最新一条时跳过它。改过调出来的内容 → 退出浏览、改后的文字就是新草稿（不丢字）。浏览状态挂在该会话的草稿上；发送后重置。输入框禁用（对话框 / 已退出）时不处理。
- **左侧 prompt 小格子**（`static/minimap.js` 的 `PromptMinimap`；用户需求，移植自 T3 Code 的 TimelineMinimap，MIT）：纯前端只读覆盖层，不发请求、不按任何键、不碰剪贴板。`#chat-host` 里只挂一个（`position:absolute`，宽 72px、`z-index: 4`：高于时间线与「最新」按钮，低于输入框弹层 / 抽屉 / 侧栏抽屉 / 弹窗 / 灯箱；自身 `pointer-events:none`），跟随当前会话与历史预览（`renderAll` 里 `setSession`），没有会话时隐藏，子代理抽屉里没有；不改变 `.chat-col` 的宽度 / 位置和时间线高度。条目 = 时间线上每条 `kind:'user'`（含只有图片的），少于 2 条不显示；条目列表只在 render.js 的 `state.onFlush` 钩子（`flushSession` / 「显示更早」/ `revealItem` 之后）里增量同步，条数变了才重建横条。外观：每格 2px 高、24px 宽、左对齐，`top = i/(n-1)·100%`，小格子竖向居中、离左边 12px，高度 `min((n-1)·8px, max(4rem, 100% − 9rem))`（T3 是 100% − 14.75rem，它的时间线区域含输入框，我们的不含；上下各留 4.5rem 放得下两端的预览卡和箭头）；默认 `scaleX(.333)`，悬停（或键盘高亮）那一格 1、相邻 .667、再远一格 .417（`data-d`），只动 transform / opacity（0.15s，减弱动效时无过渡）。一轮 = 本条用户消息到下一条之前，与视口相交的轮次 `data-in-view=true` 加深（视口只剩尾部草稿 / 对话框时算最后一轮）；滚动时 rAF 节流、二分查找已渲染条目的位置，值不变不写 DOM。颜色 `--pmap-strip / --pmap-strip-hot / --pmap-strip-inview / --pmap-card-bg / --pmap-card-border`（浅色 / 深色各一套）。预览卡（悬停 / 键盘高亮时）：小格子右侧 32px、宽 320px（按 `#chat-host` 收窄，不出右边、不撑出横向滚动）、以该格为中线、`pointer-events:none`；第一行 = 用户文字（一行省略号；只有图片 → 「图片消息（n 张）」），下面 = 该轮最后一段助手文字最多 3 行；都是纯文本（粗略去 markdown 记号，最多处理 1500 字）。点击 / Enter / 空格：目标在 `renderStart` 之前 → `revealItem` 先补渲染（与「显示更早」同法保持滚动位置），再把消息顶端滚到视口顶端下方 24px（平滑；减弱动效时瞬移），离开底部时先 `view.stick=false`，之后新内容照旧出「最新」按钮；mousedown 拦掉，不抢输入框焦点、不开始选文字。当前下标 = 视口（顶端 25px 以下）里第一条可见的用户消息，没有就是正在读的那一轮；上 / 下箭头（悬停或聚焦小格子时出现）= 当前下标 ±1，最前一条时「上一轮」禁用、已到底（差 ≤1px）时「下一轮」禁用。键盘：小格子是一个可聚焦按钮，聚焦时高亮当前那一格，↑ / ↓ / Home / End 移动高亮，Enter / 空格跳转，失焦清除（只处理自己的按键，不影响输入框 ↑/↓）。滚轮在小格子上 → 转发给时间线（`passive:false` + `preventDefault`；deltaMode 行 = 16px、页 = 时间线可视高度；触控板直接滚，普通滚轮 150ms 缓动，减弱动效时直接滚）。从别处按下开始的拖动（选文字）经过小格子 → 整个小格子 `pointer-events:none` 穿透，松开（或下一次不带按键的 mousemove）后恢复。显示条件：左侧留白 = `#chat-host` 左边到 `.chat-col` 内容起点（含 col 的左内边距与滚动条槽：那里本来就空着；不含的话 1000px 宽窗口留白为 0、小格子完全不能用）≥ 48px 时常驻；否则平时透明，只在 `(pointer: fine)` 下悬停 / 聚焦时 0.15s 淡入（非精确指针不显示）；命中宽 = clamp(留白 − 12, 0, 40)，为 0 时不可交互，< 14 时箭头不可点。`#chat-host` 尺寸变化（窗口缩放、侧栏开合、终端面板 / 右侧预览面板 / 子代理抽屉）由 ResizeObserver 重新测量。
- **重启 / 续接进度横幅**（`static/opprogress.js`）：位于输入框上方（替换「在新作业上续接」等提示条，点哪里就在哪里看到反馈，不随时间线滚动）：当前阶段 + 转圈 + 「第 n/m 步」+ 已用秒数（每秒原地更新）+ 已完成阶段打勾并标用时；阶段超过 `slow_after` 显示 `slow_hint`（如「比平时慢，仍在进行（gpu-cluster 的 Slurm 偶尔不稳）」）；失败显示原因与「重试」，作业已结束时按钮换成「在新作业上续接」。进行中时会话头重启按钮转圈并显示「正在重启…」/「正在续接…」，换 API、改 jobid、关闭一并禁用；时间线尾部的「正在启动 claude…」不再重复显示；HTTP 请求本身失败也写进横幅，不再弹 toast。
- **本地命令输出**：/context → `static/ctxcard.js` 卡片（总量 + 用量条：颜色取 CLI 的 ANSI 颜色、自动压缩预留在最右用斜纹；分类表；自动压缩窗口；小节，有分组的可展开；原文可展开：ANSI 上色、方块固定 1ch 宽对齐、灰色用 `--text-3`）；其它带 ANSI 的命令输出显示为上色的等宽原文（以前 `[38;5;245m` 之类控制码原样露出）。颜色变量 `--ctx-free` / `--ctx-buf` 浅色、深色都有定义。
- **排版细节**（参考 T3 Code，`style.css` / `render.js`）：消息悬停（或键盘聚焦）才显示「时间 + 复制原文」——用户消息在气泡左侧，助手只在一轮最后一段文字下方，绝对定位在条目间距里、不改变布局；相邻工具卡拼成一组（共用圆角外框、1px 分隔，组摘要即组标题）；输入框与消息列同宽同中线；「最新」按钮是输入框上方居中的半透明胶囊；实时追加的少量条目/新对话框/草稿 0.24s 淡入，草稿"工作中"状态文字有闪光；`prefers-reduced-motion` 下所有动画停住（`animation-iteration-count: 1`）。
- 去掉 v1 的：审批 control_request 卡、v1 的 cost 计算（v2 花费见统计条）、slash 自动补全（可保留静态列表）、v1 的图片附件上传（交互模式改为 claude 自己的 Ctrl+V / 粘贴图片路径，见下条）。
- **发图片（本机会话，`static/attach.js`；用户需求：截屏后不用先存文件再贴路径）**：做法 = 用户在终端里的操作，本程序不读写剪贴板、不另存文件。
  - ⌘V：输入框 `paste` 事件的 `clipboardData` 里有图片文件 → `POST /image/paste`（后端按 Ctrl+V，claude 自己读剪贴板）；只有图片时 `preventDefault`，同时有文字时文字照常粘进我们的输入框（只拦图片）。浏览器给的 File 只在本页内存里做一张缩略图（blob URL，屏幕上对应的 `[Image #N]` 消失时释放）。服务器会话 / 手机远程口 / 已退出 / 有对话框 / 断线：不发请求，toast 说明（服务器会话：「服务器会话暂不支持直接发图片：远端的 claude 读不到这台 Mac 的剪贴板（文字已照常粘贴）」）。
  - 拖放：`window` 上全局拦 `dragover` / `drop`（`dataTransfer.types` 含 `Files` 时 `preventDefault`，防止浏览器 / WKWebView 把文件当导航打开）。原生窗口（§14.6）把被拖入的本机文件路径以 `ccr-native-drag`（`phase: enter|leave`）/ `ccr-native-drop` 事件交给页面：图片 → `POST /image/paths`；其它文件 = 终端拖放：转义后的路径插到我们输入框的光标处（前后补空格）；服务器会话只提示。浏览器（以及原生窗口里没有文件路径的拖动，如从网页拖来的图片）拿不到路径：toast 提示改用 ⌘V（截屏时加按 Control 直接存进剪贴板）。拖动经过时 `.main` 上显示覆盖层（虚线框 + 卡片，`pointer-events:none`；可放 = 说明几张图片进 claude 输入框、几个文件以路径插入；不可放 = 警示色说明原因）。
  - 图片条（`ImageTray`，输入框 `composer-box` 顶部一行；用户需求「放进去图片之后能不能有个叉叉删除；既然是可视化，希望是小的预览图，也可以点击放大查看」）：数据 = 屏幕解析的 `input.images`（claude 输入框里此刻真正有的 `[Image #N]`）+ 进行中的图片操作。每张 = 56px 方形小缩略图（`object-fit: cover`、10px 圆角、左下角 `#N` 角标）：⌘V 的用浏览器 paste 事件给的图片（blob URL，只在本页内存里）；拖入的经 `GET image/file/<token>`（§4.3，带 token 头 fetch → blob URL；加载中转圈，读不到 / 太大显示图片图标）；页面刷新后 ⌘V 的没有数据了 → 图片图标 + tooltip 说明（不为此读剪贴板）。有缩略图的是按钮（`aria-label`「放大查看 [Image #N]」），点了用灯箱（§10.2）打开原图，图片条上有图的几张可 ←/→ 切换，拖入的另有「在 Finder 中显示」；右上角 ×（20px 圆钮，`aria-label`「删除 [Image #N]」，键盘可达）= `POST image/remove {n, expect: 图片条此刻的编号}`，只由点击触发、点 × 不会打开灯箱；删除进行中那一张变淡、说明换成「正在从 claude 的输入框里删掉 [Image #N]…」、× 暂时都不显示（一次一张）。服务器会话 / 已退出 / 对话框 / 断线 / 其它图片操作进行中没有 ×。说明「在 claude 的输入框里，发送时和文字一起发出」；进行中转圈「正在让 claude 读取剪贴板里的图片…」；输入框里还有别的文字时警示色说明「发送时会先确认清空（图片也会一起清掉）」（× 仍可用：后端按光标逐张删，文字不动）；「清除」= `POST /image/clear`（只由点击触发；有别的文字 / 进行中 / 已退出 / 对话框时不显示）。图片操作结束后马上 `GET /screen` 取一帧最新屏幕（推送的屏幕帧有节流，图片条不闪回旧状态）；缩略图在屏幕上出现过、之后又没了才释放（刚加进来、屏幕帧还没到的保留最多 10 秒）。很多张时横向滚动，不换行、不撑宽；内容不变不重建；输入框只多出这一行的高度（约 66px），textarea 的 autosize 不受影响。
  - 发送：图片操作进行中时先等它结束并 `GET /screen` 取一帧最新屏幕（等待期间再按回车不重复发）；总是带 `expect_images`（图片条上此刻的编号，可以是空数组：屏幕上冒出用户没看到的图片时后端 409 `images_changed`，不会把它一起发出去），有图片时文本框为空也能发（只发图片）；只有图片的消息不进 ↑/↓ 历史。409 `images_changed` 等错误照常 toast、文字放回输入框。
  - 时间线：带图片的用户消息里 CLI 把开头的 `[Image #1] [Image #2] ` 也写进了文字，图片已在气泡里显示，渲染时去掉这串开头的占位（↑/↓ 历史、小格子预览、待出现消息的对账都按去掉后的文字）；isMeta 的 `[Image: source: …]` 不显示。
  - 颜色只用现有变量（`--bg-subtle`、`--border-strong`、`--text-2/3`、`--accent(-soft)`、`--warn`），浅色 / 深色同一套写法（回放页第 1e 节量对比度）。
- 对话框卡片的选项按钮 `choose` 总是带卡片渲染时的指纹 `expect`；409 `dialog_changed` → toast"对话框已变化，请重新确认"并立即 `GET /screen` 刷新卡片。
- 作业已结束（meta.link=`ended`）：横幅"作业 J 已结束，可在新作业上续接"+"在新作业上续接"按钮（= 改 jobid 续接），头部"重连"变成"换作业续接"，不再显示"正在自动重连…"。
- 关闭/重启遇 409 `link_down` → 确认框"强制关闭/强制重启"（带 `force:true`）；删除遇 409 → 确认框"强制删除"（`DELETE ?force=1`）。meta.state=waiting 但画面没识别出对话框时，横幅引导打开终端面板。
- **子代理**（`static/subagents.js`）：输入框上方的托盘**只列出正在运行的子代理**（状态图标、description、agentType · model、实时走秒、工具调用次数、最近一个动作）；子代理完成/失败/终止后立即从托盘移除，没有运行中的子代理时托盘隐藏（用户要求）；点击任一项（或 Agent 卡上的"查看过程"、结束通知行）打开右侧抽屉：顶部 prompt / 状态 / usage / 结果，下方复用主聊天渲染器显示完整过程并实时追加。主聊天里 Agent/Task 卡显示子代理状态行；`<task-notification>` 不再显示为用户消息，而是小系统行"子代理「…」已完成 · 2.8s · 0 次工具调用"（结果可展开）；其它 `promptSource=system` / 非 human 的 `origin.kind` 注入消息显示为系统行（历史预览同样处理，`ccr/history.py` 保留 `origin.kind`/`promptSource`）。

## 8. 测试
- `python3 -m unittest discover -s tests -t . -v` 全过。
- A：node_agent 单测（白名单、引用、EOF 退出）+ 本机 tmux（`/opt/homebrew/bin/tmux`）+ fake_tui 的端到端（start/paste/keys/capture/watch 事件/kill/stop_keeper）。
- B：tui_parse 对全部 fixtures 画面的断言；SessionManager 用 fake 模式（依赖 A 的 AgentLink 与 fake_tui；A 未完成前用桩）；server 新接口。
- C：`static/dev/` 下用 fixtures 回放屏幕 + transcript 的自测页；fake 模式联调。
- 上下文窗口与 /context：`tests/test_stats.py::TestContextWindow`（用户日志的四个数字、带/不带 [1m]、按表、未知模型、/context 覆盖、网关模型名、超过窗口升到 1M、换模型、状态栏优先、持久化）、`tests/test_tui_parse.py::TestContextCmd`、`tests/test_ctxcard.py`（node 跑纯函数）、`tests/test_fake_tui.py::ContextCmdTests`、`tests/test_session.py::TestFakeEndToEnd::test_stats_window_from_context_cmd`；fixtures `context_cmd/`（屏幕）、`transcript_context_{fullscreen,classic}.jsonl`；回放页第 13 节加窗口来源与 100% 封顶，第 14d 节检查 /context 卡片。
- 统计：`tests/test_tui_parse.py`（状态栏/spinner 对全部 fixtures 帧与各种写法、解析失败）、`tests/test_stats.py`（stream_frames 与 subagent_sample 真机帧回放：生成中速率、换段/计数回落、上一轮平均、transcript 后备与上限推断、持久化校验）、`tests/test_session.py::TestStats`（meta.stats 节流、退出、重启恢复）与 fake 端到端（状态栏 / 无状态栏后备）；回放页 `static/dev/fixture.html?auto=1` 第 13 节检查统计条（圆环 dashoffset 与百分比一致、tooltip 含 25k / 1M、警告/危险色、后备"约"、只有 token 数、`—`、主题变量、不发请求），第 13b 节检查输入框底栏（胶囊两个点击区与分隔线、key 不变不重建、运行中/已退出/断线的禁用与原因、模式菜单向上弹且不出窗口、菜单打开时模式变化重新挂到新 chip、选模式/再点收起/Esc 关闭、5 档宽度下一行 32px 且右侧完整可见与分级折叠、长模型名截断不撑宽输入框）。
- 发图片：`tests/test_image_attach.py`（`ccr.attach` 魔数 / 转义 / 路径校验含 FIFO 与软链接；tui_parse 对 `fixtures/image_paste/` 真机画面；node_agent 的 `C-v`、`input_images_only` guard 与粘贴 guard、与 tui_parse 输入框判定一致；fake_tui 的路径识别 / Ctrl+V / 退格 / transcript 图片块与真机同形；server 路由与校验、手机远程口禁止；fake 本机端到端：⌘V、拖入（带空格 / 括号 / 中文路径）、`images_changed`、`images_with_command`、文字 + 图片、只有图片、清除、图片 + 残留文字、剪贴板里没有图片 504、忙时排队、对话框打开时 409、服务器会话 `not_local`；原生窗口 `ccrDropEventScript` 抽出单独编译，用 JavaScriptCore 核对刁钻路径原样到达页面）、`tests/test_attach.py`（node 跑 attach.js 纯函数，转义与 Python 逐字一致）；回放页第 1e 节（真机画面的图片编号、图片条位置 / 缩略图 / 高度 / 不重建 / 横向滚动 / 清除只来自点击 / 进行中 / 警示 / 浅色深色对比度、DataTransfer 与 ClipboardEvent、覆盖层、路径插入、带图片的用户消息渲染与 ↑/↓ 历史、不自动发请求）。删除单张 / 缩略图（2026-11-25）：`tests/test_image_attach.py` 另测 `tui_parse.input_caret` / `input_image_cells` / `caret_moves` 对真机实测（`fixtures/image_paste/caret_probe.json`：← / → 每一步的光标列、跨行、中文、退格后的空格）、node_agent `caret_before` 与 tui_parse 逐格一致、cursor guard（`input_images` / `cursor_image` / `cursor_space`）与 `capture cursor:true` 的 tmux 调用（同一条命令、不带 `-J`）、白名单没有多加键、fake 的光标列与真机逐步相同、`read_image_file`（同一 inode 才读、换文件 / 软链接 / 删除 404、太大 413、非图片 415、FIFO）、server 的 `image/remove` 校验与 `image/file` 响应头、远程口禁止 `image/file`；fake 端到端：删中间 / 第一张 / 最后一张后输入框文字与光标在末尾、再拖入接在末尾、删后发送只带剩下的图片、输入框里有别的文字时只删图片、只剩一张删完不再按键、`images_changed` 不按键、缩略图 token 在删除 / 清除 / 发送 / 换文件后作废、对话框时 409、服务器会话 `not_local`。回放页第 1e 节另查 56px 缩略图、占位不是按钮、× 的位置与命中、点缩略图开灯箱（原图、2 / 2、←）、点 × 只发一次 `image/remove` 且不开灯箱、删除中 / 服务器会话 / 加载中 / 有别的文字时的状态、#N 角标与 × 的对比度（浅色 / 深色）、时间线里用户消息的图片点击开灯箱。
- 输入框 ↑/↓ 历史：`tests/test_prompthist.py`（node 跑 prompthist.js：历史列表、待出现消息的对上 / 过期 / 上限、浏览状态与草稿恢复、按键过滤、无 sizer 时按换行符判断）；回放页第 1c 节在真实 textarea 上检查（2 条真实提示、连按 ↑/↓、草稿恢复、改后不丢字、多行与自动折行（含无空格的中文长段）时先移动光标、选区 / 修饰键 / 输入法 / 禁用不处理、排队消息、不发请求）。
- 左侧 prompt 小格子：`tests/test_minimap.py`（node 跑 minimap.js 的纯函数：条目推导与增量扫描、预览纯文本化（标题 / 列表 / 链接 / 图片 / 代码 / 表格 / 转义、1500 字上限、只有图片）、高度与 top%、指针 → 下标、可见范围二分（尾部 / 空隙 / 零高度行）、一轮是否可见、当前下标、留白 → 常驻 / 命中宽度 / 箭头可点、上一轮 / 下一轮目标、滚轮换算）；回放页第 1d 节用同一个 minimap.js（条目数 = 用户消息数、少于 2 条隐藏、200 轮时 top% 与高度、宽 / 窄两档常驻与淡入、不改变 `.chat-col` 位置 / 宽度与 scrollHeight、悬停鱼眼 scaleX 与预览卡内容 / 位置 / 不溢出、点击已渲染与 renderStart 之前未渲染的条目后顶端在视口下方 24px、补渲染保持滚动位置、跳转后不贴底且新内容出「最新」、data-in-view 随滚动变化、上一轮 / 下一轮与禁用、键盘、滚轮转发、拖动穿透、浅色 / 深色对比度、无会话隐藏、不发请求）。

## 9. 本机模式（本地 Mac 上的交互式 Claude Code CLI）
用户要求：同一个 app 里多一个"运行位置"选项——**服务器**（上文 §1–§8，经 ssh + srun 在计算节点上）或**本机**（这台 Mac）；其它功能完全一样，同样是驱动**官方交互式** `claude`，不改 Claude Code 的任何配置、系统提示词、hook。

### 9.1 目标标识
- 本机 = 特殊 jobid 字符串 `"local"`（`remote.LOCAL_JOBID`）。按 jobid 分组的逻辑（`SessionManager._links`、`session.jobid`、sessions.json、SSE、meta）原样复用：本机所有会话共用一个 AgentLink。
- `remote.validate_target(s)`：`"local"` 或合法 Slurm jobid；会话的运行位置（`create`/`restart`/`_restore`/`AgentLink.__init__`）一律用它。`remote.validate_jobid` **仍只接受 Slurm 作业号**（srun/squeue 命令只用它，`build_agent_remote_command("local")`、`build_keeper_remote_command("local", …)` 直接 ValueError），config 的"当前作业"也仍只能是作业号。
- 本机与服务器作业**不能互相续接**（transcript 在各自机器的 `~/.claude/projects`）：`restart(sid, jobid=…)` 跨运行位置 → 409 `target_mismatch`（什么都不动）。前端本机会话没有"修改 jobid / 换作业续接"。

### 9.2 本机 agent（同一份 `ccr/node_agent.py`）
- AgentLink（jobid=`local`）：`argv = [sys.executable, "-I", "-u", <node_agent.py>, "--local"]`，本地 `subprocess`、`shell=False`、新会话。**本机模式只用 argv 标志传递，不用环境变量**（tmux server 与 claude 会继承 agent/keeper 的环境，绝不能让 `CCR_*` 之类的变量漏进 claude）。
- env = `agent_link.local_env()` = **白名单**，等同终端.app 新开窗口：只保留 `HOME USER LOGNAME SHELL TMPDIR SSH_AUTH_SOCK __CF_USER_TEXT_ENCODING` 与 `LANG`/`LC_*`，`PATH` 重置为 `/usr/bin:/bin:/usr/sbin:/sbin`（login(1) 默认），其余（Homebrew、npm-global、代理…）全部由 `$SHELL -l -i` 按用户自己的 ~/.zprofile、~/.zshrc 加载；没有 UTF-8 的 LANG 时按系统地区（`defaults read -g AppleLocale`）补 `LANG=<地区>.UTF-8`（与终端.app"启动时设置语言环境变量"一致）。**绝不原样继承 app 进程环境**：实测 app 若从另一个 Claude Code 会话里启动，环境里会有 `CLAUDECODE=1`、`CLAUDE_CODE_SESSION_ID`、`CLAUDE_CODE_MESSAGING_SOCKET`、`ANTHROPIC_AUTH_TOKEN`、`AI_AGENT` 等，原样传给本机 claude 会让它以为自己是别的会话的子进程。`TMUX*`、`CCR_*` 自然也不会带上。
- node_agent `LOCAL = "--local" in sys.argv[1:]`（远端以 `python3 -I -u -c <源码>` 运行时 `sys.argv == ["-c"]`，不可能误入）：
  - tmux：`shutil.which("tmux")`，再试 `/opt/homebrew/bin/tmux`、`/usr/local/bin/tmux`；找不到报"本机找不到 tmux（可用 brew install tmux 安装）"。所有调用仍是 `-L ccr -f /dev/null`。
  - `claude_command()`：`[<shell>, "-l", "-i", "-c", 'exec claude "$@"', "claude"]`。`<shell>` = `$SHELL`，必须是 `/etc/shells` 里列出的绝对路径、basename ∈ {zsh, bash, sh, ksh, dash, mksh}（命令串用了 POSIX 的 `"$@"`；fish 等不行）且可执行，否则 `/bin/zsh`（再否则 `/bin/bash`）。理由：等同用户在终端.app 里（登录 + 交互式 shell）敲 `claude`，PATH / 代理 / nvm 等与平时完全一致。远端同样是登录 + 交互式：`bash -l -i -c`（见 §2 start）。参数白名单（§0.2）与 `--settings` 规则完全相同，参数作为 `"$@"` 原样传给 claude，不经 shell 拼接。
  - `hello` 多返回 `"local": true`（远端/fake 为 false），本机 `slurm_node` 恒为 null。
  - 其余（读 `~/.claude/sessions/<pid>.json` —— 只认 `<数字>.json`，同目录的 `*.key`、`*.tmp`、`compaction-log.txt` 不读；transcript / subagents 监视；key 文件写在**本机** `~/.cc-remote-chat/keys/`）与远端完全相同。macOS 没有 `/proc`，进程树走 `ps -A -o pid=,ppid=` 分支。
- 作业状态：本机没有 Slurm。`probe_job` 恒为 `{"ended": False}`、`check_job()` 恒为 False、`_keeper_steps()` 恒为 `[]`，**不跑 ssh/squeue**；link 永远不会进入 `ended`。连不上/agent 崩溃 → `down` → 指数退避重连（与远端相同）。连接断开时关闭/删除 → 409 `link_down`（提示"本机 tmux 里的 claude 可能残留"），`force` 只在本地标记。
- keeper：与远端**同一份** `keeper_script(tmux_path)`（只用 POSIX/bash 3.2 语法：`$((…))`、`$(…)`、`[ … -ge … ]`、`printf | grep -q`），由 `subprocess.Popen(["/bin/bash", "-c", script], start_new_session=True, stdin/stdout/stderr=DEVNULL, cwd=$HOME, env=local_env())` 后台运行（macOS 自带 `/bin/bash` 3.2；有测试实跑）。`start_new_session`：不在 app 的进程组里，app 退出/崩溃/重启后 tmux server 与 claude 继续跑，app 再启动时按 §4.2 重新附着（与远端行为一致）；后台线程 `wait()` 回收，避免僵尸进程（app 先退出则由 launchd 接管）。已有 keeper → 新 keeper 立即退出；无 `ccr-` 会话 600s 自停；最后一个本机会话关闭时 `stop_keeper` 并关闭本机 link。
- fake 模式：本机仍走 fake_tui（`argv` 同样带 `--local`，但 env = `fake_env(cfg, local=True)`，fake 分支优先），HOME / TMUX_TMPDIR 是与 fake 服务器分开的一套（`remote.fake_home(cfg, local=True)` 默认 = fake_home 同级的 `local-home`，`fake_tmpdir(cfg, local=True)` = 同级的 `local-tmux`），模拟"另一台机器"（独立 tmux server、历史、key 文件）。keeper 仍是 `_start_fake_keeper`。

### 9.3 红线在本机的含义
- §0.1/§0.2/§0.5/§0.6 不变：不用 `-p`/stream-json/SDK；claude 参数严格白名单；按键只来自用户操作；`/model` "保存为默认" = 用户点了才按 Enter，由 **claude 自己**写本机 `~/.claude/settings.json`（与在终端里按 Enter 一样，本程序不写）。
- §0.3：不注入任何环境变量（env 只删 `TMUX`/`TMUX_PANE`/`CCR_*`，不加）。用户自己的 hooks / 插件 / statusline / `settings.json` 照常生效——那是用户自己的配置，本程序不碰。
- §0.4：本机运行时痕迹 = tmux 套接字 `${TMUX_TMPDIR:-/tmp}/tmux-<uid>/ccr`（及其 keeper 进程）+（仅当选择了 API key 时）`~/.cc-remote-chat/keys/<指纹>.json`（0600，规则同 §0.4）。**绝不写** `~/.claude*`、任何 dotfile。
- 开发/测试：不运行真实 claude（本机也不行）、不启动 `zsh -lic 'exec claude'`；所有用到 tmux 的测试都用 `/tmp` 下的临时 `TMUX_TMPDIR`，不在真实的 `/tmp/tmux-<uid>/ccr` 上起 server。

### 9.4 历史 / 目录
- `HistoryService.list_history(limit, target=None)`、`read_history(uid, target=None)`、`list_dirs(path, hidden, target=None)`：target ∈ {None/"server"（默认，与以前完全一致）, "local"}，其它 → 400。
- local：同一份只读 `REMOTE_SCRIPT` 用**本机** `python3 -I -`（`sys.executable`，不改环境）运行，读本机真实的 `~/.claude/projects`；目录默认 `~`（config 的 `default_cwd` 是服务器上的目录）；不占 ssh 并发名额（独立信号量），错误文案为"本机…"。fake：local 读 `fake_home(cfg, local=True)/.claude/projects`（fake_tui 写的，嵌套布局）。
- API：`GET /api/history?target=local`、`/api/history/<uuid>?target=local`、`/api/dirs?path=…&target=local`；缺省/`server` = 服务器（调用与以前完全一致，不带 target 参数）。`/api/history` 的 `api_key` 字段（session_keys.json）对本机同样适用。
- `POST /api/sessions {"jobid":"local", …}` 新建本机会话；不给 cwd 时本机默认 `~`。

### 9.5 API key、统计、其它
- 本机同样可以按会话选 key（同一套 `--settings` key 文件机制，写本机 `~/.cc-remote-chat/keys/`）；"默认" = 不加 `--settings` = 本机 `~/.claude/settings.json` 的 key。前端本机会话的默认显示为"本机默认 / 默认（本机 settings.json）"，不套用"服务器默认"条目的名称与用量。
- 统计条：本机 statusline 是用户自己的脚本、格式与服务器不同。不为它写死格式；`parse_status_stats` 解析不到的部分为 None，上下文自动回退 transcript 后备（"约"），花费无 `$x` 时为 `—`，绝不报错（`tests/test_local.py::TestLocalStatsFallback`）。真机画面由维护者补 fixture。
- 模型/effort 面板、子代理托盘与抽屉、权限/提问/信任对话框、终端面板、换 API、重启/重连：全部复用，不依赖 Slurm/ssh。

### 9.6 前端
- 新建会话对话框：「运行位置」二选一 `服务器（作业 J）` / `本机`（`localStorage["ccr.target"]` 记住上次选择；从历史续接时固定为该历史所在机器、不可切换）。本机隐藏 jobid、目录浏览走 `target=local`、默认目录 `~`（两边各记各的目录），说明文字与 API 默认项随之切换。
- 会话头：本机会话的徽章是绿色「本机」（不可点，不能改 jobid），没有"作业已结束 / 换作业续接"；连接断开横幅为"与本机 agent 的连接已断开…"；重启/重连/换 API/关闭照常。侧栏会话项显示「本机」代替 `job N`。
- 历史面板：顶部 `服务器 / 本机` 切换（`localStorage["ccr.histTarget"]`，两边列表各自缓存），预览与"在此继续"都带对应 target。
- 颜色只用现有变量（本机 = `--ok` / `--ok-soft`），亮/暗主题都已检查。

### 9.7 测试（`tests/test_local.py`）
目标标识校验；本机 agent argv/env（白名单，无 `CLAUDE*`/`ANTHROPIC_*`/`CCR_`）；`build_agent_remote_command("local")` 被拒；本机 link 不调用 ssh/squeue、永不 ended、文案为"本机"；keeper 用 `/bin/bash` + `start_new_session` + 干净 env（mock）以及 `/bin/bash` 3.2 实跑 keeper_script（临时 TMUX_TMPDIR）；node_agent `--local` 的 `claude_command`/`local_shell`/tmux 解析/`op_start` 的 tmux 命令（mock，不启动 shell）、子进程 `--local` 的 hello/list；HistoryService 的 target 路由（本机 runner、默认目录、fake 本机 HOME）；server `?target=`；SessionManager 的本机会话（meta、与作业 link 分开、跨运行位置 409、持久化/重新附着、link_down 文案、本机 key）；统计回退；fake 本机端到端（独立 tmux server、本机历史、app 重启重新附着、关闭后 keeper 停止）。

### 9.8 本机：发图片（2026-11-25，用户需求："截屏了一张图片，没法直接拉到聊天框中，必须保存之后，把截图的文件路径放进去"）
- 只在本机会话适配（服务器会话：远端 claude 读不到这台 Mac 的剪贴板和文件，给提示）。原则：**等价于用户在终端里操作**——⌘V 截图 = 在终端里按 Ctrl+V（claude 2.1.332 在 macOS 上自己读剪贴板图片，状态栏 Tip 也这么写）；拖入图片 = 终端把文件路径作为一次粘贴交给 claude（claude 自己把图片路径变成 `[Image #N]`，§1「图片」实测）。所以：不用 claude 的任何非交互接口、不注入环境变量、不改设置、**本程序不读写剪贴板、不另存文件**（只读被拖入文件的开头 16 字节核对魔数）；Ctrl+V 只在用户这一次 ⌘V 里有图片时发一次，guard 确认输入框在、没有对话框。
- 实现见 §4.2「图片」（`paste_image` / `attach_paths` / `clear_images` / `remove_image` / `read_drop_image`、`send_message` 的 `expect_images`）、§4.3、§7「发图片」、§14.6（原生窗口截下文件拖动）。
- 图片条上的小缩略图、点击放大、每张一个 ×（2026-11-25，用户需求："放进去图片之后，这个图片能不能有个叉叉按钮，可以进行删除，然后既然我们是可视化，我希望是小的预览图，然后也可以点击放大查看"）：× 等价于用户在终端里把光标移到那张图后面按退格（← / → / 退格，都是已有白名单里的键，每步带 guard 就地核对光标与图片编号）；拖入图片的缩略图只读刚拖入的那个文件（会话级白名单 + 同一 inode + 魔数，不是任意文件读取），⌘V 的缩略图只在页面内存里。
- 已知限制：① 浏览器 / Chrome 应用窗口里拖放拿不到文件路径，只能 ⌘V；② 原生窗口里没有文件 URL 的拖动（从网页拖来的图片、只给"文件承诺"的来源）同样拿不到路径，提示改用 ⌘V；③ ⌘V 时剪贴板里既有文字又有图片（例如从表格软件复制）会两样都要：文字进输入框、图片进 claude 输入框（不想要的图片点「清除」）；④ Ctrl+V 读剪贴板的是 claude，若用户在 `~/.claude/keybindings.json` 改掉了 Ctrl+V（`chat:imagePaste`），或剪贴板在 ⌘V 之后被别的程序改掉，claude 读不到图片 → 504 `image_not_found`；⑤ 真机只实测了"粘贴图片路径"（不碰剪贴板），Ctrl+V 由 fake 仿真，真机行为需用户在原生窗口里确认一次；⑥ 图片条只看得到屏幕上的 `[Image #N]`；⌘V 的缩略图只在本页内存里，刷新页面后只剩图标（图片仍在 claude 输入框里、照常发出）；拖入的缩略图读原文件，文件在发出前被移动 / 替换就只剩图标；发出后时间线里的图片来自 transcript；⑦ 逐张删除依赖 CLI 把终端光标停在输入框插入点（2.1.332 fullscreen 渲染器实测；经典渲染器按代码同样停放但未实测）——读不到光标、光标不在输入框、`[Image #N]` 被折行拆开时 409 `cursor_unknown`、一个键都不按，可用「清除」或终端面板；删除过程中用户同时在终端面板里打字，guard 会让它停下（可能停在光标已挪动、还没退格的状态，再点一次即可）。

## 10. 可视化

用户需求：Claude Code 生成网页、截图等内容时，在聊天界面里直接可视化（参考 T3 Code 的可视化/聊天部分）。**不修改 Claude Code 的任何配置、系统提示词与 hook**：全部功能是"只读 + 显示"，数据只来自 transcript 与（仅本机会话）本机文件的只读访问。

**本机会话** = `meta.jobid == "local"`（本机模式，`remote.LOCAL_JOBID`），或 fake 模式（fake claude 本来就在本机跑）；后端 `ccr.preview.is_local(meta, cfg)`，前端 `viz.isLocal(s)`；历史预览（`hist:…`）一律不是。

| 功能 | 服务器会话 | 本机会话 |
|---|---|---|
| 截图/图片完整显示（ccr_ref + blob）、灯箱 | ✔ | ✔（灯箱另有"在 Finder 中显示""用默认应用打开"，需知道文件路径） |
| 本轮改动卡片 + 右侧面板 diff（structuredPatch） | ✔ | ✔ |
| 文件路径 chip | 只复制路径 | 存在的文件可点 → 右侧面板预览 |
| 右侧面板 file / url 标签、网页预览卡片、本轮图片、localhost 预览 | ✘ | ✔ |
| 工具组摘要、代码高亮、彩色区分 | ✔ | ✔ |

### 10.1 图片引用（node agent）
- 转发条目（主 transcript 与子代理）时，`message.content[i]` 或 `message.content[i].content[j]`（tool_result 内）里 base64 长度 ≥ 16384 的图片块换成
  `{"type":"image","source":{"type":"ccr_ref","media_type":…,"ref":…,"bytes":解码后字节数}}`；更小的继续内联。
  `ref = m.<sessionId>.<该行字节偏移>.<块路径>.<data 的 sha256 前 16 位>`（子代理 `a.<sessionId>.<agentId>.<偏移>.<块路径>.<hash>`），块路径 `i` 或 `i-j`。
- `toolUseResult` 里的重复数据直接丢掉：Read 图片的 `file.base64`（换成 `file.ccr_dropped:"base64"`）、MCP 截图列表里的图片（`source.type:"ccr_dropped"`）；超过 20000 字符的 `originalFile`/`originalFileContents` 置 null（diff 用 structuredPatch）。
- `blob` op：ref 里的 sessionId 必须是该 sid 监视期间见过的（或本地随请求给出的当前 `claude_session_id`），文件路径只由 `find_transcript` / `subagents_dir` 推出（`~/.claude/projects/*/<uuid>.jsonl`、`<uuid>/subagents/agent-<id>.jsonl`），`O_NOFOLLOW` 打开普通文件，从偏移处**按 256KB 块读到换行为止**（上限 48MB，不会先读满），按块路径取出图片、校验 sha256 前缀（不符 → `code:"mismatch"`），base64 上限 16MB。不接受任意路径，不写任何文件。blob 在**独立线程**执行（信号量最多 2 个并发，排队超过 16 个回 `code:"busy"`），主循环继续处理 ping/keys/capture；响应经同一把输出锁整行写出。本地 `AgentLink` 按 64KB 块读 stdout 并记录最近收到数据的时间：ping 超时但数据一直在流入（正在传大图片）不算断开；`get_blob` 用独立的 90s 超时。前端同一时间最多取 2 张引用图片。
- 本地：`SessionManager.get_blob(sid, ref)`（ref 的 sessionId ≠ 当前 `claude_session_id` → 403）；`GET /api/sessions/<sid>/blob/<ref>`（token）→ 解码后**按文件头**识别 png/jpeg/gif/webp（其它 415），`Content-Type: image/*` + `nosniff` + `Cache-Control: private, max-age=86400, immutable` + CSP `default-src 'none'; sandbox`；服务端 LRU（64MB/256 张，`ccr/blobs.py`）。link 不可用时 503（前端显示"图片加载失败 · 点击重试"）。
- 前端（`static/blobimg.js`、`static/viz.js`）：小图 `data:`（`safeImageSrc` 白名单不变）；引用图片进入视口（IntersectionObserver，提前 600px）才用带 `X-CCR-Token` 的 fetch 取回 → objectURL（同一 ref 只取一次，最多缓存 200 个）。
- 历史回看（`/api/history/<uuid>` 走 `history.py` 的 `_image_placeholder`）本次不改，仍显示"[图片 … 已省略]"。

### 10.2 灯箱（`static/lightbox.js`，缩放算法移植自 T3 Code ZoomableImage，MIT）
点击任意聊天图片（用户附件、工具结果、工具卡缩略图条、markdown 本机图片、本轮图片；以及输入框图片条上的缩略图，§7）打开：适应窗口；单击 = 适应窗口 ↔ 原始尺寸（已是原尺寸时 2×）；Ctrl+滚轮 / 触控板捏合缩放（`wheel` 监听 `passive:false` + `preventDefault` 拦住页面缩放），以鼠标位置为锚点；`+`/`-`/`0`；放大后拖拽平移；同一条目及相邻连续工具卡内的图片 ←/→ 切换；Esc 关闭（`preventDefault`，不会顺带关掉子代理抽屉）；按钮：复制图片（`navigator.clipboard.write`，非 png 先经 canvas 转 png）、下载、本机且知道文件路径时"在 Finder 中显示""用默认应用打开"。Read 读图片、MCP 截图的工具卡在标题下直接显示缩略图条。

### 10.3 文件路径 chip（`static/filelinks.js`，规则移植自 T3 Code markdownLinks.ts / fileLinks.ts）
来源：文件工具（Read/Write/Edit/MultiEdit/NotebookEdit）标题行右侧的小 chip；markdown 行内代码与链接里像路径的文本（含 `/` 或带 `:行号`、末段有扩展名或是 Makefile 等、排除主机名/版本号/省略段 `…/`、带空格只认绝对路径）；markdown 图片 `![](本机路径)`（本机显示缩略图）；Bash 命令参数与输出里的绝对路径（工具卡展开后"涉及的文件 / 地址"）。相对路径按会话 cwd（transcript 条目的绝对 `cwd`）解析。
本机：`POST /local/stat` 批量确认（40ms 合批、每批 ≤200 个，多出来的连同等待者放回下一批；缓存 30s/不存在 4s），存在的文件可点 → 面板，目录 → 在 Finder 中显示，不存在灰显、点击复制路径；服务器：点击复制绝对路径。
`http://localhost:PORT/…`、`http://127.0.0.1:PORT/…`（**排除本 app 自己的端口**）旁边有"预览"按钮（仅本机）→ 面板 url 标签。

### 10.4 右侧预览面板（`static/preview.js`）
`.app` 里 main 右侧的 `<aside class="pv">`：左缘拖拽改宽度（存 `localStorage ccr.pvw`，双击复原）、收起（右缘"预览 · N"胶囊展开）/ 关闭全部、每个会话一组标签（最多 12 个）；≤1100px 变成覆盖层。标签：
- **file**（仅本机）：image / svg（当图片显示）/ html（`<iframe sandbox="allow-scripts allow-forms allow-popups allow-modals">`，**绝不 allow-same-origin**）/ markdown（markdown.js，相对链接按该文件目录解析）/ 文本代码（行号、可跳到 `:行号`、极简高亮，>2MB 或 >2 万行截断）/ pdf（不加 sandbox 的 iframe：Chrome 的 PDF 查看器不能在沙箱里运行；响应是 `application/pdf` + nosniff）/ 音视频 / 二进制（"用默认应用打开"）。按钮：刷新、在浏览器中打开、在 Finder 中显示、用默认应用打开、复制路径。打开期间每 1.5s `POST /local/stat` 查 mtime/size（HTML 还盯本会话改过的同目录文件），变了自动刷新（iframe 加 `?ccr_v=`）。
- **url**（仅本机）：localhost 页面直接作为 iframe 地址（不同端口 = 不同 origin），默认同样的严格沙箱；Vite 等模块脚本页面在不透明 origin 下可能空白，可手动点"兼容模式"对**这一个 localhost 标签**加 `allow-same-origin`（它的 origin 是 localhost:其它端口，碰不到本程序；本程序不用 Cookie，API 需要 token 头），或"在浏览器中打开"。
- **diff**（服务器 + 本机）：本轮某文件的全部编辑，逐次显示 structuredPatch（hunk 头 + 新旧行号）；新建文件显示全部内容为 `+`；没有 structuredPatch 时退回 LCS（无行号）。只用 transcript 数据，不读文件。
- **changes「改动」**（服务器 + 本机，`static/changes.js`；用户需求，参考 CLI 自带的 `/diff` 面板、T3 Code DiffPanel、Orca 每轮改动，MIT）：固定在最左、不可关闭；会话头「改动」按钮（图标 + 改动文件数角标，窄时只留图标和角标）切换显示，开关状态全局记在 `localStorage ccr.chg`，面板右上角 × 连同「改动」一起关；窄窗口（≤1100px）启动时若记住打开，先收成右缘胶囊。数据**只**来自前端已有的 transcript 条目（主会话工具调用 + 子代理条目）：不跑 git、不读 file-history、不读文件、不发任何请求或按键（在 Claude Code 看来与用户本人交互式使用没有任何区别）。只统计成功的 Edit / Write / MultiEdit / NotebookEdit（失败、被拒绝、未完成的不算）。头部：范围下拉（「本会话（全部 N 轮）」+ 有改动的轮，最新在上，「第 N 轮 · 时间 · 第一句提问前 30 字（k 个文件）」）、「N 个文件 +A −D」（1.2k 紧凑计数，0 的一侧不显示）、全部展开/折叠。文件行：相对 cwd 的路径（目录灰、文件名粗，cwd 外保持绝对路径），按路径排序，标签「新建 / 单元格 / 子代理 / ≈（已截断或覆盖写入时不知道旧内容）」、「N 次」、+/−。展开后逐次编辑按时间顺序排列，每段标第几次、工具、第几轮、时间、子代理名；有 structuredPatch 的带新旧行号，新建文件用 content 生成全新增并带行号，NotebookEdit 用 old_source/new_source、其它没有 patch 的用 LCS（均无行号）；超过 400 行的段先放占位，进入视口才渲染。本机会话有「打开文件」（现有本机文件预览），都有「复制路径」（只在点击时 copyText）。底部固定一行口径说明：只统计 Edit / Write / NotebookEdit，Bash 或会话外的改动不在其中，+/− 是各次编辑相加、不是 git 净改动。增量维护：ChangeTracker 只看新条目和待定的调用，`s.items` 被整体替换（reset）时重建；子代理按 (gen, base, 条目数…) 判断变化；实时更新时内容没变的文件节点原样复用、锚定第一个可见文件，不动时间线。页面打开前就跑完的子代理条目可能不全，顶部提示「打开该子代理的抽屉即可加载」；不做合并后的净 diff（两次编辑之间可能有 Bash/外部修改，无法保证正确）。

### 10.5 每轮结束的卡片（`static/turnfiles.js`）
一轮的边界：`system` `turn_duration`、assistant `stop_reason:"end_turn"`、下一条用户消息、中断标记（`render.js` 的 `closeTurn`，同一轮只加一次）。本轮有 Write/Edit/MultiEdit/NotebookEdit 或 Bash 时追加 `turnsum` 条目：
- 「本轮改动 · N 个文件 +A −D」：每个文件一行（多次编辑合并显示"N 次"），点击 → 打开「改动」标签、切到这一轮、只展开该文件并滚到顶部（短暂高亮；面板不可用时退回 diff 标签）。Write 没有结构化结果时，结果文字以 `File created successfully` 开头才算新建（Orca 规则），否则算编辑。`+/−` 来自 structuredPatch；新建文件按内容行数；没有 structuredPatch 用 LCS。
- 「网页预览」（本机）：本轮写/改的 `*.html` → 320px 高的沙箱 iframe + 全屏（`requestFullscreen`）/ 侧栏打开 / 浏览器打开（`window.open(预览地址)`）；只有每个文件最新一轮的卡片自动加载，较早的点"加载预览"。
- 「本轮图片」（本机）：本轮 Write 的图片、Bash 命令/输出里出现且 stat 确认存在的 png/jpg/gif/webp…，缩略图点击进灯箱。
工具卡里的 Edit/Write/MultiEdit diff 也优先用 structuredPatch（带行号）。

### 10.6 彩色区分（`static/style.css`、`static/theme.js`）
`<html data-colorful="on|off">`（theme.js 首帧前写好；默认开；存 `localStorage ccr.colorful`，`'0'` = 关；侧栏主题按钮左边的调色板按钮切换）。所有彩色规则挂在 `:root[data-colorful="on"]` 下，关掉 = 原来的单色样式。颜色全部是 CSS 变量，浅色 `:root` / 深色 `:root[data-theme="dark"]` 各一套：用户 `--c-user*`（淡蓝气泡）、Claude 回答 `--c-claude*`（中性底 + 左侧 Claude 橙色条）、思考 `--c-think*`（靛紫淡底 + 左色条，标签"思考"，保持折叠）、工具按类别 `--c-read`（读取/搜索，青）`--c-edit`（编辑/写入，琥珀）`--c-bash`（命令，石板灰蓝，命令块终端风格 `--c-term-bg/fg`）`--c-mcp`（浏览器/MCP，紫）`--c-web`（联网，天蓝）`--c-agent`（子代理，玫红）`--c-todo`（绿）——左侧 3px 色条 + 图标/名称着色 + 标题行约 6–8% 色调；失败/拒绝 = 红色条，运行中 = 强调色 + 流光动画，成功 = 绿色小勾；权限/信任对话框 `--c-perm`（琥珀）；系统通知 / notice / 子代理结束行 `--c-sys-bg`（灰色淡底）；语法高亮 `--hl-*`。正文文字一律 `--text`/`--text-2`（对淡底 ≥ 4.5:1）。顺带把浅色 `--text-3` 从 `#8d939c`（约 3.1:1）改为 `#636973`（对白底 5.5:1、对 `--bg-subtle` 5.0:1）。

### 10.7 其它
- 连续工具调用组（中间夹着的思考不打断，≥2 个）在组首显示"N 个工具调用：读取 3 个文件、运行 2 条命令"（`static/toolgroups.js`，参考 T3 summarizeToolGroup；追加条目时 O(1) 维护）。
- 代码块：已有复制按钮；加极简正则高亮（`static/highlight.js`：js/ts、py、sh、json、html/xml、css，只生成 span + 文本节点）。
- 页面 CSP（`server.CSP` 与 index.html 的 meta）增加 `frame-src 'self' http://127.0.0.1:* http://localhost:*`。

### 10.8 本机文件预览服务（`ccr/preview.py`，只读）
- `GET|HEAD /preview/<cap>/<绝对路径>`：不走 token（iframe 子资源带不了头），也不做 Origin/Sec-Fetch-Site 检查（沙箱页面 = 不透明 origin），**Host 校验保留**；其它方法 405。
- cap：每个（本机会话, 根目录）`secrets.token_urlsafe(24)`（32 字符），只在内存、app 重启即失效、只经 `/local/stat` 发给本机会话；会话被删或不再是本机会话 → 404。根目录 = 会话 cwd（文件在 cwd 下且 cwd 不是 `/`、home、`/tmp` 这类宽泛目录），否则 = 文件所在目录（宽泛目录下的文件只授权该文件本身）。页面里的第三方脚本即使读到自己的地址，也碰不到根目录以外。
- 路径策略（请求路径与 realpath 都查；字符串预检先做大小写折叠、去掉 `/System/Volumes/Data` 前缀，**最终按 `(st_dev, st_ino)` 从文件逐级向上比对**拒绝目录——APFS 不区分大小写、realpath 不规范大小写、firmlink 别名都绕不过；打开后再用 `fcntl F_GETPATH` 取当前路径复核一遍；cap 根目录的授权、宽泛目录/cwd 的判断同样按身份）：任何以 `.` 开头的路径段（`.ssh`、`.claude`、`.cc-remote-chat`、`.git`、`.env`、`..`…）、`~/Library`（含 Keychains）、`/Library/Keychains`、`/dev`、`/private/var/db`、本程序数据目录（fake 模式的假 HOME 除外）一律 403；目录不列（404）；设备/FIFO/套接字 403；>50MB 413；realpath 不在 cap 根目录内（软链接逃逸）403。`O_NOFOLLOW|O_NONBLOCK` 打开后 fstat 复核仍是同一个普通文件。
- 响应：按扩展名的 Content-Type（未知扩展名像文本就 `text/plain`，否则 `application/octet-stream` + `attachment`）、`nosniff`、`Referrer-Policy: no-referrer`、`Cache-Control: no-store`、`Cross-Origin-Resource-Policy: cross-origin`，不发 `X-Frame-Options`；HTML/XHTML/SVG/XML 带 `Content-Security-Policy: sandbox allow-scripts allow-forms allow-popups allow-modals allow-downloads`；PDF 不带 CSP；其它带 `default-src 'none'; style-src 'unsafe-inline'; media-src 'self'; sandbox`。只有 `Origin: null` 且 `Sec-Fetch-Dest ∈ {script, style, font}` 请求 JS/CSS/字体时回 `Access-Control-Allow-Origin: null`（模块脚本与 @font-face 需要 CORS）；fetch/XHR（dest=empty）、JSON、图片读不到内容。
- `GET /api/sessions/<sid>/local/info` → `{local, cwd, home}`；`POST /api/sessions/<sid>/local/stat {paths:[≤200]}` → `{results:[{path, abs, real, exists, type:"file|dir", size, mtime, ok, kind, url, reason}]}`（被策略拒绝的一律 `exists:false`，不透露隐藏文件是否存在）；`POST /api/sessions/<sid>/local/open {path, action:"reveal"|"open"}` → `open -R <realpath>` / `open <realpath>`（argv、`shell=False`、超时 15s）。`open`（用默认应用打开）是**白名单**：图片、PDF、文本/代码（不含 .py/.sh/.rb 等可能被解释器启动器直接运行的脚本）、Markdown、HTML（用浏览器打开）、音视频、常见 Office 文档、CSV/JSON，且无可执行位、不是目录；其余类型只能"在 Finder 中显示"。stat 结果带 `openable`，前端据此隐藏按钮。会话删除 / 重启时 `preview.forget(sid)` 作废其 cap（cap 指向已删会话时也顺手清理）。三者都需要 token，非本机会话 403 `not_local`；只在用户点击时调用 open。
- 只读：不写任何用户文件、不跑 git、不做 git 影子快照。

### 10.9 测试
- `tests/test_node_agent.py::ImageRefTests`：引用/偏移/块路径、toolUseResult 去重、伪造 hash/偏移/路径、越权 sessionId 与 sid、非法 ref 格式、子代理引用、软链接拒绝、不给 ref 时行为不变。
- `tests/test_preview.py`：路径策略（dot 段、`..`、软链接逃逸、特殊文件、大小上限、数据目录与 fake HOME、`~/Library`）、根目录与 cap、HTTP（CSP、CORS、方法限制、Host、cap 错误、非本机/已删会话、HEAD、相对资源）、`/local/*`（仅本机、token、open 的 argv 与拒绝）、blob 路由（文件头识别、LRU、415/403/404、token）、`SessionManager.get_blob` 越权。
- `tests/test_changes.py`（node 跑 changes.js：紧凑计数、路径、各类编辑、卡片汇总、子代理条目解析、ChangeTracker 的轮 / 增量 / 待定结果 / reset / 子代理归轮与移除 / NotebookEdit / 长会话增量成本）；回放页第 14b 节「右侧代码改动栏」（空状态、文件数与 +/−、各标签、按轮切换、行号、懒渲染、全部展开/折叠、实时更新时时间线 scrollHeight / scrollTop / 列宽与会话头高度不变、联动、开关与记忆、浅色深色对比度 ≥ 4.5、不发请求）。
- 回放页 `static/dev/fixture.html?auto=1` 第 14 节"可视化"（桩 api 与桩 blob，不连后端）；工具栏"可视化示例"按钮（可勾"服务器"看服务器会话效果）。
- fake 模式：消息里含 `/html`（在 cwd/ccr-demo 写网页 + Edit + 假装起 http.server）、`/shot`（MCP 截图 + 保存 + Read 图片 + markdown 图片）。

## 11. CLI 快捷键提示（ctrl+b 转后台 / ctrl+x ctrl+s 立即发送排队消息）
维护者 2026-11-23 在 123456 上用真实 claude v2.1.330（tmux 200x50）实测（原样摘录见 `fixtures/tui_screens_keys.ndjson` 的 `bg_queued`，其余帧为按实测描述合成、待真机补）：
- 正在运行的 Bash 命令下方：`(ctrl+b ctrl+b (twice) to run in background)`（也兼容 `ctrl+b to run in background`）。tmux `send-keys` 不经过 tmux 前缀键，**发一次 `C-b`** 即转入后台；之后屏幕显示 `Ran 1 shell command`，底栏 `⏵⏵ auto mode on · 1 shell · esc to interrupt · ↓ to manage`，回合结束行 `✻ Crunched for 13s · done 12:13 AM · 1 shell still running`。
- 忙时提交的消息进入队列：多条排队消息是**连续的** `❯ <消息>` 块（多行消息续行两空格缩进），后面**只有一行**共用的 `  ⎿  Queued · ctrl+x ctrl+s to send now`（真机实测，fixture `three_queued_shared`）；输入框占位文字变成 `Press up to edit queued messages`（**不是用户输入**）。`C-x` 再 `C-s` = 立刻中断当前生成并发送排队的消息。
- 真机 v2.1.331：排队块之间也有空行（`❯ Q1` 空行 `❯ Q2` 空行 … `⎿ Queued`）。输入框里有文字时，CLI 的模式行**不显示** `esc to interrupt`（此时 Esc 是清空输入框），即使正在生成。
- 输入框正上方右对齐的 `○ low · /effort`、`tmux focus-events off · add 'set -g focus-events on' to ~/.tmux.conf …` 是 CLI 的提示行，不进草稿；我们**不**改 tmux 配置。

解析（`ccr/tui_parse.py`，纯函数）：`state.hints = {"background": bool, "send_now": bool, "queued": [排队消息文字…], "shells": int|null}`。
- 提示只认**整行**固定写法（`HINT_BACKGROUND_RE` / `HINT_SEND_NOW_RE`，与 `node_agent.GUARD_HINTS` 相同），回复正文里出现同样的字不算；对话框区域不算。
- `queued`：从每个 `⎿  Queued` 行往上收集连续的 `❯` 块（空行跳过；两空格缩进的行是候选续行，往上遇到 `❯` 才归入该块；遇到 `● `、`⎿`、spinner 等其它内容就停，没归属的候选行是助手正文、丢弃），按屏幕顺序；也兼容每条下面各有一行 Queued。排队消息不当作 `echo`（最后一条用户消息）、不进 `live`；后台提示行也不进 `live`（由按钮呈现）。本轮还没有输出（thinking）时，真正发出的那条消息紧挨在排队块上方：`_queued_blocks` 在 ≥2 个 ❯ 块、且再往上是本轮结束行 / `Interrupted` / 启动横幅时（`_turn_boundary_above`），把最上面那块当回显、不当排队消息（只有 1 块或排队块从屏幕顶端开始时保持原样——可能滚出屏幕，无法判断）。否则 echo 会退回上一轮，实时草稿把上一轮整段回复重复显示，开始输出时整段缩回，时间线大幅跳动（用户报告的「发出去之后聊天框一直抖」）。按 Up 会把排队消息拉回输入框编辑（此时 Esc = 中断当前回复），我们不提供 Up 操作。
- 忙时的 busy 补回：`SessionManager._apply_screen` 用 `_busy_hidden_by_input`——输入框有文字、解析为不忙、且没有对话框时，有排队或后台提示即为忙；否则看 claude 自己的 session status == busy（且画面上没有本轮的结束行），读不到 status 时看 spinner。这样忙时「粘贴 → 确认 → 回车」的几帧不会闪成空闲（占位文字、中断按钮、草稿、侧栏状态、统计都不再闪）。fake_tui 同样在输入框有文字时不显示 `esc to interrupt`。测试：`tests/test_queue_busy.py`（按真机版式重建的画面、busy 补回判断、fake 端到端忙时连发 3 条每帧都是 busy）。
- `input.text`：输入框内容**正好**是 `Press up to edit queued messages` 时为空串，另给 `input.placeholder`（`_ensure_input_empty` 等不会误判为残留文字）。
- `shells`：模式行里的 `N shell(s)`，其次结束行的 `N shell(s) still running`。

动作（用户点击按钮或键盘和弦才发送，§0.5 确认类按键）：
- `SessionManager.run_in_background(sid)` / `POST /api/sessions/<sid>/background`：先抓最新屏幕——有对话框（或 status=waiting、trust）→ 409 `dialog_open`；`hints.background` 不为真 → 409 `no_background_hint`；否则发 `["C-b"]`，guard `{input_box:true, hint:"background"}`（agent 发键前重抓屏复核，提示已消失 → 一个键都不按，409 `screen_changed`）。
- `SessionManager.send_now(sid)` / `POST /api/sessions/<sid>/send_now`：同上，`hints.send_now`，无提示 409 `no_send_now_hint`；发 `["C-x","C-s"]`（一次 keys 请求，间隔 40ms），guard `{input_box:true, hint:"send_now"}`。
- 前端（`static/hotkeys.js`）：输入框上方提示条——排队中的消息（标「排队中」）+「转到后台运行（Ctrl+B Ctrl+B）」「立即发送排队消息（Ctrl+X Ctrl+S）」按钮（有对话框 / 已退出 / 断线时不显示按钮）；会话头显示「后台 shell N」。键盘和弦 Ctrl+B Ctrl+B、Ctrl+X Ctrl+S（两次间隔 ≤ 1.5 秒）：**只在对应提示出现、没有对话框、没有弹窗时**拦截并 preventDefault，其余时候不影响输入（macOS textarea 里这些 Ctrl 组合是 emacs 光标键）。
- fake_tui：`/bg` 场景（进度 + ctrl+b 提示，Ctrl+B 转后台、`N shell`、结束行 `… · N shell still running`），忙时的排队消息显示 + 占位文字，Ctrl+X Ctrl+S（1.5 秒内）。

## 12. 提示音（`ccr/notify.py`）
用户需求：做完了、或做到一半需要用户输入 / 授权时发出声音；声音与本机 Claude Code CLI 完成任务时一样（本机 `~/.claude/settings.json` 的 Stop hook = `/usr/bin/afplay /System/Library/Sounds/Blow.aiff`）。服务器会话与本机会话都适用。
- 由**本地后端**在 Mac 上执行 `/usr/bin/afplay <文件>`（argv、不经 shell、`start_new_session` 后台进程、后台线程回收，不阻塞）；浏览器不播放（播不了 aiff）。只被动观察会话状态，不发任何按键、不改 Claude 的任何配置。
- 触发（SessionManager 在每次 meta 推送前，若 `(state, 对话框指纹)` 变化就调用 `Notifier.observe`）：
  - 完成：`busy → idle`，且基线之后确实看到过 busy；用户中断（`interrupt`、`cancel_dialog`、终端面板 Esc/Ctrl+C）或屏幕 `interrupted` 的不响。`send_now` **不**压制（真机：被打断的一轮直接进入排队消息的新一轮、中间没有 idle，压制会吞掉新一轮的完成提示）。
  - 需要操作：从非 waiting 进入 `waiting` / `trust`，对话框种类不是 `model`/`effort`（本程序或用户自己打开的面板不响）；status=waiting 但没解析出对话框也响。同一对话框（指纹 = 种类、标题、问题、预览、非自由输入项的选项）已经提醒过、且用户没操作过就不再响；用户回答过（`choose`/`keys`/`cancel_dialog`）后再出现算新的一次。"已提醒"只在真正播放（或因设置关闭而跳过）时记录，候选阶段不记。
  - 去抖：完成 = 0.8s 内状态一次都没变才播放（马上又忙起来不响）；需要操作 = 0.8s 后**此刻**仍处于 waiting/trust 且对话框指纹与候选时相同就播放（真机出现时会短暂闪成 busy 再回来，不影响），判断与记录在同一把锁里，多个候选只响一次；全局 1.5s 内最多响一次；`reset()`（启动后、重新附着、重新 watch、link 断开/重连中）之后的首次状态同步与 2s 内的变化只当基线、不响；`starting`/`exited`/`error` 之后的首个状态也是基线。
- 设置（`config.json`，`config.py` 白名单校验）：`notify_enabled`（总开关，默认 true）、`notify_on_done`、`notify_on_attention`（默认 true）、`notify_sound`（只接受 `/System/Library/Sounds/<名>.aiff` 真实存在的普通文件名，默认 `Blow`；文件不在了读取时退回默认）。运行时覆盖 `notify_dry_run`（`app.py --no-sound`：只记日志不播放；不写盘、不给前端、不能经 API 设置）。
- API：`GET /api/notify` → `{sounds, default_sound, local_stop_hook, available}`；`POST /api/notify/test {sound?}` → 立即试听一次（不看开关；0.5s 内重复点 400）。设置经 `POST /api/config` 保存。前端：侧栏齿轮按钮 →「设置」弹窗（三个开关、声音下拉、试听、说明）。
- 本机会话防重复（预留给本机模式，jobid == `"local"`）：本机 `~/.claude/settings.json` 的 `hooks.Stop` 里已有含 `afplay` 的命令（**只读**检查，按 mtime/size 缓存）→ 跳过"完成时"（CLI 自己会响），"需要操作时"照常。设置弹窗显示说明与检测结果。
- `SessionManager(notifier=None)` 默认 `NullNotifier`（`Notifier` 的子类：状态机与回调照常，设置视为关闭、永不播放、没有试听；测试用）；`app.py` 传入真实 `Notifier(store.get)`。测试一律 mock afplay，不真的出声。
- **完成回调**（侧栏「新回复」，§15.1）：`Notifier.add_listener(fn)`。"完成"候选在 `_confirm` 里通过 muted（`quiet_until`）与 gen（确认延迟内状态没变）两步检查即为**本轮完成确认**，此时回调 `fn(sid, "done")`；之后的 disabled（总开关 / 完成时开关）、local_stop_hook、global_gap 只决定响不响，不影响回调。回调在释放 `Notifier._lock` 之后、在定时器线程里调用（锁顺序是 `s.lock → Notifier._lock`，反过来会死锁）；回调抛异常只记日志。"需要操作"不回调。

## 13. 本机选目录（系统对话框）、默认跳过权限、权限模式选择栏
需求：本机用苹果自带的"选择文件夹"对话框选启动目录；新建对话框可选 `--dangerously-skip-permissions`（启动时没带，进入对话后就切不到 bypass），且按运行位置记住上次的选择；权限模式做成选择栏，能直接看到并选择所有模式。

### 13.1 本机：系统"选择文件夹"对话框（`ccr/macdialog.py`）
- `POST /api/local/choose_folder {"default"?: 路径}`（需要 token，只在用户点击「选择文件夹…」时调用）→ `{"path": "/Users/…/dir"}`（去掉末尾 `/`，根目录 `/` 除外）或 `{"cancelled": true}`（osascript 报 -128）。
- 执行 `/usr/bin/osascript -e <固定脚本行>… [默认目录]`：argv、不经 shell；默认目录只作为 `on run argv` 的 `item 1` 传入，**绝不**拼进 AppleScript 源码。脚本：`on run argv` → `tell me to activate` → `choose folder with prompt "选择 claude 的启动目录" [default location (POSIX file (item 1 of argv))]` → `return POSIX path of f`。只用 StandardAdditions，**不**用 `tell application "Finder"` / `"System Events"`（会触发 TCC 自动化授权）。
- 默认目录：`~` / `~/…` 展开为本机 HOME，规范化后必须是已存在的目录，否则对话框不带默认位置。
- 同一时间只允许一个对话框（再请求 → 409 `dialog_busy`）；超时 10 分钟（杀掉 osascript，504 `timeout`）；其它失败 502 `dialog_error`；fake 模式 / 非 macOS / 没有 `/usr/bin/osascript` → 501 `unavailable`。
- 前端：新建会话对话框运行位置为本机时，「浏览…」变成「选择文件夹…」= 这个接口（返回后填入工作目录，取消不变）；应用内目录浏览保留为次要入口（"应用内浏览"小链接）。服务器侧不变。测试一律 mock subprocess（测试类里另有"任何未 mock 的 osascript 调用直接失败"的安全网），绝不真的弹框。

### 13.2 新建会话的默认权限（`static/launchprefs.js`）
- 「权限」下拉按运行位置分别记住用户上次的选择（`localStorage` `ccr.perm.local` / `ccr.perm.server`，创建成功时保存）。初始值：本机、服务器都 = 默认（不加参数）。preset 明确给了权限参数时以 preset 为准；从历史续接时用该运行位置记住的值。
- 已确认本机与服务器的 settings.json 都有 `skipDangerousModePermissionPrompt: true`，带该参数启动不会弹警告；万一弹出也按通用对话框显示。

### 13.3 权限模式选择栏
真机实测（claude v2.1.330）Shift+Tab 循环与模式行：不带参数 `⏵⏵ auto mode on (shift+tab to cycle)` → `⏸ manual mode on` → `⏵⏵ accept edits on (shift+tab to cycle)` → `⏸ plan mode on (shift+tab to cycle)` → auto；带 `--dangerously-skip-permissions`：`⏵⏵ bypass permissions on …` → auto → manual → accept edits → plan → bypass；带 `--permission-mode manual`：manual → accept edits → plan → auto。模式行后面可能跟 ` · ← for agents`、` · esc to interrupt`、` · 1 shell` 等。**循环顺序固定为 manual → acceptEdits → plan → [bypassPermissions，仅以跳过权限启动] → auto → manual；dontAsk 不在循环里。** fake_tui 按此顺序模拟。
- `session.mode_cycle(launch, seen)` / meta `mode_cycle`：本次启动后能循环到的模式。bypass 只在 launch 带 `skip_permissions`、或 `permission_mode == bypassPermissions`、或模式行上实际看到过时才在循环里（`modes_seen` 每次重启清空）。
- `POST /api/sessions/<sid>/mode {"mode": X}` → `SessionManager.set_mode`：先抓最新屏幕——有对话框（含 status=waiting、trust）→ 409 `dialog_open`；看不到模式行 → 409 `no_mode_line`；已经是 X → `{"ok":true,"mode":X,"steps":0}`；X 不在循环里 → 409 `mode_needs_restart`（一个键都不按）。否则每次发一个 `BTab`（guard `{input_box:true}`：agent 发键前重抓屏复核，不通过 → 409 `screen_changed`，什么都没按），发完等模式行变成新模式再决定是否继续，最多 6 次：到达 → `{"ok":true,"mode":X,"steps":n}`；模式行没变 → 409 `mode_stuck`；途中出现对话框 → 409 `dialog_open`；转回已经过的模式（转了一圈）或次数用完 → 409 `mode_unreachable`。错误信息都说明当前停在哪个模式。忙碌（生成中）也允许切换。与 `mode/cycle`（单次 Shift+Tab）共存。
- `restart(sid, …, mode=X)` / `POST /restart {"mode": X}`：覆盖权限启动参数，在"会话里改过的模型/effort/模式沿用当前值"（`_carry_launch_locked`）**之后**应用：X = bypassPermissions → `--dangerously-skip-permissions`（去掉 `--permission-mode`）；其它 → `--permission-mode X`（去掉跳过权限）。不给 mode 时行为与以前完全一致。与 v2.2.1 的"还没有对话就以新会话启动"兼容：是否带 `--resume` 由 tx_loaded/tx_conv/tx_noconv 决定，与权限覆盖互不影响；覆盖后的参数写回 launch，之后"--resume 报 No conversation found 自动以新会话重启"也沿用它。
- 前端（`static/modes.js` + `static/composerbar.js`）：输入框底栏的模式 chip（中文名，英文 id 在 tooltip；点击弹出列表，下方放不下就向上弹、不出窗口，再点一次收起）列出 手动审批 manual、自动接受编辑 acceptEdits、计划模式 plan、跳过权限 bypassPermissions、自动 auto、不询问 dontAsk（附简短说明），标出「当前」；不在本次循环里的标「需重启」。选循环里的 → `POST /mode`；选「需重启」的 → 确认框（以 --resume 重启、对话历史保留、正在生成会被中断）→ `POST /restart {"mode": X}`。列表底部与 chip 旁各保留一个「Shift+Tab」小按钮（`mode/cycle`；底栏窄时写成 `⇧Tab`）。有对话框 / 已退出 / 断线时循环里的模式不可选（「需重启」仍可选）。

## 14. 原生 Mac 应用（`tools/macapp/main.swift`，分支 feat-native）
用户问题（2026-11-23）："打开这个软件，缩放之后看它是 Chrome，并不是 CC Remote Chat"——旧 `.app` 只是 shell 启动壳，窗口其实是 `open -na "Google Chrome" --args --app=…`，所以程序坞、Cmd+Tab、最小化后显示的都是 Chrome。现在 `.app` 是 **Swift + AppKit + WKWebView 的原生应用**（单文件，不用 Xcode 工程）。§0 红线不变：原生应用不碰 Claude Code 的任何配置/提示词/hook，不读写 `~/.claude*`，只读写数据目录里的 `window_request`、`window_login_url`、`logs/launcher.log`。

### 14.1 组成与进程关系
- 后端仍是 `python3 app.py`，**独立的后台进程**：关窗口、退出本应用（⌘Q）都不停后端（与 Chrome 方案一致）；远端/本机的 claude 本来就不受影响。停止后端仍用 `python3 app.py --stop`。
- 原生应用 = 窗口 + 菜单 + WKWebView（显示 `http://127.0.0.1:<端口>/`），以及"取登录链接 / 必要时启动后端"。认证不变：仍是一次性登录码（120 秒、只能兑换一次，放在 URL fragment）换 `X-CCR-Token`。
- 原生应用自己的数据只有系统按 bundle id 保存的：UserDefaults（缩放 `CCRPageZoom`、窗口位置）与 WebKit 存储（`~/Library/WebKit/<bundle id>`、`~/Library/Caches/<bundle id>` 等，里面是页面的 localStorage / sessionStorage）。

### 14.2 登录链接交接（`app.py`，`tests/test_app_window.py`）
- 原生应用先建 `<数据目录>/window_request`（0600），再运行 `python3 app.py`：没有实例就启动新实例；已有实例时沿用原逻辑发 SIGUSR1 → `reopen`。两条路径最终都调用 `open_window(server, data_dir, no_browser)`。
- `open_window` 每次生成新登录码；`take_window_request(data_dir)`：请求文件存在、是本人的普通文件、60 秒内创建（`WINDOW_REQUEST_MAX_AGE`，原生应用最多等 25 秒；容许 5 秒时钟偏差）→ 删掉它，`write_window_login_url` 把 URL **原子**写入 `<数据目录>/window_login_url`（同目录 mkstemp 0600 → fsync → `os.replace`，目标是软链接时替换链接本身），**不再**打开 Chrome，返回 `"native"`；日志只记"已把登录链接交给原生应用窗口"，不记链接。过期 / 软链接 / 不是普通文件的请求同样删除，但按原行为开窗（防残留请求劫持之后在终端里手动运行的实例）。没有请求 → 原行为（Chrome 应用窗口，或 `--no-browser` 打印）。有请求时 `--no-browser` 也写文件（请求优先）。写文件失败只记日志（返回 `"error"`，不退回去开 Chrome；原生窗口会超时并显示错误页）。
- 测试：请求的存在/过期/未来时间/软链接/目录、原子写与 0600/覆盖/不跟随软链接/失败清理、三种去向（浏览器用 mock）、请求失败不开 Chrome；fake 端到端（临时数据目录、`--port 0`）：新启动 → 写文件；再运行 app.py → SIGUSR1 → 写文件（新登录码、同端口）；没有请求 → 打印；`--stop`。

### 14.3 原生应用：连接（后台线程，`Backend.connect`）
1. 校验 Info.plist 的 `CCRPython`（可执行）与 `CCRAppPy`（存在），否则直接显示错误页（"重新运行 tools/make_app.sh"）。
2. 建数据目录与 `logs/`（0700）→ 删旧的 `window_login_url` → 删旧请求后 `O_CREAT|O_EXCL|O_NOFOLLOW` 0600 新建 `window_request`。
3. `posix_spawn` 运行 `<python> <app.py> [--data-dir D] [CCRArgs…]`：`POSIX_SPAWN_SETSID`（新会话，与本应用脱离）、`POSIX_SPAWN_CLOEXEC_DEFAULT`（只继承下面 3 个 fd）、信号屏蔽字清空 / 处置复位；stdin=`/dev/null`，stdout/stderr 追加到 `<数据目录>/logs/launcher.log`（0600），cwd=HOME。后台线程 `waitpid` 回收（常驻后端会一直阻塞在那里，属正常）。
4. **环境变量白名单**：`HOME USER LOGNAME SHELL TMPDIR SSH_AUTH_SOCK LANG __CF_USER_TEXT_ENCODING`（没有 LANG 时设 `zh_CN.UTF-8`；`__CF_USER_TEXT_ENCODING` 与终端.app 新开窗口一致、不是凭据），`PATH=/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin`。绝不把调用者的 `CLAUDECODE`、`CLAUDE_CODE_*`、`ANTHROPIC_*` 等带进后端（之前出过问题）。实测后端进程的环境只有这些键（外加 python.org 框架版 python 自己设的 `__PYVENV_LAUNCHER__`）。
5. 每 0.1 秒轮询，最多 25 秒：读到 `window_login_url`（`O_NOFOLLOW`、本人的普通文件、<4KB、内容必须是 `http://127.0.0.1:<端口>/#code=…`）→ 删掉 → 主线程 `webView.load`。app.py 非 0 退出（如"无法确定其进程号"）→ 立即失败；超时 → 删掉 `window_request` 后失败。
6. 失败 → WKWebView 里显示本地错误页（`loadHTMLString`，无脚本）：失败原因、launcher.log 路径、提示"后端是旧版本（不支持原生窗口，会改开 Chrome）时先 `--stop`"、「重试」与「在 Finder 中显示日志」按钮（`ccr-native://retry`、`ccr-native://reveal-log`，只在主框架且当前是本地页时处理）。连接中显示"正在连接后端…"页（已在显示应用页面时不替换）。原生应用自己的事件也以 `[原生应用]` 前缀追加到 launcher.log。

### 14.4 重连
- 后端存活判断 `Backend.livePid`：读 `app.pid`（`{"pid": N}`）→ `kill(N, 0)` → `KERN_PROCARGS2` 读 argv，必须有 `app.py`（防 pid 复用）。
- 用户再次打开应用（`applicationShouldHandleReopen`：点程序坞图标 / 再次 `open`）或应用变为活动：后端不在 → 重新连接（会启动后端）；pid 与上次连接（成功或失败）时不同 → 重新取登录链接；页面正显示"会话已失效" → 重新取；否则只把窗口提到前面。上次失败时后端还活着（例如旧版后端）就不在每次激活时重试，等用户点「重试」/⌘R。
- 每 3 秒查一次 pid：变成新的存活 pid（后端被重启过，例如部署）→ 自动重连；后端死了**不**自动拉起（可能是用户主动 `--stop`；页面自己显示"连接断开，正在重连…"）。
- "会话已失效"探测：在**隔离的** `WKContentWorld.defaultClient` 里注入一段只看 `body > .auth-lost` 是否出现的脚本（MutationObserver 只观察 body 的直接子节点），经同一隔离世界的 `ccrNative` 消息通知原生应用；页面脚本看不到这个 handler；只接受主框架、本应用 origin 的消息。后端活着时自动重取登录链接（5 分钟内最多 3 次）。
- 加载新登录链接：页面已在 `/` 时是同文档 fragment 跳转 → 前端原有的 `hashchange` 处理兑换登录码并 `location.reload()`；在本地页时是普通加载。
- WebContent 进程崩溃 → 应用页面 `reload()`（token 在 sessionStorage，实测崩溃后仍在，无需重新登录；万一丢了会走上面的"会话已失效"自动重取）；本地页则重新显示/重新连接。

### 14.5 窗口与菜单
- 标题跟随 `document.title`（KVO `webView.title`，空时「CC Remote Chat」）；标题前缀 `(n) ` = 有新回复的会话数 → 程序坞角标（§15.1）；`setFrameAutosaveName("CCRMainWindow")` 记住位置大小；`tabbingMode = .disallowed`、`isRestorable = false`。
- 关闭（⌘W / 红色按钮）= `orderOut` 隐藏，不销毁 WebView、不断开 SSE；全屏时先退出全屏再隐藏；点程序坞图标重新显示（最小化的先还原）。`applicationShouldTerminateAfterLastWindowClosed` = false。
- 菜单（中文）：应用菜单（关于、隐藏 ⌘H、隐藏其他 ⌥⌘H、全部显示、退出 ⌘Q）；编辑（撤销 ⌘Z、重做 ⇧⌘Z、剪切、拷贝、粘贴、全选——**必须有**，否则 WKWebView 里 ⌘C/⌘V 不工作）；显示（重新载入 ⌘R、实际大小 ⌘0、放大 ⌘=、缩小 ⌘-、进入全屏 ⌃⌘F）；窗口（最小化 ⌘M、缩放、关闭 ⌘W、前置全部窗口；`NSApp.windowsMenu`）。系统自动补的项（听写、表情与符号、全部关闭、平铺…）保留。缩放用 `pageZoom`（0.5–3.0 共 13 档），存 UserDefaults `CCRPageZoom`。
- `CFBundleDevelopmentRegion=zh_CN` + `CFBundleAllowMixedLocalizations`：系统提供的文字（关于面板、NSAlert、右键菜单、窗口菜单自动项）跟随系统语言显示中文。

### 14.6 WKWebView 行为
- 外部打开（`NSWorkspace.shared.open`，只允许 http/https/mailto，其它协议如 `file:` 忽略并记日志）：`target=_blank`（targetFrame 为 nil）、`window.open`（`createWebViewWith` 返回 nil）、主框架里跳到非本应用 origin 的导航、本应用的 `/preview/…` 地址（"浏览器打开""在浏览器中打开"）。子框架（预览 iframe）照常加载 http/https/about/data/blob。
- 下载（WKDownload）：`<a download>`、blob/data 链接（`shouldPerformDownload`）、`canShowMIMEType == false`、`Content-Disposition: attachment` → 保存到 `~/Downloads`（文件名取最后一段、去掉控制字符和 `/ : \`、去掉开头的点，重名加 ` (1)`），完成后发 `com.apple.DownloadFileFinished` 让程序坞"下载"叠放跳一下；失败弹 NSAlert。
- JS `alert`/`confirm`/`prompt` → NSAlert（窗口可见时用 sheet）；`<input type=file>` → NSOpenPanel（前端目前没有用到）。
- **拖入文件**（§9.8）：WebView 是 `CCRWebView`（`WKWebView` 子类），重写 `draggingEntered/Updated/Exited`、`prepareForDragOperation`、`performDragOperation`、`concludeDragOperation`：拖动粘贴板（不是系统剪贴板）上有本机文件 URL（`readObjects(forClasses:[NSURL], options:[.urlReadingFileURLsOnly])`，最多 20 个）且当前显示的是本应用页面（`pageState == .app`）时，这次拖动整个由本类处理、WebKit 完全不知道：进入 / 离开 / 放下分别在页面里派发 `ccr-native-drag`（`{phase:"enter"|"leave", paths}`）/ `ccr-native-drop`（`{phase:"drop", paths}`）事件（脚本由 `ccrDropEventScript` 用 `JSONSerialization` 生成，路径只是字符串内容），光标显示"拷贝"；不读文件内容、不转义（转义在页面 / 后端做，有单测）。其它拖动（页面里拖文字、侧栏排序、从网页拖来的没有文件 URL 的图片、错误页上的拖动）一律交给 WebKit；文件被当成导航时照旧被导航策略拦下（`file:` 不打开）。手工验证：打开原生窗口进入一个本机会话 → 从 Finder 拖一张 png 进窗口（出现覆盖层「松开即可 · 1 张图片…」）→ 松开后输入框顶部出现 `#N` 图片条、终端面板里 claude 输入框是 `[Image #N]`；截屏（⌘⇧4）后把右下角浮动缩略图拖进窗口同样；拖一个 .py 文件 → 转义后的路径插进输入框；服务器会话拖入 → 提示不支持。
- `isElementFullscreenEnabled = true`（12.3+，"网页预览"卡片的全屏）；`isInspectable = true`（13.3+，可用 Safari 的"开发"菜单调试）；`javaScriptCanOpenWindowsAutomatically = false`；`allowsMagnification`/返回手势关闭；UA 追加 `CCRemoteChat/1.0`；数据存储 `WKWebsiteDataStore.default()`（按 bundle id 持久化 localStorage）。

### 14.7 Info.plist 与 `tools/make_app.sh`
- 有 `xcrun swiftc`：`-O -swift-version 5 -target <arch>-apple-macos12.0 -framework Cocoa -framework WebKit`，可执行文件名 = `CFBundleExecutable` = 「CC Remote Chat」（ps、活动监视器、System Events 里的进程名也一致）；Info.plist 由 plistlib 生成：`CFBundleName`/`CFBundleDisplayName` = CC Remote Chat、`CFBundleIdentifier=local.cc-remote-chat`、图标 `AppIcon`、版本取 `git describe --tags --abbrev=0`、**没有** `LSUIElement`、`NSHighResolutionCapable`、`NSPrincipalClass=NSApplication`、`NSAppTransportSecurity{NSAllowsLocalNetworking}`、`LSMinimumSystemVersion=12.0`、`CCRPython`/`CCRAppPy`。然后 `codesign --force --sign - --timestamp=none <bundle>`（ad-hoc）、`touch`、`lsregister -f`。
- 没有 swiftc / 编译失败 / `CCR_NO_NATIVE=1`：回退为原来的 shell 启动壳（`LSUIElement`、Chrome 应用窗口），并打印提示。
- 仅自测用的环境变量（正式版不写这些键）：`CCR_BUNDLE_ID`（如 `local.cc-remote-chat.test`，与正式版的 LaunchServices 注册、UserDefaults、WebKit 数据分开）、`CCR_DATA_DIR`（→ `CCRDataDir`，后端加 `--data-dir`，launcher.log 也写在这里）、`CCR_ARGS`（→ `CCRArgs` 数组，如 `--fake --port 18765 --no-sound --no-browser`）、`CCR_SANDBOX_PROFILE`（→ `CCRSandboxProfile`，后端用 `/usr/bin/sandbox-exec -f <profile>` 包一层）、`CCR_DOWNLOAD_DIR`（→ `CCRDownloadDir`）、`CCR_SELFTEST=1`（`-D CCR_SELFTEST`：`<数据目录>/selftest/cmd-*.json` 命令文件驱动 eval / 合成点击与按键 / 菜单 / 关窗 / 重开 / 快照 / 网页进程 pid；外部链接只记录不打开；不提供读写系统剪贴板的操作。正式版不编译这段代码）。

### 14.8 兼容性（自测版 + fake 后端，WKWebView 实测，macOS 27.2 / arm64）
- 可用：登录码换 token；SSE（"已连接"、流式回复、后端重启后自动重连并重新附着 3 个会话）；`_check_origin`（WebKit 同源 POST/GET、SSE 全部通过，Host=127.0.0.1:端口）；剪贴板（代码块"复制"的 `navigator.clipboard.writeText` 成功、灯箱"复制图片"的 `ClipboardItem` 成功——127.0.0.1 是安全上下文；⌘A/⌘C/⌘V/⌘X/⌘Z/⇧⌘Z 经编辑菜单可用）；预览 iframe（sandbox + CSP 的网页预览卡片与右侧面板，样式/脚本/图片子资源正常）；灯箱（打开、+/- 缩放、下载、Esc）；提示条（toast、快捷键提示条与"转到后台运行"）；设置弹窗；主题与彩色区分切换（localStorage 持久化，重开应用后保留）；新建会话对话框（`<dialog>` showModal、运行位置切换）；PDF 预览（不加 sandbox 的 iframe 里由 WebKit 内置 PDF 查看器正常显示）；JS confirm（NSAlert sheet）；下载（`<a download>`、attachment、重名去重）；外链 / window.open / 预览地址交给默认浏览器、`file:` 被忽略。
- 差异与改动：① WebKit 里占位文字比输入框宽时会算出横向溢出，接了鼠标（传统滚动条）时横向滚动条把单行输入框挤得只剩半行 → `.composer-box textarea` 加 `overflow-x: hidden`；② 输入框隐藏时 `autosize()` 会把高度写成 0px（被 min-height 撑到 26px、比一行矮，出现竖向滚动条）→ `scrollHeight` 为 0 时保持 auto；③ Safari/WKWebView 的触控板捏合不发 ctrl+wheel，而是非标准 `gesturestart/gesturechange` → 灯箱加了这组监听（Chrome 没有这些事件，不受影响；真机捏合待验证）。
- 未实测（避免打扰正在使用这台 Mac 的用户）：⌃⌘F 窗口全屏与网页元素全屏（只确认 `document.fullscreenEnabled` 为 true）；`<input type=file>`（前端没用到）；Stage Manager 开启时 ⌘M 后 `isMiniaturized` 仍为 false（⌘M 已送达窗口，最小化后在程序坞里的标题 = 窗口标题）。

### 14.9 已知限制
- Chrome 应用窗口里 localStorage 存的偏好（主题、彩色区分、侧栏宽度、上次的运行位置/权限/目录、历史面板位置等）**不会迁移**到原生应用（WebKit 的存储按 bundle id 单独保存），需要重新设一次。
- ad-hoc 签名：每次重新生成 `.app` 签名都会变，macOS 可能把它当成新应用，之前授予的"文件夹访问 / 自动化"等隐私权限要重新允许（后端子进程的这类权限归属本应用）。Gatekeeper 不影响本机生成的应用。
- 正在运行的后端如果是旧版本（不认识 `window_request`），收到 SIGUSR1 会照旧打开 Chrome，原生窗口 25 秒后显示错误页：部署后先 `python3 app.py --stop` 再打开应用。
- 项目目录移动或换了 Python 后需要重新运行 `tools/make_app.sh`（路径写在 Info.plist）。

### 14.10 内置模式与 dmg（`tools/make_dmg.sh`，给没有开发环境的人用）
目标：Apple 芯片 Mac、macOS 12+，对方只要自己装好 Claude Code 并在终端里登录过一次；双击 dmg → 把 app 拖进「应用程序」→ 第一次打开在「系统设置 → 隐私与安全性」点一次「仍要打开」（ad-hoc 签名，没有开发者账号）即可用，不需要 Homebrew、Python、tmux 或任何配置。**所有新逻辑只在内置模式下生效**；没有 `CCRBundled` / `--bundled` 时（从源码运行、tools/make_app.sh 生成的 app）行为、argv、keeper 命令逐字不变（`tests/test_bundled.py` 锁定）。
- **组成**：`Contents/MacOS/CC Remote Chat`（原生壳，`-target arm64-apple-macos12.0`）；`Contents/Resources/`：`AppIcon.icns`、`python/`（python-build-standalone 20261110 的 CPython 3.12.16 `install_only_stripped`，裁剪掉 include/share/libpython 动态库/tcl-tk/idlelib/tkinter/turtledemo/ensurepip/lib2to3/pydoc_data/venv/config-3.12-darwin/pip/`_tkinter.so` 与 bin 里的多余文件）、`app/`（只有 `app.py`、`ccr/`、`static/`（不含 `static/dev/`）、`assets/`）、`bin/tmux`（tmux 3.6a，静态链接 libevent 2.1.12 与 utf8proc 2.11.3，动态链接系统 ncurses，`-mmacosx-version-min=12.0`，`--sysconfdir=/etc`）、`licenses/`（本项目 LICENSE/THIRD_PARTY_NOTICES 与 Python、tmux、libevent、utf8proc 的许可证，`BUNDLED_COMPONENTS.txt` 记版本、URL、SHA256）。
- **Info.plist**：`CFBundleIdentifier=local.cc-remote-chat.bundled`（与 tools/make_app.sh 生成的 `local.cc-remote-chat` 分开：LaunchServices、UserDefaults、WebKit 存储、隐私授权都不混）、`CCRBundled=true`、`CCRDataDir=~/.cc-remote-chat-app`（不带空格）、`LSMinimumSystemVersion=12.0`、`LSArchitecturePriority=[arm64]`、版本号（`--version` > `git describe --tags` > 项目根目录 `VERSION` 文件）、`NS*FolderUsageDescription`（系统询问「文稿/桌面/下载」访问权限时显示的说明）。
- **原生壳（`CCRBundled=true`）**：python = `<Resources>/python/bin/python3`，app.py = `<Resources>/app/app.py`；后端 argv = `[python, -I, -B, app.py, --bundled, --data-dir <CCRDataDir 展开>]`（`-I`：不看 `PYTHON*` 环境变量与用户 site-packages；`-B`：不往 .app 里写 `__pycache__`，否则会破坏签名）。环境变量白名单与 §14.3 相同。
  - 放错位置不启动后端：bundle 路径含 `/AppTranslocation/`（Gatekeeper 隔离运行，路径每次都变）、在 `/Volumes/` 下（还在 dmg 里）、或所在卷只读（`statfs` 的 `MNT_RDONLY`）→ 只显示「请先把 CC Remote Chat 拖到『应用程序』文件夹，再从那里打开」页面。
  - `livePid` 比对**完整的** app.py 路径；`app.pid` 里是活着的 app.py 但路径不是本 .app 的（app 被挪过位置 / 换了新版本放在别处）→ 先 SIGTERM 旧后端、最多等 15 秒退出（释放实例锁），再启动新的；tmux 里的 claude 不受影响。
  - 错误页不提 `make_app.sh`，改为「重试 / 退出重开 / 把日志发给给你 app 的人」。
- **升级（新 .app 原地替换旧 .app）**：后端在关窗口、⌘Q 后仍常驻，替换后路径不变，原生壳的 `livePid` 仍认它是自己的、`stalePid` 不触发；壳照常运行新的 app.py，以前新 app.py 发现已有实例只会 SIGUSR1 后退出 → 用户连上的一直是旧版 Python 后端（磁盘上的 `static/` 已是新版，前后端混用），也没有界面能重启后端。现在在 **app.py 的「已有实例」分支**处理（不改原生壳）：
  - 构建标识：`tools/make_dmg.sh` 写 `<Resources>/app/BUILD`（一行：`版本 构建时间(UTC) app 目录的代码哈希前 16 位`，同版本重新打包也不同；在预编译与签名之前写，属于签名内容）。`app.py --bundled` 读它（`read_build_id`：可打印 ASCII、≤200 字，否则当没有），拿到实例锁后写进 `app.pid`：`{"build": "…", "pid": N}`（`pid` 字段不变，原生壳只读 `pid`）。**非内置模式不读 BUILD，`app.pid` 仍只有 `{"pid": N}`**；deploy.sh 不读 `app.pid`。
  - 已有实例、本该发 SIGUSR1（重新开窗）时：自己有标识且与 `app.pid` 里的不同（包括旧版根本没有标识，例如 2.9）→ SIGTERM 旧后台，每 0.2 秒尝试拿实例锁，最多 15 秒（`TAKEOVER_TIMEOUT`，与壳的 `stopStale` 相同；旧后台退出时先关 HTTP 端口、断开 agent，再删 `app.pid`、释放锁），拿到后照常启动成为新实例（`window_request` 仍在，`open_window` 照常写 `window_login_url`），日志记「已停止旧版本的后台（pid、构建）由本进程接管」。标识相同 / 自己没有标识（从源码运行 `--bundled`）→ 照旧 SIGUSR1。`--stop`（SIGTERM）与 `--jobid --no-browser`（SIGUSR2）不变。
  - 15 秒内没拿到锁 → stderr（launcher.log）「旧版本的后台（pid N）15 秒内没有退出；没有强行结束它（tmux 里的 claude 不受影响）。请稍后重试」，退出码 1 → 壳立即显示「后端启动失败（app.py 退出码 1）」错误页（有「重试」）。绝不 SIGKILL。
  - tmux 里的 claude 不受影响（与 deploy.sh 重启后台同理：keeper 保活，新后台重新附着 `sessions.json` 里的会话）。原生壳：spawn 的 app.py 就是新后台，`window_login_url` 在 `app.pid` 更新之后才写，`connect` 成功时 `livePid` 读到的就是新 pid（路径相同，通过 argv 校验）→ `knownPid` = 新 pid，`checkPid` 不会再触发多余的重连；若窗口原本开着（旧页面），3 秒轮询看到 pid 变化也会自动重取登录链接、加载新版页面。
  - 测试（`tests/test_bundled.py`）：BUILD 读取与校验、`app.pid` 只在给了标识时带 `build`；进程内（假的旧实例拿住锁）：旧后台不退出 → 退出码 1、只发过一次 SIGTERM、旧进程仍活着；标识相同 / 自己没有标识 / 非内置模式 → SIGUSR1；`--stop`、`--jobid --no-browser` 不变；端到端（fake、临时数据目录与 TMUX_TMPDIR、`--port 0`）：无标识的旧实例 → 新构建接管（旧进程退出码 0、新 `app.pid` 带标识、拿到登录链接）→ 同一构建再运行只 SIGUSR1（同端口、新登录码）→ 另一构建再接管；非内置模式即使有 BUILD 也只 SIGUSR1、`app.pid` 只有 pid。
- **后端（`app.py --bundled`）**：运行时配置（不写盘）`local_tmux=<app.py 所在目录>/../bin/tmux`、`tmux_label=ccr-app`；没有 `--data-dir` 时默认 `~/.cc-remote-chat-app`。`public_config` 额外给前端 `tmux_label`（界面上"本机 tmux（-L …）"的说明据此显示；平时没有这个键）。
- **agent_link**：本机 agent argv 追加 `--tmux=<路径> --tmux-label=ccr-app`（只在运行时配置给了这两项时；路径允许空格，`_TMUX_PATH_RE` 放开空格，进 keeper 脚本前一律 `shlex.quote`）；本机 keeper 与 fake 的本机 keeper 用同一标签；fake 的 `CCR_FAKE_TMUX` 优先用 `local_tmux`。后端以 `-B` 运行（`sys.flags.dont_write_bytecode`）时，agent、fake_tui、历史脚本等 Python 子进程也加 `-B`。
- **node_agent `--local`**：`--tmux=` 指定的 tmux 只用它（不可执行就报错，不退回 PATH / Homebrew）；`--tmux-label=` 换掉 `-L ccr`；取值不合法直接退出（宁可起不来也不退回 `-L ccr`）。远端（`python3 -I -u -c <源码>`，argv 只有 `-c`）不受影响。
- **terminfo**：macOS 14 起系统才有 `/usr/share/terminfo/74/tmux-256color`。本机 agent 的 `start` 在该文件不存在时命令前加 `set-option -g default-terminal screen-256color ;`（macOS 12/13）；文件存在时（14+）命令与以前完全相同。所有本机会话都做这个判断（不只内置模式）：它只在缺文件时生效，14+ 上行为不变，用 Homebrew tmux 从源码运行的用户在 12/13 上也受益。不打包 terminfo、不设 `TERMINFO_DIRS`。
- **找不到 claude**：本机会话退出码 127（登录 shell 里没有 `claude`）时，时间线提示改为「找不到 claude 命令：请先安装 Claude Code，并确认在『终端』里能运行 claude」（`static/exitnotice.js`）；服务器会话与其它退出码文字不变。
- **`tools/make_dmg.sh`**：下载件固定版本 + URL + SHA256，缓存在 `build/dmg-cache/`（gitignore）；整个组装在 `mktemp -d /tmp/ccr-dmg.XXXXXX` 里进行（swift 源码也先拷进去再编译），PATH 只有系统目录、`PKG_CONFIG_LIBDIR` 指向自建依赖（不让 Homebrew 混进来）；`compileall --invalidation-mode unchecked-hash -s <Resources> -p /` 预编译标准库与 `app/`（pyc 里没有构建路径，运行时不检查 mtime、也不会回写）；`chmod`、`xattr -cr`；逐个 `codesign -f -s - --timestamp=none` 所有内部 Mach-O，最后签外层 app（不用 `--deep` 签名），`codesign --verify --deep --strict` 校验；暂存目录放 app、`Applications -> /Applications`、`先读我.txt`，`hdiutil create -fs HFS+ -format UDZO -imagekey zlib-level=9` 输出 `dist/CC Remote Chat <版本>.dmg`。不用 Finder/AppleScript 布置 dmg 窗口，不弹任何 GUI、不用 `open`。
- **数据与进程**：数据目录 `~/.cc-remote-chat-app`、tmux `-L ccr-app`，与从源码运行的实例（`~/.cc-remote-chat`、`-L ccr`）互不干扰；关掉窗口后后端与 claude 继续在后台运行，下次打开自动接上（§14.4）。claude 由 .app 自带的 tmux 启动，系统的「文稿/桌面」等隐私询问会以「CC Remote Chat」的名义弹出。
- **验证（不开 GUI）**：`hdiutil attach -nobrowse -readonly -noautoopen` 后 `codesign --verify --deep --strict`；把 app 拷到带空格的临时路径，`env -i` 下直接运行 bundle 里的 `python3 -I -B app.py --bundled --fake …`，headless Chrome 建会话收发消息；再用 `sandbox-exec` 禁止读 `/opt/homebrew`、`/usr/local`、`/Library/Frameworks/Python.framework`、`/Library/Developer` 重跑；运行前后 bundle 内文件不变；`otool -l` 看所有 Mach-O 的 minos ≤ 12.0、arm64，`otool -L` 只依赖 `/usr/lib` 与 `/System`；对解开的所有文件（二进制用 `strings`）做隐私扫描。


### 14.11 dmg 版自动更新（`ccr/updater.py`、`static/update.js`、`tools/release_key.sh`、`tools/release.sh`）
用户需求（2026-11-24）：dmg 版提示有新版本、用户点「立即更新」后一键更新（不静默）。发布在 GitHub 公开仓库的 Release 上。**只在内置模式、且 .app 里有 `Resources/app/UPDATE.json` 时启用**；从源码运行（私有版、make_app.sh 的 app）不创建更新器：没有 `/api/update*`、bootstrap 没有 `update` 键、SSE 不推 `event: update`，argv、app.pid、`--help` 与以前相同（`--update-test` 是 `argparse.SUPPRESS` 的测试钩子）。
- **发布签名**：`tools/release_key.sh init` 生成自签名代码签名证书（`/usr/bin/openssl`，RSA 3072，CN「CC Remote Chat Release」，codeSigning EKU，10 年）放在 `~/.cc-remote-chat-release/`（0700）：单独的 `release.keychain-db`（`security create-keychain -p`，**不加入搜索列表、不碰登录钥匙串**；`import -T /usr/bin/codesign` + `set-key-partition-list -S apple-tool:,apple:,codesign: -k`，codesign 用它不弹授权框）、`keychain-password`（0600）、`release-key.p12`（同一密码加密的备份）、`cert.pem`、`fingerprints.txt`；已存在就拒绝（绝不覆盖）；前后核对 `security list-keychains -d user` / `default-keychain` 不变；最后在 /tmp 的小 bundle 上自检签名 + 钉死要求。好处：更新来源可验证；指定要求稳定（`identifier … and certificate leaf = H"…"`），「文稿 / 桌面」等隐私授权在更新后保留。Gatekeeper 照旧（没有公证）：第一次仍要「仍要打开」。
- **`make_dmg.sh --release-key <目录> [--update-repo owner/name]`**：用 `codesign -s <证书 SHA-1> --keychain <钥匙串>` 逐个签内部 Mach-O、最后签外层（签名前 `security unlock-keychain -p`，签完 `lock-keychain`）；`--update-repo` 把 `{"cert_sha1", "cert_sha256", "repo"}` 写进 `Resources/app/UPDATE.json`（与 BUILD 一起在预编译与签名之前写）；签完再用 `codesign --verify --deep --strict -R "=identifier \"local.cc-remote-chat.bundled\" and certificate leaf = H\"<sha1>\""` 校验。`--update-repo` 必须同时给 `--release-key`；都不给 = ad-hoc + 自动更新关闭（别人自己从开源版打包）。
- **检查**（`Updater._loop`）：后端启动 30 秒后一次、之后每 24 小时一次；设置里「自动检查更新」（默认开，存 `<数据目录>/updates/state.json`，不进 config.json）+「立即检查」（不看开关）。GET `https://github.com/<repo>/releases/latest/download/update.json`（不用 API）。`urllib` + 显式 SSL 上下文（`/etc/ssl/cert.pem`、校验主机名、TLS ≥ 1.2、不走代理环境变量、不带 Cookie）；起始地址与每一跳重定向都必须是 https 且主机在白名单（github.com、objects / release-assets / github-releases.githubusercontent.com）；清单 ≤ 64KB、超时 30 秒；UA `CC-Remote-Chat/<版本>`，不带任何用户信息。404（仓库还没有 Release）→ 结果 `none`，不提示、不算错误（2026-11-24 对真实仓库实测：github.com 直接 404，无重定向）。
- **清单**：`{version, build, dmg: {name, sha256, size}, notes, min_macos}`，严格校验（版本 `^\d+(\.\d+){0,3}(-后缀)?$`、name `^[A-Za-z0-9][A-Za-z0-9._-]*\.dmg$`（GitHub 会把资源名里的空格换成点，所以不带空格）、sha256 小写 64 位、0 < size ≤ 512MB、notes ≤ 8000 字）；dmg 地址 = `https://github.com/<repo>/releases/download/v<version>/<name>`（不信任清单里的任何 URL）；版本的数字部分严格大于当前（`BUILD` 第一段）才算新版；`min_macos` 高于本机 → `unsupported`，不提示。
- **界面**（`static/update.js`，只有 bootstrap 带 `update` 时才挂）：主区域最上方、会话头之上的一条提示。新版本可用：「新版本 X 可用 · 更新说明（展开纯文本 + 在 GitHub 上查看）· [立即更新] [稍后]」（稍后 = 24 小时内不再提示同一版本）；等待：「等所有会话空闲后自动开始更新到 X（还在忙：…）[取消]」；下载 %（进度条）→ 校验 → 安装 → 「安装完成，即将重新打开窗口…」；失败：原因 + [重试] [打开下载页] [关闭]；无法自动替换：原因 + [打开已下载的安装包] [重试] [打开下载页] [关闭]；新版本第一次启动：「已更新到 X」[知道了]（确认前一直显示）。所有请求只在用户点击时发出；「打开下载页」「在 GitHub 上查看」是 `target=_blank` 链接（原生壳交给默认浏览器）。状态经 `event: update` 实时推送。设置弹窗多一节「更新」。回放自测页 §14c 覆盖各状态、点击才请求、浅色 / 深色对比度。
- **网络**（2.14 起；用户要求：不绕过系统代理、不直连、只走 GitHub 地址）：跟随 macOS 系统代理（`_scproxy`，python-build-standalone 内置；不看环境变量）。设了 HTTPS / HTTP 代理 → 每个请求（含每一跳重定向）都经该代理的 CONNECT 隧道，不做 proxy_bypass、代理连不上就报错、**不回退直连**；只有 PAC / 自动发现 / SOCKS（`scutil --proxy` 只读判断）→ 不发任何请求，状态里写明「暂不支持，未连接 GitHub」；完全没设代理 → 直接访问（与浏览器一致）。白名单（github.com、release-assets / objects / github-releases.githubusercontent.com）、TLS 校验、大小上限、sha256、签名钉死都不变。错误「无法连接 <主机>：<原因>（经系统代理 <主机:端口> / 未设置系统代理）」；日志只写主机名（下载 URL 带临时签名，不进日志与状态）。状态多 `network`、`hosts`、`available.snoozed_until`。2.12 / 2.13 强制直连、忽略系统代理：需要代理才能上 GitHub 的网络里它们检查不到新版，只能手动装一次 2.14。
- **设置「更新」一节**（2.14 起）：有新版本时**始终**显示「发现新版本 X」+ 立即更新 + 更新说明 + 打开下载页（与提示条共用 `UpdateBox`，进度 / 等待空闲 / 失败 / 手动安装同步）；显示「已暂时隐藏提醒（… 后再提醒）」、会访问的域名、网络方式（经系统代理 … / 未设置系统代理 / PAC·SOCKS 暂不支持）。提示条「稍后」改为「24 小时后再提醒」（按版本记，新版本不受影响）；**用户点「立即检查」时清除**，自动检查不清除（2.12 / 2.13 的「立即检查」不清除，所以设置里只看到「发现新版本」却没有安装按钮——用户实测问题）。
- **安装**（后台线程，用户点击后）：
  1. 会话忙（`busy_titles`：回复中 / 等审批或信任目录 / 启动中 / 有子代理在干活）→ `waiting`，每 2 秒看一次，可取消；
  2. 下载到 `<数据目录>/updates/<版本>/<name>`（目录 0700；`.part` → 校验 size + sha256 → `os.replace`；已下载且校验通过的直接用）；
  3. `hdiutil attach -nobrowse -readonly -noautoopen -mountpoint /tmp/ccr-upd-mnt.*` → 顶层唯一的 `.app` → `verify_app`：`CFBundleIdentifier=local.cc-remote-chat.bundled`、`CCRBundled=true`、`CCRVersion` 等于清单版本且大于当前、`Resources/app/BUILD` 等于清单 build、`codesign --verify --deep --strict -R "=<钉死要求>"`（证书 SHA-1 来自**当前**运行的 app 的 UPDATE.json）、`codesign -d --extract-certificates` 取叶证书比对 SHA-256 → `ditto` 到当前 .app 同目录的 `.CC Remote Chat.app.new-<版本>`（同一卷）→ 对副本再校验一次 → detach；
  4. 换装（`swap_bundles`）：rename 当前 .app → `.CC Remote Chat.app.old-<当前版本>`，rename 新 .app → 原名；第二步失败把旧的挪回来。EPERM / EACCES / EROFS（非管理员、macOS 13+「App 管理」拦截）或 ditto 没有写权限 → 删掉临时副本、当前 .app 原样保留，进入 `manual`：提示到「系统设置 → 隐私与安全性 → App 管理」允许后重试，或「打开已下载的安装包」（用户点击时 `open <dmg>`，由用户拖进「应用程序」）；
  5. 记 `state.json` 的 `installed = {version, from, old_bundle, at, acked:false}` → `relaunching`：找本 bundle 的原生壳（`proc_listallpids` + `proc_pidpath`，可执行路径必须正好是 `<bundle>/Contents/MacOS/<CFBundleExecutable>`；`window_request` 的内容（壳写的是它自己的 pid，app.py 现在读出来交给更新器）与 `getppid()` 只是优先看，同样要核对路径；换装**之前**找，因为换装后壳的路径变成隐藏旧名）→ SIGTERM → 最多等 5 秒退出（僵尸进程算已退出）→ `/usr/bin/open <bundle>`（启动器可注入，测试里只记录）。没有退出 / open 失败 → `failed`「新版本已安装…请退出后重新打开」。新壳 → 新 app.py → §14.10「升级」逻辑 SIGTERM 本进程（旧后台）并接管；tmux 里的 claude 不受影响。60 秒后还没被接管 → `failed`「请从『应用程序』重新打开」。不改原生壳（Swift）。
  6. 新版本启动（`_after_update`）：`installed.version == 当前` → `just_updated`；删掉 `old_bundle`（必须与当前 bundle 同目录、名字以 `.<bundle 名>.old-` 开头、是目录不是软链接）与 `updates/<版本>/` 下载件。
- **发布**（`tools/release.sh <prev> <new> --version X.Y`，私有，export-ignore）：export.sh 导出 → 导出目录 `tools/test.sh` → 发布密钥 + `--update-repo <发布仓库>` 打 dmg → `dist/release-X.Y/{CC-Remote-Chat-X.Y.dmg, update.json, notes.md}`（挂载核对 Info.plist / BUILD / UPDATE.json、codesign 钉死要求、`validate_manifest`）→ dmg 内容隐私扫描（只打印命中数）→ **只打印** `git push` / `gh release create` 命令。Release 不能是 draft / prerelease（`releases/latest` 不包含它们）。
- **测试**（`tests/test_updater.py`）：版本比较、清单校验、UPDATE.json、`create` 的关闭条件（没有 UPDATE.json / BUILD、`--fake` 没给测试源）；本机 https（openssl 临时证书）：可用 / 404 安静 / 不是新版 / 坏 JSON / 超大 / 500 / 重定向到 http 或白名单外主机 / 不信任的 TLS / 关闭开关后不发请求 / 打开开关立即检查；安装（测试证书由 release_key.sh 在临时目录生成，前后核对钥匙串搜索列表）：sha256 不符、ad-hoc 与另一张测试证书签的 app 被拒、Info.plist 版本不符、正确证书 → 换装（「Apps With Space」路径）+ 假壳（Contents/MacOS 上重新签名的 sleep）被 SIGTERM + 启动器收到 bundle 路径 + 新版本启动后「已更新」与清理、目录不可写 → manual + open_dmg 只交给启动器、rename EPERM → 回滚 manual、第二步 rename 失败回滚、会话忙时等待 / 取消 / 空闲后自动继续、window_request 里的 pid 不是本 bundle 的壳时不发信号；服务器：没有 updater 时 404 / bootstrap 无 update，有时各路由；端到端：真实 app.py（`--bundled --fake --update-test`），A 起后台建 fake 会话 → API 检查、安装 → 壳被 SIGTERM、启动器收到路径 → 按壳的方式运行新 app.py → 接管、会话还在能收发、tmux pane 不变、「已更新」、旧 .app 已删。
- **未在真机验证**（需要有 GUI 的 Mac）：「App 管理」权限是否拦截 ad-hoc 以外的自签名 app 改自己的 bundle、系统弹窗的样子；SIGTERM 原生壳后 `open` 新版的体验（窗口闪一下、程序坞图标）；自签名证书下「文稿 / 桌面」授权更新后是否保留。

## 15. 侧栏：会话标题、手动排序、"在干活"（v2.6）

- **标题**（`ccr/titles.py`）：与 CLI `/resume` 列表一致——`custom-title`（CLI 里 `/rename`）> `ai-title` > `summary` > 首条真正的用户输入（去掉 CLI 注入的标签）> 首条斜杠命令。node agent 额外转发这三种很小的条目（`TRANSCRIPT_TYPES`），前端渲染忽略它们。meta：`cli_title`、`title_user`（用户在本应用里起过名字 = True；清空改名 = False，回到 CLI 标题；旧记录 None = 待判断：目录名/"续接 xxxxxxxx" 视为自动，或等 transcript 到达后标题是 CLI 标题的前缀也视为自动）、`display_title`（前端显示用）。新建时填了标题 = 用户起的名字；从历史续接不再预填标题。
- **/rename 后的画面**：输入框上边线右侧嵌着会话名（`──── 名字 ─`），`tui_parse._RULE_RE` 与 node agent 的 `G_RULE_RE` 都允许这种上边线（否则整屏被当成未知对话框 = waiting）。
- **手动排序**：meta.`order`（小的在上，持久化到 sessions.json）；没排过的（None）按创建时间、新的在最上并排在所有排过的前面。`POST /api/sessions/order {"sids": [从上到下]}` 把列出的会话依次设为 0,1,2…。前端：拖动、右键菜单（移到最上/最下）、悬停"移到最下"按钮、Alt+↑/↓。
- **会话删除与事件**（用户报告：删除的会话仍显示在侧栏）：新建会话的第一条 meta 带 `added:true`；删除后后端推一条 `app/removed`（不进缓冲）、标记 `s.removed`，之后该 sid 的任何发布都丢弃，同时取消它的定时器并尽力 `watch on=False`。前端只凭 bootstrap、新建接口的返回、`added` 的 meta 创建会话，其它未知 sid 的事件一律丢弃（以前 `applyEnvelope` 对任何信封 `ensureSession`，迟到的 exited meta / 子代理 / 屏幕事件会把已删会话「复活」）；`forgetSession` 记住本页删除过的 sid，永不复活；收到 `removed` 立即移除（含暂存中的）。前后端全部按 sid 路由，不按标题、cwd 或 claude_session_id 合并。顺带修复 `_events_since_locked`：screen / subagent 事件不进全局缓冲导致 gseq 不连续，以前按「gseq − 首条 gseq」当下标取事件会在重连回放时漏掉 transcript 与 meta，改为从尾部往前找。测试 `tests/test_session_remove.py`；回放页第 14f 节。
- **在干活**：meta.`working` = 主会话 busy，或主会话 idle 但 `subagents.running > 0`（子代理状态见 ccr/subagents.py：meta.json 的 `finished` + task-notification）。侧栏/会话头显示"子代理运行中"。提示音状态机看到的也是这个"有效状态"：子代理全部结束才算 busy→idle、才响"完成"（本机若 CLI 自己的 Stop hook 已 afplay 仍按原规则跳过）。

### 15.1 新回复（完成但未看）与等待审批（v2.7，参考 T3 Code 的 unseen completion）
用户需求：多个会话时分清"哪个会话刚完成、还没看"和"早就闲着"。
- **判定在后端**（`ccr/session.py`）：`_Session.done_at`（最近一次确认完成的后端 `time.time()`）、`seen_at`（已看水位）；meta 输出 `done_at`、`seen_at`、`unread = done_at is not None and done_at > (seen_at or 0)`。完成 = 提示音的同一个判定（§12「完成回调」→ `SessionManager._on_notify_event` → `done_at = now`（保证单调递增）→ `_emit_meta`，cmp 变化即落盘）。所以"响一声"与"变红"一一对应；声音被关掉、被 1.5s 间隔合并、本机由 CLI 的 Stop hook 去响时照样变红。对话框（attention）**不**设 `done_at`；子代理全部结束才算完成；用户中断（`suppress_done`）、关闭 / 重启（`mute`）、启动 / 重新附着的基线（`reset`）都不变红——全部继承提示音状态机。前端**绝不**自己根据状态跃迁判断完成（meta 会随 `/events` 回放）。
- **持久化**：`done_at` / `seen_at` 存在 sessions.json 的 meta 里；`_restore` 只接受 `1e9 < v < 1e11` 的数字（排除 bool / NaN / inf），没有 `done_at` 时 `seen_at` 丢弃。部署 / 重启 app 后状态还在；原生窗口与浏览器经 SSE 的 meta 天然同步。
- **app 停止期间完成的回复**：持久化时 `state == "busy"`（或记录里 `done_pending: true`）的存活会话恢复后 `done_pending = True`；重新附着后，有效状态（含子代理）为 idle、transcript 已从头收到（`tx_loaded` 非空）、且稳定 `resume_done_delay`（3s）→ 只设 `done_at`、不响。期间又忙起来就等它真正结束；退出 / 出错放弃；用户中断、发消息、回答对话框、关闭 / 重启都会取消。
- **清除**（只认用户操作）：`POST /api/sessions/<sid>/seen {"done_at": <客户端看到的 done_at>}` → `seen_at = max(seen_at, min(done_at, 服务器 done_at))`，只能往前推；用客户端看到的值而不是"现在"，客户端还没渲染出来的新一次完成不会被一起盖掉。`done_at` 必须是数字（否则 400）。`POST /unseen`（右键「标为未读」）→ `seen_at = done_at - 0.001`；从没完成过 → 400。`send_message`、`choose`、`cancel_dialog`、`interrupt`、终端面板 Esc / Ctrl+C 顺手把 `seen_at` 推到当前 `done_at`。
- **前端**（`static/sessrow.js` 纯函数，app.js 与回放自测共用）：状态优先级 作业已结束 / 错误 / 已退出 / 连接断开 / 启动中 > 待信任目录 / 待处理 > 运行中 / 子代理运行中 > **新回复**（只在本该显示「空闲」时）> 空闲。侧栏行 class：`unread`（浅红底 `--unread-bg*`、实心红点带 3px `--err-soft` 光圈且不闪、标签「新回复」红色加粗、标题 700）、`attn`（待处理 / 待信任目录整行浅琥珀 `--attn-bg*`，实时状态，对话框消失即恢复，与已读无关）。选中行不被选中灰底盖掉：更深一档的同色底 + `inset 3px` 左侧色条。错误行没有底色（只有无光圈的红点 / 红字）。这两种行的第二行文字用 `--text-2`（浅色下 `--text-3` 在选中档的浅红底上只有约 4.3:1）。
- **一律变红、点一下才清除**：即使正打开着这个会话、窗口在前台，完成时照样变红。清除 = 点侧栏那一行（含已选中的那一行；键盘 Enter 同）、在当前会话的时间线或输入框上 `pointerdown`（拖滚动条不算）、或上面的后端动作。窗口重新获得焦点、滚动、启动时自动选中上次的会话、删除后自动切换都**不**清除。乐观更新：本地记 `localSeen = done_at` 立即重画（迟到的旧 meta 不会让行重新变红），以服务器回推的 meta 为准；失败回滚并 toast。右键菜单：未读 →「标为已读」；有过完成、当前已读 →「标为未读」；没完成过不显示。
- **窗口标题 / 程序坞角标**：有新回复时 `document.title = "(n) CC Remote Chat"`（n = 侧栏显示「新回复」的行数），否则 `CC Remote Chat`。在 meta 合并的回调里**直接**更新（不放进 rAF：窗口在后台时 rAF 不执行），bootstrap、断线重连、删除会话后也各算一次。原生壳 `updateTitle()`（KVO `webView.title`）解析前缀 `(n) ` → `NSApp.dockTile.badgeLabel`（n > 0 才显示）；窗口标题照常显示含 `(n)` 的页面标题。关窗只是 `orderOut`、页面与 SSE 还活着，角标照常更新。不需要 JS 桥。
