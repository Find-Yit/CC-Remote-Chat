"""回复进行中发出的消息（用户报告：模型在处理时再发一条——立即发送或排队——聊天记录里都不显示）。

CLI 2.1.330–2.1.332 真机实测：主会话这一轮中途吸收的用户消息**只**写成一条
`{"type":"attachment","attachment":{"type":"queued_command","commandMode":"prompt","origin":{"kind":"human"},
"prompt":…,"source_uuid","delivery_id","humanTurn":true,…}}`，不另写 user 条目（工具结果之后被吸收；Ctrl+X Ctrl+S
在命令运行中按下时命令转入后台、消息同样被吸收）；没被吸收的在本轮结束后作为新的一轮发出（user 条目
promptSource=queued），流式输出中按 Ctrl+X Ctrl+S 也是这种（截断当前输出、不写中断标记）。带图片时 prompt 是
[文字块, 图片块…]。别的会话发来的（origin.kind=peer）CLI 记为 isMeta、正文包在 <agent-message from=…> 里。
条目全部是合成的（不含任何真实 transcript 内容）。
"""
import base64
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ccr import fake_tui
from ccr import history as H
from ccr import node_agent as NA
from tests.test_node_agent import SID, UUID, big_b64

ROOT = pathlib.Path(__file__).resolve().parent.parent


def att_line(attachment, **top):
    d = {"parentUuid": "p-1", "isSidechain": False, "type": "attachment", "uuid": "a-1",
         "timestamp": "2026-11-25T08:00:00.000Z", "attachment": attachment, "rendered": [{"content": "x" * 3000}],
         "renderedRole": "system", "cwd": "/w", "sessionId": UUID, "version": "2.1.332", "userType": "external"}
    d.update(top)
    return json.dumps(d).encode()


def human(prompt, **kw):
    a = {"type": "queued_command", "prompt": prompt, "source_uuid": "s-1", "delivery_id": "d-1",
         "commandMode": "prompt", "origin": {"kind": "human"}, "timestamp": "2026-11-25T08:00:00.000Z", "humanTurn": True}
    a.update(kw)
    return a


class NoteAttachmentTests(unittest.TestCase):
    def test_human_prompt_forwarded_and_trimmed(self):
        got = NA.filter_entry(att_line(human("中途发的消息\n第二行")))
        self.assertEqual(got, {
            "type": "attachment", "uuid": "a-1", "parentUuid": "p-1", "timestamp": "2026-11-25T08:00:00.000Z",
            "isSidechain": False, "attachment": {"type": "queued_command", "commandMode": "prompt",
                                                 "prompt": "中途发的消息\n第二行", "origin": {"kind": "human"},
                                                 "source_uuid": "s-1"}})

    def test_origin_missing_and_peer(self):
        a = human("没有 origin 的旧版本")
        del a["origin"]
        got = NA.filter_entry(att_line(a))
        self.assertEqual(got["attachment"], {"type": "queued_command", "commandMode": "prompt",
                                             "prompt": "没有 origin 的旧版本", "source_uuid": "s-1"})
        peer = human('<agent-message from="helper">\n报告\n</agent-message>', isMeta=True,
                     origin={"kind": "peer", "from": "x", "name": "helper", "body": "报告", "handback": True})
        got = NA.filter_entry(att_line(peer))
        self.assertEqual(got["attachment"]["origin"], {"kind": "peer", "name": "helper"})
        self.assertIn("<agent-message", got["attachment"]["prompt"])

    def test_not_forwarded(self):
        for bad in (human("CLI 自己的隐藏提示", isMeta=True),
                    human("plugin 提示", isMeta=True, origin={"kind": "plugin", "name": "p"}),
                    human("转发的意图", origin={"kind": "unclassified"}),
                    human("   "), human([{"type": "text", "text": " "}]), human(None),
                    {"type": "queued_command", "prompt": "bash 模式", "commandMode": "bash", "origin": {"kind": "human"}},
                    {"type": "date", "date": "2026-11-25"}):
            self.assertIsNone(NA.filter_entry(att_line(bad)), bad)
        self.assertIsNone(NA.filter_entry(att_line(human("子代理里的"), isSidechain=True)))
        self.assertIsNone(NA.filter_entry(att_line(human("子代理 transcript")), NA.SUBAGENT_TYPES, sidechain=True))

    def test_task_notification_unchanged(self):
        note = "<task-notification>\n<task-id>ag1</task-id>\n<status>killed</status>\n</task-notification>"
        got = NA.filter_entry(att_line({"type": "queued_command", "prompt": note, "commandMode": "task-notification",
                                        "source_uuid": "s", "origin": {"kind": "task-notification"}}))
        self.assertEqual(got["attachment"], {"type": "queued_command", "commandMode": "task-notification",
                                             "prompt": note, "origin": {"kind": "task-notification"}})

    def test_long_prompt_truncated(self):
        got = NA.filter_entry(att_line(human("长" * (NA.MAX_STR + 50))))
        self.assertTrue(got["truncated"])
        self.assertEqual(len(got["attachment"]["prompt"]), NA.MAX_STR)

    def test_bad_source_uuid_dropped(self):
        got = NA.filter_entry(att_line(human("x", source_uuid="u" * 65)))
        self.assertNotIn("source_uuid", got["attachment"])


