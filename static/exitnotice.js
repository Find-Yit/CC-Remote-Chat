// claude 进程退出时时间线里的提示文字（纯函数，便于单测：tests/test_modes.py 用 node 跑）。
// 本机会话退出码 127 = 登录 shell 找不到 claude 命令（多半是还没装 Claude Code，或装了但 PATH 没配好）：
// 给出能照着做的说明，而不是只显示退出码。服务器会话与其它退出码的文字与以前相同。
export const CLAUDE_NOT_FOUND = '找不到 claude 命令：请先安装 Claude Code，并确认在『终端』里能运行 claude';

export function exitNoticeText(code, expected, local) {
  if (expected) return `claude 进程已结束${code != null ? `（退出码 ${code}）` : ''}`;
  if (local && code != null && Number(code) === 127) return CLAUDE_NOT_FOUND;
  return `claude 进程意外退出（退出码 ${code ?? '无'}）`;
}
