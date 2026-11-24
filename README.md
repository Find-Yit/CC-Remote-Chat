# CC Remote Chat

[English](README.en.md) | 中文

一个 macOS 上的本地聊天界面，用来驱动**官方的交互式 Claude Code CLI**——可以跑在这台 Mac 上，也可以跑在远程 Slurm 集群的计算节点上。

它不替代 Claude Code，也不改 Claude Code：程序在一个私有 tmux 里运行真正的 `claude`（真实终端），自己只做三件事：

1. 把你在界面里输入的文字/按键发给终端；
2. 读终端屏幕；
3. **只读** Claude Code 自己写的文件（会话 transcript、`~/.claude/sessions/*.json`）。

所以在 Claude Code 看来，就是你本人在终端里用交互式 `claude`：没有 `claude -p`、没有 stream-json、没有 SDK，不注入环境变量，不改 `~/.claude` 里的任何配置、hook 或系统提示词。

> 本项目与 Anthropic 无关，不包含也不分发 Claude Code。你需要自己安装并登录 Claude Code。

---

## 功能

- **两种运行位置**
  - **本机**：在这台 Mac 的私有 tmux（`tmux -L ccr`）里，用你的登录 shell 运行 `claude`，和在终端里一样。
  - **服务器**：`ssh <主机>` → `srun --jobid=<作业> --overlap` → 计算节点 tmux 里运行 `claude`（需要 Slurm）。
- **聊天界面**
  - 多会话并行；生成过程中逐字显示草稿。
  - 思考、回答、工具调用用不同颜色区分（可关）；连续的工具调用合成一组卡片。
  - Markdown、代码高亮、暗色模式。
- **对话框原样呈现，由你点击作答**：权限审批、AskUserQuestion、计划审批、信任目录。程序绝不自动替你回答。
- **常用操作都有按钮**
  - 权限模式选择栏（不用一遍遍按 Shift+Tab）、`/model` 与 `/effort` 面板、中断（Esc）。
  - 转入后台（Ctrl+B）、立即发送排队消息（Ctrl+X Ctrl+S）。
  - 终端面板：显示屏幕原文，界面识别不了的画面可以直接在这里按键。
- **可视化**
  - 截图和图片完整显示，点开可放大（灯箱，支持触控板缩放）。
  - 「本轮改动」diff 卡片。
  - 本机会话还能预览文件和生成的网页（沙箱 iframe）。
- **代码改动**
  - 右侧「代码改动」栏：本会话里 Claude 改了哪些文件、每个文件 +/− 多少行，按轮或整个会话查看，点开看 diff（只读 transcript，不跑 git、不发按键）。
  - 适配 Claude Code CLI 自带的右侧 diff 面板：在终端里打开它时，读屏和对话框照常工作。
- **输入框 ↑/↓ 历史**：把这个会话里之前发过的消息放回输入框，用法和终端里的 claude 一样。
- **左侧 prompt 小格子**：时间线左边每条提问一格，悬停预览、点击跳到那一轮，可用键盘和滚轮。
- **子代理**：托盘与抽屉，查看子代理的进度和输出。
- **历史会话**：浏览 `~/.claude/projects/` 里的历史会话，一键 `--resume` 续接。
- **不怕断线**：关窗口、退出 app、ssh 断开都不会打断 `claude`，下次打开自动重新连上。
- **提示音**：回复完成、或需要你操作（审批/提问）时，用系统声音提醒。
- **侧栏**：显示 Claude Code 里的会话标题（/rename > 自动标题 > 首条输入）；可拖动或右键手动排序；回复完成且还没看的会话整行标红「新回复」，等待审批的会话标黄；窗口标题与程序坞角标显示未读数。
- **输入框底栏**：模型 / effort、权限模式（也可 Shift+Tab）、上下文占用 · 花费（读取你自己的 statusline）· 生成速度 tok/s。
- **按会话选 API key**（可选）：用 `claude --settings <文件>` 只覆盖该会话的 key / base_url。
- **原生 Mac 应用**：Swift + WKWebView。程序坞和 Cmd+Tab 里都显示「CC Remote Chat」。
- **自动更新（dmg 版）**：有新版本时窗口上方提示，点「立即更新」一键更新（等会话空闲、校验签名后替换，不会静默更新）。

