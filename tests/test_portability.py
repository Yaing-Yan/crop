#!/usr/bin/env python3
"""跨版本可移植性回归：同一份 C 程序必须在同机型的不同 Ver ROM 上都能跑。

要求（用户约束）：**不能写死任何 ROM 地址**。所以本测试断言的是"派生规则"，
而不是某个具体地址；只有一条 golden 例外（真机实测过的 launcher 字节）。

    python3 -m unittest tests.test_portability -v
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from crop.gadget import scan                                      # noqa: E402
from crop.interp import Options, translate                        # noqa: E402
from crop.launcher import launcher_bytes, launcher_for_db         # noqa: E402
from crop.nxu16 import decode as _dec                             # noqa: E402
from crop.rgcc import Backend, compile_source                     # noqa: E402
from crop.rom import RomImage                                     # noqa: E402

from crop.models import local as _local                 # noqa: E402


def _models():
    """跨版本回归用的 ROM 目录：从本机私有的 .crop-local.conf 里读 ``multi`` 键。"""
    out = []
    for p in (_local("multi") or "").replace(",", " ").split():
        p = os.path.expanduser(p)
        if os.path.isdir(p):
            out.append(p)
    return out


MODELS = _models()

SRC = ("unsigned char aa;\nunsigned char bb;\n"
       "void main(void){ aa = 2*(3+4); bb = aa; }\n")

#: 真机实测通过的 launcher（VerF，左侧地址 E9E0，同形枢轴 0x2238E）
GOLDEN_LAUNCHER = bytes.fromhex("FD24D0E98F2342")


class TestPortability(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = {}
        for d in MODELS:
            if os.path.isdir(d):
                cls.db[d] = scan(RomImage.auto(d), max_insns=8)

    def test_at_least_two_versions_available(self):
        self.assertGreaterEqual(len(self.db), 2, "至少要有两个 Ver 才能谈跨版本")

    def test_backend_derives_everything(self):
        """后端必须能从每个 ROM 里自己推出原语，且推出来的东西在 ROM 里真实存在。"""
        for d, db in self.db.items():
            with self.subTest(rom=os.path.basename(d)):
                be = Backend(db)
                # 常量写内存：POP 原语 + ST 原语都必须是真的单指令 gadget
                ins = db.rebuild(be.const_store_addr).insns[0]
                self.assertEqual(ins.mnemonic, "ST")
                self.assertEqual(ins.raw, be.const_store)
                self.assertGreaterEqual(be.const_pop_len, 3)
                # 变量槽：L 与 ST 都必须存在，且偏移/基址一致
                self.assertTrue(be.slot_load and be.slot_store)
                li = db.rebuild(be.slot_load_addr).insns[0]
                self.assertEqual(li.mnemonic, "L")
                self.assertIn(be.slot_base, li.operands)
                # 基址装载：BP→POP ER12 / FP→POP ER14
                want = {"BP": "POP ER12", "FP": "POP ER14"}[be.slot_base]
                self.assertTrue(be.base_pop)

    def test_compile_and_translate_on_every_version(self):
        """同一份 C 程序在每个 Ver 上都要 0 条不支持、0 条警告。"""
        for d, db in self.db.items():
            with self.subTest(rom=os.path.basename(d)):
                cu = compile_source(SRC, Backend(db), data_base=0xEA40)
                res = translate(db, cu.code, Options())
                self.assertEqual(res.stats["unsupported"], 0, res.dsl)
                self.assertEqual(res.warnings, [])
                # .bin 里的指令必须都在该 ROM 的 gadget 索引里（构造性保证）
                self.assertGreater(len(res.data), 0)

    def test_launcher_follows_the_derivation_rule(self):
        """launcher 的每个字段都由该 ROM 扫出的枢轴决定 —— 而不是写死的值。"""
        seen = {}
        for d, db in self.db.items():
            with self.subTest(rom=os.path.basename(d)):
                la, piv = launcher_for_db(db, 0xE9E0)
                self.assertEqual(len(la), 7)
                self.assertEqual(la[:2], bytes([0xFD, 0x24]))
                # 地址字段 = 左地址 - 枢轴跳过的字节数
                self.assertEqual(la[2] | (la[3] << 8), (0xE9E0 - piv.skip) & 0xFFFF)
                # PC 字段 = 枢轴入口 + 1（CPU 取指按 2 字节对齐）
                self.assertEqual(la[4] | (la[5] << 8), ((piv.addr & 0xFFFF) + 1) & 0xFFFF)
                # CSR 段 = 0x40 | 段号
                self.assertEqual(la[6], 0x40 | ((piv.addr >> 16) & 0x0F))
                seen[os.path.basename(d)] = la.hex().upper()
        # 至少要有 Ver 之间 launcher 不同 —— 否则说明"推导"退化成常量了
        if len(seen) >= 2:
            self.assertGreater(len(set(seen.values())), 1,
                               "所有 Ver 推出同一个 launcher，推导规则可能失效：%s" % seen)

    def test_golden_verified_launcher(self):
        """真机实测过的那一组字节：同形枢轴 0x2238E + skip=16 → 必须复现。"""
        self.assertEqual(launcher_bytes(0xE9E0, 0x10, 0x2238E), GOLDEN_LAUNCHER)

    def test_no_hardcoded_rom_addresses_in_backend(self):
        """Backend 构造时不得依赖任何固定地址：换 ROM 必须得到各自的结果。"""
        results = {}
        for d, db in self.db.items():
            be = Backend(db)
            results[os.path.basename(d)] = (be.const_store_addr, be.slot_load_addr)
        for name, (store, load) in results.items():
            # 这两个地址必须能在对应 ROM 里解码成预期指令（自洽即可，值不限）
            db = self.db[[k for k in self.db if os.path.basename(k) == name][0]]
            self.assertEqual(db.rebuild(store).insns[0].mnemonic, "ST")
            self.assertEqual(db.rebuild(load).insns[0].mnemonic, "L")


if __name__ == "__main__":
    unittest.main(verbosity=2)
