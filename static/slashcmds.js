// 交互式 claude 的内置 / 命令（SPEC §7「输入框：/ 命令与 @ 文件补全」）：按 CLI 版本的静态表。
// 2.1.332 = 2026-11-28 在真机（macOS，haiku，API Usage Billing 账号）空输入框只输入 `/`，按 ↓ 翻完整个补全列表逐项记录
// （共 85 项：这里的 80 项 + 用户自己的 3 个 skill + 2 个插件命令；没有按 Enter 执行任何命令）；别名来自同版本二进制里的命令定义。
// 账号类型 / 设置不同，CLI 显示的内置命令会略有不同（例如订阅账号多 /upgrade 等）；这里只是补全提示，发送后仍由 claude 自己处理。
// d = 中文说明，en = CLI 原文（悬停可见）；skill = 随 CLI 附带的 skill；aliases = CLI 也认的别名（选中后插入正式名字）。
// 磁盘上的自定义命令 / skills / 插件命令由后端只读列出（ccr/complete.py），不在这里。

const T_2_1_332 = [
  ['add-dir', '添加一个工作目录', 'Add a working directory'],
  ['auto-mode-setup', '让自动模式了解你的环境', 'Teach auto mode about your environment'],
  ['autocompact', '设置上下文多满时自动压缩', 'Set how full the context gets before auto-summarizing'],
  ['background', '把本会话转到后台，释放终端', 'Send this session to the background and free the terminal', ['bg']],
  ['branch', '在当前位置给对话建一个分支', 'Create a branch of the current conversation at this point'],
  ['btw', '问个旁支问题，不打断 Claude', 'Ask a side question without interrupting Claude'],
  ['bug', '报告 bug 或分享会话', 'Report a bug or share your session'],
  ['cd', '把本会话移到另一个工作目录', 'Move this session to a new working directory'],
  ['clear', '以空上下文开始新会话（旧会话留在 /resume 里）', 'Start a new session with empty context; loops carry over, and the old one stays in /resume', ['reset', 'new']],
  ['color', '设置本会话输入栏的颜色', 'Set the prompt bar color for this session'],
  ['compact', '总结目前的对话，释放上下文', 'Free up context by summarizing the conversation so far'],
  ['config', '打开设置', 'Open settings', ['settings']],
  ['context', '查看上下文被什么占用', 'Show what fills the context'],
  ['copy', '把上一条回复复制到剪贴板（/copy 2 = 再前一条）', 'Copy the last response to clipboard (/copy 2 for the one before)'],
  ['diff', '开关未提交改动的 diff 面板', 'Toggle the diff panel showing uncommitted changes'],
  ['effort', '设置模型的 effort 档位', "Set the model's effort level"],
  ['exit', '退出 CLI', 'Exit the CLI', ['quit']],
  ['export', '把本会话导出到文件或剪贴板', 'Export this session to a file or clipboard'],
  ['fast', '开关快速模式（Opus 5.5）', 'Toggle fast mode (Opus 5.5)'],
  ['feedback', '给 Anthropic 发反馈或报告 bug', 'Send feedback to Anthropic or report a bug', ['share']],
  ['focus', '开关专注视图：只看你的提示、摘要和回复', 'Toggle focus view: just your prompt, summary, and response'],
  ['fork', '把对话复制到新的后台会话，这里继续', 'Copy this conversation into a new background session and keep working here'],
  ['goal', '设一个 Claude 结束前要检查的目标', 'Set a goal Claude checks before stopping'],
  ['help', '显示帮助和可用命令', 'Show help and available commands'],
  ['hooks', '查看工具事件的 hook 配置', 'View hook configurations for tool events'],
  ['ide', '管理 IDE 集成、查看状态', 'Manage IDE integrations and show status'],
  ['keybindings', '打开键盘快捷键文件', 'Open your keyboard shortcuts file'],
  ['list-agents', '列出可以发消息的子代理、队友和其它 Claude 会话', 'List subagents, teammates, and other Claude sessions you can message', ['peers']],
  ['login', '用 Anthropic 账号登录', 'Sign in with your Anthropic account'],
  ['logout', '退出 Anthropic 账号', 'Sign out from your Anthropic account'],
  ['mcp', '管理 MCP 服务器', 'Manage MCP servers'],
  ['memory', '编辑 CLAUDE.md 和记忆设置', 'Edit CLAUDE.md files and memory settings'],
  ['mobile', '显示下载 Claude 手机 App 的二维码', 'Show QR code to download the Claude mobile app', ['ios', 'android']],
  ['model', '切换模型', 'Switch model'],
  ['output-style', '列出输出风格或切换', 'List output styles or switch to one'],
  ['permissions', '管理工具权限的允许 / 拒绝规则', 'Manage allow and deny tool permission rules', ['allowed-tools']],
  ['plan', '打开计划模式或查看计划', 'Turn on plan mode or view the plan'],
  ['plugin', '管理 Claude Code 插件', 'Manage Claude Code plugins', ['plugins', 'marketplace']],
  ['powerup', '用小课程学习 Claude Code 功能', 'Learn Claude Code features in short lessons'],
  ['radio', '收听 Claude FM lo-fi 电台', 'Listen to Claude FM lo-fi radio'],
  ['recap', '立即生成一行会话回顾', 'Generate a one-line session recap now'],
  ['release-notes', '查看更新说明', 'View release notes'],
  ['reload-plugins', '让待生效的插件改动在本会话生效', 'Activate pending plugin changes in the current session'],
  ['reload-skills', '载入本会话期间磁盘上新增或改过的 skill', 'Pick up skills added or changed on disk during this session'],
  ['rename', '重命名当前对话', 'Rename the current conversation', ['name']],
  ['resume', '续接之前的会话', 'Resume a previous session', ['continue']],
  ['rewind', '恢复之前的代码、对话或两者', 'Restore earlier code, conversation, or both', ['checkpoint', 'undo']],
  ['sandbox', '沙箱设置（终端里显示当前状态）', 'Sandbox settings (status shown in the terminal)'],
  ['scroll-speed', '调整鼠标滚轮的滚动速度', 'Adjust mouse wheel scroll speed'],
  ['skills', '列出可用的 skill', 'List available skills'],
  ['status', '显示版本、账号、模型、MCP 服务器和设置警告', 'Show version, account, model, MCP servers, and setup warnings'],
  ['stickers', '查看 Claude Code 贴纸', 'Check Claude Code sticker availability'],
  ['subtask', '带着完整上下文派一个子代理，结果回到这里', 'Send a subagent off with your full context; its result comes back here'],
  ['tasks', '查看和管理本会话的后台任务', "View and manage this session's background work", ['bashes']],
  ['terminal-setup', '设置 Shift+Enter 换行', 'Set up Shift+Enter for newlines'],
  ['theme', '更改主题', 'Change the theme'],
  ['tui', '设置终端界面的渲染方式（classic | fullscreen）', 'Set the terminal UI renderer (classic | fullscreen)'],
  ['usage', '显示本会话花费、套餐用量和活动统计', 'Show session cost, plan usage, and activity stats', ['cost', 'stats']],
  ['workflows', '浏览运行中和已完成的 workflow', 'Browse running and completed workflows'],
  // 随 CLI 附带的 skills（列表里排在内置命令之后）
  ['init', '初始化 CLAUDE.md（代码库说明）', 'Initialize a new CLAUDE.md file with codebase documentation', null, true],
  ['batch', '规划大改动，由多个后台代理各开一个 PR', 'Plan a large change; background agents each open a PR', null, true],
  ['claude-api', '构建和调试使用 Claude API 的应用', 'Build and debug apps that use the Claude API', null, true],
  ['code-review', '检查当前 diff 或某个 PR 里的 bug', 'Review the current diff or a PR for bugs', null, true],
  ['dataviz', '图表与仪表盘设计指南', 'Chart and dashboard design guidance', null, true],
  ['debug', '打开调试日志并排查问题', 'Turn on debug logging and investigate problems', null, true],
  ['deep-research', '深度调研：并行搜索、抓取来源、交叉核实，写出带引用的报告', '[dynamic workflow] Deep research harness — fan-out web searches, fetch sources, adversarially verify claims, synthesize a cited report.', null, true],
  ['doctor', '检查安装与配置的健康状况并修复问题', 'Health-check your setup and fix issues: installation, unused extensions, duplicated or bloated memory files, slow hooks, updates, permissions', ['checkup'], true],
  ['fewer-permission-prompts', '按你的使用情况预先批准安全的只读命令', 'Pre-approve safe read-only commands based on your usage', null, true],
  ['insights', '生成分析你的 Claude Code 会话的报告', 'Generate a report analyzing your Claude Code sessions', null, true],
  ['loop', '定时重复一个提示或命令（例如 /loop 5m /foo）', 'Repeat a prompt or command on an interval (e.g. /loop 5m /foo)', null, true],
  ['plugin-authoring', '编写 mod：Claude Code 的面板、视图或行为改动', 'Write a mod: a live pane, view or behaviour change in Claude Code', null, true],
  ['run', '启动本项目的应用，看改动是否生效', 'Launch this project’s app to see your change working', null, true],
  ['run-skill-generator', '创建一个会运行本项目应用的 skill', 'Create a skill that knows how to run this project’s app', null, true],
  ['security-review', '对当前分支待提交的改动做安全审查', 'Complete a security review of the pending changes on the current branch', null, true],
  ['simplify', '清理改过的代码，不改变行为', 'Clean up the changed code without changing behavior', null, true],
  ['statusline', '设置状态栏', 'Set up the status line', null, true],
  ['team-onboarding', '按你的用法生成指南，帮队友上手 Claude Code', 'Help teammates ramp on Claude Code with a guide from your usage', null, true],
  ['update-config', '修改设置：hooks、权限、环境变量', 'Change settings: hooks, permissions, environment variables', null, true],
  ['verify', '试运行你的改动并报告是否有效', 'Try your change and report if it works', null, true],
  ['workflow-authoring', '载入编写 Workflow 工具脚本的参考', 'Load the reference for writing Workflow tool scripts', null, true],
];