## 环境要求

| | 要求 |
|---|---|
| 系统 | macOS 12 或更新（Apple 芯片 / Intel 均可） |
| Python | 3.9+，**只用标准库**，不需要 `pip install`（系统自带的 `/usr/bin/python3` 就行） |
| Xcode 命令行工具 | `xcode-select --install`。用来编译原生应用；没有也能用（回退为 Chrome 应用窗口） |
| 本机模式 | `tmux`（`brew install tmux`）；已安装并登录的 Claude Code，`claude` 在你的登录 shell 的 PATH 里 |
| 服务器模式 | 见下文「服务器模式」 |

## 安装

```bash
git clone https://github.com/<your-name>/cc-remote-chat.git ~/cc-remote-chat
cd ~/cc-remote-chat
bash tools/make_app.sh          # 生成 ~/Applications/CC Remote Chat.app
open ~/Applications/"CC Remote Chat.app"
```

`make_app.sh` 只在本地编译、生成 .app，不安装别的东西，也不碰 `~/.claude`。

- **路径**：.app 里记录了项目目录和 Python 的路径。移动了项目目录、或换了 Python 之后，重新运行一次 `make_app.sh`。
- **其它用法**：
  - `PYTHON=/path/to/python3 bash tools/make_app.sh`：指定 Python；
  - `bash tools/make_app.sh /some/dir`：生成到别的目录。

不想要 .app 也可以直接运行（用 Google Chrome 的应用窗口打开，没有 Chrome 就用默认浏览器）：

```bash
python3 app.py
```

### 更新

```bash
cd ~/cc-remote-chat
python3 app.py --stop           # 先停掉后台进程（正在运行的 claude 不受影响）
git pull
bash tools/make_app.sh
open ~/Applications/"CC Remote Chat.app"
```

### 给朋友用：dmg 安装（不需要 Python、tmux、Homebrew）

可以直接从本仓库的 **Releases** 页面下载 `CC-Remote-Chat-<版本>.dmg`，也可以自己打（见下文）。dmg 里的 app 自带 Python 和 tmux。

**首次安装**

1. 自己装好 Claude Code，并在「终端」里运行一次 `claude` 登录；
2. 双击 dmg，把「CC Remote Chat」拖到旁边的「应用程序」（不要直接在 dmg 窗口里双击）；
3. 从「应用程序」打开。第一次如果提示无法验证开发者（没有 Apple 开发者账号，app 没有经过公证）：打开「系统设置 → 隐私与安全性」，
   在最下面点「仍要打开」；或者在终端运行 `xattr -dr com.apple.quarantine "/Applications/CC Remote Chat.app"` 再打开。

**从 2.9 升级（手动一次）**：下载新的 dmg → 把 CC Remote Chat 拖进「应用程序」，选「替换」→ 从「应用程序」打开
（可能需要在「系统设置 → 隐私与安全性」再点一次「仍要打开」）。会话、设置、API key 都保留（在 `~/.cc-remote-chat-app/`），
正在跑的 claude 不受影响，新版会自动换掉还在后台运行的旧版。

**以后的更新**：有新版本时窗口上方会提示「新版本 X 可用」，点「立即更新」：app 会等所有会话空闲，下载并校验签名后替换自己，
然后重新打开窗口。第一次自动更新时 macOS 可能要求在「系统设置 → 隐私与安全性 → App 管理」里允许 CC Remote Chat；
不想允许也没关系，按提示把已下载的新版拖进「应用程序」即可。会话、设置、API key 都保留，claude 不中断。
检查更新只访问 github.com 上本仓库 Release 里的 `update.json`，不发送任何个人信息；可以在「设置 → 更新」里关掉自动检查。

dmg 版只支持 **Apple 芯片、macOS 12 或更新**；数据在 `~/.cc-remote-chat-app/`，本机 tmux 用 `-L ccr-app`，和从源码运行的实例互不干扰。
claude 第一次读「文稿」「桌面」时系统会问是否允许「CC Remote Chat」访问，点允许。关掉窗口后 claude 仍在后台运行，下次打开会自动接上。

