"""本机会话发图片（SPEC §4.2「图片」、§9.8）：⌘V = claude 自己的 Ctrl+V、原生窗口拖入 = 粘贴图片路径。

1. ccr.attach：魔数、终端拖放式转义、路径校验（不另存文件，只读开头 16 字节）。
2. tui_parse：CLI 2.1.332 真机画面（fixtures/image_paste/）里输入框的 [Image #N]、回显下的 "⎿  [Image #N]"、排队。
3. node_agent：C-v 白名单、guard.input_images_only、paste 的 guard（与 tui_parse 的输入框判定一致）。
4. fake_tui：粘贴图片路径 / Ctrl+V → [Image #N]，提交后 transcript 里是 image 块（仿真机）。
5. server：路由、参数校验、手机远程口禁止。
6. fake 端到端（本机）：paste_image / attach_paths / 只有图片 / 文字 + 图片 / 排队 / clear_images / 各种 409。
7. 删除单张图片（图片条上的 ×）：tui_parse 的光标定位对 CLI 2.1.332 真机实测（fixtures/image_paste/caret_probe.json）、
   node_agent 的 cursor guard 与 tui_parse 逐格一致、fake 的光标与真机同步、拖入图片的只读缩略图通道、端到端。
红线：不读写系统剪贴板（fake 的 Ctrl+V 生成示意图片）、不运行真实 claude、不碰真实的 /tmp/tmux-<uid>/ccr。
"""
import base64
import io
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from ccr import attach, fake_tui, node_agent, remote, session as S, tui_parse
from ccr.session import OpError
from tests.test_node_agent import HAVE_TMUX, SID, TMUX
from tests.test_session import wait_until
from tests.test_server import Base as ServerBase, StubManager

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIX = ROOT / "fixtures" / "image_paste"
REAL_SOCKET_DIR = "/tmp/tmux-%d" % os.getuid()
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
JPG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = base64.b64decode("UklGRhoAAABXRUJQVlA4TA0AAAAvAAAAEAcQERGIiP4HAA==")


def screen(name):
    return (FIX / (name + ".txt")).read_text(encoding="utf-8")


CARET = json.loads((FIX / "caret_probe.json").read_text(encoding="utf-8"))


def caret_frame(name):
    return next(f for f in CARET["frames"] if f["name"] == name)


def caret_screen(fr, rows=None):
    """真机实测的输入框几行 → 50 行屏幕（行号 = 终端行号，与 tmux #{cursor_y} 对齐）。"""
    lines = [""] * fr["top"] + list(rows or fr["rows"])
    return "\n".join(lines + [""] * (50 - len(lines)))


