"""macOS 系统通知（SPEC §12.1 / §14.12）：
1. 后端：Notifier 的「需要操作」确认回调（只给用 kinds 订阅了 attention 的监听者；与提示音同一个判定、不看响不响）、
   SessionManager 据此记 meta.attn_at（不改 done_at / 未读）。
2. 页面（static/sysnotify.js，node）：基线、done_at / attn_at 变大才通知、回放不重复、前台当前会话不弹、同一会话合并、
   正文文字、消息字段、打开会话（含冷启动先记下）、浏览器里没有 handler 时什么都不做；static/keepawake.js 的状态文字。
3. 原生壳（tools/macapp/main.swift）：抽出 ccr-notify-script 一段单独编译，核对消息校验与「打开会话」事件脚本
   （JavaScriptCore 执行，不开任何窗口、不发通知）；源码规则（授权只在 enable 里请求、通知不带声音、handler 校验来源）。
DOM（设置弹窗里的开关、stub 掉 handler 的调用时机）在回放自测页第 14s 节。"""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from ccr import notify as N, session as S
from tests.test_notify import Base as NotifyBase
from tests.test_session import Base as SessionBase, StubLink, wait_until

ROOT = pathlib.Path(__file__).resolve().parent.parent


# ============================================================================ 1. 后端
class TestAttentionCallback(NotifyBase):
    def setUp(self):
        super().setUp()
        self.done_only, self.both = [], []
        self.n.add_listener(lambda sid, kind: self.done_only.append((sid, kind)))                 # 旧用法：只有 done
        self.n.add_listener(lambda sid, kind: self.both.append((sid, kind)), kinds=("done", "attention"))

    def baseline(self, sid="s1"):
        self.n.reset(sid)
        self.obs("idle", sid=sid)
        self.clock.t += 3

    def test_confirmed_attention_calls_back(self):
        self.baseline()
        self.obs("waiting", "permission", "sigA")
        self.assertEqual(self.both, [])               # 确认延迟里还不算
        self.settle()
        self.assertEqual(self.both, [("s1", "attention")])
        self.assertEqual(self.done_only, [])          # 默认的监听者收不到 attention
        self.assertEqual(len(self.played), 1)
        # 同一个对话框（用户没操作过）再闪一下：不重复
        self.obs("busy")
        self.obs("waiting", "permission", "sigA")
        self.settle()
        self.assertEqual(self.both, [("s1", "attention")])
        # 用户回答过 → 再出现算新的一次
        self.n.user_acted("s1")
        self.obs("busy")
        self.obs("waiting", "permission", "sigA")
        self.settle()
        self.assertEqual(self.both, [("s1", "attention")] * 2)
        # 完成：两个监听者都收到
        self.obs("busy")
        self.obs("idle")
        self.settle()
        self.assertEqual(self.done_only, [("s1", "done")])
        self.assertEqual(self.both[-1], ("s1", "done"))

    def test_same_trigger_rules_as_sound(self):
        # 对话框在确认前消失：不回调
        self.baseline()
        self.obs("waiting", "permission", "sigA")
        self.obs("idle")
        self.settle()
        # /model、/effort 面板：不回调
        self.obs("waiting", "model", "sigM")
        self.settle()
        self.obs("idle")
        # 启动 / 重新附着的基线里已有的对话框：不回调
        self.n.reset("s2")
        self.obs("waiting", "question", "sigQ", sid="s2")
        self.settle()
        # 关闭 / 重启（mute）之后：不回调
        self.baseline("s3")
        self.n.mute("s3")
        self.obs("waiting", "permission", "sigX", sid="s3")
        self.settle()
        self.assertEqual(self.both, [])

    def test_not_affected_by_sound_settings(self):
        self.cfg = {"notify_enabled": False}
        self.baseline()
        self.obs("waiting", "permission", "sigA")
        self.settle()
        self.cfg = {"notify_on_attention": False}
        self.baseline("s2")
        self.obs("trust", "trust", "sigT", sid="s2")
        self.settle()
        self.assertEqual(self.both, [("s1", "attention"), ("s2", "attention")])
        self.assertEqual(self.played, [])
        # 全局间隔内两个会话都要处理：只响一次，两个都回调
        self.cfg = {}
        self.baseline("s3")
        self.baseline("s4")
        self.obs("waiting", "permission", "a", sid="s3")
        self.obs("waiting", "question", "b", sid="s4")
        self.settle()
        self.assertEqual(self.both[-2:], [("s3", "attention"), ("s4", "attention")])
        self.assertEqual(len(self.played), 1)


