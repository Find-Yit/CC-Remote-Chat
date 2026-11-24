"""ccr.history 测试：用 fixtures 测解析纯函数 + 本地跑同一份远端脚本 + HistoryService（打桩远端）。"""

import ast
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

from ccr import history as H

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures"
T1 = "285bd6a9-08c3-46e0-b111-3fe228694c59"
T2 = "9cdf8e9b-fd70-4c80-8b58-e1189e65abe1"


def fixture_path(uid):
    return FIX / ("transcript_%s.jsonl" % uid)


def jl(*objs):
    return [json.dumps(o, ensure_ascii=False).encode("utf-8") + b"\n" for o in objs]


def user(text, **kw):
    d = {"parentUuid": None, "isSidechain": False, "type": "user",
         "message": {"role": "user", "content": text}, "uuid": "u-%d" % id(text),
         "timestamp": "2026-11-22T08:00:00.000Z", "cwd": "/home/x/proj", "sessionId": T1}
    d.update(kw)
    return d


class TestFixturesParse(unittest.TestCase):
    def test_summarize_fixture_titles_and_cwd(self):
        with open(fixture_path(T1), "rb") as f:
            s = H.summarize_lines(f)
        self.assertEqual(s["cwd"], "/mnt/ssd/home/demouser")
        self.assertTrue(s["title"].startswith("请先简单思考一下"))
        self.assertLessEqual(len(s["title"]), 80)
        with open(fixture_path(T2), "rb") as f:
            s2 = H.summarize_lines(f)
        self.assertTrue(s2["title"].startswith("请先用 Bash 执行"))

    def test_read_fixture_entries_filtered(self):
        with open(fixture_path(T2), "rb") as f:
            r = H.read_lines(f)
        self.assertEqual(r["cwd"], "/mnt/ssd/home/demouser")
        ents = r["entries"]
        self.assertTrue(ents)
        self.assertEqual({e["type"] for e in ents}, {"user", "assistant"})
        # 原始 transcript 中 user/assistant 行数（fixtures 中没有 meta/sidechain）
        raw = 0
        with open(fixture_path(T2), "rb") as f:
            for line in f:
                d = json.loads(line)
                if d.get("type") in ("user", "assistant") and not d.get("isMeta") and not d.get("isSidechain"):
                    raw += 1
        self.assertEqual(len(ents), raw)
        for e in ents:
            self.assertEqual(set(e) - {"truncated", "origin", "promptSource", "toolUseResult"},
                             {"type", "uuid", "timestamp", "message"})
            self.assertIn("content", e["message"])
            self.assertIn(e["message"]["role"], ("user", "assistant"))
        # thinking 的 signature 被去掉
        thinking = [b for e in ents if isinstance(e["message"]["content"], list)
                    for b in e["message"]["content"] if b.get("type") == "thinking"]
        self.assertTrue(thinking)
        self.assertTrue(all("signature" not in b for b in thinking))
        # 工具调用/结果都保留
        kinds = [b.get("type") for e in ents if isinstance(e["message"]["content"], list)
                 for b in e["message"]["content"]]
        self.assertIn("tool_use", kinds)
        self.assertIn("tool_result", kinds)
        ask = [b for e in ents if isinstance(e["message"]["content"], list)
               for b in e["message"]["content"] if b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion"]
        self.assertEqual(len(ask), 1)
        self.assertIn("questions", ask[0]["input"])
        # 同一 message.id 多次出现（每次一个 block），id 保留给前端合并
        ids = [e["message"].get("id") for e in ents if e["type"] == "assistant"]
        self.assertTrue(all(ids))

    def test_read_fixture_first_user_text(self):
        with open(fixture_path(T1), "rb") as f:
            r = H.read_lines(f)
        first = r["entries"][0]
        self.assertEqual(first["type"], "user")
        self.assertIn("ccr_fixture_probe", first["message"]["content"])


class TestTitle(unittest.TestCase):
    def test_tags_removed(self):
        t = H.clean_title_text("<system-reminder>ignore me</system-reminder>\n  你好   世界 ")
        self.assertEqual(t, "你好 世界")
        self.assertEqual(H.clean_title_text("<local-command-caveat>Caveat...</local-command-caveat>"), "")
        self.assertEqual(H.clean_title_text("[Request interrupted by user]"), "")
        self.assertEqual(
            H.clean_title_text("<command-name>/review</command-name>\n<command-message>review</command-message>"
                               "<command-args>abc</command-args>"), "/review abc")

    def test_truncate_80(self):
        t = H.clean_title_text("字" * 200)
        self.assertEqual(len(t), 80)
        self.assertTrue(t.endswith("…"))

    def test_priority(self):
        base = [user("<local-command-caveat>x</local-command-caveat>", isMeta=True),
                user("<command-name>/model</command-name><command-args></command-args>"),
                user("真正的问题"), user("第二个问题")]
        self.assertEqual(H.summarize_lines(jl(*base))["title"], "真正的问题")
        self.assertEqual(H.summarize_lines(jl(*base[:2]))["title"], "/model")
        s = H.summarize_lines(jl(*base, {"type": "summary", "summary": "摘要标题", "leafUuid": "x"}))
        self.assertEqual(s["title"], "摘要标题")
        s = H.summarize_lines(jl(*base, {"type": "ai-title", "aiTitle": "AI 标题", "sessionId": T1},
                                 {"type": "summary", "summary": "摘要标题"}))
        self.assertEqual(s["title"], "AI 标题")
        s = H.summarize_lines(jl({"type": "custom-title", "customTitle": "旧名"}, *base,
                                 {"type": "ai-title", "aiTitle": "AI 标题"},
                                 {"type": "custom-title", "customTitle": "我的命名"}))
        self.assertEqual(s["title"], "我的命名")
        self.assertEqual(s["cwd"], "/home/x/proj")

    def test_sidechain_and_meta_skipped(self):
        lines = jl(user("子代理", isSidechain=True), user("meta", isMeta=True),
                   user([{"type": "tool_result", "tool_use_id": "t", "content": "x"}]),
                   user([{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA"}},
                         {"type": "text", "text": "看图"}]))
        self.assertEqual(H.summarize_lines(lines)["title"], "看图")

    def test_garbage_lines_ignored(self):
        lines = [b"not json\n", b"\n", b'{"type":"user"\n'] + jl(user("ok"))
        self.assertEqual(H.summarize_lines(lines)["title"], "ok")
        self.assertEqual(len(H.read_lines(lines)["entries"]), 1)


class TestTruncate(unittest.TestCase):
    def test_large_fields_truncated(self):
        big = "x" * 50000
        big_cn = "汉" * 20000  # 60KB utf-8
        img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 40000}}
        recs = [
            user(big),
            {"type": "assistant", "uuid": "a1", "timestamp": "t", "isSidechain": False,
             "message": {"id": "msg_1", "role": "assistant", "model": "m", "usage": {"x": 1},
                         "content": [{"type": "text", "text": big_cn}]}},
            {"type": "assistant", "uuid": "a2", "timestamp": "t",
             "message": {"id": "msg_2", "role": "assistant",
                         "content": [{"type": "tool_use", "id": "tu", "name": "Write",
                                      "input": {"file_path": "/a", "content": big}}]}},
            user([{"type": "tool_result", "tool_use_id": "tu", "content": big, "is_error": False}]),
            user([{"type": "tool_result", "tool_use_id": "tu2", "content": [{"type": "text", "text": "hi"}, img]}]),
            user([img, {"type": "text", "text": "small"}]),
            user("small one"),
        ]
        ents = H.read_lines(jl(*recs))["entries"]
        self.assertEqual(len(ents), 7)
        lim = H.MAX_FIELD_BYTES + 64
        self.assertTrue(ents[0]["truncated"])
        self.assertLessEqual(len(ents[0]["message"]["content"].encode()), lim)
        self.assertTrue(ents[1]["truncated"])
        self.assertLessEqual(len(ents[1]["message"]["content"][0]["text"].encode()), lim)
        self.assertNotIn("usage", ents[1]["message"])
        self.assertEqual(ents[1]["message"]["id"], "msg_1")
        self.assertTrue(ents[2]["truncated"])
        self.assertEqual(ents[2]["message"]["content"][0]["input"]["file_path"], "/a")
        self.assertLessEqual(len(ents[2]["message"]["content"][0]["input"]["content"]), lim)
        self.assertTrue(ents[3]["truncated"])
        tr = ents[3]["message"]["content"][0]
        self.assertEqual(tr["tool_use_id"], "tu")
        self.assertLessEqual(len(tr["content"].encode()), lim)
        self.assertTrue(ents[4]["truncated"])
        self.assertIn("hi", ents[4]["message"]["content"][0]["content"])
        self.assertIn("已省略", ents[4]["message"]["content"][0]["content"])
        self.assertTrue(ents[5]["truncated"])
        self.assertEqual(ents[5]["message"]["content"][0]["type"], "text")
        self.assertNotIn("truncated", ents[6])
        self.assertLess(len(json.dumps(ents, ensure_ascii=False).encode()), 8 * lim)