**自己打 dmg**（需要 Apple 芯片的 Mac、Xcode 命令行工具和网络）：

```bash
bash tools/make_dmg.sh                 # 输出 dist/CC Remote Chat <版本>.dmg；版本号取 VERSION 文件或 git tag
bash tools/make_dmg.sh --version 2.9   # 指定版本号
```

脚本会下载固定版本的 python-build-standalone（CPython 3.12）以及 tmux / libevent / utf8proc 源码（都校验 SHA256，缓存在 `build/dmg-cache/`），
在 `/tmp` 下编译 tmux、裁剪 Python、预编译 pyc、签名，然后用 `hdiutil` 生成 dmg。不弹任何窗口，也不碰 `~/.claude`。
详细说明见 `docs/SPEC_V2_TUI.md` §14.10。

自己打的 dmg 默认是 ad-hoc 签名、**没有**自动更新。要发布带自动更新的版本：先 `bash tools/release_key.sh init` 生成发布签名证书
（在 `~/.cc-remote-chat-release/`，务必备份：丢了以后发的更新会验证失败，只能手动重装），再
`bash tools/make_dmg.sh --version X.Y --release-key ~/.cc-remote-chat-release --update-repo <GitHub 用户名>/<仓库名>`，
把 dmg（文件名不要带空格）和 `update.json` 上传到该仓库的 Release（不能是 draft / prerelease）。`update.json` 的格式见 SPEC §14.11。

## 快速上手（本机）

1. 打开应用，点「**新建会话**」，运行位置选「**本机**」。
2. 点「**选择文件夹…**」（系统自带的文件夹对话框），选项目目录。
3. 模型、权限、effort 都可以留「默认」：什么参数都不加，等同于你在终端里直接敲 `claude`。
   - 想让本机会话跳过权限审批，在「权限」里选「跳过权限（`--dangerously-skip-permissions`）」，下次会记住。
   - 注意：本机会话启动时没带这个参数的话，之后无法再切到 bypass 模式。
4. 在输入框里聊天：**Enter** 发送，**Shift+Enter** 换行。
   - 出现权限审批或提问时，点对话框里的按钮回答。
   - 看不懂的画面，打开「终端」面板直接按键。

## 服务器模式（Slurm 集群）

适合「claude 要跑在集群计算节点上，但在本地 Mac 上聊天」的场景。远端拓扑：

```
Mac ──ssh <主机别名>──▶ 登录节点 ──srun --jobid=<J> --overlap──▶ 计算节点 tmux ──▶ claude
```

**前提**

- 这台 Mac 能**免密** ssh 到登录节点（程序用 `BatchMode=yes`，不会弹密码框）。主机别名写在 `~/.ssh/config` 里，例如：
  ```
  Host my-cluster
      HostName login.example.edu
      User alice
      IdentityFile ~/.ssh/id_ed25519
  ```
- 登录节点：有 `srun`、`squeue`、`python3`（3.8+）。
- 你已经有一个**运行中的作业**（`squeue -u $USER` 能看到）。
- 计算节点：有 `claude`（已登录或已配置 key），有 `tmux`。
  - 同一作业里的所有 step 必须用**同一个** tmux 二进制。
  - 如果自己编译了 tmux，放在 `~/.local/x86_64/bin/tmux`，程序会优先用它。

**设置**

1. 侧栏左上角齿轮 → **设置** → 「服务器（SSH 主机别名）」填 `my-cluster`。
2. 侧栏「当前作业」填作业号，或点「我的作业」从 `squeue` 列表里选。
3. 新建会话时运行位置选「服务器」。

**在服务器上留下了什么**

- 程序本身不安装到服务器上：远端 agent 的代码每次通过 ssh 的 stdin 传过去，用 `python3 -I` 运行。
- 运行时痕迹只有这些：
  - tmux 套接字 `/tmp/tmux-<uid>/ccr`；
  - Slurm step：`ccr-keeper` 负责让 tmux 保持运行，`ccr-agent` 负责连接；
  - 仅当你给会话选了 API key 时，才有 `~/.cc-remote-chat/keys/` 下权限为 0600 的 key 文件。
- 一个作业的所有会话都结束后，keeper 会自己退出。

## 配置