const TABLES = { '2.1.332': T_2_1_332 };
export const KNOWN_VERSIONS = Object.keys(TABLES);
export const LATEST = '2.1.332';

function vparts(v) {
  const m = /^(\d+)\.(\d+)\.(\d+)/.exec(String(v || ''));
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}
export function cmpVersion(a, b) {
  const x = vparts(a), y = vparts(b);
  if (!x || !y) return 0;
  for (let i = 0; i < 3; i++) if (x[i] !== y[i]) return x[i] < y[i] ? -1 : 1;
  return 0;
}

/** 会话的 CLI 版本 → 用哪一版的表：不晚于它的最新一版；没有更早的 / 版本未知 → 最新一版。
 *  返回 {version: 表的版本, exact: 是否正好是这一版} */
export function tableVersion(version) {
  if (!vparts(version)) return { version: LATEST, exact: false };
  const older = KNOWN_VERSIONS.filter((k) => cmpVersion(k, version) <= 0).sort(cmpVersion);
  const pick = older.length ? older[older.length - 1] : LATEST;
  return { version: pick, exact: cmpVersion(pick, version) === 0 };
}

/** 内置命令列表（新数组，可以随意改）：[{name, desc, en, aliases, src:'builtin', kind:'command'|'skill'}] */
export function builtinCommands(version) {
  const t = TABLES[tableVersion(version).version] || T_2_1_332;
  return t.map(([name, d, en, aliases, skill]) => ({
    name, desc: d, en, aliases: Array.isArray(aliases) ? aliases.slice() : [], src: 'builtin', kind: skill ? 'skill' : 'command',
  }));
}