class TestSessionAttnAt(SessionBase):
    def mgr_with(self, notifier):
        def factory(cfg, jobid, on_event, on_state):
            return StubLink(self.rem, cfg, jobid, on_event, on_state)
        m = S.SessionManager(lambda: dict(self.cfg), pathlib.Path(self.tmp), link_factory=factory, notifier=notifier)
        m.step_timeout = 1.0
        m.poll_interval = 0.01
        m.link_timeout = 3.0
        m.exit_timeout = 2.0
        self.addCleanup(lambda: m._shutdown or m.shutdown())
        return m

    def test_attn_at_in_meta(self):
        n = N.Notifier(lambda: {"notify_enabled": False}, play=lambda p: True, sound_dir=None)
        n.CONFIRM_DELAY = 0.05
        n.QUIET_AFTER_RESET = 0.0
        n.GLOBAL_GAP = 0.0
        m = self.mgr_with(n)
        sid = self.start(m)
        self.assertIsNone(m.get_meta(sid)["attn_at"])
        t = self.tui(sid)
        t.view = "perm"
        m._fresh(m._get(sid))
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["attn_at"] is not None, 3), n.history)
        mt = m.get_meta(sid)
        self.assertEqual(mt["state"], "waiting")
        self.assertIsNone(mt["done_at"])              # 对话框不是「新回复」
        self.assertFalse(mt["unread"])
        a1 = mt["attn_at"]
        # 回答后再弹一个：attn_at 变大（单调）
        d = m.get_screen(sid)["state"]["dialog"]
        m.choose(sid, 1, expect=S.dialog_fingerprint(d))
        t.view = "idle"
        m._fresh(m._get(sid))
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle", 3))
        time.sleep(0.15)
        t.view = "perm"
        m._fresh(m._get(sid))
        self.assertTrue(wait_until(lambda: (m.get_meta(sid)["attn_at"] or 0) > a1, 3), n.history)

    def test_attn_monotonic_and_ignored_kinds(self):
        m = self.mgr()
        sid = self.start(m)
        s = m._get(sid)
        with mock.patch.object(S.time, "time", return_value=2_000_000_000.0):
            m._on_notify_event(sid, "attention")
            m._on_notify_event(sid, "attention")
        self.assertAlmostEqual(s.attn_at, 2_000_000_000.001, places=4)
        m._on_notify_event(sid, "bogus")
        m._on_notify_event("nope", "attention")
        self.assertIsNone(m.get_meta(sid)["done_at"])