大部分设置在界面里改，数据都在 `~/.cc-remote-chat/config.json`：

| 字段 | 默认值 | 说明 |
|---|---|---|
| `host` | `""` | 服务器模式的 ssh 主机别名；空 = 只用本机模式（界面：设置 → 服务器） |
| `jobid` | `""` | 当前作业，新的服务器会话默认用它 |
| `default_cwd` | `~` | 新会话的默认工作目录（服务器路径） |
| `ssh_multiplex` | `true` | 复用 ssh 连接（控制套接字在 `~/.cc-remote-chat/ssh/`） |
| `extra_srun_args` | `""` | 额外的 srun 参数，**严格白名单**：`--gres`、`--gpus`、`--gpus-per-node`、`--gpus-per-task`、`--cpus-per-task`、`--cpus-per-gpu`、`--mem`、`--mem-per-cpu`、`--mem-per-gpu`、`--time`、`--cpu-bind`、`--gpu-bind`，必须写成 `--选项=值` |
| `notify_enabled` / `notify_on_done` / `notify_on_attention` | `true` | 提示音总开关 / 完成时 / 需要操作时 |
| `notify_sound` | `Blow` | `/System/Library/Sounds` 里的声音名 |

命令行参数：

```bash
python3 app.py                  # 启动（已在运行时只会重新打开窗口）
python3 app.py --stop           # 停止后台进程（claude 不受影响）
python3 app.py --fake           # 演示模式：本机 tmux + 假的 claude，不花钱、不连服务器
python3 app.py --no-browser     # 不自动开窗口，在终端打印一次性登录链接
python3 app.py --port 9000      # 指定端口（被占用会自动递增），默认 8765
python3 app.py --jobid 123456   # 设置当前作业
python3 app.py --data-dir DIR   # 换数据目录（默认 ~/.cc-remote-chat）
```

### API key（可选）

默认情况下 claude 用的就是你自己 `~/.claude/settings.json` 里的配置，什么都不用设。

如果想让不同会话用不同的 key 或网关，可以在会话头部的「API」下拉框里添加并选择。

- **生效方式**：启动时加 `--settings <只含这个 key 的文件>`，只覆盖 `ANTHROPIC_AUTH_TOKEN` 和 `ANTHROPIC_BASE_URL`。
- **换 key**：claude 只在启动时读 key，所以换 key = 用 `--resume` 重启。对话还在，提示缓存可能失效。
- **存放**：明文 key 只存在 `~/.cc-remote-chat/api_keys.json`（0600）。界面、日志、接口里只出现掩码。
- **用量查询**：可选功能，兼容 sub2api 风格的 `GET <base_url>/v1/usage`。网关不支持时不显示，不影响其它功能。

## 不改动 Claude Code

这是本项目的硬性约定：

- **启动命令** = `claude` + 你在界面上**明确选择**的标准参数，白名单只有：
  - `--resume`、`--model`、`--permission-mode`、`--dangerously-skip-permissions`、`--effort`；
  - 以及选了 API key 时的 `--settings`。

  什么都不选，就什么参数都不加。
- **按键只来自你的操作**：权限审批、信任目录、提问绝不自动回答。
  - 程序自己发出的按键序列都由你点某个按钮触发，例如关闭会话时输入 `/exit`、切换模型时在 `/model` 面板里导航。
  - 每个确认类按键都先重新读屏核对，核对不上就一个键都不按。
- **不写**本地或远端的 `~/.claude*`，也不写任何 dotfile。本机会话的环境变量走白名单，不会把启动 app 的那个进程的环境带进 claude。
- **本应用自己的数据**只在 `~/.cc-remote-chat/` 里：配置、会话列表、日志、ssh 控制套接字、（可选的）API key。

## 安全

- **监听**：后端只监听 `127.0.0.1`。
- **登录**：
  - 每次开窗口都生成一个**一次性登录码**（120 秒内有效、只能用一次），放在 URL 的 `#fragment` 里，不进日志；
  - 页面用它换一个只存在内存里的 token，之后的请求都带 `X-CCR-Token` 头；
  - 不使用 Cookie。
