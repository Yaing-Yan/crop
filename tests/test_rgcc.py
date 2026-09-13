#!/usr/bin/env python3
"""CROP 第 3 阶段自测：rGCC（C 子集 → 可 100% 翻译的 .bin）。"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from crop.gadget import scan                                      # noqa: E402
from crop.interp import Options, translate                        # noqa: E402
from crop.nxu16 import decode as _dec                             # noqa: E402
from crop.rgcc import Backend, RgccError, compile_source, verify_translatable  # noqa: E402
from crop.rom import RomImage                                     # noqa: E402

MODEL = os.path.expanduser("~/casioemu/models/fx991cnxfVirtual")

DEMO = """
unsigned char flag;
unsigned char dot;

void main(void) {
    flag = 1;
    dot = 0;
    while (1) {
        flag = 0;
        dot = 255;
    }
}
"""


@unittest.skipUnless(os.path.isdir(MODEL), "缺少参考机型")
class TestRgcc(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = RomImage.auto(MODEL)
        cls.db = scan(cls.rom, max_insns=8)
        cls.be = Backend(cls.db)

    def test_compiles_and_is_fully_translatable(self):
        """.bin 里每条指令都必须有等价 gadget（不然"覆盖率 100%"是假的）。"""
        cu = compile_source(DEMO, self.be, data_base=0xD0F5)
        verify_translatable(cu.code, self.db)          # 不抛异常即通过
        res = translate(self.db, cu.code, Options())
        self.assertEqual(res.stats["unsupported"], 0, res.dsl)
        # 4 次写内存 = 8 条指令，加 1 条回跳 = 9 条；内联数据不计
        self.assertEqual(res.stats["l1"] + res.stats["l2"] + res.stats["jump"], 9)
        self.assertEqual(res.warnings, [])

    def test_write_byte_imm_uses_rop_idiom(self):
        """`x = 1;` → POP XR0(内联数据: 地址+值) + ST R2,[ER0]（RopIDE 的 pop-xr0/byte-set）。"""
        cu = compile_source("unsigned char x;\nvoid main(void){ x = 1; }\n",
                           self.be, data_base=0xD0F5)
        self.assertEqual(cu.code, bytes.fromhex("2EF0" + "F5D00100" + "0192"))
        res = translate(self.db, cu.code, Options())
        kinds = [d.kind for d in res.decisions]
        self.assertEqual(kinds, ["pop_block", "block"])
        self.assertEqual(res.stats["unsupported"], 0)

    def test_loop_becomes_pivot_jump(self):
        cu = compile_source(DEMO, self.be, data_base=0xD0F5)
        res = translate(self.db, cu.code, Options())
        jumps = [d for d in res.decisions if d.kind == "jump"]
        self.assertEqual(len(jumps), 1)
        # 跳转用的枢轴必须是"能续链"的（以 POP PC 结尾）
        pivot = self.db.rebuild(jumps[0].addr)
        self.assertEqual(pivot.insns[0].text, "POP ER14")

    def test_constant_expressions_are_folded(self):
        """常量表达式在编译期求值：优先级、括号、一元、位运算。"""
        src = ("unsigned char a;\nunsigned char b;\nunsigned char c;\n"
               "void main(void){ a = 2 * (3 + 4); b = 100 - 1; c = (1 << 5) | 3; }\n")
        cu = compile_source(src, self.be, data_base=0xEA40)
        self.assertEqual([cu.vars[v].addr for v in ("a", "b", "c")], [0xEA40, 0xEA41, 0xEA42])
        # 三个地址相邻的常量赋值会被合并成一次**块写**（链更短），
        # 所以这里只断言"值确实在 .bin 里"，不绑具体偏移。
        for v in (14, 99, 0x23):
            self.assertIn(bytes([v]), cu.code)
        res = translate(self.db, cu.code, Options())
        self.assertEqual(res.stats["unsupported"], 0)

    def test_single_constant_uses_pop_and_store(self):
        """单个常量赋值仍走「POP 宽寄存器 + 内联 4 字节 + ST」这条最短路径。"""
        cu = compile_source("unsigned char a;\nvoid main(void){ a = 14; }\n",
                            self.be, data_base=0xEA40)
        self.assertEqual(cu.code[:2], self.be.const_pop)
        self.assertEqual(cu.code[4], 14)                 # 内联数据里的值
        self.assertIn(self.be.const_store, cu.code)
        res = translate(self.db, cu.code, Options())
        self.assertEqual(res.stats["unsupported"], 0)

    def test_variable_copy_uses_bp_switch(self):
        """`x = y;` 走运行时内存搬运：POP ER12(基址) + L R7,-10h[BP] + … + ST。"""
        src = ("unsigned char x;\nunsigned char y;\nvoid main(void){ x = y; }\n")
        cu = compile_source(src, self.be, data_base=0xEA40)
        code = cu.code
        self.assertEqual(code[:2], bytes.fromhex("1EFC"))            # POP ER12
        self.assertEqual(code[4:6], bytes.fromhex("30D7"))            # L R7,-10h[BP]
        self.assertEqual(code[6:8], bytes.fromhex("1EFC"))            # POP ER12（切到目的变量）
        self.assertEqual(code[10:12], bytes.fromhex("B0D7"))          # ST R7,-10h[BP]
        self.assertEqual((code[2] | (code[3] << 8)), 0xEA41 + 0x10)   # 源 y 的基址
        self.assertEqual((code[8] | (code[9] << 8)), 0xEA40 + 0x10)   # 目的 x 的基址
        res = translate(self.db, cu.code, Options())
        self.assertEqual(res.stats["unsupported"], 0, res.dsl)

    def test_runtime_arithmetic_fails_loudly(self):
        for bad in ("unsigned char x;\nvoid main(void){ x = x + 1; }\n",
                    "unsigned char x;\nunsigned char y;\nvoid main(void){ x = y * 2; }\n"):
            with self.assertRaises(RgccError) as cm:
                compile_source(bad, self.be, data_base=0xEA40)
            self.assertIn("暂不支持", str(cm.exception))

    def test_unsupported_constructs_report_clearly(self):
        for src, frag in [
            ("unsigned char x;\nvoid main(void){ if (1) { x = 1; } }\n", "条件分支"),
            ("unsigned char x;\nvoid main(void){ while (x) { x = 1; } }\n", "while"),
            ("unsigned char x;\nvoid main(void){ x = 300; }\n", "0..255"),
            ("unsigned char x;\nvoid main(void){ x = 300; }\n", "0..255"),
            ("unsigned char x;\nvoid main(void){ y = 1; }\n", "未声明"),
        ]:
            with self.assertRaises(RgccError) as cm:
                compile_source(src, self.be)
            self.assertIn(frag, str(cm.exception))

    def test_generated_bin_has_no_chain_breaking_instructions(self):
        cu = compile_source(DEMO, self.be, data_base=0xD0F5)
        a = 0
        while a + 2 <= len(cu.code):
            ins = _dec.decode_at(cu.code, a)
            if ins.cls == "pop_data":
                a += ins.size + ins.sp_delta
                continue
            self.assertIn(ins.cls, ("normal", "jump"), ins.text)
            self.assertEqual(ins.sp_delta, 0, ins.text)
            a += ins.size


if __name__ == "__main__":
    unittest.main(verbosity=2)