class AttachmentImageRefTests(unittest.TestCase):
    """带图片的 prompt：块下标与原行一致，大图换成 ccr_ref，blob op 按 attachment.prompt[i] 取回。"""

    def setUp(self):
        self.td = os.path.realpath(tempfile.mkdtemp(dir="/tmp"))
        self.proj = os.path.join(self.td, ".claude", "projects", "-x")
        os.makedirs(self.proj)
        self.envp = mock.patch.dict(os.environ, {"HOME": self.td})
        self.envp.start()
        self.out = []
        self.emitp = mock.patch.object(NA, "emit", self.out.append)
        self.emitp.start()
        self.w = NA.Watched(SID)
        self.watcher = NA.Watcher.__new__(NA.Watcher)
        NA._known.clear()

    def tearDown(self):
        self.emitp.stop()
        self.envp.stop()
        NA._known.clear()
        shutil.rmtree(self.td)

    def test_image_prompt_ref_and_blob(self):
        big = big_b64(30000, b"q")
        small = base64.b64encode(b"\x89PNG small").decode()
        img = lambda d: {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": d}}  # noqa: E731
        prompt = [{"type": "text", "text": "[Image #1] [Image #2] 看这两张图", "cache_control": {"type": "x"}},
                  {"type": "document", "source": {"data": "zzz"}}, img(big), img(small)]
        first = json.dumps({"type": "user", "uuid": "u0", "message": {"role": "user", "content": "开始"}}) + "\n"
        line = att_line(human(prompt, imagePasteIds=[1, 2]), uuid="a-img").decode() + "\n"
        with open(os.path.join(self.proj, UUID + ".jsonl"), "w", encoding="utf-8") as f:
            f.write(first + line)
        self.out.clear()
        self.watcher.poll_transcript(self.w, UUID, "/x")
        got = [e for ev in self.out if ev["ev"] == "transcript" for e in ev["entries"]]
        self.assertEqual([e["uuid"] for e in got], ["u0", "a-img"])
        p = got[1]["attachment"]["prompt"]
        self.assertEqual(p[0], {"type": "text", "text": "[Image #1] [Image #2] 看这两张图"})
        self.assertEqual(p[1], {"type": "document"})
        src = p[2]["source"]
        self.assertEqual(src["type"], "ccr_ref")
        self.assertEqual(src["ref"].split(".")[2:4], [str(len(first.encode())), "2"])
        self.assertEqual(p[3], img(small))
        self.assertNotIn("truncated", got[1])
        self.assertEqual(NA.op_blob({"sid": SID, "ref": src["ref"]})["data"], big)


class HistoryTests(unittest.TestCase):
    def test_read_lines(self):
        big = big_b64(30000, b"h")
        lines = [
            json.dumps({"type": "user", "uuid": "u1", "timestamp": "t", "message": {"role": "user", "content": "第一条"}}).encode(),
            json.dumps({"type": "queue-operation", "operation": "enqueue", "content": "中途"}).encode(),
            att_line(human("中途"), uuid="a1"),
            att_line(human("隐藏", isMeta=True), uuid="a2"),
            att_line(human('<agent-message from="h">报告</agent-message>', isMeta=True,
                           origin={"kind": "peer", "name": "h"}), uuid="a3"),
            att_line(human([{"type": "text", "text": "[Image #1] 图"},
                            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": big}},
                            {"type": "document"}]), uuid="a4"),
            att_line({"type": "date", "date": "x"}, uuid="a5"),
            att_line(human("子代理"), uuid="a6", isSidechain=True),
        ]
        r = H.read_lines(lines)
        atts = [e for e in r["entries"] if e["type"] == "attachment"]
        self.assertEqual([e["uuid"] for e in atts], ["a1", "a3", "a4"])
        self.assertEqual(atts[0]["attachment"], {"type": "queued_command", "commandMode": "prompt", "prompt": "中途",
                                                 "origin": {"kind": "human"}, "source_uuid": "s-1"})
        self.assertEqual(atts[1]["attachment"]["origin"], {"kind": "peer", "name": "h"})
        p = atts[2]["attachment"]["prompt"]
        self.assertEqual([b["type"] for b in p], ["text", "text"])   # 大图片换成"已省略"文字，未知块丢掉
        self.assertIn("已省略", p[1]["text"])
        self.assertTrue(atts[2]["truncated"])


class FakeTuiQueueTests(unittest.TestCase):
    """fake_tui 仿真机行为：/bg 的命令结果之后吸收排队消息（只写 attachment）；Ctrl+X Ctrl+S。"""

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="ccrqp", dir="/tmp")
        self.home = os.path.realpath(self.td)
        os.makedirs(os.path.join(self.home, "proj"))

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def app(self):
        a = fake_tui.FakeClaude([], env={"HOME": self.home, "USER": "u", "CCR_FAKE_DELAY": "0", "CCR_FAKE_THINK": "0"},
                                size=(100, 30), cwd=os.path.join(self.home, "proj"))
        a.host = "node1"
        return a

    def steps(self, a, n):
        for _ in range(n):
            a.turn_due = 0
            a.step_turn()

    def run_turn(self, a):
        for _ in range(5000):
            if a.turn is None:
                return
            a.turn_due = 0
            a.step_turn()
        raise AssertionError("回合没有结束")

    def submit(self, a, text):
        a.on_text(text)
        a.on_key("Enter")

    def transcript(self, a):
        with open(a.tpath, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]

    def frame(self, a):
        return [x.rstrip() for x in a.frame()]

    def test_absorbed_after_tool_result(self):
        a = self.app()
        self.submit(a, "/bg 数一数")
        self.steps(a, 3)
        self.assertTrue(a.bg_running)
        self.submit(a, "中途发的消息")
        self.assertEqual(a.queue, ["中途发的消息"])
        self.assertIn("  ⎿  " + fake_tui.QUEUED_HINT, self.frame(a))
        self.run_turn(a)
        tr = self.transcript(a)
        users = [e for e in tr if e["type"] == "user" and isinstance(e["message"]["content"], str)]
        self.assertEqual([u["message"]["content"] for u in users], ["/bg 数一数"])   # 没有 user 条目
        k = next(i for i, e in enumerate(tr) if e["type"] == "attachment" and e["attachment"]["type"] == "queued_command")
        att = tr[k]
        self.assertEqual((att["attachment"]["commandMode"], att["attachment"]["origin"], att["attachment"]["prompt"],
                          att["attachment"]["humanTurn"]), ("prompt", {"kind": "human"}, "中途发的消息", True))
        conv = [e["type"] + ("/result" if e["type"] == "user" and isinstance(e["message"]["content"], list) else "")
                for e in tr if e["type"] in ("user", "assistant") or e is att]
        self.assertEqual(conv, ["user", "assistant", "user/result", "attachment", "assistant"])
        last = [e for e in tr if e["type"] == "assistant"][-1]
        self.assertEqual(last["parentUuid"], att["uuid"])
        self.assertIn("收到回复进行中发来的消息：中途发的消息", last["message"]["content"][0]["text"])
        got = self.frame(a)
        self.assertIn("❯ 中途发的消息", got)
        self.assertNotIn("  ⎿  " + fake_tui.QUEUED_HINT, got)
        # agent 转发后前端能渲染（精简版）
        fwd = NA.filter_entry(json.dumps(att).encode())
        self.assertEqual(fwd["attachment"]["prompt"], "中途发的消息")

    def test_absorbed_with_image(self):
        a = self.app()
        self.submit(a, "/bg")
        self.steps(a, 3)
        a.add_image(None, b"\x89PNG\r\n\x1a\nfake")
        a.on_text(" 看图")
        a.on_key("Enter")
        self.run_turn(a)
        att = next(e for e in self.transcript(a) if e["type"] == "attachment" and e["attachment"]["type"] == "queued_command")
        p = att["attachment"]["prompt"]
        self.assertEqual(p[0], {"type": "text", "text": "[Image #1] 看图"})
        self.assertEqual(p[1]["source"]["media_type"], "image/png")
        self.assertEqual(att["attachment"]["imagePasteIds"], [1])
        self.assertIn("[Image #1]", "\n".join(self.frame(a)))

    def test_send_now_while_command_runs(self):
        a = self.app()
        self.submit(a, "/bg")
        self.steps(a, 3)
        self.submit(a, "立即发送这条")
        a.on_key("C-x")
        a.on_key("C-s")
        self.run_turn(a)
        tr = self.transcript(a)
        res = [b for e in tr if e["type"] == "user" and isinstance(e["message"]["content"], list)
               for b in e["message"]["content"] if b.get("type") == "tool_result"]
        self.assertTrue(res[-1]["content"].startswith("Command running in background"))
        atts = [e["attachment"]["prompt"] for e in tr if e["type"] == "attachment" and e["attachment"]["type"] == "queued_command"]
        self.assertEqual(atts, ["立即发送这条"])
        self.assertFalse(any(e["type"] == "user" and e["message"]["content"] == "立即发送这条" for e in tr))
        self.assertEqual(a.shells(), 1)

    def test_send_now_while_streaming_and_dequeued(self):
        a = self.app()
        self.submit(a, "/long")
        self.steps(a, 5)
        self.submit(a, "截断后发送")
        a.on_key("C-x")
        a.on_key("C-s")
        tr = self.transcript(a)
        texts = [e["message"]["content"] for e in tr if e["type"] == "user"]
        self.assertFalse(any("Request interrupted" in json.dumps(t) for t in texts))   # 不写中断标记
        u = [e for e in tr if e["type"] == "user" and e["message"]["content"] == "截断后发送"]
        self.assertEqual(len(u), 1)
        self.assertEqual(u[0]["promptSource"], "queued")
        self.assertNotIn("Interrupted · What should Claude do instead?", "\n".join(self.frame(a)))
        self.run_turn(a)
        # 没有工具结果的一轮：排队消息等本轮结束后作为新的一轮发出（promptSource=queued）
        self.submit(a, "第一条")
        self.submit(a, "第二条")
        self.run_turn(a)
        self.run_turn(a)
        u2 = [e for e in self.transcript(a) if e["type"] == "user" and e["message"]["content"] == "第二条"]
        self.assertEqual(u2[0]["promptSource"], "queued")
        self.assertFalse(any(e["type"] == "attachment" and e["attachment"]["type"] == "queued_command"
                             for e in self.transcript(a)))


