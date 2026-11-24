# CC Remote Chat

English | [中文](README.md)

A local macOS chat UI that drives the **official interactive Claude Code CLI**, either on this Mac or on a compute node of a remote Slurm cluster.

It does not replace or modify Claude Code. The app runs the real `claude` in a private tmux (a real terminal) and only does three things:

1. sends the text and keys you type to the terminal;
2. reads the terminal screen;
3. **reads** (never writes) the files Claude Code writes itself (session transcripts, `~/.claude/sessions/*.json`).

From Claude Code's point of view, you are simply using interactive `claude` in a terminal. There is no `claude -p`, no stream-json, no SDK, and no injected environment variables. Nothing in `~/.claude` (settings, hooks, system prompts) is changed.

> The UI is currently in Chinese. This project is not affiliated with Anthropic and does not include or redistribute Claude Code; install and log in to Claude Code yourself.

## Features

**Where Claude runs**
- **Local**: `claude` runs in a private tmux (`tmux -L ccr`) through your login shell, exactly like in Terminal.
- **Server**: `ssh <host>` → `srun --jobid=<J> --overlap` → `claude` inside tmux on the compute node (requires Slurm).

**Chat**
- Several sessions at once, with live streaming drafts.
- Thinking, answers and tool calls are colour-coded (can be turned off).
- Grouped tool cards, Markdown, syntax highlighting and dark mode.

**Dialogs**
- Permission prompts, AskUserQuestion, plan approval and folder trust are shown as they are, and you answer by clicking.
- The app never answers them for you.

**Controls**
- Buttons for the permission-mode picker, the `/model` and `/effort` panels, interrupt (Esc), run in background (Ctrl+B) and send queued messages now (Ctrl+X Ctrl+S).
- A raw terminal panel as a fallback.

**Visualisation**
- Full-size images and screenshots with a zoomable lightbox.
- A per-turn diff card.
- Local sessions also get file and web-page preview in a sandboxed iframe.

**Code changes**
- A "Changes" panel on the right: which files Claude edited in this session, +/− lines per file, per turn or for the whole session, with diffs (read from the transcript only — no git, no keystrokes).
- Works with Claude Code's own right-hand diff panel: screen reading and dialogs keep working when you open it in the terminal.

**Input**
- ↑/↓ in the input box recalls messages you sent earlier in this session, like the terminal `claude`.
- A prompt minimap on the left of the timeline: one tick per prompt, hover to preview, click to jump (keyboard and wheel work too).

**Sessions**
- Sub-agent tray, plus a history browser with one-click `--resume`.
- Closing the window, quitting the app or losing ssh does not stop `claude`; the app reattaches next time.

**Extras**
- Notification sounds when a turn finishes or when you need to act.
- Sidebar shows Claude Code's own session titles (/rename > auto title > first prompt), supports manual ordering (drag / right-click), highlights sessions with an unseen reply (red) or waiting for approval (yellow); unread count in the window title and Dock badge.
- A bar under the input box: model / effort, permission mode (Shift+Tab works too), context, cost (from your own statusline) and tok/s.
- Optional per-session API key (via `claude --settings <file>`).
- A native macOS app (Swift + WKWebView).
- Updates for the dmg build: when a new version is out, a banner offers "Update now" (it waits until sessions are idle and checks the signature; never updates silently).

## Requirements