# ============================================================================ 2. 页面（node）
def run_js(body, module="sysnotify.js"):
    url = (ROOT / "static" / module).as_uri()
    js = "const m = await import(%s);\n%s" % (json.dumps(url), body)
    r = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestSysNotifyJs(unittest.TestCase):
    def test_detect_events(self):
        out = run_js(r"""
const tr = new Map();
const now = 2000000000;
const r = {};
r.first = m.detectEvents(tr, { sid: 'a', done_at: now - 5, unread: true, attn_at: now - 9, state: 'waiting' }, now);   // 基线
r.replay = m.detectEvents(tr, { sid: 'a', done_at: now - 50, unread: true, state: 'idle' }, now);                    // 回放的旧 meta
r.same = m.detectEvents(tr, { sid: 'a', done_at: now - 5, unread: true, state: 'idle' }, now);
r.done = m.detectEvents(tr, { sid: 'a', done_at: now - 1, unread: true, state: 'idle' }, now);
r.again = m.detectEvents(tr, { sid: 'a', done_at: now - 1, unread: true, state: 'idle' }, now);
r.seenAlready = m.detectEvents(tr, { sid: 'a', done_at: now, unread: false, state: 'idle' }, now);                 // 已经看过（别的窗口）
r.attn = m.detectEvents(tr, { sid: 'a', done_at: now, attn_at: now, state: 'waiting' }, now);
r.attnGone = m.detectEvents(tr, { sid: 'a', attn_at: now + 1, state: 'busy' }, now);                               // 已经不在等了
r.trust = m.detectEvents(tr, { sid: 'a', attn_at: now + 2, state: 'trust' }, now);
r.stale = m.detectEvents(tr, { sid: 'a', done_at: now - 4000, unread: true, state: 'idle' }, now);                 // 比记下的小
r.old = m.detectEvents(new Map([['b', { done: 1, attn: 0 }]]), { sid: 'b', done_at: now - 700, unread: true }, now); // 10 分钟以前
r.both = m.detectEvents(new Map([['c', { done: 0, attn: 0 }]]), { sid: 'c', done_at: now - 1, unread: true, attn_at: now - 2, state: 'waiting' }, now);
r.bad = [m.detectEvents(tr, null, now), m.detectEvents(tr, { done_at: now }, now),
  m.detectEvents(new Map([['d', { done: 0, attn: 0 }]]), { sid: 'd', done_at: 'x', attn_at: NaN, unread: true }, now)];
// seed 只会抬高基线
const t2 = new Map();
m.seedTrack(t2, { sid: 'z', done_at: 10, attn_at: 5 });
m.seedTrack(t2, { sid: 'z', done_at: 3, attn_at: null });
r.seed = t2.get('z');
r.track = tr.get('a');
console.log(JSON.stringify(r));
""")
        now = 2000000000
        self.assertEqual(out["first"], [])
        self.assertEqual(out["replay"], [])
        self.assertEqual(out["same"], [])
        self.assertEqual(out["done"], [{"sid": "a", "kind": "done", "at": now - 1}])
        self.assertEqual(out["again"], [])
        self.assertEqual(out["seenAlready"], [])
        self.assertEqual(out["attn"], [{"sid": "a", "kind": "attention", "at": now}])
        self.assertEqual(out["attnGone"], [])
        self.assertEqual(out["trust"], [{"sid": "a", "kind": "attention", "at": now + 2}])
        self.assertEqual(out["stale"], [])
        self.assertEqual(out["old"], [])
        self.assertEqual([e["kind"] for e in out["both"]], ["attention", "done"])   # 按时间先后
        self.assertEqual(out["bad"], [[], [], []])
        self.assertEqual(out["seed"], {"done": 10, "attn": 5})
        self.assertEqual(out["track"], {"done": now, "attn": now + 2})

    def test_decide_coalesce_text(self):
        out = run_js(r"""
const r = {};
const ev = { sid: 'a', kind: 'done', at: 1 };
r.decide = [
  m.shouldNotify(ev, { enabled: false, foreground: false, activeSid: 'b' }),
  m.shouldNotify(ev, { enabled: true, foreground: true, activeSid: 'a' }),    // 窗口在前台、正看着它
  m.shouldNotify(ev, { enabled: true, foreground: false, activeSid: 'a' }),   // 窗口不在前台
  m.shouldNotify(ev, { enabled: true, foreground: true, activeSid: 'b' }),    // 不是当前会话
  m.shouldNotify(ev, { enabled: true, foreground: true, activeSid: null }),
  m.shouldNotify(null, { enabled: true }),
];
const last = new Map();
r.co = [m.coalesce(last, ev, 1000), m.coalesce(last, ev, 1000 + m.COALESCE_MS - 1), m.coalesce(last, { sid: 'a', kind: 'attention' }, 1000 + m.COALESCE_MS - 1),
  m.coalesce(last, { sid: 'b', kind: 'attention' }, 1000), m.coalesce(last, { sid: 'a', kind: 'attention' }, 1000 + 2 * m.COALESCE_MS)];
r.body = [
  m.notifyBody('done', null, {}),
  m.notifyBody('attention', { kind: 'permission', tool: 'Bash command', title: 'Bash command' }, {}),
  m.notifyBody('attention', { kind: 'permission', tool: 'Create file' }, {}),
  m.notifyBody('attention', { kind: 'permission' }, {}),
  m.notifyBody('attention', { kind: 'question', header: '选择方案', question: '用哪个？' }, {}),
  m.notifyBody('attention', { kind: 'question', question: '用\n哪个？' }, {}),
  m.notifyBody('attention', { kind: 'question' }, {}),
  m.notifyBody('attention', { kind: 'trust', title: 'Accessing workspace' }, {}),
  m.notifyBody('attention', { kind: 'generic', title: 'Ready to code?' }, {}),
  m.notifyBody('attention', null, { state: 'waiting', dialog_kind: 'permission' }),
  m.notifyBody('attention', null, { state: 'trust' }),
  m.notifyBody('attention', null, { state: 'waiting' }),
  m.notifyBody('attention', { kind: 'question', header: 'x'.repeat(300) }, {}).length,
];
r.title = [m.notifyTitle({ display_title: '  修 bug\n第二行 ', title: 't' }), m.notifyTitle({ title: 't' }), m.notifyTitle({}), m.notifyTitle(null),
  m.notifyTitle({ display_title: 'a'.repeat(500) }).length];
r.clean = m.cleanText('a\u0000b\u2028c\td  e', 50);
r.payload = m.postPayload({ sid: 'a', kind: 'attention' }, { display_title: '会话 A', state: 'waiting' }, { kind: 'permission', tool: 'Bash command' }, 'b');
r.payload2 = m.postPayload({ sid: 'b', kind: 'done' }, { title: 'B' }, null, 'b');
r.sid = ['s0123abc', 'a.b-c_d', '', 'a/b', 'x'.repeat(81), '中文'].map((s) => m.SID_RE.test(s));
r.hint = [m.authHint(null), m.authHint({ available: true, auth: 'denied' }), m.authHint({ available: true, auth: 'notDetermined' }),
  m.authHint({ available: true, auth: 'authorized', enabled: true }), m.authHint({ available: true, auth: 'authorized', enabled: false }),
  m.authHint({ available: true, auth: 'notDetermined', enabled: false, error: '不允许此应用发送通知' })];
console.log(JSON.stringify(r));
""")
        self.assertEqual(out["decide"], [False, False, True, True, True, False])
        self.assertEqual(out["co"], [True, False, True, True, True])
        b = out["body"]
        self.assertEqual(b[:12], ["完成", "需要审批：Bash", "需要审批：Create file", "需要审批", "需要回答：选择方案", "需要回答：用 哪个？",
                                  "需要回答问题", "需要确认：信任这个目录", "需要处理：Ready to code?", "需要审批",
                                  "需要确认：信任这个目录", "需要你处理"])
        self.assertLessEqual(b[12], 160)
        self.assertEqual(out["title"][:4], ["修 bug 第二行", "t", "会话", "会话"])
        self.assertEqual(out["title"][4], 120)
        self.assertEqual(out["clean"], "a b c d e")
        self.assertEqual(out["payload"], {"op": "post", "sid": "a", "kind": "attention", "title": "会话 A",
                                          "body": "需要审批：Bash", "current": False})
        self.assertEqual(out["payload2"]["current"], True)
        self.assertEqual(out["sid"], [True, True, False, False, False, False])
        self.assertIn("不能", out["hint"][0])
        self.assertIn("系统设置", out["hint"][1])
        self.assertIn("授权框", out["hint"][2])
        self.assertIn("已授权", out["hint"][3])
        self.assertIn("请求通知授权失败：不允许此应用发送通知", out["hint"][5])

    def test_controller_with_stub_bridge(self):
        """SysNotify 控制器：stub 掉 window.webkit.messageHandlers.ccrNotify，核对调用时机与内容（不碰 DOM）。"""
        out = run_js(r"""
const calls = [];
const listeners = {};
let status = { available: true, enabled: true, auth: 'authorized' };
const win = {
  webkit: { messageHandlers: { ccrNotify: { postMessage: (msg) => { calls.push(msg); return Promise.resolve(msg.op === 'status' || msg.op === 'enable' ? { ...status, ...(msg.op === 'enable' ? { enabled: msg.on } : {}) } : { ok: true }); } } } },
  addEventListener: (t, fn) => { listeners[t] = fn; },
};
const opened = [];
let fg = true, active = 'a';
const sessions = new Map();
const n = new m.SysNotify(win);
const r = {};
r.available = n.available;
n.init({ activeSid: () => active, hasSession: (sid) => sessions.has(sid), foreground: () => fg, dialog: (s) => s.dialog || null, onOpen: (sid) => opened.push(sid) });
await new Promise((res) => setTimeout(res, 0));
r.status = n.status;
const now = Date.now() / 1000;
const S = (sid, meta, extra) => ({ sid, meta: { sid, ...meta }, ...(extra || {}) });
sessions.set('a', S('a', { display_title: 'A', done_at: now - 100, unread: false, state: 'idle' }));
sessions.set('b', S('b', { display_title: 'B', state: 'idle' }));
n.seed(sessions.values());
calls.length = 0;
// 当前会话 a、窗口在前台：完成不弹
n.onMeta(S('a', { display_title: 'A', done_at: now - 1, unread: true, state: 'idle' }));
// 不是当前会话：弹
n.onMeta(S('b', { display_title: 'B', done_at: now - 1, unread: true, state: 'idle' }));
// 同一会话同一种 20 秒内：合并
n.onMeta(S('b', { display_title: 'B', done_at: now, unread: true, state: 'idle' }));
// 需要审批：正文带工具名
n.onMeta(S('b', { display_title: 'B', done_at: now, attn_at: now, state: 'waiting' }, { dialog: { kind: 'permission', tool: 'Bash command' } }));
// 窗口不在前台：当前会话也弹（current: true，原生壳再核对一次前台）
fg = false;
n.onMeta(S('a', { display_title: 'A', done_at: now + 0.5, unread: true, state: 'idle' }));
// 只读的回放会话不管
n.onMeta({ sid: 'h', readOnly: true, meta: { sid: 'h', done_at: now + 9, unread: true } });
r.posts = calls.map((c) => [c.op, c.sid, c.kind, c.title, c.body, c.current]);
// 关掉开关：照样记基线，但不发
await n.setEnabled(false);
r.afterOff = n.status.enabled;
calls.length = 0;
n.onMeta(S('b', { display_title: 'B', done_at: now + 60, unread: true, state: 'idle' }));
r.offCalls = calls.map((c) => c.op);
await n.setEnabled(true);
calls.length = 0;
n.onMeta(S('b', { display_title: 'B', done_at: now + 60, unread: true, state: 'idle' }));   // 关着时那次已记入基线
r.onCalls = calls.map((c) => c.op);
// 打开会话：已加载 → 立即；没加载 → 等 seed；非法 sid → 忽略；事件要 preventDefault（原生壳据此知道页面接手了）
let prevented = 0;
listeners['ccr-native-open-session']({ detail: { sid: 'b' }, preventDefault: () => prevented++ });
listeners['ccr-native-open-session']({ detail: { sid: 'zz' }, preventDefault: () => prevented++ });
listeners['ccr-native-open-session']({ detail: { sid: '../x' }, preventDefault: () => prevented++ });
r.opened1 = opened.slice();
sessions.set('zz', S('zz', { state: 'idle' }));
n.seed(sessions.values());
r.opened2 = opened.slice();
r.prevented = prevented;
// 冷启动：status 回复里带 open
status = { available: true, enabled: true, auth: 'authorized', open: 'a' };
await n.refresh();
r.opened3 = opened.slice();
// clear：开着才发
calls.length = 0;
n.clear('b');
n.clear('../x');
r.clear = calls.map((c) => [c.op, c.sid]);
// 浏览器里没有 handler：什么都不做
const plain = new m.SysNotify({ addEventListener: () => { throw new Error('不该注册'); } });
plain.init({});
plain.onMeta(S('b', { done_at: now + 99, unread: true }));
r.plain = [plain.available, plain.status, await plain.refresh(), await plain.setEnabled(true)];
// handler 报错 / 拒绝：不抛出
const bad = new m.SysNotify({ webkit: { messageHandlers: { ccrNotify: { postMessage: () => Promise.reject(new Error('rejected')) } } }, addEventListener() {} });
r.bad = await bad.refresh();
console.log(JSON.stringify(r));
""")
        self.assertTrue(out["available"])
        self.assertEqual(out["status"], {"available": True, "enabled": True, "auth": "authorized", "error": None})
        self.assertEqual(out["posts"], [
            ["post", "b", "done", "B", "完成", False],
            ["post", "b", "attention", "B", "需要审批：Bash", False],
            ["post", "a", "done", "A", "完成", True],
        ])
        self.assertFalse(out["afterOff"])
        self.assertEqual(out["offCalls"], [])
        self.assertEqual(out["onCalls"], [])
        self.assertEqual(out["opened1"], ["b"])
        self.assertEqual(out["opened2"], ["b", "zz"])
        self.assertEqual(out["prevented"], 3)
        self.assertEqual(out["opened3"], ["b", "zz", "a"])
        self.assertEqual(out["clear"], [["clear", "b"]])
        self.assertEqual(out["plain"], [False, None, None, None])
        self.assertIsNone(out["bad"])

    def test_keepawake_text(self):
        out = run_js(r"""
console.log(JSON.stringify([
  m.keepAwakeText(null, false),
  m.keepAwakeText(null, true),
  m.keepAwakeText({ available: false }, true),
  m.keepAwakeText({ available: true, active: true, working: 2, release_in: null }, true),
  m.keepAwakeText({ available: true, active: true, working: 0, release_in: 41.2 }, true),
  m.keepAwakeText({ available: true, active: false, working: 0 }, true),
  m.keepAwakeText({ available: true, active: true, working: 1, dry_run: true }, true),
]));
""", module="keepawake.js")
        self.assertIn("已关闭", out[0])
        self.assertIn("正在读取", out[1])
        self.assertIn("找不到", out[2])
        self.assertEqual(out[3], "正在阻止空闲睡眠：2 个会话在干活。")
        self.assertEqual(out[4], "会话都空闲了，约 42 秒后恢复正常睡眠。")
        self.assertEqual(out[5], "没有会话在干活，Mac 正常睡眠。")
        self.assertIn("演示模式", out[6])


