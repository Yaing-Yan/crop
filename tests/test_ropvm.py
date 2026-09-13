#!/usr/bin/env python3
"""CROP 第 4 阶段自测：ROP 链在真 ROM 模拟器上端到端执行。

这是"翻译正确性"的最终证据：把 rgcc 产出的 .bin 翻译成链，写进 RAM，
用真 ROM 跑，看 RAM 终态是否等于 C 程序的语义。

依赖 <LIFTED_PY>（缺失则跳过）。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from crop.models import model_dir   # noqa: E402


def _m(which):
    try:
        return model_dir(which)
    except LookupError:
        return ""

from crop.chain import encode_gadget                        # noqa: E402
from crop.gadget import scan                                # noqa: E402
from crop.interp import Options, translate                  # noqa: E402
from crop.rgcc import Backend, compile_source               # noqa: E402
from crop.rom import RomImage                               # noqa: E402
from crop.ropvm import BRK_ADDR, LIFTED_PATH, run_rop_chain  # noqa: E402

def _m(which):
    try:
        return model_dir(which)
    except LookupError:
        return ""


MODEL = _m("verf")


@unittest.skipUnless(os.path.isdir(MODEL) and os.path.isfile(LIFTED_PATH),
                     "缺少机型 ROM 或 lifted 模拟器")
class TestRopExecution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = RomImage.auto(MODEL)
        cls.db = scan(cls.rom, max_insns=8)
        cls.be = Backend(cls.db)

    def _chain(self, src, base=0xD180):
        cu = compile_source(src, self.be, data_base=base)
        res = translate(self.db, cu.code, Options())
        return res, res.data + encode_gadget(BRK_ADDR)

    def test_straight_line_program_writes_expected_ram(self):
        """`aa = 7; bb = 9;` → 链在真 ROM 上执行后 D180=7, D181=9。"""
        res, chain = self._chain("unsigned char aa;\nunsigned char bb;\n"
                                 "void main(void){ aa = 7; bb = 9; }\n")
        self.assertEqual(res.stats["unsupported"], 0)
        run = run_rop_chain(chain, watch=[0xD180, 0xD181], max_steps=5000)
        self.assertTrue(run.stopped, "链没有干净停机：%s" % run.reason)
        self.assertEqual(run.reason, "哨兵 BRK")
        self.assertEqual(run.ram_at(0xD180), 7)
        self.assertEqual(run.ram_at(0xD181), 9)

    def test_pop_pc_is_four_bytes(self):
        """对照实验：POP PC 必须按 4 字节槽；按 3 字节会立刻跑飞。"""
        _res, chain = self._chain("unsigned char aa;\nunsigned char bb;\n"
                                  "void main(void){ aa = 7; bb = 9; }\n")
        ok = run_rop_chain(chain, watch=[0xD180, 0xD181], max_steps=5000, pop_pc_bytes=4)
        bad = run_rop_chain(chain, watch=[0xD180, 0xD181], max_steps=5000, pop_pc_bytes=3)
        self.assertEqual(ok.ram_at(0xD180), 7)
        self.assertNotEqual(bad.ram_at(0xD180), 7,
                            "3 字节模型竟然也对了，说明实验没能区分两种约定")

    def test_loop_pivot_keeps_the_chain_alive(self):
        """`while(1)` 的回跳枢轴必须真的循环：跑到步数上限而不停机，
        且 RAM 里留下的是循环体的值（flag=0, dot=255）。"""
        src = ("unsigned char flag;\nunsigned char dot;\n"
               "void main(void){ flag = 1; dot = 0;\n"
               "  while (1) { flag = 0; dot = 255; } }\n")
        res, chain = self._chain(src)
        self.assertEqual(res.stats["unsupported"], 0)
        self.assertEqual(len([d for d in res.decisions if d.kind == "jump"]), 1)
        run = run_rop_chain(chain, watch=[0xD180, 0xD181], max_steps=2000)
        self.assertFalse(run.stopped, "死循环不该停机，实际：%s" % run.reason)
        self.assertEqual(run.ram_at(0xD180), 0)
        self.assertEqual(run.ram_at(0xD181), 255)


if __name__ == "__main__":
    unittest.main(verbosity=2)