class TestFsFunctions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name) / "projects"
        p1 = self.root / "-home-x-a"
        p2 = self.root / "-home-x-b"
        p1.mkdir(parents=True)
        p2.mkdir()
        self.u_old = "11111111-1111-4111-8111-111111111111"
        self.u_new = "22222222-2222-4222-8222-222222222222"
        (p1 / (self.u_old + ".jsonl")).write_bytes(b"".join(jl(user("旧的"))))
        (p2 / (self.u_new + ".jsonl")).write_bytes(b"".join(jl(user("新的"), {"type": "custom-title",
                                                                             "customTitle": "改名"})))
        (p2 / "not-a-uuid.jsonl").write_bytes(b"".join(jl(user("bad"))))
        (p2 / (self.u_new + ".txt")).write_text("x")
        sub = p2 / self.u_new / "subagents"
        sub.mkdir(parents=True)
        (sub / ("33333333-3333-4333-8333-333333333333.jsonl")).write_bytes(b"".join(jl(user("sub"))))
        now = time.time()
        os.utime(p1 / (self.u_old + ".jsonl"), (now - 100, now - 100))
        os.utime(p2 / (self.u_new + ".jsonl"), (now, now))

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_history(self):
        r = H.list_history(str(self.root), 100)
        ids = [s["claude_session_id"] for s in r["sessions"]]
        self.assertEqual(ids, [self.u_new, self.u_old])
        s0 = r["sessions"][0]
        self.assertEqual(s0["title"], "改名")
        self.assertEqual(s0["project_dir"], "-home-x-b")
        self.assertEqual(s0["cwd"], "/home/x/proj")
        self.assertEqual(set(s0), {"claude_session_id", "cwd", "title", "mtime", "size", "project_dir"})
        self.assertGreater(s0["size"], 0)
        self.assertEqual(len(H.list_history(str(self.root), 1)["sessions"]), 1)
        self.assertEqual(H.list_history(str(self.root / "nope"), 5), {"sessions": []})

    def test_read_history(self):
        r = H.read_history(str(self.root), self.u_old.upper())
        self.assertEqual(r["claude_session_id"], self.u_old)
        self.assertEqual(r["entries"][0]["message"]["content"], "旧的")
        self.assertEqual(H.read_history(str(self.root), "33333333-3333-4333-8333-333333333333")["__error__"],
                         "not_found")
        for bad in ("../../etc/passwd", "*", "11111111-1111-4111-8111-11111111111", ""):
            self.assertEqual(H.read_history(str(self.root), bad)["__error__"], "bad_request")

    def test_list_dirs(self):
        base = pathlib.Path(self.tmp.name) / "d"
        (base / "B").mkdir(parents=True)
        (base / "a").mkdir()
        (base / ".hid").mkdir()
        (base / "file.txt").write_text("x")
        os.symlink(str(base / "a"), str(base / "link"))
        r = H.list_dirs(str(base) + "/./", False)
        self.assertEqual(r["path"], str(base))
        self.assertEqual(r["parent"], str(base.parent))
        self.assertEqual(r["dirs"], ["a", "B", "link"])
        self.assertIn(".hid", H.list_dirs(str(base), True)["dirs"])
        self.assertIsNone(H.list_dirs("/", False)["parent"])
        self.assertEqual(H.list_dirs(str(base / "file.txt"), False)["__error__"], "not_found")
        self.assertEqual(H.list_dirs(str(base / "zz"), False)["__error__"], "not_found")
        self.assertEqual(H.list_dirs("relative/x", False)["__error__"], "bad_request")
        self.assertEqual(H.list_dirs("/a\nb", False)["__error__"], "bad_request")
        home = H.list_dirs("~", False)
        self.assertEqual(home["path"], os.path.expanduser("~"))