# ============================================================================ 1. ccr.attach
class TestAttachPure(unittest.TestCase):
    def test_image_kind(self):
        self.assertEqual(attach.image_kind(PNG[:16]), "png")
        self.assertEqual(attach.image_kind(JPG[:16]), "jpeg")
        self.assertEqual(attach.image_kind(GIF[:16]), "gif")
        self.assertEqual(attach.image_kind(WEBP[:16]), "webp")
        for bad in (b"", b"hello world", b"RIFF\x00\x00\x00\x00WAVE", b"\x89PNG", b"<svg"):
            self.assertIsNone(attach.image_kind(bad), bad)

    def test_escape_drop_path(self):
        cases = {
            "/tmp/a.png": "/tmp/a.png",
            "/tmp/my shot.png": "/tmp/my\\ shot.png",
            "/tmp/a (1)&b.png": "/tmp/a\\ \\(1\\)\\&b.png",
            "/x/截屏2026-11-25 21.00.00.png": "/x/截屏2026-11-25\\ 21.00.00.png",   # 非 ASCII 不转义（真机实测可识别）
            "/x/it's \"q\".png": "/x/it\\'s\\ \\\"q\\\".png",
            "/x/a$b`c\\d.png": "/x/a\\$b\\`c\\\\d.png",
            "/x/~u/a+b,c:d@e%f=g-h_i.png": "/x/\\~u/a+b,c:d@e%f=g-h_i.png",
        }
        for raw, want in cases.items():
            self.assertEqual(attach.escape_drop_path(raw), want, raw)
        self.assertEqual(attach.escape_drop_path("/a/ x.png"), "/a/ x.png")   # macOS 截图名里的窄空格

    def test_check_image_path(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            ok = {}
            for name, data in (("a.png", PNG), ("b.JPG", JPG), ("c.gif", GIF), ("d.webp", WEBP), ("my shot.PNG", PNG)):
                p = os.path.join(td, name)
                pathlib.Path(p).write_bytes(data)
                ok[name] = p
                self.assertEqual(attach.check_image_path(p), p)
            link = os.path.join(td, "link.png")
            os.symlink(ok["a.png"], link)
            self.assertEqual(attach.check_image_path(link), link)   # 软链接到图片：跟随（与终端拖放一致）
            pathlib.Path(td, "fake.png").write_bytes(b"not an image at all")
            pathlib.Path(td, "empty.png").write_bytes(b"")
            pathlib.Path(td, "note.txt").write_bytes(PNG)
            os.mkdir(os.path.join(td, "dir.png"))
            fifo = os.path.join(td, "pipe.png")
            os.mkfifo(fifo)   # 不能因为 open 一个 FIFO 卡住
            bad = [None, 5, "", "rel/a.png", "~/a.png", os.path.join(td, "missing.png"),
                   os.path.join(td, "fake.png"), os.path.join(td, "empty.png"), os.path.join(td, "note.txt"),
                   os.path.join(td, "dir.png"), fifo, ok["a.png"] + "\n/etc/passwd", "/tmp/a\x00.png",
                   "/" + "a" * 5000 + ".png"]
            for b in bad:
                with self.assertRaises(ValueError, msg=repr(b)):
                    attach.check_image_path(b)
            with mock.patch.object(attach, "MAX_IMAGE_BYTES", 10):
                with self.assertRaises(ValueError):
                    attach.check_image_path(ok["a.png"])

    def test_no_writes_in_attach_module(self):
        """attach.py 只读：没有任何写文件 / 剪贴板相关调用。"""
        src = (ROOT / "ccr" / "attach.py").read_text(encoding="utf-8")
        for bad in ("O_CREAT", "O_WRONLY", "O_RDWR", "open(", "pbcopy", "pbpaste", "osascript", "NSPasteboard", "mkstemp"):
            self.assertNotIn(bad, src.replace("os.open(", ""), bad)


# ============================================================================ 2. tui_parse（真机画面）
class TestReadImageFile(unittest.TestCase):
    """拖入图片的缩略图通道：只读校验时的那个文件（同一个 dev/inode），再核对魔数。"""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ccr-rf-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.p = os.path.join(self.tmp, "a b.png")
        pathlib.Path(self.p).write_bytes(PNG)

    def test_reads_same_file_only(self):
        path, ident = attach.check_image_file(self.p)
        self.assertEqual(path, self.p)
        self.assertEqual(attach.read_image_file(self.p, ident), ("image/png", PNG))
        with self.assertRaises(attach.ImageFileError) as cm:
            attach.read_image_file(self.p, ident, max_bytes=10)
        self.assertEqual(cm.exception.status, 413)
        # 原地改成非图片（同一个 inode）→ 415
        with open(self.p, "r+b") as f:
            f.write(b"hello world, not an image")
        with self.assertRaises(attach.ImageFileError) as cm:
            attach.read_image_file(self.p, ident)
        self.assertEqual(cm.exception.status, 415)
        # 换成另一个文件（inode 不同，即使也是图片）→ 404
        other = os.path.join(self.tmp, "o.png")
        pathlib.Path(other).write_bytes(PNG)
        os.replace(other, self.p)
        with self.assertRaises(attach.ImageFileError) as cm:
            attach.read_image_file(self.p, ident)
        self.assertEqual(cm.exception.status, 404)
        # 换成指向别的文件的软链接 → 404
        secret = os.path.join(self.tmp, "secret.png")
        pathlib.Path(secret).write_bytes(GIF)
        os.unlink(self.p)
        os.symlink(secret, self.p)
        with self.assertRaises(attach.ImageFileError) as cm:
            attach.read_image_file(self.p, ident)
        self.assertEqual(cm.exception.status, 404)
        os.unlink(self.p)
        with self.assertRaises(attach.ImageFileError) as cm:
            attach.read_image_file(self.p, ident)
        self.assertEqual(cm.exception.status, 404)
        # FIFO：校验就不通过（不会阻塞）
        fifo = os.path.join(self.tmp, "f.png")
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            attach.check_image_file(fifo)


class TestParseImages(unittest.TestCase):
    def test_split_input_images(self):
        f = tui_parse.split_input_images
        self.assertEqual(f(""), ([], ""))
        self.assertEqual(f(None), ([], ""))
        self.assertEqual(f("hello"), ([], "hello"))
        self.assertEqual(f("[Image #1]"), ([1], ""))
        self.assertEqual(f("[Image #21] [Image #22] 这两张图？"), ([21, 22], "这两张图？"))
        self.assertEqual(f("比较两张图：[Image #2] [Image #3]"), ([2, 3], "比较两张图："))
        self.assertEqual(f("[Image #1] /tmp/x/note.txt"), ([1], "/tmp/x/note.txt"))
        self.assertEqual(f("[Image #x] [image #2]"), ([], "[Image #x] [image #2]"))

    def test_fixture_inputs(self):
        st = tui_parse.parse_screen(screen("input_image_only"))
        self.assertEqual(st["input"], {"visible": True, "text": "[Image #21]", "images": [21]})
        self.assertIsNone(st["dialog"])
        st = tui_parse.parse_screen(screen("input_images_text"))
        self.assertEqual(st["input"]["images"], [21, 22])
        self.assertEqual(st["input"]["text"], "[Image #21] [Image #22] 这两张图各写了什么单词？")
        st = tui_parse.parse_screen(screen("input_image_multiline"))
        self.assertEqual(st["input"]["images"], [19])
        self.assertEqual(st["input"]["text"], "[Image #19] 第一行\n第二行 这张图写的什么？\n第三行")
        # 忙时（回复还在往屏幕上刷）输入框里也可以有图片
        st = tui_parse.parse_screen(screen("busy_input_image_text"))
        self.assertEqual(st["input"]["images"], [23])

    def test_echo_image_lines_not_live(self):
        st = tui_parse.parse_screen(screen("done_after_image"))
        self.assertEqual(st["echo"], "[Image #20]")
        self.assertEqual(st["live"], '⏺ 这张图上写的是 "OK"。')   # "  ⎿  [Image #20]" 属于回显
        self.assertNotIn("images", st["input"])
        # 旧画面（没有图片）的解析不变：input 里没有 images 键
        for p in sorted((ROOT / "fixtures" / "cli_2_1_332").glob("*.txt")):
            self.assertNotIn("images", tui_parse.parse_screen(p.read_text(encoding="utf-8"))["input"], p.name)

    def test_queued_image_message(self):
        st = tui_parse.parse_screen(screen("queued_image"))
        self.assertEqual(st["hints"]["queued"], ["[Image #18] 这张图写的什么？"])
        self.assertEqual(st["input"]["text"], "")
        self.assertTrue(st["busy"])


# ============================================================================ 3. node_agent
class TestAgentGuard(unittest.TestCase):
    def test_c_v_key_and_guard_key(self):
        self.assertIn("C-v", node_agent.KEYS)
        node_agent.validate_guard({"input_box": True, "input_images_only": True})
        for bad in ("1", 1, None, []):
            with self.assertRaises(node_agent.OpError):
                node_agent.validate_guard({"input_images_only": bad})

    def test_images_only_guard_on_real_screens(self):
        g = {"input_box": True, "input_images_only": True}
        self.assertIsNone(node_agent.check_guard(screen("input_image_only"), g))
        self.assertEqual(node_agent.check_guard(screen("input_images_text"), g), "输入框里不只有图片")
        self.assertEqual(node_agent.check_guard(screen("input_image_multiline"), g), "输入框里不只有图片")
        self.assertEqual(node_agent.check_guard(screen("done_after_image"), g), "输入框里不只有图片")
        two = screen("input_images_text").replace(" 这两张图各写了什么单词？", "")
        self.assertIsNone(node_agent.check_guard(two, g))
        self.assertIsNone(node_agent.check_guard(screen("input_images_text"), {"input_box": True, "input_nonempty": True}))

    def test_input_box_matches_tui_parse(self):
        for p in sorted(FIX.glob("*.txt")):
            text = p.read_text(encoding="utf-8")
            lines = node_agent.screen_lines(text)
            self.assertEqual(node_agent.find_input_box(lines), tui_parse._find_input_box(tui_parse.screen_lines(text)),
                             p.name)

    def test_paste_guard(self):
        calls = []
        scr = {"text": screen("input_image_only")}

        def fake_tmux(args, input_bytes=None, timeout=15):
            calls.append(args[0])
            if args[0] == "capture-pane":
                return 0, scr["text"], ""
            return 0, "", ""

        with mock.patch.object(node_agent, "tmux", side_effect=fake_tmux):
            node_agent.op_paste({"sid": SID, "text": "/tmp/a.png", "guard": {"input_box": True}})
            self.assertEqual(calls, ["capture-pane", "load-buffer", "paste-buffer"])
            calls.clear()
            # 对话框弹出来了：一个字都不粘
            scr["text"] = "\n".join(["❯ hi", "", "─" * 80, " Bash command", "", "   ls", "",
                                     " Do you want to proceed?", " ❯ 1. Yes", "   2. No", "", " Esc to cancel"])
            with self.assertRaises(node_agent.OpError) as cm:
                node_agent.op_paste({"sid": SID, "text": "/tmp/a.png", "guard": {"input_box": True}})
            self.assertEqual(cm.exception.code, "guard")
            self.assertEqual(calls, ["capture-pane"])
            calls.clear()
            # 不带 guard：与以前完全相同（不抓屏）
            node_agent.op_paste({"sid": SID, "text": "x"})
            self.assertEqual(calls, ["load-buffer", "paste-buffer"])
            with self.assertRaises(node_agent.OpError):
                node_agent.op_paste({"sid": SID, "text": "x", "guard": {"bogus": 1}})


# ============================================================================ 3b. 光标定位（删除单张图片，SPEC §4.2「图片」）
class TestCaretParse(unittest.TestCase):
    def lines(self, name, rows=None):
        return tui_parse.screen_lines(caret_screen(caret_frame(name), rows))

    def test_input_caret_on_real_frames(self):
        L = self.lines("three_end")
        c = tui_parse.input_caret(L, 34, 46)
        self.assertEqual((c["box"], c["row"], c["rows"], c["col"], c["before"], c["after"]),
                         ((45, 47), 0, 1, 32, "[Image #1] [Image #2] [Image #3]", ""))
        c = tui_parse.input_caret(L, 23, 46)
        self.assertEqual((c["before"], c["after"]), ("[Image #1] [Image #2]", " [Image #3]"))
        c = tui_parse.input_caret(L, 2, 46)
        self.assertEqual((c["before"], c["after"]), ("", "[Image #1] [Image #2] [Image #3]"))
        for cx, cy in ((34, 45), (34, 47), (1, 46), (0, 46), (-1, 46), (True, 46), (None, 46), (34, 48)):
            self.assertIsNone(tui_parse.input_caret(L, cx, cy), (cx, cy))
        # 行尾 rstrip 掉的空格：光标在文字之后 → 那是真的空格
        L2 = self.lines("three_end", ["", "─" * 200, "❯ [Image #1] [Image #2]", "─" * 200, "  status", "  mode"])
        self.assertEqual(tui_parse.input_caret(L2, 24, 46)["before"], "[Image #1] [Image #2] ")
        # 多行 + 中文（两列宽）：光标落在宽字符中间 → None
        L3 = self.lines("multiline_end")
        c = tui_parse.input_caret(L3, 31, 45)
        self.assertEqual((c["row"], c["rows"], c["before"], c["after"]), (0, 2, "[Image #4] [Image #3] 中文abc", ""))
        c = tui_parse.input_caret(L3, 26, 45)
        self.assertEqual((c["before"], c["after"]), ("[Image #4] [Image #3] 中", "文abc"))
        self.assertIsNone(tui_parse.input_caret(L3, 25, 45))
        c = tui_parse.input_caret(L3, 13, 46)
        self.assertEqual((c["row"], c["before"], c["after"]), (1, "second line", ""))

    def test_image_cells_and_end(self):
        L = self.lines("three_end")
        self.assertEqual(tui_parse.input_image_cells(L, 1), [(12, 46)])
        self.assertEqual(tui_parse.input_image_cells(L, 2), [(23, 46)])
        self.assertEqual(tui_parse.input_image_cells(L, 3), [(34, 46)])
        self.assertEqual(tui_parse.input_image_cells(L, 9), [])
        self.assertEqual(tui_parse.input_end_cell(L), (34, 46))
        L3 = self.lines("multiline_end")
        self.assertEqual(tui_parse.input_image_cells(L3, 3), [(23, 45)])
        self.assertEqual(tui_parse.input_end_cell(L3), (13, 46))
        dlg = tui_parse.screen_lines("\n".join(["❯ hi", "", "─" * 80, " Bash command", "", "   ls", "",
                                                 " Do you want to proceed?", " ❯ 1. Yes", "   2. No", "", " Esc to cancel"]))
        self.assertEqual(tui_parse.input_image_cells(dlg, 1), [])
        self.assertIsNone(tui_parse.input_end_cell(dlg))

    def test_moves_match_real_cli(self):
        """caret_moves 预测的 ← / → 步数 = 真机实测光标真正走到那里所需的步数（fixtures/image_paste/caret_probe.json）。"""
        f = caret_frame("three_end")
        L = self.lines("three_end")
        for i, pos in enumerate(f["left"][:5]):
            self.assertEqual(tui_parse.caret_moves(L, tuple(f["cursor"]), tuple(pos)), ["Left"] * (i + 1), pos)
        f = caret_frame("three_home")
        for i, pos in enumerate(f["right"][:5]):
            self.assertEqual(tui_parse.caret_moves(L, tuple(f["cursor"]), tuple(pos)), ["Right"] * (i + 1), pos)
        self.assertEqual(tui_parse.caret_moves(L, (34, 46), (34, 46)), [])
        self.assertEqual(tui_parse.caret_moves(L, (34, 46), (2, 46), limit=3), ["Left"] * 3)
        # 删除 [Image #2]：从末尾走到它后面 = 两次 ←（真机：34 → 24 → 23）
        self.assertEqual(tui_parse.caret_moves(L, (34, 46), tui_parse.input_image_cells(L, 2)[0]), ["Left", "Left"])
        # 多行：先 ← 到本行行首再多一步跨到上一行行尾（真机：Home 后 ← = (31,45)），再按列走（真机 6 步到 23）
        f = caret_frame("multiline_end")
        L3 = self.lines("multiline_end")
        tgt = tui_parse.input_image_cells(L3, 3)[0]
        self.assertEqual(tgt, tuple(f["left_more"][-1]))
        self.assertEqual(tui_parse.caret_moves(L3, (13, 46), tgt), ["Left"] * 12)
        self.assertEqual(tuple(f["home_left"]), (31, 45))
        self.assertEqual(tui_parse.caret_moves(L3, (31, 45), tgt), ["Left"] * len(f["left_more"]))
        # → 从不越过目标：从上一行走到下一行末尾，先走到本行行尾 + 1 步
        self.assertEqual(tui_parse.caret_moves(L3, (23, 45), (13, 46)), ["Right"] * 7)
        self.assertEqual(tui_parse.caret_moves(L3, (2, 46), (13, 46)), ["Right"] * 11)

    def test_agent_caret_same_as_tui_parse(self):
        """node_agent.caret_before 与 tui_parse.input_caret 逐格一致（agent 就地复核用同一规则）。"""
        texts = [caret_screen(f) for f in CARET["frames"]]
        texts += [screen(n) for n in ("input_image_only", "input_images_text", "input_image_multiline")]
        for text in texts:
            L1, L2 = node_agent.screen_lines(text), tui_parse.screen_lines(text)
            self.assertEqual(L1, L2)
            box = node_agent.find_input_box(L1)
            self.assertEqual(box, tui_parse._find_input_box(L2))
            for cy in range(len(L1)):
                for cx in range(0, 70):
                    c = tui_parse.input_caret(L2, cx, cy)
                    self.assertEqual(node_agent.caret_before(L1, box, (cx, cy)), c and c["before"], (cx, cy))


class TestAgentCursorGuard(unittest.TestCase):
    def test_validate_and_keys(self):
        for k in ("Left", "Right", "BSpace"):
            self.assertIn(k, node_agent.KEYS)
        for k in ("End", "Home", "C-a", "C-e", "DC"):   # 只用已有的 ← / → / 退格，白名单没有多加键
            self.assertNotIn(k, node_agent.KEYS)
        node_agent.validate_guard({"input_box": True, "input_images": [1, 3], "cursor_image": 1})
        node_agent.validate_guard({"input_box": True, "input_images": [], "cursor_space": True})
        for bad in ({"input_images": [1]}, {"cursor_image": 1}, {"cursor_space": True},
                    {"input_box": True, "input_images": "1"}, {"input_box": True, "input_images": [True]},
                    {"input_box": True, "input_images": [-1]}, {"input_box": True, "input_images": list(range(51))},
                    {"input_box": True, "cursor_image": True}, {"input_box": True, "cursor_image": "1"},
                    {"input_box": True, "cursor_space": 1}, {"input_box": True, "cursor_xy": [1, 2]}):
            with self.assertRaises(node_agent.OpError, msg=repr(bad)):
                node_agent.validate_guard(bad)

    def test_check_guard_cursor(self):
        text = caret_screen(caret_frame("three_after_2"))
        g = {"input_box": True, "input_images": [1, 2, 3], "cursor_image": 2}
        self.assertIsNone(node_agent.check_guard(text, g, (23, 46)))
        self.assertEqual(node_agent.check_guard(text, g, (34, 46)), "光标前不是 [Image #2]")
        self.assertEqual(node_agent.check_guard(text, g, (12, 46)), "光标前不是 [Image #2]")
        self.assertEqual(node_agent.check_guard(text, g, (23, 45)), "终端光标不在输入框里")
        self.assertEqual(node_agent.check_guard(text, g, None), "终端光标不在输入框里")
        self.assertEqual(node_agent.check_guard(text, dict(g, input_images=[1, 3]), (23, 46)), "输入框里的图片已变化")
        sp = caret_screen(caret_frame("double_space"))
        gs = {"input_box": True, "input_images": [1, 3], "cursor_space": True}
        self.assertIsNone(node_agent.check_guard(sp, gs, (13, 46)))
        self.assertEqual(node_agent.check_guard(sp, gs, (12, 46)), "光标前不是空格")
        self.assertEqual(node_agent.check_guard(sp, gs, (2, 46)), "光标前不是空格")
        dlg = "\n".join(["❯ hi", "", "─" * 80, " Bash command", "", "   ls", "", " Do you want to proceed?",
                         " ❯ 1. Yes", "   2. No", "", " Esc to cancel"])
        self.assertEqual(node_agent.check_guard(dlg, g, (23, 46)), "输入框不可见（可能弹出了对话框）")
        # 不带光标的 guard：与以前完全相同
        self.assertIsNone(node_agent.check_guard(text, {"input_box": True, "input_images": [1, 2, 3]}))

    def test_op_keys_and_capture_with_cursor(self):
        text = caret_screen(caret_frame("three_after_2"))
        calls = []
        cur = {"xy": "23 46"}

        def fake_tmux(args, input_bytes=None, timeout=15):
            calls.append(list(args))
            if args[0] == "display-message":
                return 0, cur["xy"] + "\n" + text, ""
            if args[0] == "capture-pane":
                return 0, text, ""
            return 0, "", ""

        g = {"input_box": True, "input_images": [1, 2, 3], "cursor_image": 2}
        with mock.patch.object(node_agent, "tmux", side_effect=fake_tmux):
            node_agent.op_keys({"sid": SID, "keys": ["BSpace"], "guard": g})
            self.assertEqual([c[0] for c in calls], ["display-message", "send-keys"])
            disp = calls[0]
            self.assertIn("#{cursor_x} #{cursor_y}", disp)
            cap = disp[disp.index(";") + 1:]
            self.assertEqual(cap[:2], ["capture-pane", "-p"])
            self.assertNotIn("-J", cap)   # 行号要与光标行号一致
            calls.clear()
            cur["xy"] = "34 46"   # 光标被挪走了：一个键都不按
            with self.assertRaises(node_agent.OpError) as cm:
                node_agent.op_keys({"sid": SID, "keys": ["BSpace"], "guard": g})
            self.assertEqual(cm.exception.code, "guard")
            self.assertEqual([c[0] for c in calls], ["display-message"])
            calls.clear()
            # 没有光标 guard：照旧用 -J 抓屏
            node_agent.op_keys({"sid": SID, "keys": ["Left"], "guard": {"input_box": True, "input_images": [1, 2, 3]}})
            self.assertEqual(calls[0][:3], ["capture-pane", "-p", "-J"])
            cur["xy"] = "23 46"
            self.assertEqual(node_agent.op_capture({"sid": SID, "cursor": True}), {"text": text, "cursor": {"x": 23, "y": 46}})
            for bad in ({"cursor": 1}, {"cursor": True, "history": 10}):
                with self.assertRaises(node_agent.OpError):
                    node_agent.op_capture(dict(bad, sid=SID))
            cur["xy"] = "garbage"
            with self.assertRaises(node_agent.OpError):
                node_agent.op_capture({"sid": SID, "cursor": True})


# ============================================================================ 4. fake_tui
class TestFakeTuiImages(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ccr-img-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, "proj"))
        self.img = os.path.join(self.tmp, "my shot.png")
        pathlib.Path(self.img).write_bytes(PNG)
        self.jpg = os.path.join(self.tmp, "b.jpg")
        pathlib.Path(self.jpg).write_bytes(JPG)

    def app(self, **env):
        e = {"HOME": self.tmp, "USER": "u", "CCR_FAKE_DELAY": "0.01", "CCR_FAKE_THINK": "0.01"}
        e.update(env)
        return fake_tui.FakeClaude([], env=e, size=(200, 50), out=io.StringIO(), cwd=os.path.join(self.tmp, "proj"))

    @staticmethod
    def paste(a, text):
        a.feed(b"\x1b[200~" + text.encode("utf-8") + b"\x1b[201~")

    def parse(self, a):
        return tui_parse.parse_screen("\n".join(a.frame()))

    def test_drop_paths(self):
        a = self.app()
        self.assertEqual(a.drop_paths(self.img), [self.img])                       # 未转义的空格（整段一个路径）
        self.assertEqual(a.drop_paths(attach.escape_drop_path(self.img)), [self.img])
        self.assertEqual(a.drop_paths(attach.escape_drop_path(self.img) + " " + self.jpg), [self.img, self.jpg])
        self.assertEqual(a.drop_paths("'%s'" % self.img), [self.img])
        self.assertIsNone(a.drop_paths(self.img + "\n" + self.jpg))               # 换行分隔：不识别（真机实测）
        self.assertIsNone(a.drop_paths(self.jpg + " 这是什么？"))                  # 路径 + 文字：不识别（真机实测）
        self.assertIsNone(a.drop_paths(os.path.join(self.tmp, "nope.png")))
        self.assertIsNone(a.drop_paths("hello"))

    def test_paste_path_ctrl_v_backspace(self):
        a = self.app()
        self.paste(a, attach.escape_drop_path(self.img))
        a.feed(b"\x16")   # Ctrl+V：fake 生成示意图片，绝不读真实剪贴板
        self.paste(a, " 这两张？")
        st = self.parse(a)
        self.assertEqual(st["input"]["text"], "[Image #1] [Image #2] 这两张？")
        self.assertEqual(st["input"]["images"], [1, 2])
        self.paste(a, self.jpg + " 文字")   # 路径 + 文字：普通文字
        self.assertTrue(self.parse(a)["input"]["text"].endswith(self.jpg + " 文字"))
        b = self.app()
        self.paste(b, self.img)
        self.paste(b, self.jpg)
        self.assertEqual(self.parse(b)["input"]["text"], "[Image #1] [Image #2]")
        b.feed(b"\x7f")   # 退格整块删掉占位
        self.assertEqual(self.parse(b)["input"]["text"], "[Image #1]")
        c = self.app(CCR_FAKE_CLIPBOARD="none")
        c.feed(b"\x16")
        self.assertEqual(self.parse(c)["input"]["text"], "")

    def test_submit_writes_image_blocks(self):
        a = self.app()
        self.paste(a, self.img)
        a.feed(b"\x16")
        self.paste(a, " 描述一下")
        a.feed(b"\r")
        st = self.parse(a)
        self.assertEqual(st["echo"], "[Image #1] [Image #2] 描述一下")
        users = [json.loads(x) for x in pathlib.Path(a.tpath).read_text(encoding="utf-8").splitlines()
                 if '"type":"user"' in x]
        main = users[0]
        self.assertEqual(main["imagePasteIds"], [1, 2])
        c = main["message"]["content"]
        self.assertEqual(c[0], {"type": "text", "text": "[Image #1] [Image #2] 描述一下"})
        self.assertEqual([b["type"] for b in c], ["text", "image", "image"])
        self.assertEqual(base64.b64decode(c[1]["source"]["data"]), PNG)
        self.assertEqual(c[1]["source"]["media_type"], "image/png")
        meta = users[1]
        self.assertTrue(meta["isMeta"])
        self.assertEqual(meta["message"]["content"], [{"type": "text", "text": "[Image: source: %s]" % self.img}])
        # 与真机 transcript 同形（fixtures/image_paste/transcript_image.jsonl）
        real = [json.loads(x) for x in (FIX / "transcript_image.jsonl").read_text(encoding="utf-8").splitlines()]
        r0 = next(e for e in real if e.get("imagePasteIds") == [15, 16])
        self.assertEqual([b["type"] for b in r0["message"]["content"]], ["text", "image", "image"])
        self.assertEqual(r0["message"]["content"][0]["text"][:24], "[Image #15] [Image #16] ")


    def caret(self, a):
        """fake 的插入点 → (cx, cy, 输入框文字, 光标前的文字)（与真机一样由 tmux 光标坐标读出）。"""
        lines = a.frame()
        row, col = a.caret_pos
        L = tui_parse.screen_lines("\n".join(lines))
        c = tui_parse.input_caret(L, col, row)
        return col, row, tui_parse.parse_screen("\n".join(lines))["input"]["text"], c and c["before"]

    def test_caret_like_real_cli(self):
        """fake 的光标与 CLI 2.1.332 真机一致：← / → 的列、退格整块删、删后空格、在中间插入不补空格。"""
        a = self.app()
        for p in (self.img, self.jpg, self.img):
            self.paste(a, p)
        cx, cy, text, before = self.caret(a)
        self.assertEqual((cx, text, before), (34, "[Image #1] [Image #2] [Image #3]", text))
        cols = []
        for _ in range(6):
            a.feed(b"\x1b[D")
            cols.append(self.caret(a)[0])
        self.assertEqual([[c, cy] for c in cols], [[x, cy] for x, _ in caret_frame("three_end")["left"]])
        cols = []
        for _ in range(6):
            a.feed(b"\x1b[C")
            cols.append(self.caret(a)[0])
        self.assertEqual(cols, [x for x, _ in caret_frame("three_home")["right"]])
        for _ in range(2):
            a.feed(b"\x1b[D")
        self.assertEqual(self.caret(a)[0], 23)
        a.feed(b"\x7f")   # 退格：整块删掉 [Image #2]，两边空格留着
        self.assertEqual(self.caret(a)[:3], (13, cy, "[Image #1]  [Image #3]"))
        a.feed(b"\x7f")
        self.assertEqual(self.caret(a)[:3], (12, cy, "[Image #1] [Image #3]"))
        self.paste(a, self.jpg)   # 光标在中间：插在光标处，不补空格（真机实测）
        self.assertEqual(self.caret(a)[:3], (22, cy, "[Image #1][Image #4] [Image #3]"))
        # 多行 + 中文
        b = self.app()
        self.paste(b, self.img)
        self.paste(b, self.jpg)
        self.paste(b, " 中文abc\nsecond line")
        cx, cy, text, before = self.caret(b)
        self.assertEqual((cx, before), (13, "second line"))
        L = tui_parse.screen_lines("\n".join(b.frame()))
        moves = tui_parse.caret_moves(L, (cx, cy), tui_parse.input_image_cells(L, 2)[0])
        self.assertEqual(moves, ["Left"] * 12)
        for k in moves:
            b.on_key(k)
        self.assertEqual(self.caret(b)[:2], (31, cy - 1))
        cols = []
        for _ in range(6):
            b.on_key("Left")
            cols.append(self.caret(b)[0])
        self.assertEqual(cols, [x for x, _ in caret_frame("multiline_end")["left_more"]])
        b.feed(b"\x7f")
        self.assertEqual(self.caret(b)[2], "[Image #1]  中文abc\nsecond line")
        # Ctrl+U：光标在末尾时与以前一样删掉最后一行
        c = self.app()
        self.paste(c, "第一行\n第二行")
        c.feed(b"\x15")
        self.assertEqual(self.caret(c)[2], "第一行")


