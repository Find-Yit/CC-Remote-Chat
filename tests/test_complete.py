"""输入框补全的后端（ccr/complete.py，SPEC §7「/ 命令与 @ 文件补全」、§4.3）：只读列出自定义命令 / skills / 插件命令与
cwd 下的文件；不跟随软链接、有上限；settings.json 只取 enabledPlugins；服务器会话经 ssh 跑同一份脚本；HTTP 接口与手机远程口。"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from ccr import complete as CP

ROOT = pathlib.Path(__file__).resolve().parent.parent


def write(p, text):
    p = pathlib.Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


class TestFrontmatter(unittest.TestCase):
    def test_forms(self):
        fm = CP.parse_frontmatter('---\nname: x\ndescription: "带 \\"引号\\" 的说明"\nargument-hint: <PR 编号>\n'
                                  "user-invocable: false\n---\nbody")
        self.assertEqual(fm["name"], "x")
        self.assertEqual(fm["description"], '带 "引号" 的说明')
        self.assertEqual(fm["argument-hint"], "<PR 编号>")
        self.assertEqual(fm["user-invocable"], "false")
        fm = CP.parse_frontmatter("---\ndescription: >\n  第一行\n  第二行\nallowed-tools:\n  - Bash\n  - Read\n"
                                  "when_to_use: |\n  a\n  b\n---\n")
        self.assertEqual(fm["description"], "第一行 第二行")
        self.assertEqual(fm["allowed-tools"], "")
        self.assertEqual(fm["when_to_use"], "a\nb")
        fm = CP.parse_frontmatter("---\ndescription: 普通\n  续行\nname: 'it''s'\n---\n")
        self.assertEqual(fm["description"], "普通 续行")
        self.assertEqual(fm["name"], "it's")
        self.assertEqual(CP.parse_frontmatter("no frontmatter"), {})
        self.assertEqual(CP.parse_frontmatter("---\ndescription: 没有结尾的 --- 也能读\n"), {"description": "没有结尾的 --- 也能读"})


class TestListCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name) / "home"
        c = self.home / ".claude"
        write(c / "commands" / "review-pr.md", "---\ndescription: 审查 PR\nargument-hint: <编号>\n---\n正文")
        write(c / "commands" / "frontend" / "component.md", "没有 frontmatter 时取第一行\n第二行")
        write(c / "commands" / "a" / "b" / "deep.md", "---\ndescription: 两层命名空间\n---\n")
        write(c / "commands" / "hidden.md", "---\ndescription: 隐藏\nuser-invocable: false\n---\n")
        write(c / "commands" / "notes.txt", "不是 .md")
        write(c / "commands" / "pack" / "SKILL.md", "---\ndescription: commands 里的 skill\n---\n")
        write(c / "skills" / "demo" / "SKILL.md", "---\nname: demo\ndescription: >\n  演示\n  skill\n---\n")
        write(c / "skills" / "renamed" / "SKILL.md", "---\nname: 另一个名字\ndescription: 名字不同\n---\n")
        (c / "skills" / "empty").mkdir(parents=True)
        # 软链接：只列名字、不读内容（链接目标里放一个会被读到就能看出来的说明）
        outside = pathlib.Path(self.tmp.name) / "outside"
        write(outside / "SKILL.md", "---\ndescription: 不应该被读到\n---\n")
        write(outside / "cmd.md", "---\ndescription: 不应该被读到\n---\n")
        os.symlink(str(outside), str(c / "skills" / "linked"))
        os.symlink(str(outside / "cmd.md"), str(c / "commands" / "linked-cmd.md"))
        # 项目：cwd 与上级目录（到 home 为止）
        self.proj = self.home / "work" / "proj"
        write(self.proj / ".claude" / "commands" / "deploy.md", "---\ndescription: 部署\n---\n")
        write(self.proj / ".claude" / "skills" / "proj-skill" / "SKILL.md", "---\ndescription: 项目 skill\n---\n")
        write(self.home / "work" / ".claude" / "commands" / "parent.md", "---\ndescription: 上级目录的命令\n---\n")
        write(self.proj / ".claude" / "commands" / "review-pr.md", "---\ndescription: 项目里的同名命令\n---\n")

    def names(self, res):
        return {e["name"]: e for e in res["commands"]}

    def test_user_and_project(self):
        res = CP.list_commands(str(self.home), str(self.proj))
        got = self.names(res)
        self.assertEqual(got["review-pr"]["src"], "project")   # 同名：近的（项目）优先，只留一个
        self.assertEqual(got["review-pr"]["desc"], "项目里的同名命令")
        self.assertEqual(got["deploy"]["src"], "project")
        self.assertEqual(got["parent"]["src"], "project")
        self.assertEqual(got["proj-skill"]["kind"], "skill")
        self.assertEqual(got["frontend:component"]["desc"], "没有 frontmatter 时取第一行")
        self.assertEqual(got["a:b:deep"]["desc"], "两层命名空间")
        self.assertEqual(got["pack"]["kind"], "skill")
        self.assertEqual(got["demo"]["desc"], "演示 skill")
        self.assertEqual(got["renamed"]["display"], "另一个名字")
        self.assertNotIn("hidden", got)          # user-invocable: false
        self.assertNotIn("notes", got)
        self.assertNotIn("empty", got)           # 没有 SKILL.md
        self.assertTrue(got["linked"]["link"])   # 软链接：只有名字
        self.assertEqual(got["linked"]["desc"], "")
        self.assertTrue(got["linked-cmd"]["link"])
        self.assertNotIn("不应该被读到", json.dumps(res, ensure_ascii=False))
        # 近的在前：项目（cwd）→ 项目（上级）→ 用户
        order = [e["name"] for e in res["commands"]]
        self.assertLess(order.index("deploy"), order.index("parent"))
        self.assertLess(order.index("parent"), order.index("frontend:component"))
        self.assertNotIn("truncated", res)

    def test_hint_and_tilde_cwd(self):
        got = self.names(CP.list_commands(str(self.home), "~/work/proj"))
        self.assertIn("deploy", got)
        got = self.names(CP.list_commands(str(self.home), str(self.home)))   # cwd = home：没有项目级
        self.assertEqual(got["review-pr"]["hint"], "<编号>")
        self.assertEqual(got["review-pr"]["src"], "user")

    def test_symlinked_claude_dirs_not_followed(self):
        real = pathlib.Path(self.tmp.name) / "real-claude"
        write(real / "commands" / "x.md", "---\ndescription: x\n---\n")
        h2 = pathlib.Path(self.tmp.name) / "home2"
        h2.mkdir()
        os.symlink(str(real), str(h2 / ".claude"))
        res = CP.list_commands(str(h2), str(h2))
        self.assertEqual(res["commands"], [])
        self.assertTrue(any("软链接" in n for n in res.get("notes", [])))
        # 项目里的 .claude/commands 是软链接：不读
        p = pathlib.Path(self.tmp.name) / "proj2"
        (p / ".claude").mkdir(parents=True)
        os.symlink(str(real / "commands"), str(p / ".claude" / "commands"))
        self.assertNotIn("x", self.names(CP.list_commands(str(self.home), str(p))))

    def test_limits(self):
        c = self.home / ".claude" / "commands" / "many"
        for i in range(30):
            write(c / ("c%02d.md" % i), "---\ndescription: d\n---\n")
        write(self.home / ".claude" / "commands" / "big.md", "---\ndescription: " + "长" * 5000 + "\n---\n")
        with mock.patch.dict(CP._NS, {"MAX_ITEMS": 10}):
            res = CP.list_commands(str(self.home), str(self.home))
        self.assertEqual(len(res["commands"]), 10)
        self.assertTrue(res["truncated"])
        got = self.names(CP.list_commands(str(self.home), str(self.home)))
        self.assertLessEqual(len(got["big"]["desc"]), CP._NS["DESC_MAX"])
        # 太深的命名空间不看
        write(self.home / ".claude" / "commands" / "1" / "2" / "3" / "4" / "5" / "6" / "too-deep.md", "x")
        self.assertFalse(any(n.endswith("too-deep") for n in self.names(CP.list_commands(str(self.home), str(self.home)))))

    def test_plugins(self):
        c = self.home / ".claude"
        root = c / "plugins" / "cache" / "mk" / "hud" / "1.0"
        write(root / ".claude-plugin" / "plugin.json", json.dumps({"name": "hud"}))
        write(root / "commands" / "setup.md", "---\ndescription: 配置\n---\n")
        write(root / "skills" / "tips" / "SKILL.md", "---\ndescription: 插件 skill\n---\n")
        off = c / "plugins" / "cache" / "mk" / "off" / "1.0"
        write(off / "commands" / "nope.md", "---\ndescription: 没启用\n---\n")
        evil = pathlib.Path(self.tmp.name) / "evil"
        write(evil / "commands" / "evil.md", "---\ndescription: 安装位置在 plugins 之外\n---\n")
        write(c / "plugins" / "installed_plugins.json", json.dumps({"version": 2, "plugins": {
            "hud@mk": [{"scope": "user", "installPath": str(root)}],
            "off@mk": [{"scope": "user", "installPath": str(off)}],
            "evil@mk": [{"scope": "user", "installPath": str(evil)}],
            "dots@mk": [{"scope": "user", "installPath": str(c / "plugins" / ".." / ".." / "x")}],
        }}))
        write(c / "settings.json", json.dumps({"enabledPlugins": {"hud@mk": True, "off@mk": False, "evil@mk": True,
                                                                  "dots@mk": True},
                                               "env": {"ANTHROPIC_AUTH_TOKEN": "sk-secret-value"}}))
        res = CP.list_commands(str(self.home), str(self.home))
        got = self.names(res)
        self.assertEqual(got["hud:setup"]["src"], "plugin")
        self.assertEqual(got["hud:setup"]["plugin"], "hud")
        self.assertEqual(got["hud:tips"]["kind"], "skill")
        self.assertNotIn("off:nope", got)
        self.assertFalse(any("evil" in n for n in got))
        self.assertNotIn("sk-secret-value", json.dumps(res))   # settings.json 只取 enabledPlugins
        # 项目设置可以关掉用户启用的插件（近的覆盖）
        write(self.proj / ".claude" / "settings.local.json", json.dumps({"enabledPlugins": {"hud@mk": False}}))
        self.assertNotIn("hud:setup", self.names(CP.list_commands(str(self.home), str(self.proj))))
        # 安装目录里有软链接：不读
        os.rename(str(root), str(root) + "-real")
        os.symlink(str(root) + "-real", str(root))
        self.assertNotIn("hud:setup", self.names(CP.list_commands(str(self.home), str(self.home))))

    def test_read_only_source(self):
        """脚本里没有任何写文件的调用（只读）"""
        src = CP.SCRIPT
        for bad in ("O_WRONLY", "O_RDWR", "O_CREAT", "open(", "mkdir", "makedirs", "remove(", "unlink", "rename",
                    "rmtree", "chmod", "os.symlink(", "subprocess", "system("):
            self.assertFalse(bad in src.replace("os.open(path, flags)", ""), bad)
        self.assertIn("O_NOFOLLOW", src)


class TestListFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name) / "proj"
        for f in ("README.md", "src/app.py", "src/lib/util.py", "docs/my notes.md", ".env", ".github/workflows/ci.yml",
                  "node_modules/x/index.js", ".git/config", "__pycache__/a.pyc", ".DS_Store"):
            write(self.root / f, "x")
        outside = pathlib.Path(self.tmp.name) / "outside"
        write(outside / "secret.txt", "x")
        os.symlink(str(outside), str(self.root / "linkdir"))

    def test_bfs_ignore_symlink(self):
        res = CP.list_files(str(self.root), str(self.tmp.name))
        e = res["entries"]
        self.assertEqual(os.path.realpath(res["root"]), os.path.realpath(str(self.root)))
        self.assertIn("src/", e)
        self.assertIn("src/lib/util.py", e)
        self.assertIn("docs/my notes.md", e)
        self.assertIn(".github/workflows/ci.yml", e)
        self.assertIn(".env", e)
        self.assertIn("linkdir", e)                 # 软链接本身列出（不带 /），不进去
        self.assertFalse(any(x.startswith("linkdir/") for x in e))
        for bad in ("node_modules/", ".git/", "__pycache__/", ".DS_Store"):
            self.assertFalse(any(x.startswith(bad) for x in e), bad)
        # 广度优先：浅的在前
        self.assertLess(e.index("README.md"), e.index("src/app.py"))
        self.assertLess(e.index("src/app.py"), e.index("src/lib/util.py"))
        self.assertNotIn("truncated", res)

    def test_limits_and_errors(self):
        with mock.patch.dict(CP._NS, {"FILE_MAX": 3}):
            res = CP.list_files(str(self.root), str(self.tmp.name))
        self.assertEqual(len(res["entries"]), 3)
        self.assertTrue(res["truncated"])
        with mock.patch.dict(CP._NS, {"FILE_DEPTH": 1}):
            res = CP.list_files(str(self.root), str(self.tmp.name))
        self.assertFalse(any("/" in x.rstrip("/") for x in res["entries"]))
        self.assertTrue(res["truncated"])
        self.assertEqual(CP.list_files(str(self.root / "nope"), "/")["__error__"], "not_found")
        self.assertEqual(CP.list_files("relative", "/")["__error__"], "bad_request")
        self.assertEqual(CP.list_files("", "/")["__error__"], "bad_request")
        # ~ 按给定的 home 展开
        res = CP.list_files("~/proj", str(self.tmp.name))
        self.assertIn("README.md", res["entries"])


class TestService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = pathlib.Path(self.tmp.name)
        write(self.d / "proj" / "a.txt", "x")

    def test_local_runs_in_process_and_server_over_ssh(self):
        calls = []

        def run_python(cfg, script, args, timeout):
            calls.append(args)
            # 模拟远端：python3 -I - 跑同一份脚本
            p = subprocess.run([sys.executable, "-I", "-"] + args, input=script.encode(), capture_output=True,
                               timeout=30)
            return p.returncode, p.stdout.decode(), p.stderr.decode()

        svc = CP.CompleteService(lambda: {"host": "h"}, run_python=run_python)
        with mock.patch.dict(os.environ, {"HOME": str(self.d)}):
            r = svc.get({"jobid": "local", "cwd": str(self.d / "proj")}, "files")
        self.assertEqual(r["target"], "local")
        self.assertEqual(r["entries"], ["a.txt"])
        self.assertEqual(calls, [])                      # 本机：本进程里直接读
        r = svc.get({"jobid": "123", "cwd": str(self.d / "proj")}, "files")
        self.assertEqual(r["target"], "server")
        self.assertEqual(calls, [["files", "~", str(self.d / "proj")]])
        r = svc.get({"jobid": "123", "cwd": "~"}, "commands")
        self.assertIn("commands", r)
        with self.assertRaises(CP.CompleteError) as cm:
            svc.get({"jobid": "123", "cwd": str(self.d / "nope")}, "files")
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(CP.CompleteError) as cm:
            svc.get({"jobid": "local", "cwd": ""}, "files")
        self.assertEqual(cm.exception.status, 409)
        with self.assertRaises(CP.CompleteError) as cm:
            svc.get({"jobid": "local", "cwd": "/"}, "nope")
        self.assertEqual(cm.exception.status, 404)

    def test_ssh_failures(self):
        svc = CP.CompleteService(lambda: {}, run_python=lambda *a: (255, "", "ssh: connect to host h port 22: refused"))
        with self.assertRaises(CP.CompleteError) as cm:
            svc.get({"jobid": "1", "cwd": "/x"}, "files")
        self.assertEqual(cm.exception.status, 502)
        self.assertIn("ssh", cm.exception.message)

        def timeout(*a):
            raise subprocess.TimeoutExpired("ssh", 40)
        svc = CP.CompleteService(lambda: {}, run_python=timeout)
        with self.assertRaises(CP.CompleteError) as cm:
            svc.get({"jobid": "1", "cwd": "/x"}, "files")
        self.assertEqual(cm.exception.status, 504)

    def test_fake_uses_fake_homes(self):
        cfg = {"fake": True, "fake_home": str(self.d / "fh"), "fake_local_home": str(self.d / "flh")}
        write(self.d / "fh" / ".claude" / "commands" / "srv.md", "---\ndescription: 服务器\n---\n")
        write(self.d / "flh" / ".claude" / "commands" / "loc.md", "---\ndescription: 本机\n---\n")
        svc = CP.CompleteService(lambda: cfg, run_python=lambda *a: self.fail("fake 不走 ssh"))
        srv = [e["name"] for e in svc.get({"jobid": "100001", "cwd": "/"}, "commands")["commands"]]
        loc = [e["name"] for e in svc.get({"jobid": "local", "cwd": "/"}, "commands")["commands"]]
        self.assertEqual((srv, loc), (["srv"], ["loc"]))

    def test_script_runs_standalone(self):
        """远端执行方式：python3 -I - <命令>，输出一行 JSON"""
        p = subprocess.run([sys.executable, "-I", "-", "files", str(self.d), str(self.d / "proj")],
                           input=CP.SCRIPT.encode(), capture_output=True, timeout=30)
        self.assertEqual(json.loads(p.stdout.decode().strip())["entries"], ["a.txt"])
        p = subprocess.run([sys.executable, "-I", "-", "bogus"], input=CP.SCRIPT.encode(), capture_output=True, timeout=30)
        self.assertEqual(json.loads(p.stdout.decode().strip())["__error__"], "bad_request")


class TestHttp(unittest.TestCase):
    """GET /api/sessions/<sid>/complete/{commands,files}：沿用 token 鉴权（手机远程口见 test_remote_access）。"""

    def setUp(self):
        from tests import test_server as TS
        self.TS = TS
        self.base = TS.Base("run")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        write(pathlib.Path(self.tmp.name) / "f.txt", "x")
        self.base.mgr.metas["s000000000001"] = {"sid": "s000000000001", "jobid": "local", "cwd": self.tmp.name}

    def test_routes(self):
        b = self.base
        st, js, _, _ = b.req("GET", "/api/sessions/s000000000001/complete/files")
        self.assertEqual(st, 200, js)
        self.assertEqual(js["entries"], ["f.txt"])
        st, js, _, _ = b.req("GET", "/api/sessions/s000000000001/complete/commands")
        self.assertEqual(st, 200, js)
        self.assertIsInstance(js["commands"], list)
        b.assertErr(b.req("GET", "/api/sessions/s000000000001/complete/other"), 404)
        b.assertErr(b.req("GET", "/api/sessions/s000000000999/complete/files"), 404)
        st, _, _, _ = b.req("GET", "/api/sessions/s000000000001/complete/files", auth=False)
        self.assertEqual(st, 401)
        b.mgr.metas["s000000000002"] = {"sid": "s000000000002", "jobid": "local", "cwd": self.tmp.name + "/nope"}
        b.assertErr(b.req("GET", "/api/sessions/s000000000002/complete/files"), 404)



class TestPopupCli2_1_332(unittest.TestCase):
    """真机 2.1.332 的 / 补全列表（fixtures/cli_2_1_332/slash_popup_*.txt，已脱敏）：skill 的长说明会折到第二行，
    折行也属于列表——tui_parse 的 popup 与 agent guard 的 popup 都要认出高亮项（以前遇到折行就停，高亮项会被当成没有）。"""

    def frame(self, name):
        return (ROOT / "fixtures" / "cli_2_1_332" / (name + ".txt")).read_text(encoding="utf-8")

    def test_wrapped_description(self):
        from ccr import node_agent, tui_parse
        t = self.frame("slash_popup_wrapped")
        st = tui_parse.parse_screen(t)
        self.assertEqual([p["cmd"] for p in st["popup"]], ["/init", "/deep-research", "/long-skill", "/demo-plugin:setup"])
        self.assertEqual([p["cmd"] for p in st["popup"] if p["selected"]], ["/long-skill"])
        self.assertTrue(st["popup"][2]["desc"].endswith("description column)…"))   # 续行并进说明
        self.assertEqual(st["input"], {"visible": True, "text": "/"})
        self.assertFalse(st["live"])
        lines = node_agent.screen_lines(t)
        self.assertEqual(node_agent.popup_selected(lines, node_agent.find_input_box(lines)[0]), "/long-skill")
        self.assertIsNone(node_agent.check_guard(t, {"input_box": True, "popup": "/long-skill"}))
        self.assertIn("/long-skill", node_agent.check_guard(t, {"input_box": True, "popup": "/init"}))
        self.assertIn("/long-skill", node_agent.check_guard(t, {"input_box": True, "popup": None}))
        # 高亮项正好是最后一项、说明折行在最底下
        cut = t.replace(next(l for l in t.split("\n") if "/demo-plugin:setup" in l) + "\n", "")
        st = tui_parse.parse_screen(cut)
        self.assertEqual([p["cmd"] for p in st["popup"] if p["selected"]], ["/long-skill"])
        lines = node_agent.screen_lines(cut)
        self.assertEqual(node_agent.popup_selected(lines, node_agent.find_input_box(lines)[0]), "/long-skill")

    def test_builtin_page_and_tasks_panel(self):
        from ccr import node_agent, tui_parse
        st = tui_parse.parse_screen(self.frame("slash_popup_builtin"))
        self.assertEqual([p["cmd"] for p in st["popup"]], ["/clear", "/color", "/compact", "/config", "/context"])
        self.assertEqual([p["cmd"] for p in st["popup"] if p["selected"]], ["/compact"])
        self.assertIsNone(node_agent.check_guard(self.frame("slash_popup_builtin"), {"input_box": True, "popup": "/compact"}))
        # CLI 自己的任务清单面板（spinner 下面的 ◻ 行、模式行 "ctrl+t to hide tasks"）：不是补全列表也不是对话框
        st = tui_parse.parse_screen(self.frame("tasks_panel_busy"))
        self.assertTrue(st["busy"])
        self.assertIsNone(st["popup"])
        self.assertIsNone(st["dialog"])
        self.assertEqual(st["input"], {"visible": True, "text": ""})
        self.assertIn("◻ 心算 3+3", st["live"])

    def test_old_frames_unchanged(self):
        """其它真机画面的 popup 结论不变（折行规则只在最上面是命令行时才生效）"""
        from ccr import node_agent, tui_parse
        for p in sorted((ROOT / "fixtures" / "cli_2_1_332").glob("*.txt")):
            if p.name.startswith("slash_popup"):
                continue
            t = p.read_text(encoding="utf-8")
            self.assertIsNone(tui_parse.parse_screen(t)["popup"], p.name)
            lines = node_agent.screen_lines(t)
            box = node_agent.find_input_box(lines)
            if box:
                self.assertIsNone(node_agent.popup_selected(lines, box[0]), p.name)


if __name__ == "__main__":
    unittest.main()
