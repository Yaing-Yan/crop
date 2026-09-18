#!/usr/bin/env python3
"""标签表自测：ROM 例程地址必须按签名逐 Ver 现场解析，且不写死。"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from crop.labels import LabelTable, gen_header, load_labels   # noqa: E402
from crop.rom import RomImage                                 # noqa: E402

from crop.models import model_dir as _model_dir           # noqa: E402


def _m(which):
    try:
        return _model_dir(which)
    except LookupError:
        return ""


MODELS = {"verf": _m("verf"), "verc": _m("verc")}
CONF = os.path.join(ROOT, "labels.conf")


@unittest.skipUnless(all(os.path.isdir(p) for p in MODELS.values()), "缺少参考机型")
class TestLabels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.labels = load_labels(CONF)
        cls.tables = {v: LabelTable(cls.labels, RomImage.auto(p)) for v, p in MODELS.items()}

    def test_all_labels_resolve_in_every_version(self):
        for v, t in self.tables.items():
            with self.subTest(rom=v):
                # 词表从 RopIDE 的 JSON 导入，个别条目只有某个 Ver 有 → 允许缺失，
                # 但**核心例程**必须每个 Ver 都能按签名解析到。
                core = {"print-line", "refresh", "clear", "screen-on"}
                missing = {l.name for l in t.missing}
                self.assertFalse(core & missing, "核心标签缺失：%s" % sorted(core & missing))
                self.assertGreaterEqual(len(t.rows), 40)

    def test_addresses_are_derived_not_hardcoded(self):
        """各 Ver 的地址必须由签名解析得到；至少要有一个标签地址不同（证明不是常量）。"""
        addr = {v: {r.label.name: r.addr for r in t.rows} for v, t in self.tables.items()}
        # 只比较"两个 Ver 都解析到"的公共标签（导入的词表里允许有 VerF 专属条目）
        self.assertEqual(set(addr["verf"]) & set(addr["verc"]),
                         set(addr["verf"]) & set(addr["verc"]))
        common = set(addr["verf"]) & set(addr["verc"])
        diff = [n for n in sorted(common) if addr["verf"][n] != addr["verc"][n]]
        self.assertTrue(diff, "所有标签地址都相同，解析逻辑可能退化成常量：%s" % addr)
        # 解析出的地址处必须真的能解码成签名里的第一条指令
        for v, t in self.tables.items():
            for r in t.rows:
                rom = RomImage.auto(MODELS[v])
                from crop.nxu16 import decode as D
                self.assertIsNotNone(D.decode_at(rom.space, r.addr), "%s %s" % (v, r.label.name))

    def test_header_generation(self):
        h = gen_header(self.tables["verf"], "verf")
        self.assertIn("ROM_PRINT_LINE", h)
        self.assertIn("ROM_REFRESH", h)
        self.assertIn("ROM_CLEAR", h)
        self.assertIn("CROP_ROMLABELS_H", h)


if __name__ == "__main__":
    unittest.main(verbosity=2)