# ============================================================================ 5. server
class ImgStub(StubManager):
    def send_message(self, sid, text, clear_input=False, expect_images=None):
        self._act("send_message", sid, text, clear_input, expect_images)
        return {"ok": True, "local_id": "L1"}

    def paste_image(self, sid):
        self._act("paste_image", sid)
        return {"ok": True, "images": [1], "added": [1]}

    def attach_paths(self, sid, paths):
        self._act("attach_paths", sid, paths)
        return {"ok": True, "images": [1, 2], "added": [2]}

    def clear_images(self, sid):
        self._act("clear_images", sid)
        return {"ok": True, "images": []}

    def remove_image(self, sid, n, expect):
        self._act("remove_image", sid, n, expect)
        return {"ok": True, "images": [i for i in expect if i != n], "removed": n}

    def read_drop_image(self, sid, token):
        self._act("read_drop_image", sid, token)
        if token != "A" * 24:
            raise OpError("图片不存在或已作废", code="not_found", status=404)
        return "image/png", PNG


class TestServerRoutes(ServerBase):
    def setUp(self):
        super().setUp()
        self.mgr.__class__ = ImgStub

    def test_routes_and_validation(self):
        st, js, _, _ = self.req("POST", "/api/sessions", {"title": "t"})
        self.assertEqual(st, 200, js)
        sid = js["sid"]
        P = "/api/sessions/%s/" % sid
        self.assertEqual(self.req("POST", P + "image/paste", {})[1], {"ok": True, "images": [1], "added": [1]})
        self.assertEqual(self.mgr.calls[-1], ("paste_image", sid))
        self.assertEqual(self.req("POST", P + "image/paths", {"paths": ["/tmp/a b.png"]})[0], 200)
        self.assertEqual(self.mgr.calls[-1], ("attach_paths", sid, ["/tmp/a b.png"]))
        for bad in ({}, {"paths": []}, {"paths": "/tmp/a.png"}, {"paths": [1]}, {"paths": [None]}):
            self.assertErr(self.req("POST", P + "image/paths", bad), 400)
        self.assertEqual(self.req("POST", P + "image/clear", {})[0], 200)
        self.assertEqual(self.mgr.calls[-1], ("clear_images", sid))
        self.assertErr(self.req("POST", P + "image/other", {}), 404)
        # 删除单张：n + expect（前端此刻显示的编号）
        st, js, _, _ = self.req("POST", P + "image/remove", {"n": 2, "expect": [1, 2, 3]})
        self.assertEqual((st, js), (200, {"ok": True, "images": [1, 3], "removed": 2}))
        self.assertEqual(self.mgr.calls[-1], ("remove_image", sid, 2, [1, 2, 3]))
        for bad in ({}, {"n": 2}, {"expect": [2]}, {"n": "2", "expect": [2]}, {"n": True, "expect": [1]},
                    {"n": -1, "expect": [1]}, {"n": 2, "expect": "2"}, {"n": 2, "expect": [True]},
                    {"n": 2, "expect": list(range(51))}, {"n": 10 ** 6, "expect": [1]}):
            self.assertErr(self.req("POST", P + "image/remove", bad), 400)
        # 拖入图片的缩略图：只读通道，按文件头给类型、不缓存、nosniff、沙箱 CSP
        st, raw_js, r, raw = self.req("GET", P + "image/file/" + "A" * 24)
        self.assertEqual(st, 200)
        self.assertEqual(raw, PNG)
        self.assertEqual(r.getheader("Content-Type"), "image/png")
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertIn("sandbox", r.getheader("Content-Security-Policy"))
        self.assertEqual(self.mgr.calls[-1], ("read_drop_image", sid, "A" * 24))
        self.assertErr(self.req("GET", P + "image/file/" + "B" * 24), 404)
        self.assertEqual(self.req("GET", P + "image/file/" + "A" * 24, auth=False)[0], 401)
        self.assertErr(self.req("POST", P + "image/file/" + "A" * 24, {}), 404)
        # message：只有图片也能发；expect_images 校验
        self.assertEqual(self.req("POST", P + "message", {"text": "", "expect_images": [3, 4]})[0], 200)
        self.assertEqual(self.mgr.calls[-1], ("send_message", sid, "", False, [3, 4]))
        self.assertEqual(self.req("POST", P + "message", {"text": "看图", "expect_images": []})[0], 200)
        self.assertEqual(self.mgr.calls[-1], ("send_message", sid, "看图", False, []))
        self.assertEqual(self.req("POST", P + "message", {"text": "x"})[0], 200)
        self.assertEqual(self.mgr.calls[-1], ("send_message", sid, "x", False, None))
        for bad in ({"text": "", "expect_images": []}, {"text": "x", "expect_images": "1"},
                    {"text": "x", "expect_images": [True]}, {"text": "x", "expect_images": ["1"]},
                    {"text": "x", "expect_images": list(range(51))},
                    {"text": "x", "images": [{"media_type": "image/png", "data": "iVBORw0KGgo="}]}):
            self.assertErr(self.req("POST", P + "message", bad), 400)
        # 终端面板的 /keys 白名单里没有 C-v（只由 paste_image 发）
        self.assertErr(self.req("POST", P + "keys", {"keys": ["C-v"]}), 400)
        # 没有 token：401
        self.assertEqual(self.req("POST", P + "image/paste", {}, auth=False)[0], 401)

    def test_op_error_mapping(self):
        sid = self.req("POST", "/api/sessions", {"title": "t"})[1]["sid"]
        self.mgr.errors = {"paste_image": OpError("服务器会话暂不支持直接发图片：…", code="not_local")}
        st, js, _, _ = self.req("POST", "/api/sessions/%s/image/paste" % sid, {})
        self.assertEqual((st, js.get("code")), (409, "not_local"))
        self.mgr.errors = {"paste_image": OpError("没读到", code="image_not_found", status=504)}
        st, js, _, _ = self.req("POST", "/api/sessions/%s/image/paste" % sid, {})
        self.assertEqual((st, js.get("code")), (504, "image_not_found"))

    def test_remote_port_forbidden(self):
        try:   # 手机远程口是私有版专用（开源版没有 ccr/remote_access.py）
            from ccr.remote_access import RemoteAccess
        except ImportError:
            self.skipTest("没有 ccr.remote_access（开源版）")
        for segs in (["sessions", "s0123456789ab", "image", "paste"], ["sessions", "s0123456789ab", "image", "paths"]):
            self.assertIn("手机", RemoteAccess.forbidden("POST", segs))
        self.assertIsNone(RemoteAccess.forbidden("POST", ["sessions", "s0123456789ab", "image", "clear"]))
        self.assertIsNone(RemoteAccess.forbidden("POST", ["sessions", "s0123456789ab", "image", "remove"]))
        # 拖入图片的缩略图读的是 Mac 上的本机文件：远程口禁止
        self.assertIn("手机", RemoteAccess.forbidden("GET", ["sessions", "s0123456789ab", "image", "file", "A" * 24]))
        self.assertIsNone(RemoteAccess.forbidden("POST", ["sessions", "s0123456789ab", "message"]))