- **请求校验**：校验 `Host`（防 DNS rebinding）、`Origin` 与 `Sec-Fetch-Site`。
- **内容渲染**：消息内容一律按纯文本处理后再渲染（不用 innerHTML），不加载任何外部资源。
- **本机预览**：在 CSP sandbox 的 iframe 里打开；路径用一次性能力 URL；敏感目录（如 `~/.ssh`、`~/.claude`）在黑名单里。

## 卸载

```bash
cd ~/cc-remote-chat && python3 app.py --stop
rm -rf ~/Applications/"CC Remote Chat.app" ~/.cc-remote-chat
# 可选：结束本机私有 tmux 里剩下的会话（会结束其中仍在运行的 claude）
tmux -L ccr kill-server
```

## 常见问题

- **窗口显示「会话已失效」**
  - 原因：后台进程重启过，旧窗口里的 token 失效了。
  - 处理：重新打开应用（或再运行一次 `python3 app.py`）。
- **本机提示找不到 tmux / claude**
  - 先 `brew install tmux`。
  - 再确认在新开的终端里能直接运行 `claude`：程序用你的登录 shell（`$SHELL -l -i`）启动它。
- **服务器会话连不上**
  - 先在终端里试 `ssh -o BatchMode=yes my-cluster squeue -u $USER`，确认免密登录和作业都正常。
  - 会话报错时，点会话头部的「重连」。
- **服务器上残留的会话**
  - tmux 会话名为 `ccr-<id>`，在计算节点上 `tmux -L ccr ls` 可以看到。
  - 所有会话结束后 keeper 会自行退出，不需要 `scancel`。
- **界面没识别出某个画面**：打开「终端」面板看屏幕原文，用键盘区按键处理。
- **日志**：`~/.cc-remote-chat/logs/app.log`。

## 开发

```bash
python3 -m unittest discover -s tests -t .    # 全部单测：不连服务器、不运行真实 claude
python3 app.py --fake --dev                   # 演示模式 + 回放自测页 /static/dev/fixture.html
```

- **测试会启动 tmux**：部分测试会用独立的 `TMUX_TMPDIR` 启动临时 tmux（`tests/__init__.py` 会自动设置）。
- **建议在沙箱里跑**：如果你本机同时在用这个 app，可以用 `sandbox-exec` 禁止测试访问 `/private/tmp/tmux-<uid>`，彻底避免误伤真实会话。
- **fixtures**：`fixtures/` 里是真实 Claude Code v2.1.330 的屏幕录制与 transcript，已经脱敏；屏幕解析测试会逐帧比对。
- **文档**：
  - 设计规格（中文）：[`docs/SPEC_V2_TUI.md`](docs/SPEC_V2_TUI.md)，代码注释里的「§x」指它的章节；
  - HTTP 接口：[`docs/API.md`](docs/API.md)。

代码结构：

```
app.py               入口：单实例、开窗口、信号处理
ccr/server.py        HTTP API + SSE + 静态文件 + 安全校验
ccr/session.py       会话管理：状态、按键策略、事件、持久化与重新附着
ccr/tui_parse.py     交互式 Claude Code 屏幕解析（纯函数）
ccr/agent_link.py    本地 ↔ agent 连接（本机子进程 / ssh+srun）、断线重连、keeper
ccr/node_agent.py    agent：tmux 操作、读屏、读 transcript（不写任何文件）
ccr/remote.py        ssh / srun 命令构造与参数白名单
ccr/history.py       历史会话、目录、作业（只读）
ccr/api_keys.py      API key 存储（0600）；ccr/usage.py 用量查询
ccr/preview.py       本机文件/网页预览；ccr/blobs.py 图片
ccr/notify.py        提示音；ccr/macdialog.py 系统「选择文件夹」
ccr/fake_tui.py      假的交互式 claude（演示模式/测试）
static/              前端（原生 HTML/CSS/JS，无构建）
tools/make_app.sh    生成 .app；tools/macapp/main.swift 原生应用
```

## 许可证

[MIT](LICENSE)。部分前端代码改编自 [T3 Code](https://github.com/pingdotgg/t3code)（MIT），见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

“Claude”和“Claude Code”是 Anthropic, PBC 的商标。本项目是非官方的第三方工具，与 Anthropic 无关。
