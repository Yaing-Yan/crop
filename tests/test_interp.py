#!/usr/bin/env python3
"""CROP 第 2 阶段自测：链编码 / RopIDE DSL / ROP 解释器。

跑法：  python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from crop.chain import LEFT, RIGHT, ChainBuilder, encode_gadget, encode_value  # noqa: E402
from crop.gadget import scan                                                  # noqa: E402
from crop.interp import Options, translate                                    # noqa: E402
from crop.nxu16 import decode as _dec                                         # noqa: E402
from crop.rom import RomImage                                                 # noqa: E402
from crop.ropdsl import compile_rop_dsl, load_rop_file, tokenize              # noqa: E402

MODEL = os.path.expanduser("~/casioemu/models/fx991cnxfVirtual")
ROPFILE = os.path.expanduser("~/Downloads/Pixel Editor 𝑷𝒓𝒐 - v1.1.rop")

# 项目期望.md 里给出的 Pixel Editor Pro v1.1 编译产物前 64 字节（真值）
PIXEL_TRUTH = bytes.fromhex("".join("""
34 61 01 00 F5 D0 01 00 D2 03 02 00 46 38 01 00
01 01 1E D3 D4 BA 02 00 AE 8F 00 00 EE 00 0C EA
EC D3 E0 D3 C4 52 01 00 8A CD 00 00 34 61 01 00
D6 D0 FA EA C8 03 02 00 7C 93 00 00 72 87 00 00
""".split()))


class TestChainEncoding(unittest.TestCase):
    def test_gadget_slot_encoding(self):
        """4 字节槽 = [PC_lo][PC_hi][CSR_段][00/30]；RopIDE 的左右两种形式。"""
        self.assertEqual(encode_gadget(0x16134), bytes.fromhex("34610100"))   # pop-xr0
        self.assertEqual(encode_gadget(0x203D2), bytes.fromhex("D2030200"))   # byte-set
        self.assertEqual(encode_gadget(0x2BAD4), bytes.fromhex("D4BA0200"))   # rt-fix
        self.assertEqual(encode_gadget(0x0937C), bytes.fromhex("7C930000"))   # screen-on
        # 左式：CSR 段 3X 30，低字节 00→01
        self.assertEqual(encode_gadget(0x16134, LEFT), bytes.fromhex("34613130"))
        self.assertEqual(encode_gadget(0x10000, LEFT), bytes.fromhex("01003130"))  # 00→01 的怪癖

    def test_value_encoding(self):
        self.assertEqual(encode_value(0x1234), bytes.fromhex("3412"))
        self.assertEqual(encode_value(-2), bytes.fromhex("FEFF"))

    def test_forward_reference_anchor(self):
        cb = ChainBuilder(left_base=0xE9E0, right_base=0xD3C0)
        cb.value_expr("$later - 2")       # 前向引用
        cb.raw(b"\xAA\xBB\xCC\xDD")
        cb.anchor("later", LEFT)
        data = cb.finalize()
        # later = E9E0 + 6；值 = later - 2
        self.assertEqual(data[:2], encode_value(0xE9E0 + 6 - 2))
        self.assertEqual(data[2:], b"\xAA\xBB\xCC\xDD")


class TestRopDsl(unittest.TestCase):
    def test_tokenizer(self):
        toks = [t.kind for t in tokenize('$a = 1;\n#pop-er0; [x] <-y> zz // c')
                if t.kind != "other"]
        self.assertEqual(toks, ["const", "gadget", "value", "anchor"])

    @unittest.skipUnless(os.path.isfile(ROPFILE), "缺少参考 .rop")
    def test_real_pixel_editor_first_64_bytes(self):
        """用真值对拍：RopIDE 的 DSL → 我们编译出的字节必须逐字节相同。"""
        src = load_rop_file(ROPFILE)
        data, warns, consts = compile_rop_dsl(src)
        self.assertEqual(warns, [])
        self.assertEqual(data[:len(PIXEL_TRUTH)], PIXEL_TRUTH)
        # 锚点/常量也要对
        self.assertEqual(consts["init"], 0xE9E0)
        self.assertEqual(consts["launcher"], 0xEAFA)
        self.assertEqual(consts["main"], 0xD3EC)
        self.assertEqual(consts["main-s"], 0xEA0C)


@unittest.skipUnless(os.path.isdir(MODEL), "缺少参考机型")
class TestInterpreter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = RomImage.auto(MODEL)
        cls.db = scan(cls.rom, max_insns=8)

    def test_l1_blocks_are_real_rom_gadgets(self):
        """每个 L1 块：ROM 里该地址逐字节相同，且后面紧跟 POP PC。"""
        # 从索引里挑几段真实块拼成 .bin
        picks = sorted(self.db.index.items(), key=lambda kv: -len(kv[0]))[:5]
        code = b"".join(k for k, _ in picks)
        res = translate(self.db, code, Options())
        blocks = [d for d in res.decisions if d.kind == "block"]
        self.assertTrue(blocks)
        for d in blocks:
            blk = code[d.off:d.off + d.size]
            self.assertEqual(bytes(self.rom.space[d.addr:d.addr + len(blk)]), blk,
                             "块 %s 在 @%05X 处不匹配" % (blk.hex(), d.addr))
            self.assertEqual(bytes(self.rom.space[d.addr + len(blk):d.addr + len(blk) + 2]),
                             b"\x8e\xf2", "@%05X 后面不是 POP PC" % d.addr)

    def test_l2_mov_imm_uses_pop_gadget_with_chain_payload(self):
        """`MOV R0,#224` 没有现成块，应转义成 POP R0 + 链上 2 字节（十进制解析！）。"""
        res = translate(self.db, bytes.fromhex("E000"), Options())
        ev = [d for d in res.decisions if d.kind == "pop_imm"]
        self.assertEqual(len(ev), 1, res.dsl)
        self.assertIn("224", ev[0].detail)
        # 链上：POP R0 的 gadget 槽 + 值（小端 2 字节，低字节 = 224）
        addr = ev[0].addr
        self.assertEqual(res.data[:4], encode_gadget(addr))
        self.assertEqual(res.data[4:6], bytes([224, 0]))
        # 该地址确实是 `POP R0; POP PC`
        g = self.db.rebuild(addr)
        self.assertEqual(g.insns[0].text, "POP R0")
        self.assertEqual(g.insns[-1].text, "POP PC")

    def test_l3_backward_jump_uses_clean_pivot(self):
        """内部 `B` 跳转：用 POP ERn + 干净枢轴实现，链地址必须是左基准。"""
        code = bytes.fromhex("0000E00000F00000")     # MOV R0,#0 ; MOV R0,#224 ; B 0:0000
        res = translate(self.db, code, Options(pivot_side=LEFT))
        jumps = [d for d in res.decisions if d.kind == "jump"]
        self.assertEqual(len(jumps), 1, res.dsl)
        loader = self.db.rebuild(jumps[0].addr)
        self.assertEqual(loader.insns[0].text, "POP ER14")   # 取数原语：把跳转地址装进 ER14
        # 链里必须出现枢轴 gadget，且枢轴"跳过字节数"与表达式 $L0000 - skip 一致
        # 交叉校验（直接构造 vs DSL 回编译）已在 translate 内完成
        self.assertEqual([w for w in res.warnings if "不一致" in w], [])
        # 链里必须出现一个"干净枢轴"（MOV SP, ERn 且以 POP PC 结尾）
        gmap = res.rop.gadget_map()
        pivots = [g.addr for g in gmap.values()
                  if any(i.cls == "sp_set" for i in self.db.rebuild(g.addr).insns)]
        self.assertTrue(pivots, "链里没有栈枢轴：%s" % res.dsl)
        self.assertTrue(all(self.db.rebuild(a).term == "pop_pc" for a in pivots))

    def test_dsl_and_bytes_agree(self):
        code = bytes.fromhex("0000E00000F00000")
        res = translate(self.db, code, Options())
        data2, warns, _ = compile_rop_dsl(res.rop, strict=False)
        self.assertEqual(warns, [])
        self.assertEqual(data2, res.data)

    def test_unsupported_is_reported_not_guessed(self):
        """`PUSH LR` 这类会破坏链的指令必须明确报不支持，而不是硬翻。"""
        res = translate(self.db, bytes.fromhex("CEF8"), Options())
        self.assertEqual(res.stats["unsupported"], 1)
        self.assertEqual(res.decisions[-1].kind, "unsupported")


if __name__ == "__main__":
    unittest.main(verbosity=2)