# ============================================================================ 6. fake 端到端（本机）
def _real_socket_exists():
    return os.path.exists(os.path.join(REAL_SOCKET_DIR, "ccr"))


@unittest.skipUnless(HAVE_TMUX, "需要 tmux")
class TestLocalImagesEndToEnd(unittest.TestCase):
    """真实 SessionManager + AgentLink（fake 的"本机"：node_agent --local + fake_tui + 临时 TMUX_TMPDIR 里的 tmux）。"""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="ccr-ie-", dir="/tmp"))
        self.cfg = {"host": "gpu-cluster", "jobid": "12345", "default_cwd": "~", "fake": True,
                    "fake_home": os.path.join(self.tmp, "home"), "fake_tmpdir": os.path.join(self.tmp, "tmux"),
                    "fake_tmux": TMUX, "fake_env": {"CCR_FAKE_DELAY": "0.03", "CCR_FAKE_THINK": "0.4"}}
        self.lhome = remote.fake_home(self.cfg, local=True)
        self.ltmux = remote.fake_tmpdir(self.cfg, local=True)
        os.makedirs(os.path.join(self.lhome, "proj"))
        self.imgdir = os.path.join(self.tmp, "imgs dir")   # 带空格：按终端拖放的规则转义
        os.makedirs(self.imgdir)
        self.png = os.path.join(self.imgdir, "截屏 2026-11-25 (1).png")
        pathlib.Path(self.png).write_bytes(PNG)
        self.real_before = _real_socket_exists()
        self.addCleanup(self._cleanup)

    def _tmux(self, tmpdir, *args):
        sock = os.path.join(tmpdir, "tmux-%d" % os.getuid(), "ccr")
        if not os.path.isdir(tmpdir):
            return subprocess.CompletedProcess([TMUX], 1, "", "no such dir")
        env = dict(os.environ)
        env.pop("TMUX", None)
        return subprocess.run([TMUX, "-S", sock, "-f", "/dev/null"] + list(args), env=env,
                              capture_output=True, text=True, timeout=10)

    def _cleanup(self):
        for d in (self.ltmux, remote.fake_tmpdir(self.cfg)):
            self._tmux(d, "kill-server")
        shutil.rmtree(self.tmp, True)
        self.assertEqual(_real_socket_exists(), self.real_before, "不应在真实套接字上起 tmux server")

    def mgr(self):
        m = S.SessionManager(lambda: dict(self.cfg), pathlib.Path(self.tmp, "data"))
        m.exit_timeout = 8
        m.image_timeout = 4.0
        self.addCleanup(lambda: m._shutdown or m.shutdown())
        return m

    def ready(self, m, jobid="local"):
        sid = m.create(jobid=jobid, cwd="~/proj" if jobid == "local" else "~", title="图")["sid"]
        ok = wait_until(lambda: m.get_meta(sid)["state"] in ("idle", "trust"), 20, 0.1)
        self.assertTrue(ok, m.get_meta(sid))
        if m.get_meta(sid)["state"] == "trust":
            d = m.get_screen(sid)["state"]["dialog"]
            m.choose(sid, next(o["index"] for o in d["options"] if o["label"].startswith("Yes")))
            self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle", 20, 0.1))
        return sid

    def user_entries(self, m, sid):
        out = []
        for e in m.session_events(sid):
            if e["kind"] == "transcript":
                out += [x for x in e["data"]["entries"] if x.get("type") == "user" and not x.get("isMeta")]
        return out

    def images_now(self, m, sid):
        return tui_parse.split_input_images(m.get_screen(sid)["state"]["input"].get("text"))[0]

    def test_paste_drop_send_clear(self):
        m = self.mgr()
        sid = self.ready(m)
        # ⌘V → Ctrl+V：claude 自己把剪贴板图片放进输入框
        r = m.paste_image(sid)
        self.assertEqual((r["images"], r["added"]), ([1], [1]))
        # 拖入（路径带空格、括号、中文）：转义后单独粘贴一次 → [Image #2]
        r = m.attach_paths(sid, [self.png])
        self.assertEqual((r["images"], r["added"]), ([1, 2], [2]))
        # 前端看到的图片和屏幕不一致 → 不发
        with self.assertRaises(OpError) as cm:
            m.send_message(sid, "看图", expect_images=[1])
        self.assertEqual(cm.exception.api_code, "images_changed")
        with self.assertRaises(OpError) as cm:
            m.send_message(sid, "/model", expect_images=[1, 2])
        self.assertEqual(cm.exception.api_code, "images_with_command")
        self.assertEqual(self.images_now(m, sid), [1, 2])   # 图片没被清掉
        # 文字 + 图片：文字接在图片后面，一起发出
        m.send_message(sid, "这两张图是什么？", expect_images=[1, 2])
        self.assertTrue(wait_until(lambda: any(isinstance(u["message"]["content"], list)
                                               for u in self.user_entries(m, sid)), 20, 0.1))
        u = [x for x in self.user_entries(m, sid) if isinstance(x["message"]["content"], list)][-1]
        c = u["message"]["content"]
        self.assertEqual(c[0]["text"], "[Image #1] [Image #2] 这两张图是什么？")
        self.assertEqual([b["type"] for b in c], ["text", "image", "image"])
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle", 20, 0.1))
        # 只有图片、没有文字
        m.attach_paths(sid, [self.png])
        self.assertEqual(self.images_now(m, sid), [3])
        m.send_message(sid, "", expect_images=[3])
        self.assertTrue(wait_until(lambda: any(isinstance(x["message"]["content"], list) and
                                               x["message"]["content"][0]["text"] == "[Image #3]"
                                               for x in self.user_entries(m, sid)), 20, 0.1))
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle", 20, 0.1))
        # 清除：只有图片时 Ctrl+U
        m.paste_image(sid)
        self.assertEqual(self.images_now(m, sid), [4])
        self.assertEqual(m.clear_images(sid)["images"], [])
        self.assertEqual(self.images_now(m, sid), [])
        self.assertEqual(m.clear_images(sid)["images"], [])   # 没有图片：什么都不按
        # 输入框里没有图片却只发图片 → 不发
        with self.assertRaises(OpError) as cm:
            m.send_message(sid, "", expect_images=[4])
        self.assertEqual(cm.exception.api_code, "images_changed")
        # 图片 + 终端里别的文字：清除拒绝；发消息要求确认清空（说明含图片）
        m.paste_image(sid)
        m._req("local", "paste", sid=sid, text=" 残留", enter=False)
        self.assertTrue(wait_until(lambda: "残留" in m.get_screen(sid)["state"]["input"]["text"], 5, 0.05))
        with self.assertRaises(OpError) as cm:
            m.clear_images(sid)
        self.assertEqual(cm.exception.api_code, "input_not_only_images")
        with self.assertRaises(OpError) as cm:
            m.send_message(sid, "新消息", expect_images=[5])
        self.assertEqual(cm.exception.api_code, "input_not_empty")
        self.assertIn("含 1 张图片", str(cm.exception))
        self.assertEqual(self.images_now(m, sid), [5])

    def test_busy_queue_and_errors(self):
        # 剪贴板里没有图片（fake：CCR_FAKE_CLIPBOARD=none 时 Ctrl+V 什么都不发生；环境在本机 tmux server 启动时定下）
        self.cfg["fake_env"]["CCR_FAKE_CLIPBOARD"] = "none"
        m = self.mgr()
        sid = self.ready(m)
        # 路径校验在发任何东西之前
        for bad in ([], ["rel.png"], [os.path.join(self.tmp, "missing.png")], ["/etc/hosts"], "x", [self.png] * 11):
            with self.assertRaises(ValueError, msg=repr(bad)[:60]):
                m.attach_paths(sid, bad)
        with self.assertRaises(OpError) as cm:
            m.paste_image(sid)
        self.assertEqual((cm.exception.api_code, cm.exception.http_status), ("image_not_found", 504))
        self.assertEqual(self.images_now(m, sid), [])
        # 忙时：图片 + 文字进入 claude 的输入队列，之后照常带图片发出
        m.send_message(sid, "/long 慢慢说")
        self.assertTrue(wait_until(lambda: m.get_screen(sid)["state"]["busy"], 10, 0.05))
        m.attach_paths(sid, [self.png])
        m.send_message(sid, "排队的图", expect_images=[1])
        self.assertTrue(wait_until(lambda: any(isinstance(x["message"]["content"], list)
                                               for x in self.user_entries(m, sid)), 30, 0.1),
                        m.get_screen(sid)["text"])
        # 对话框打开时：不按 Ctrl+V、不粘路径
        m.send_message(sid, "/tool 跑个命令")
        self.assertTrue(wait_until(lambda: (m.get_screen(sid)["state"].get("dialog") or {}).get("kind") == "permission",
                                   30, 0.1), m.get_screen(sid)["text"])
        for fn in (lambda: m.paste_image(sid), lambda: m.attach_paths(sid, [self.png])):
            with self.assertRaises(OpError) as cm:
                fn()
            self.assertEqual(cm.exception.api_code, "dialog_open")

    def input_text(self, m, sid):
        return m.get_screen(sid)["state"]["input"].get("text")

    def test_remove_single_image_and_thumbs(self):
        m = self.mgr()
        sid = self.ready(m)
        b = os.path.join(self.imgdir, "b c.png")
        c = os.path.join(self.imgdir, "c.gif")
        pathlib.Path(b).write_bytes(PNG)
        pathlib.Path(c).write_bytes(GIF)
        r = m.attach_paths(sid, [self.png, b, c])
        self.assertEqual((r["images"], r["added"]), ([1, 2, 3], [1, 2, 3]))
        files = r["files"]
        self.assertEqual(sorted(files), ["1", "2", "3"])
        # 缩略图通道：只读刚拖入的那几个文件
        self.assertEqual(m.read_drop_image(sid, files["3"]), ("image/gif", GIF))
        for bad in ("A" * 24, "../../etc/passwd", "", None, files["1"] + "x"):
            with self.assertRaises(OpError) as cm:
                m.read_drop_image(sid, bad)
            self.assertEqual(cm.exception.http_status, 404)
        # 前端看到的和屏幕不一致：一个键都不按
        with self.assertRaises(OpError) as cm:
            m.remove_image(sid, 2, [1, 2])
        self.assertEqual(cm.exception.api_code, "images_changed")
        with self.assertRaises(OpError) as cm:
            m.remove_image(sid, 7, [1, 2, 3])
        self.assertEqual(cm.exception.api_code, "images_changed")
        self.assertEqual(self.input_text(m, sid), "[Image #1] [Image #2] [Image #3]")
        for bad in ((True, [1]), ("2", [2]), (2, "2"), (2, [True])):
            with self.assertRaises(ValueError):
                m.remove_image(sid, *bad)
        # 删中间那张：只少这一张，空格整理好，光标回到末尾
        r = m.remove_image(sid, 2, [1, 2, 3])
        self.assertEqual(r, {"ok": True, "images": [1, 3], "removed": 2})
        self.assertEqual(self.input_text(m, sid), "[Image #1] [Image #3]")
        snap = m._caret_snap(m._get(sid))
        self.assertEqual((snap["caret"]["before"], snap["caret"]["after"]), ("[Image #1] [Image #3]", ""))
        with self.assertRaises(OpError) as cm:   # 删掉的那张：缩略图通道作废
            m.read_drop_image(sid, files["2"])
        self.assertEqual(cm.exception.http_status, 404)
        self.assertEqual(m.read_drop_image(sid, files["1"])[0], "image/png")
        # 再拖入：接在末尾（光标已挪回末尾）
        m.attach_paths(sid, [b])
        self.assertEqual(self.input_text(m, sid), "[Image #1] [Image #3] [Image #4]")
        # 删第一张、最后一张
        self.assertEqual(m.remove_image(sid, 1, [1, 3, 4])["images"], [3, 4])
        self.assertEqual(self.input_text(m, sid), "[Image #3] [Image #4]")
        self.assertEqual(m.remove_image(sid, 4, [3, 4])["images"], [3])
        self.assertEqual(self.input_text(m, sid), "[Image #3]")
        # 发送：只带剩下的那张
        m.send_message(sid, "剩下的", expect_images=[3])
        self.assertTrue(wait_until(lambda: any(isinstance(x["message"]["content"], list)
                                               for x in self.user_entries(m, sid)), 20, 0.1))
        u = [x for x in self.user_entries(m, sid) if isinstance(x["message"]["content"], list)][-1]
        self.assertEqual(u["message"]["content"][0]["text"], "[Image #3] 剩下的")
        self.assertEqual([x["type"] for x in u["message"]["content"]], ["text", "image"])
        self.assertEqual(base64.b64decode(u["message"]["content"][1]["source"]["data"]), GIF)
        with self.assertRaises(OpError) as cm:   # 发出去之后缩略图通道作废
            m.read_drop_image(sid, files["3"])
        self.assertEqual(cm.exception.http_status, 404)
        self.assertTrue(wait_until(lambda: m.get_meta(sid)["state"] == "idle", 20, 0.1))
        # claude 输入框里还有别的文字：只删图片，文字不动
        m.attach_paths(sid, [self.png, b])
        m._req("local", "paste", sid=sid, text=" 残留 文字", enter=False)
        self.assertTrue(wait_until(lambda: "残留" in self.input_text(m, sid), 5, 0.05))
        self.assertEqual(m.remove_image(sid, 5, [5, 6])["images"], [6])
        self.assertEqual(self.input_text(m, sid), "[Image #6] 残留 文字")
        self.assertEqual(m.remove_image(sid, 6, [6])["images"], [])
        self.assertEqual(self.input_text(m, sid), "残留 文字")
        m._keys(m._get(sid), ["C-u"], guard={"input_box": True})
        self.assertTrue(wait_until(lambda: not self.input_text(m, sid), 5, 0.05))
        # 只有一张：删完输入框是空的，不再按任何键
        r = m.attach_paths(sid, [c])
        self.assertEqual(m.remove_image(sid, 7, [7])["images"], [])
        self.assertEqual(self.input_text(m, sid), "")
        # 清除：缩略图通道作废
        r = m.attach_paths(sid, [c])
        tok = r["files"]["8"]
        self.assertEqual(m.read_drop_image(sid, tok)[0], "image/gif")
        m.clear_images(sid)
        with self.assertRaises(OpError):
            m.read_drop_image(sid, tok)
        # 文件被换掉（不同 inode）：不读
        r = m.attach_paths(sid, [b])
        tok = r["files"]["9"]
        other = os.path.join(self.imgdir, "other.png")
        pathlib.Path(other).write_bytes(PNG)
        os.replace(other, b)
        with self.assertRaises(OpError) as cm:
            m.read_drop_image(sid, tok)
        self.assertEqual(cm.exception.http_status, 404)
        # 对话框打开时不删（先于图片核对）
        m.clear_images(sid)
        m.send_message(sid, "/tool 跑个命令")
        self.assertTrue(wait_until(lambda: (m.get_screen(sid)["state"].get("dialog") or {}).get("kind") == "permission",
                                   30, 0.1), m.get_screen(sid)["text"])
        with self.assertRaises(OpError) as cm:
            m.remove_image(sid, 9, [9])
        self.assertEqual(cm.exception.api_code, "dialog_open")

    def test_server_session_not_local(self):
        m = self.mgr()
        sid = self.ready(m, jobid="12345")
        with self.assertRaises(OpError) as cm:
            m.paste_image(sid)
        self.assertEqual(cm.exception.api_code, "not_local")
        with self.assertRaises(OpError) as cm:
            m.attach_paths(sid, [self.png])
        self.assertEqual(cm.exception.api_code, "not_local")
        for fn in (lambda: m.remove_image(sid, 1, [1]), lambda: m.read_drop_image(sid, "A" * 24)):
            with self.assertRaises(OpError) as cm:
                fn()
            self.assertEqual(cm.exception.api_code, "not_local")
        # 纯文字消息与以前完全一样
        m.send_message(sid, "服务器你好")
        self.assertTrue(wait_until(lambda: any(x["message"]["content"] == "服务器你好"
                                               for x in self.user_entries(m, sid)), 20, 0.1))