def run_js(module, body):
    url = (ROOT / "static" / module).as_uri()
    js = "const m = await import(%s);\n%s" % (json.dumps(url), body)
    r = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(shutil.which("node"), "需要 node")
class FrontendPureTests(unittest.TestCase):
    def test_changes_mid_turn_does_not_split(self):
        out = run_js("changes.js", r"""
const items = [
  { kind: 'user', text: '第一轮', id: 'u1' },
  { kind: 'tool', toolId: 't1', name: 'Write', input: { file_path: '/r/a.txt', content: 'a\n' }, structured: { type: 'create', filePath: '/r/a.txt', content: 'a\n' }, result: 'ok', done: true },
  { kind: 'user', text: '回复中发送', id: 'u2', midTurn: true },
  { kind: 'tool', toolId: 't2', name: 'Write', input: { file_path: '/r/b.txt', content: 'b\n' }, structured: { type: 'create', filePath: '/r/b.txt', content: 'b\n' }, result: 'ok', done: true },
  { kind: 'user', text: '第二轮', id: 'u3' },
];
const t = new m.ChangeTracker();
t.update({ items, subagents: new Map() });
console.log(JSON.stringify({ turns: t.turns.map((x) => x.text), edits: t.main.map((e) => e.turn) }));
""")
        self.assertEqual(out["turns"], ["", "第一轮", "第二轮"])
        self.assertEqual(out["edits"], [1, 1])

    def test_prompthist_counts_mid_turn(self):
        out = run_js("prompthist.js", r"""
const items = [{ kind: 'user', text: 'A' }, { kind: 'tool' }, { kind: 'user', text: 'B', midTurn: true }, { kind: 'hint', text: '来自其它会话：x' }];
const pend = [];
m.addPending(pend, 'B', 1);
const r = m.mergePending(m.promptsFromItems(items), pend);
console.log(JSON.stringify({ list: r.list, pending: r.pending.length }));
""")
        self.assertEqual(out, {"list": ["A", "B"], "pending": 0})


if __name__ == "__main__":
    unittest.main()
