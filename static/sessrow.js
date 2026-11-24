// 侧栏会话行的状态判定（纯函数，app.js 与回放自测 static/dev/fixture.js 共用；SPEC §15）。
//
// 「新回复」（完成但未看，参考 T3 Code 的 unseen completion）：后端在提示音的同一个「完成」判定点记 meta.done_at，
// 用户点过 / 发消息 / 回答对话框后推进 meta.seen_at；meta.unread = done_at > seen_at。前端**不**自己根据状态跃迁
// 判断完成（meta 会随 /events 回放，重新加载时会重复或漏判），只读 meta.unread。
// localSeen = 本页已乐观标为已读的 done_at（POST /seen 还没回推 meta 时，迟到的旧 meta 不会让行重新变红）。

export const APP_TITLE = 'CC Remote Chat';

export function isDeadState(state) { return state === 'exited' || state === 'error'; }

/** 是否有未看的新回复（meta.unread，且本页没有已乐观标为已读同一个 done_at） */
export function isUnread(meta, localSeen = null) {
  if (!meta || meta.unread !== true) return false;
  const d = Number(meta.done_at);
  if (!Number.isFinite(d)) return false;
  return !(typeof localSeen === 'number' && localSeen >= d);
}

/**
 * 侧栏 / 会话头的状态 [类名后缀, 中文]。优先级：
 * 作业已结束 / 错误 / 已退出 / 连接断开 / 启动中 > 待信任目录 / 待处理（对话框，整行浅黄）
 * > 运行中 / 子代理运行中 > 新回复（整行浅红，只在本该显示「空闲」时）> 空闲。
 * x = { meta, dialog（当前对话框或 null）, busy（屏幕帧优先的忙碌判断）, subRunning, jobEnded, localSeen }
 */
export function sessionStatus(x) {
  const m = (x && x.meta) || {};
  if (x.jobEnded && isDeadState(m.state)) return ['exited', '作业已结束'];
  if (m.state === 'error') return ['error', '错误'];
  if (m.state === 'exited') return ['exited', '已退出'];
  if (m.link === 'down') return ['starting', '连接断开'];
  if (m.state === 'starting') return ['starting', '启动中'];
  const d = x.dialog;
  if (m.state === 'trust' || (d && d.kind === 'trust')) return ['pending', '待信任目录'];
  if (m.state === 'waiting' || d) return ['pending', '待处理'];
  if (x.busy) return ['busy', '运行中'];
  if (x.subRunning > 0) return ['busy', '子代理运行中'];   // 主会话回复完了，但子代理还在干活
  if (isUnread(m, x.localSeen)) return ['unread', '新回复'];
  return ['ready', '空闲'];
}

/** 侧栏行的 class：unread = 浅红底（新回复），attn = 浅黄底（等待审批 / 信任目录，实时状态） */
export function rowClass(st, active) {
  return 'sess' + (active ? ' active' : '') + (st === 'unread' ? ' unread' : st === 'pending' ? ' attn' : '');
}

/** 窗口标题：有新回复时 "(n) CC Remote Chat"（原生壳据此设置程序坞角标），否则 "CC Remote Chat" */
export function docTitle(n, base = APP_TITLE) {
  return n > 0 ? '(' + n + ') ' + base : base;
}

/** 从窗口标题解析未读数（与 tools/macapp/main.swift 的 updateTitle 同一规则）："(n) ..." → n，否则 0 */
export function titleCount(title) {
  const m = /^\((\d{1,6})\) /.exec(String(title || ''));
  return m ? Number(m[1]) : 0;
}

/** 有新回复的行数（statuses = 各行 sessionStatus 的结果） */
export function unreadCount(statuses) {
  let n = 0;
  for (const st of statuses) if (st && st[0] === 'unread') n++;
  return n;
}