# ============================================================================ 7. 原生窗口：拖入事件脚本
@unittest.skipUnless(shutil.which("xcrun"), "需要 xcrun swiftc")
class TestNativeDropScript(unittest.TestCase):
    """抽出 main.swift 里 ccrDropEventScript 那一段单独编译，用 JavaScriptCore 执行它生成的脚本，
    核对各种刁钻路径（引号、反斜杠、换行、</script>、中文、U+2028）原样到达页面的事件 detail。不开任何窗口。"""

    def test_roundtrip(self):
        src = (ROOT / "tools" / "macapp" / "main.swift").read_text(encoding="utf-8")
        a = src.index("// BEGIN ccr-drop-script")
        b = src.index("// END ccr-drop-script")
        paths = ["/tmp/a b.png", "/x/it's \"q\".png", "/x/back\\slash.png", "/x/line\nbreak.png",
                 "/x/</script><script>alert(1)</script>.png", "/x/截屏 2026.png", "/x/ sep.png", "/x/${x}`y`.png"]
        prog = src[a:b] + r"""
import Foundation
import JavaScriptCore
let paths: [String] = try! JSONSerialization.jsonObject(with: CommandLine.arguments[1].data(using: .utf8)!) as! [String]
var out: [Any] = []
for phase in ["enter", "drop", "leave", "bogus"] {
    guard let js = ccrDropEventScript(phase, phase == "leave" ? [] : paths) else { out.append(NSNull()); continue }
    let ctx = JSContext()!
    ctx.evaluateScript("var got = null; var window = { dispatchEvent: function (e) { got = { type: e.type, detail: e.detail }; } };" +
                       "function CustomEvent(t, o) { this.type = t; this.detail = o.detail; }")
    ctx.evaluateScript(js)
    out.append(ctx.evaluateScript("JSON.stringify(got)")!.toString()!)
}
print(String(data: try! JSONSerialization.data(withJSONObject: out), encoding: .utf8)!)
"""
        with tempfile.TemporaryDirectory(dir="/tmp") as td:
            f = os.path.join(td, "t.swift")
            pathlib.Path(f).write_text(prog, encoding="utf-8")
            exe = os.path.join(td, "t")
            r = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-o", exe, f], capture_output=True, text=True,
                               timeout=300)
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            r = subprocess.run([exe, json.dumps(paths)], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout)
        self.assertIsNone(res[3])   # 未知 phase 不生成脚本
        enter, drop, leave = (json.loads(x) for x in res[:3])
        self.assertEqual(enter, {"type": "ccr-native-drag", "detail": {"phase": "enter", "paths": paths}})
        self.assertEqual(drop, {"type": "ccr-native-drop", "detail": {"phase": "drop", "paths": paths}})
        self.assertEqual(leave, {"type": "ccr-native-drag", "detail": {"phase": "leave", "paths": []}})

    def test_swift_source_rules(self):
        src = (ROOT / "tools" / "macapp" / "main.swift").read_text(encoding="utf-8")
        # 拖入的处理不碰系统剪贴板（NSPasteboard.general），只读拖动粘贴板上的文件 URL
        body = src[src.index("final class CCRWebView"):src.index("// MARK: - 应用")]
        self.assertNotIn("NSPasteboard.general", body)
        self.assertNotIn("Data(contentsOf", body)
        self.assertIn(".urlReadingFileURLsOnly", body)
        self.assertIn("self?.pageState == .app", src)


if __name__ == "__main__":
    unittest.main()
