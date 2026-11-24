"""时间线左侧 prompt 小格子（static/minimap.js，SPEC §7「左侧 prompt 小格子」）的纯函数：条目推导、预览纯文本化、
top%、指针 → 下标、可见范围（二分）、一轮是否可见、当前下标、留白 → 常驻 / 命中宽度、导航目标、滚轮换算。
DOM 部分（悬停鱼眼、预览卡、跳转、滚轮转发、宽 / 窄两档）在回放自测页第 1d 节。"""
import json
import pathlib
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def run_js(body):
    url = (ROOT / "static" / "minimap.js").as_uri()
    js = "const m = await import(%s);\n%s" % (json.dumps(url), body)
    r = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestMinimap(unittest.TestCase):
    def test_constants(self):
        out = run_js(r"""
console.log(JSON.stringify([m.MM_SPACING, m.MM_MIN_ITEMS, m.MM_TARGET_OFFSET, m.MM_PERSISTENT_GUTTER, m.MM_HIT_LEFT, m.MM_HIT_MAX, m.MM_NAV_REACH, m.MM_PREVIEW_MAX]));
""")
        self.assertEqual(out, [8, 2, 24, 48, 12, 40, 14, 1500])

    def test_entries_and_preview(self):
        out = run_js(r"""
const items = [
  { kind: 'user', id: 'a', text: '第一条 **加粗** 和 `code`' },
  { kind: 'thinking', id: 't', text: '想' },
  { kind: 'text', id: 'b', text: '中间一段' },
  { kind: 'tool', id: 'c' },
  { kind: 'text', id: 'd', text: '## 标题\n- 列表 [链接](http://x/y_(z)) ![图](a.png)\n\n```js\nlet x = 1\n```\n> 引用 *斜体* snake_case_name' },
  { kind: 'turnsum', id: 'e' },
  { kind: 'user', id: 'f', text: '', imageBlocks: [{}, {}] },
  { kind: 'hint', id: 'g', text: '系统消息' },
  { kind: 'user', id: 'h', text: '', imageBlocks: [{}] },
  { kind: 'text', id: 'i', text: '   ' },
  { kind: 'user', id: 'j', text: '最后' },
  null,
];
const es = m.minimapEntries(items);
const r = {};
r.entries = es.map((e) => [e.id, e.index]);
r.p0 = m.turnPreview(items, es[0], es[1].index);
r.p1 = m.turnPreview(items, es[1], es[2].index);
r.p2 = m.turnPreview(items, es[2], es[3].index);
r.p3 = m.turnPreview(items, es[3], null);
// 增量：从第 from 条开始继续扫
const more = m.minimapEntries(items, 9, [{ id: 'x', index: 0 }]);
r.inc = more.map((e) => e.id);
r.empty = [m.minimapEntries(null).length, m.turnPreview(items, null)];
console.log(JSON.stringify(r));
""")
        self.assertEqual(out["entries"], [["a", 0], ["f", 6], ["h", 8], ["j", 10]])
        self.assertEqual(out["p0"]["user"], "第一条 加粗 和 code")
        self.assertEqual(out["p0"]["assistant"], "标题 列表 链接 图 let x = 1 引用 斜体 snake_case_name")
        self.assertEqual(out["p1"], {"user": "图片消息（2 张）", "assistant": None})
        self.assertEqual(out["p2"], {"user": "图片消息", "assistant": None})   # 空白助手文字不算
        self.assertEqual(out["p3"], {"user": "最后", "assistant": None})
        self.assertEqual(out["inc"], ["x", "j"])
        self.assertEqual(out["empty"], [0, {"user": None, "assistant": None}])

    def test_plain_text(self):
        out = run_js(r"""
const long = 'a'.repeat(1400) + ' **' + 'b'.repeat(200) + '**';
console.log(JSON.stringify([
  m.plainText('| 列 | 值 |\n|---|:--:|\n| x | 1 |'),
  m.plainText('***\n<details><summary>摘要</summary>\n内容</details>'),
  m.plainText('1. 第一\n2) 第二\n- [x] 完成'),
  m.plainText('转义 \\*不是斜体\\* ~~删掉~~ __粗__'),
  m.plainText('a * b * c'),
  m.plainText(''), m.plainText(null), m.plainText('```\n```'),
  m.plainText(long).length, m.plainText('x'.repeat(2000)).length, m.plainText('xy'.repeat(10), 5),
]));
""")
        self.assertEqual(out[0], "列 值 x 1")
        self.assertEqual(out[1], "摘要 内容")
        self.assertEqual(out[2], "第一 第二 完成")
        self.assertEqual(out[3], "转义 *不是斜体* 删掉 粗")
        self.assertEqual(out[4], "a * b * c")
        self.assertEqual(out[5:8], [None, None, None])
        self.assertLessEqual(out[8], 1500)          # 只处理前 1500 字
        self.assertEqual(out[9], 1500)
        self.assertEqual(out[10], "xyxyx")

    def test_geometry(self):
        out = run_js(r"""
const r = {};
r.h = [m.heightStyle(2), m.heightStyle(1), m.heightStyle(200)];
r.top = [m.topPercent(0, 5), m.topPercent(2, 5), m.topPercent(4, 5), m.topPercent(9, 5), m.topPercent(-1, 5), m.topPercent(0, 1)];
const P = (y, n = 5) => m.indexFromPointer({ itemCount: n, railTop: 100, railHeight: 32, pointerY: y });
r.ptr = [P(100), P(107), P(109), P(132), P(0), P(999), P(120, 1), P(120, 0),
         m.indexFromPointer({ itemCount: 3, railTop: 0, railHeight: 0, pointerY: 0 })];
r.fish = [m.fisheyeScale(0), m.fisheyeScale(1), m.fisheyeScale(2), m.fisheyeScale(3), m.fisheyeScale(9), m.fisheyeScale(null)];
console.log(JSON.stringify(r));
""")
        self.assertEqual(out["h"], [
            "min(8px, max(4rem, calc(100% - 9rem)))",
            "min(1px, max(4rem, calc(100% - 9rem)))",
            "min(1592px, max(4rem, calc(100% - 9rem)))",
        ])
        self.assertEqual(out["top"], [0, 50, 100, 100, 0, 0])
        self.assertEqual(out["ptr"], [0, 1, 1, 4, 0, 4, 0, None, None])
        self.assertEqual(out["fish"], [1, 0.667, 0.417, 0.333, 0.333, 0.333])

    def test_visible_range_and_turns(self):
        out = run_js(r"""
// 10 行，每行高 40、间距 10：第 k 行 [50k, 50k+40)
const rows = Array.from({ length: 10 }, (_, k) => ({ top: 50 * k, bottom: 50 * k + 40 }));
let reads = 0;
const at = (k) => { reads++; return rows[k]; };
const r = {};
r.mid = m.visibleRange(10, at, 120, 260);       // 第 2 行 [100,140) … 第 5 行 [250,290)
r.reads = reads;
r.edge = m.visibleRange(10, at, 140, 250);      // 140 = 第 2 行底（不含）；250 = 第 5 行顶（不含）
r.tail = m.visibleRange(10, at, 600, 800);      // 所有行都在上方：只看得到尾部 → 虚拟尾行
r.gap = m.visibleRange(10, at, 141, 149);       // 视口整个落在两行之间的空隙
r.above = m.visibleRange(10, (k) => ({ top: 1000 + k, bottom: 1001 + k }), 0, 500);
r.empty = [m.visibleRange(0, at, 0, 100), m.visibleRange(10, at, 100, 100)];
r.zero = m.visibleRange(3, (k) => ({ top: 10 * k, bottom: 10 * k }), 10, 11);   // 零高度行按 1px 算
// 用户消息在第 0、3、7 行（三轮：0-2、3-6、7-9）
const users = [0, 3, 7];
const vis = (s, e) => users.map((u, i) => m.turnVisible(u, users[i + 1], s, e));
r.turns = [vis(2, 5), vis(4, 6), vis(7, 9), vis(10, 10), vis(null, null), vis(0, 0)];
r.cur = [
  m.currentIndex({ visibleStart: 2, visibleEnd: 5, rowIndices: users }),   // 第一条可见的用户消息 = 下标 1
  m.currentIndex({ visibleStart: 4, visibleEnd: 6, rowIndices: users }),   // 没有可见的 → 正在读的那一轮（1）
  m.currentIndex({ visibleStart: 0, visibleEnd: 1, rowIndices: users }),
  m.currentIndex({ visibleStart: 10, visibleEnd: 10, rowIndices: users }), // 尾部 → 最后一轮
  m.currentIndex({ visibleStart: null, visibleEnd: null, rowIndices: users }),
  m.currentIndex({ visibleStart: 0, visibleEnd: 2, rowIndices: [] }),
];
console.log(JSON.stringify(r));
""")
        self.assertEqual(out["mid"], {"start": 2, "end": 5})
        self.assertLessEqual(out["reads"], 12)          # 二分，不是逐行读位置
        self.assertEqual(out["edge"], {"start": 3, "end": 4})
        self.assertEqual(out["tail"], {"start": 10, "end": 10})
        self.assertEqual(out["gap"], {"start": None, "end": None})
        self.assertEqual(out["above"], {"start": None, "end": None})
        self.assertEqual(out["empty"], [{"start": None, "end": None}] * 2)
        self.assertEqual(out["zero"], {"start": 1, "end": 1})
        self.assertEqual(out["turns"], [
            [True, True, False],
            [False, True, False],
            [False, False, True],
            [False, False, True],
            [False, False, False],
            [True, False, False],
        ])
        self.assertEqual(out["cur"], [1, 1, 0, 2, None, None])

    def test_gutter_and_nav(self):
        out = run_js(r"""
const g = [0, 12, 20, 25.9, 26, 47.9, 48, 52, 100, NaN, -5];
console.log(JSON.stringify({
  persistent: g.map(m.hasPersistentGutter),
  hit: g.map(m.hitStripWidth),
  nav: [0, 13, 14, 40].map(m.navInteractive),
  targets: [m.navTargets(0, 5, false), m.navTargets(2, 5, false), m.navTargets(4, 5, false), m.navTargets(2, 5, true),
            m.navTargets(null, 5, false), m.navTargets(7, 5, false)],
  wheel: [m.wheelPixels(3, 0, 600), m.wheelPixels(3, 1, 600), m.wheelPixels(-1, 2, 600)],
}));
""")
        self.assertEqual(out["persistent"], [False, False, False, False, False, False, True, True, True, False, False])
        self.assertEqual(out["hit"], [0, 0, 8, 13, 14, 35, 36, 40, 40, 0, 0])
        self.assertEqual(out["nav"], [False, False, True, True])
        self.assertEqual(out["targets"], [
            {"prev": None, "next": 1}, {"prev": 1, "next": 3}, {"prev": 3, "next": None},
            {"prev": 1, "next": None}, {"prev": None, "next": None}, {"prev": None, "next": None},
        ])
        self.assertEqual(out["wheel"], [3, 48, -600])


if __name__ == "__main__":
    unittest.main()