class TestRemoteScript(unittest.TestCase):
    """远端脚本本体：python3.8 语法 + 以 `python3 -I -`（stdin 传脚本）本地实跑。"""

    def test_py38_syntax(self):
        ast.parse(H.REMOTE_SCRIPT, feature_version=(3, 8))
        self.assertNotIn("removeprefix", H.REMOTE_SCRIPT)
        self.assertNotIn("match ", H.REMOTE_SCRIPT.replace("re.match", "").replace(".match(", ""))

    def test_script_readonly(self):
        s = H.REMOTE_SCRIPT
        for bad in ("open(path, \"w", "'w'", "\"w\"", "\"a\"", "os.remove", "os.unlink", "shutil", "subprocess",
                    "os.mkdir", "os.makedirs", "rename(", "os.system", "write_text", "write_bytes"):
            self.assertNotIn(bad, s, bad)

    def run_script(self, *args):
        p = subprocess.run([sys.executable, "-I", "-"] + list(args), input=H.REMOTE_SCRIPT.encode(),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, cwd=tempfile.gettempdir())
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_run_list_and_read_on_fixtures(self):
        r = self.run_script("--root", str(FIX), "--flat", "list", "10")
        ids = {s["claude_session_id"] for s in r["sessions"]}
        self.assertEqual(ids, {T1, T2})
        r = self.run_script("--root", str(FIX), "--flat", "read", T1)
        self.assertEqual(r["claude_session_id"], T1)
        with open(fixture_path(T1), "rb") as f:
            local = H.read_lines(f)
        self.assertEqual(r["entries"], json.loads(json.dumps(local["entries"])))

    def test_run_errors(self):
        self.assertEqual(self.run_script("read", "../x")["__error__"], "bad_request")
        self.assertEqual(self.run_script("bogus")["__error__"], "bad_request")
        self.assertEqual(self.run_script("dirs", "/definitely/not/here", "0")["__error__"], "not_found")


