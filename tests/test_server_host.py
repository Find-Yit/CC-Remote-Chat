"""未设置服务器 SSH 主机（config.host 为空）时的行为：本机模式照常，服务器操作给出明确的 400 提示。"""
import types
import unittest

from ccr import config as C
from ccr import remote
from ccr.session import SessionManager


class TestEmptyHost(unittest.TestCase):
    def test_default_is_empty(self):
        self.assertEqual(C.DEFAULT_CONFIG["host"], "")

    def test_validate_partial_allows_clearing(self):
        self.assertEqual(C.validate_partial({"host": ""}), {"host": ""})
        self.assertEqual(C.validate_partial({"host": "  "}), {"host": ""})
        self.assertEqual(C.validate_partial({"host": " my-cluster "}), {"host": "my-cluster"})
        with self.assertRaises(ValueError):
            C.validate_partial({"host": "-oProxyCommand=x"})

    def test_ssh_argv_requires_host(self):
        with self.assertRaises(ValueError) as cm:
            remote.ssh_base_argv({"host": "", "ssh_multiplex": False})
        self.assertIn("设置", str(cm.exception))

    def _mgr(self, cfg):
        return types.SimpleNamespace(_get_config=lambda: cfg)

    def test_require_server_host(self):
        req = SessionManager._require_server_host
        req(self._mgr({"host": ""}), remote.LOCAL_JOBID)            # 本机：不需要 host
        req(self._mgr({"host": "", "fake": True}), "123456")         # 演示模式：不连服务器
        req(self._mgr({"host": "my-cluster"}), "123456")
        with self.assertRaises(ValueError):
            req(self._mgr({"host": ""}), "123456")


if __name__ == "__main__":
    unittest.main()