| Requirement | Details |
|---|---|
| macOS | 12 or later (Apple silicon or Intel) |
| Python | 3.9 or later. The app uses only the standard library, so there is nothing to `pip install`; `/usr/bin/python3` works. |
| Xcode Command Line Tools | Install with `xcode-select --install`. They are needed to build the native app; without them the app falls back to a Chrome app window. |
| Local mode | `tmux` (`brew install tmux`), plus Claude Code installed and logged in, with `claude` on your login shell's PATH. |
| Server mode | See [Server mode](#server-mode-slurm). |

## Install

```bash
git clone https://github.com/<your-name>/cc-remote-chat.git ~/cc-remote-chat
cd ~/cc-remote-chat
bash tools/make_app.sh          # builds ~/Applications/CC Remote Chat.app
open ~/Applications/"CC Remote Chat.app"
```

The .app stores the paths of the project folder and of Python. Re-run `make_app.sh` if you move the project or change Python.

You can also run the app without building it:

```bash
python3 app.py
```

To update:

```bash
python3 app.py --stop
git pull
bash tools/make_app.sh
```

### For friends: install from a dmg (no Python, tmux or Homebrew needed)

Download `CC-Remote-Chat-<version>.dmg` from this repository's **Releases** page, or build it yourself (below). The app inside
bundles its own Python and tmux.

**First install**

1. Install Claude Code yourself and run `claude` once in Terminal to log in.
2. Open the dmg and drag **CC Remote Chat** onto **Applications** (do not run it from the dmg window).
3. Open it from Applications. The first time, macOS may say the developer cannot be verified (there is no Apple developer
   account and the app is not notarized). Open **System Settings → Privacy & Security** and click **Open Anyway** at the bottom,
   or run `xattr -dr com.apple.quarantine "/Applications/CC Remote Chat.app"` in Terminal and open it again.

**Upgrading from 2.9 (once, by hand)**: download the new dmg, drag CC Remote Chat onto Applications and choose **Replace**, then
open it from Applications (you may need to click **Open Anyway** again). Sessions, settings and API keys are kept
(`~/.cc-remote-chat-app/`), running claude sessions are not interrupted, and the new version replaces the old background process.

**Later updates**: when a new version is out, a banner says "新版本 X 可用" (new version available); click **立即更新** (Update now).
The app waits until all sessions are idle, downloads the update, verifies its signature, replaces itself and reopens the window.
The first time, macOS may ask you to allow CC Remote Chat under **System Settings → Privacy & Security → App Management**; if you
prefer not to, follow the banner and drag the downloaded app into Applications yourself. Sessions, settings and API keys are kept
and claude keeps running. Checking for updates only fetches `update.json` from this repository's GitHub Releases and sends no
personal information; you can turn automatic checks off in Settings.

The dmg build supports **Apple silicon and macOS 12 or later** only. It keeps its data in `~/.cc-remote-chat-app/` and uses
`tmux -L ccr-app`, so it never interferes with an instance run from source. When claude first reads Documents or Desktop,
macOS asks whether "CC Remote Chat" may access it; click Allow. Closing the window leaves claude running in the background,
and the app reattaches the next time it opens.

**Building the dmg yourself** (needs an Apple silicon Mac, the Xcode Command Line Tools and network access):

```bash
bash tools/make_dmg.sh                 # writes dist/CC Remote Chat <version>.dmg; version from the VERSION file or a git tag
bash tools/make_dmg.sh --version 2.9   # set the version explicitly
```

The script downloads pinned versions of python-build-standalone (CPython 3.12) and the tmux / libevent / utf8proc sources
(all SHA256-checked, cached in `build/dmg-cache/`), builds tmux, trims Python, precompiles .pyc files and signs everything
under `/tmp`, then creates the dmg with `hdiutil`. It opens no windows and never touches `~/.claude`.
See `docs/SPEC_V2_TUI.md` §14.10 for details.

A dmg you build yourself is ad-hoc signed and has **no** auto-update. To publish updatable releases, run
`bash tools/release_key.sh init` once to create a release signing certificate (in `~/.cc-remote-chat-release/` — back it up: without
it, later updates fail verification and users must reinstall by hand), then
`bash tools/make_dmg.sh --version X.Y --release-key ~/.cc-remote-chat-release --update-repo <github-user>/<repo>` and upload the dmg
(no spaces in the file name) plus an `update.json` to a (non-draft, non-prerelease) Release of that repository. The `update.json`
format is described in SPEC §14.11.

## Quick start (local)

1. Open the app and click **新建会话** (New session).
2. Choose **本机** (This Mac).
3. Click **选择文件夹…** (Choose folder) and pick your project.
4. Leave model, permission and effort at **默认** (Default). With no flags added, this is the same as typing `claude` in Terminal.
   - To skip permission prompts, choose `--dangerously-skip-permissions` under 权限 (Permissions). The choice is remembered.
   - A local session started without this flag cannot switch to bypass mode later.
5. Chat. **Enter** sends and **Shift+Enter** adds a new line.

## Server mode (Slurm)

The connection path is:

```
Mac ──ssh <alias>──▶ login node ──srun --jobid=<J> --overlap──▶ compute node tmux ──▶ claude
```

**Before you start, make sure:**
- **ssh**: passwordless `ssh <alias>` works (the app uses `BatchMode=yes`).
- **Login node**: it has `srun`, `squeue` and `python3` 3.8 or later.
- **Job**: you have a running job (`squeue -u $USER`).
- **Compute node**: it has `claude` (logged in or configured) and `tmux`.
  - All steps of a job must use the **same** tmux binary.
  - A self-built tmux at `~/.local/x86_64/bin/tmux` is preferred.

**Then set up the app:**
1. Click the gear icon (**设置**, Settings) and fill in **服务器（SSH 主机别名）** (server SSH alias).
2. Enter your job id in the sidebar, or pick one from **我的作业** (My jobs).
3. Create sessions with **服务器** (Server) selected.

**What the app leaves on the server:**
- Nothing is installed. The agent code is sent over ssh stdin and run with `python3 -I`.
- **tmux socket**: `/tmp/tmux-<uid>/ccr`.
- **Slurm steps**: `ccr-keeper` and `ccr-agent`.
- **Key files**: only if you pick an API key, 0600 files in `~/.cc-remote-chat/keys/`.

## Configuration

Settings live in `~/.cc-remote-chat/config.json` and are mostly edited in the UI.

| Field | Default | Meaning |
|---|---|---|
| `host` | `""` | ssh alias for server mode. Empty means local only. |
| `jobid` | `""` | Current Slurm job. |
| `default_cwd` | `~` | Default working directory on the server. |
| `ssh_multiplex` | `true` | Reuse ssh connections. |
| `extra_srun_args` | `""` | Extra srun flags. Only `--gres --gpus --gpus-per-node --gpus-per-task --cpus-per-task --cpus-per-gpu --mem --mem-per-cpu --mem-per-gpu --time --cpu-bind --gpu-bind` are allowed, written as `--flag=value`. |
| `notify_*` | on, `Blow` | Notification sounds. |

Command line:

```bash
python3 app.py [--port 8765] [--fake] [--no-browser] [--data-dir DIR] [--jobid J] [--stop]
```

`--fake` is a demo mode that uses a fake `claude` in local tmux, so it costs nothing and never touches a server.

## Optional API keys

By default `claude` uses your own `~/.claude/settings.json`. To give a session a different key or gateway, add one under the **API** drop-down.

- **How the key is passed**: `claude` is started with `--settings <file>`, which overrides only `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL`.
- **Where the key is stored**: plain keys live only in `~/.cc-remote-chat/api_keys.json` (mode 0600). The UI, logs and API show masks only.
- **Usage lookup**: optional. It is compatible with sub2api-style `GET <base_url>/v1/usage`.

## Principles

- **Command line**: `claude` plus only the standard flags you pick explicitly: `--resume`, `--model`, `--permission-mode`, `--dangerously-skip-permissions`, `--effort`, and `--settings` for an API key.
- **Keys come from you**: dialogs are never auto-answered, and every confirming key is checked against a fresh screen first.
- **Nothing is written to Claude's files**: no writes to `~/.claude*` or any dotfile.
  - Local sessions get a whitelisted environment.
  - App data stays in `~/.cc-remote-chat/`.

## Security

- **Network**: the app listens on `127.0.0.1` only.
- **Login**:
  - The window opens with a one-time login code, valid for 120 s and kept in the URL fragment.
  - The code is exchanged for an in-memory token sent as `X-CCR-Token`.
  - No cookies are used.
- **Request checks**: `Host`, `Origin` and `Sec-Fetch-Site` are checked.
- **Rendering**: messages are rendered as text (no innerHTML), and no external resources are loaded.
- **Previews**: local previews run in a CSP-sandboxed iframe behind capability URLs, with sensitive directories denylisted.

## Uninstall

```bash
python3 app.py --stop
rm -rf ~/Applications/"CC Remote Chat.app" ~/.cc-remote-chat
tmux -L ccr kill-server   # optional: ends any claude still running in the private tmux
```

## Development

```bash
python3 -m unittest discover -s tests -t .
python3 app.py --fake --dev   # demo mode + replay page /static/dev/fixture.html
```

- **Fixtures**: `fixtures/` holds anonymised real Claude Code v2.1.330 screens and transcripts.
- **Design spec**: [`docs/SPEC_V2_TUI.md`](docs/SPEC_V2_TUI.md) (Chinese).
- **HTTP API**: [`docs/API.md`](docs/API.md).

## License

[MIT](LICENSE). Parts of the frontend are adapted from [T3 Code](https://github.com/pingdotgg/t3code) (MIT); see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

"Claude" and "Claude Code" are trademarks of Anthropic, PBC. This is an unofficial third-party tool and is not affiliated with Anthropic.