class StubRunner:
    def __init__(self, result=None, rc=0, err=""):
        self.calls = []
        self.result = result
        self.rc = rc
        self.err = err

    def __call__(self, cfg, script, args, timeout):
        self.calls.append((cfg, script, list(args), timeout))
        out = "" if self.result is None else json.dumps(self.result, ensure_ascii=False) + "\n"
        return self.rc, out, self.err


class TestHistoryService(unittest.TestCase):
    def cfg(self, **kw):
        c = {"host": "gpu-cluster", "fake": False, "default_cwd": "~", "ssh_multiplex": True}
        c.update(kw)
        return lambda: dict(c)

    def test_list_passes_script_via_runner(self):
        run = StubRunner({"sessions": [{"claude_session_id": T1}]})
        svc = H.HistoryService(self.cfg(), run_python=run)
        self.assertEqual(svc.list_history(5), {"sessions": [{"claude_session_id": T1}]})
        cfg, script, args, _ = run.calls[0]
        self.assertIs(script, H.REMOTE_SCRIPT)
        self.assertEqual(args, ["list", "5"])
        with self.assertRaises(H.HistoryError) as cm:
            svc.list_history(0)
        self.assertEqual(cm.exception.status, 400)
        with self.assertRaises(H.HistoryError):
            svc.list_history("abc")

    def test_read_validates_uuid_before_remote(self):
        run = StubRunner({"claude_session_id": T1, "cwd": "/x", "entries": []})
        svc = H.HistoryService(self.cfg(), run_python=run)
        for bad in ("1;rm -rf ~", "$(id)", T1 + "x", "../" + T1, T1 + "\n"):
            with self.assertRaises(H.HistoryError) as cm:
                svc.read_history(bad)
            self.assertEqual(cm.exception.status, 400)
        self.assertEqual(run.calls, [])
        r = svc.read_history(T1.upper())
        self.assertEqual(run.calls[0][2], ["read", T1])
        self.assertEqual(r, {"claude_session_id": T1, "cwd": "/x", "entries": []})

    def test_remote_error_mapping(self):
        svc = H.HistoryService(self.cfg(), run_python=StubRunner({"__error__": "not_found", "message": "未找到"}))
        with self.assertRaises(H.HistoryError) as cm:
            svc.read_history(T1)
        self.assertEqual((cm.exception.status, cm.exception.message), (404, "未找到"))
        svc = H.HistoryService(self.cfg(), run_python=StubRunner(None, rc=255, err="kex_exchange: Connection reset"))
        with self.assertRaises(H.HistoryError) as cm:
            svc.list_history()
        self.assertEqual(cm.exception.status, 502)
        self.assertIn("ssh", cm.exception.message)
        svc = H.HistoryService(self.cfg(), run_python=StubRunner(None, rc=124, err="超时"))
        with self.assertRaises(H.HistoryError) as cm:
            svc.list_history()
        self.assertEqual(cm.exception.status, 504)

    def test_dirs(self):
        run = StubRunner({"path": "/home/u/p q", "parent": "/home/u", "dirs": ["a"]})
        svc = H.HistoryService(self.cfg(default_cwd="~/work"), run_python=run)
        self.assertEqual(svc.list_dirs("/home/u/p q", True)["dirs"], ["a"])
        self.assertEqual(run.calls[-1][2], ["dirs", "/home/u/p q", "1"])
        svc.list_dirs(None)
        self.assertEqual(run.calls[-1][2], ["dirs", "~/work", "0"])
        for bad in ("relative", "/a\nb", "/a\x00b", "~user/x"):
            with self.assertRaises(H.HistoryError) as cm:
                svc.list_dirs(bad)
            self.assertEqual(cm.exception.status, 400)

    def test_jobs(self):
        calls = []

        def run_cmd(cfg, argv, timeout):
            calls.append(argv)
            return 0, "276403|RUNNING|gpu0001|bash|1:55:06|2-22:04:54\n276500|PENDING||my|job|0:00|1:00:00\n", ""

        svc = H.HistoryService(self.cfg(), run_cmd=run_cmd)
        jobs = svc.list_jobs()["jobs"]
        self.assertEqual(calls[0][:2], ["squeue", "--me"])
        self.assertIn("%i|%T|%N|%j|%M|%L", calls[0])
        self.assertEqual(jobs[0], {"jobid": "276403", "state": "RUNNING", "nodes": "gpu0001", "name": "bash",
                                   "elapsed": "1:55:06", "left": "2-22:04:54"})
        self.assertEqual(jobs[1]["name"], "my|job")
        svc = H.HistoryService(self.cfg(), run_cmd=lambda c, a, t: (1, "", "squeue: error"))
        with self.assertRaises(H.HistoryError) as cm:
            svc.list_jobs()
        self.assertEqual(cm.exception.status, 502)

    def test_parse_squeue_ignores_garbage(self):
        self.assertEqual(H.parse_squeue("\nwarning: blah\nx|y\n"), [])
        self.assertEqual(H.parse_squeue("123_4|RUNNING|n1|a|1:00|2:00")[0]["jobid"], "123_4")

    def test_fake_mode_uses_local_fixtures_and_never_remote(self):
        def boom(*a, **k):
            raise AssertionError("fake 模式不应调用远端")

        svc = H.HistoryService(self.cfg(fake=True), run_python=boom, run_cmd=boom)
        ids = {s["claude_session_id"] for s in svc.list_history(10)["sessions"]}
        self.assertEqual(ids, {T1, T2})
        self.assertTrue(svc.read_history(T2)["entries"])
        self.assertTrue(svc.list_jobs()["jobs"])
        d = svc.list_dirs(str(ROOT))
        self.assertIn("fixtures", d["dirs"])


if __name__ == "__main__":
    unittest.main()
