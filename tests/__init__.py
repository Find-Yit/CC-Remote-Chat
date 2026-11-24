"""测试包。安全网：整个测试进程默认使用一个专用的临时 TMUX_TMPDIR，
任何继承 os.environ 的 tmux 调用都落在这里，绝不会碰到用户真实的 /tmp/tmux-<uid>/ccr
（用户可能正开着本机会话；曾有测试 cleanup 误杀真实 server）。"""
import atexit
import os
import shutil
import tempfile

_SAFE_TMUX_TMPDIR = tempfile.mkdtemp(prefix="ccr-test-tmux-", dir="/tmp")
os.environ["TMUX_TMPDIR"] = _SAFE_TMUX_TMPDIR
os.environ.pop("TMUX", None)
os.environ.pop("TMUX_PANE", None)
atexit.register(shutil.rmtree, _SAFE_TMUX_TMPDIR, True)