# ============================================================================ 3. 原生壳
SWIFT = ROOT / "tools" / "macapp" / "main.swift"


@unittest.skipUnless(shutil.which("xcrun"), "需要 xcrun swiftc")
class TestNativeNotifyScript(unittest.TestCase):
    """抽出 main.swift 里 ccr-notify-script 一段单独编译（只用 Foundation + JavaScriptCore），不开窗口、不发通知。"""

    def test_parse_and_open_script(self):
        src = SWIFT.read_text(encoding="utf-8")
        a = src.index("// BEGIN ccr-notify-script")
        b = src.index("// END ccr-notify-script")
        prog = "import Foundation\nimport JavaScriptCore\n" + src[a:b] + r"""
let cases: [Any] = try! JSONSerialization.jsonObject(with: CommandLine.arguments[1].data(using: .utf8)!) as! [Any]
var out: [Any] = []
for c in cases {
    if let r = ccrParseNotify(c) {
        out.append(["sid": r.sid, "kind": r.kind, "title": r.title, "body": r.body, "current": r.current])
    } else {
        out.append(NSNull())
    }
}
var scripts: [Any] = []
for sid in ["s0123abc", "a.b-c_d", "bad\"sid", "", "x</script>"] {
    guard let js = ccrOpenSessionScript(sid) else { scripts.append(NSNull()); continue }
    var res: [Any] = []
    for handled in [true, false] {
        let ctx = JSContext()!
        ctx.evaluateScript("var got = null; var window = { dispatchEvent: function (e) { got = { type: e.type, detail: e.detail };"
                           + (handled ? " e.defaultPrevented = true;" : "") + " return !e.defaultPrevented; } };"
                           + "function CustomEvent(t, o) { this.type = t; this.detail = o.detail; this.cancelable = o.cancelable; this.defaultPrevented = false; }")
        let ret = ctx.evaluateScript(js)!
        res.append(["ret": ret.toBool(), "got": ctx.evaluateScript("JSON.stringify(got)")!.toString()!])
    }
    scripts.append(res)
}
print(String(data: try! JSONSerialization.data(withJSONObject: ["parse": out, "scripts": scripts,
      "sid": ["ok": ccrValidSid("s1"), "slash": ccrValidSid("a/b"), "long": ccrValidSid(String(repeating: "a", count: 81)),
              "max": ccrValidSid(String(repeating: "a", count: 80)), "cjk": ccrValidSid("中")],
      "clean": ccrCleanText("  a\u{0}b\u{2028}c\n\td  ", max: 50), "cut": ccrCleanText(String(repeating: "长", count: 10), max: 5)]),
      encoding: .utf8)!)
"""
        cases = [
            {"op": "post", "sid": "s0123abc", "kind": "done", "title": "会话 A", "body": "完成", "current": False},
            {"op": "post", "sid": "s1", "kind": "attention", "title": " 多\n行\u2028标题 ", "body": "需要审批：Bash"},
            {"op": "post", "sid": "s1", "kind": "done", "title": "", "body": ""},
            {"op": "post", "sid": "s1", "kind": "attention", "title": "t" * 500, "body": "b" * 500, "current": True},
            {"op": "post", "sid": "../etc", "kind": "done", "title": "t", "body": "b"},
            {"op": "post", "sid": "s1", "kind": "evil", "title": "t", "body": "b"},
            {"op": "post", "sid": "s1", "kind": "done", "title": 5, "body": "b"},
            {"op": "post", "sid": "s1", "kind": "done", "title": "t"},
            {"op": "post", "sid": "s1", "kind": "done", "title": "x" * 2001, "body": "b"},
            {"op": "post", "sid": "s1", "kind": "done", "title": "t", "body": "b", "current": "yes"},
            {"op": "post", "sid": "s1", "kind": "done", "title": "t", "body": "b", "a": 1, "b": 2, "c": 3, "d": 4},
            ["not", "a", "dict"],
        ]
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            f = os.path.join(td, "t.swift")
            pathlib.Path(f).write_text(prog, encoding="utf-8")
            exe = os.path.join(td, "t")
            r = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-o", exe, f], capture_output=True, text=True,
                               timeout=300)
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            r = subprocess.run([exe, json.dumps(cases)], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout)
        p = res["parse"]
        self.assertEqual(p[0], {"sid": "s0123abc", "kind": "done", "title": "会话 A", "body": "完成", "current": False})
        self.assertEqual(p[1], {"sid": "s1", "kind": "attention", "title": "多 行 标题", "body": "需要审批：Bash", "current": False})
        self.assertEqual(p[2], {"sid": "s1", "kind": "done", "title": "会话", "body": "完成", "current": False})
        self.assertEqual(len(p[3]["title"]), 120)
        self.assertTrue(p[3]["title"].endswith("…"))
        self.assertEqual(len(p[3]["body"]), 160)
        self.assertTrue(p[3]["current"])
        self.assertEqual(p[4:], [None] * 8)
        ok = res["scripts"][0]
        self.assertEqual(ok[0], {"ret": True, "got": json.dumps({"type": "ccr-native-open-session", "detail": {"sid": "s0123abc"}},
                                                                 separators=(",", ":"))})
        self.assertFalse(ok[1]["ret"])                 # 页面没接手（还在加载）→ 原生壳先记下
        self.assertEqual(json.loads(res["scripts"][1][0]["got"])["detail"], {"sid": "a.b-c_d"})
        self.assertEqual(res["scripts"][2:], [None, None, None])
        self.assertEqual(res["sid"], {"ok": True, "slash": False, "long": False, "max": True, "cjk": False})
        self.assertEqual(res["clean"], "a b c d")
        self.assertEqual(res["cut"], "长长长长…")

    def test_swift_source_rules(self):
        src = SWIFT.read_text(encoding="utf-8")
        a = src.index("// MARK: - 系统通知")
        b = src.index("func sanitizeFilename")
        body = src[a:b]
        # 授权只在用户打开开关时请求（enable 里）；启动时 setUp 只设 delegate
        self.assertEqual(src.count("requestAuthorization("), 1)
        req = body.index("requestAuthorization(")
        self.assertGreater(req, body.index("func enable("))
        self.assertLess(req, body.index("func post("))
        setup = body[body.index("func setUp("):body.index("nonisolated static func authName")]
        self.assertNotIn("requestAuthorization", setup)
        self.assertIn("Self.supported && enabled", setup)
        # 不在 .app 里（没有 bundle id）时绝不调用 UNUserNotificationCenter.current()（会抛异常）
        self.assertIn('Bundle.main.bundleURL.pathExtension == "app"', body)
        # 通知不带声音（提示音由后端 afplay 负责）；同一会话同一个 identifier
        self.assertNotIn(".sound", body)
        self.assertIn('identifier: "ccr." + req.sid', body)
        # 页面消息：主框架 + 本应用 origin + 正在显示本应用页面
        self.assertIn("message.frameInfo.isMainFrame, c.pageState == .app", body)
        self.assertIn("c.isAppOrigin(message.frameInfo.securityOrigin)", body)
        self.assertIn('addScriptMessageHandler(sysNotifier, contentWorld: .page, name: "ccrNotify")', src)
        # 不碰剪贴板、不读文件
        self.assertNotIn("NSPasteboard", body)
        self.assertNotIn("Data(contentsOf", body)
        # 启动时调用 setUp（不请求授权）
        launch = src[src.index("func applicationDidFinishLaunching"):src.index("func applicationShouldTerminateAfterLastWindowClosed")]
        self.assertIn("sysNotifier.setUp(self)", launch)


if __name__ == "__main__":
    unittest.main()
