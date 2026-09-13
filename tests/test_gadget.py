#!/usr/bin/env python3
"""CROP 第 1 阶段自测：ISA 解码 / gadget 索引的保真性。

跑法：  python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from crop.gadget import scan, split_greedy           # noqa: E402
from crop.nxu16 import decode as _dec                # noqa: E402
from crop.rom import RomImage                        # noqa: E402

MODEL = os.path.expanduser("~/casioemu/models/fx991cnxfVirtual")
ROPFILE = os.path.expanduser("~/Downloads/Pixel Editor 𝑷𝒓𝒐 - v1.1.rop")

# RopIDE 的 VerF gadget 预设（人工挑出、真机验证过的），用来对拍我们的扫描器
ROPRIDE_KNOWN = {
    "pop-er0": 0x121A8,
    "pop-er8": 0x0C0F0,
    "pop-xr0": 0x16134,
    "pop-xr8": 0x13846,
    "pop-qr8": 0x13236,
    "pop-all": 0x22390,
    "byte-set": 0x203D2,
    "jump14-q8": 0x12D34,
    "jump[8]-e8": 0x21D36,
}


class TestDecode(unittest.TestCase):
    def test_known_instructions(self):
        """与 _disas.txt 对拍过的几条指令。"""
        rom = open(os.path.join(MODEL, "rom.bin"), "rb").read()
        cases = {
            0x16134: "POP XR0",
            0x16136: "POP PC",
            0x203D2: "ST R2, [ER0]",
            0x121A8: "POP ER0",
            0x12D34: "MOV SP, ER14",
            0x8FAE: "ST ER8, [ER10]",
            0x2BAD4: "BL 02h:09696h",
        }
        for addr, want in cases.items():
            ins = _dec.decode_at(rom, addr)
            self.assertIsNotNone(ins, "0x%05X 解码失败" % addr)
            self.assertEqual(ins.text, want, "0x%05X" % addr)

    def test_only_pure_pop_pc_is_a_clean_terminator(self):
        """0xF28E（纯 POP PC）是干净结尾；0xFF8E（POP LR,PSW,PC,EA）不是。"""
        self.assertEqual(_dec.classify_word(0xF28E).cls, "term_pop_pc")
        self.assertEqual(_dec.classify_word(0xFF8E).cls, "pop_pc_extra")

    def test_pop_pc_slot_is_4_bytes(self):
        """POP PC = POPL PC：本项目统一按"消耗 4 字节"（PC 2 + CSR 2）建模。"""
        info = _dec.classify_word(0xF28E)
        self.assertEqual(info.cls, "term_pop_pc")
        self.assertEqual(info.sp_delta, 4)

    def test_single_reg_pop_pushes_are_2_bytes(self):
        self.assertEqual(_dec.classify_word(0xF01E).sp_delta, 2)   # POP ER0
        self.assertEqual(_dec.classify_word(0xF02E).sp_delta, 4)   # POP XR0
        self.assertEqual(_dec.classify_word(0xF03E).sp_delta, 8)   # POP QR0
        self.assertLess(_dec.classify_word(0xF05E).sp_delta, 0)    # PUSH ER0 → SP 减
        self.assertEqual(_dec.classify_word(0xA1EA).cls, "sp_set")  # MOV SP, ER14


@unittest.skipUnless(os.path.isdir(MODEL), "缺少参考机型 " + MODEL)
class TestGadgetIndex(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = RomImage.auto(MODEL)
        cls.db = scan(cls.rom, max_insns=8)

    def test_every_indexed_gadget_is_byte_exact_and_terminated(self):
        """索引里的每个 (块字节, 地址)：ROM 中该处必须逐字节相同且后面紧跟 POP PC。"""
        space = self.rom.space
        bad = 0
        for code, addrs in self.db.index.items():
            for a in addrs:
                if bytes(space[a:a + len(code)]) != code:
                    bad += 1
                if bytes(space[a + len(code):a + len(code) + 2]) != b"\x8e\xf2":
                    bad += 1
        self.assertEqual(bad, 0, "有 %d 处索引与 ROM 不符" % bad)

    def test_ropide_known_gadgets(self):
        """RopIDE 手工挑出的 gadget 应被自动认出（除纯函数入口型）。"""
        single_pops = {
            "pop-er0": b"\x1e\xf0",
            "pop-xr0": b"\x2e\xf0",
            "byte-set": b"\x01\x92",
            "pop-all": b"\x3e\xf8\x3e\xf0",
        }
        for name, code in single_pops.items():
            addrs = self.db.lookup(code)
            self.assertTrue(addrs, "%s 的字节串 %s 未进入索引" % (name, code.hex()))
            self.assertIn(ROPRIDE_KNOWN[name], addrs,
                          "%s 应在索引地址里；实际 %s" % (name, ["%05X" % a for a in addrs]))

    def test_pivot_gadgets_are_flagged(self):
        for name in ("jump14-q8", "jump[8]-e8"):
            a = ROPRIDE_KNOWN[name]
            g = self.db.by_addr.get(a)
            self.assertIsNotNone(g, "%s(%05X) 未被收录" % (name, a))
            self.assertIn("pivot", g.flags, "%s 应标记为栈枢轴；flags=%s" % (name, g.flags))
            self.assertFalse(g.inline_safe)

    def test_rebuild_agrees_with_index(self):
        """rebuild() 还原出的块字节必须等于索引键。"""
        for code, addrs in list(self.db.index.items())[:400]:
            g = self.db.rebuild(addrs[0])
            self.assertEqual(g.code[:-2], code)
            self.assertEqual(g.term, "pop_pc")
            self.assertEqual(g.ninsn, len(g.insns) - 1)

    def test_split_greedy_finds_exact_blocks(self):
        """贪心分块必须只产出"ROM 里真实存在"的块。"""
        code = b"\x1e\xf0\x85\xf0\x00\x00"          # POP ER0 ; MOV ER0, ER8 ; MOV R0, #0
        blocks, left = split_greedy(self.db, code)
        self.assertTrue(blocks)
        for b in blocks:
            self.assertTrue(self.db.lookup(b.code), b.code.hex())
        covered = sum(b.size for b in blocks) + sum(i.size for i in left)
        self.assertEqual(covered, len(code))


@unittest.skipUnless(os.path.isfile(ROPFILE), "缺少参考 .rop 文件")
class TestAgainstRealRop(unittest.TestCase):
    def test_chain_slot_layout(self):
        """真实 .rop 编译产物必须能按 [PC][CSR][pad] 4 字节槽逐槽解释。"""
        doc = json.load(open(ROPFILE, encoding="utf-8"))
        gadgets = {g["name"]: int(g["addr"], 16) for g in doc["gadgets"]}
        self.assertEqual(doc["leftStartAddress"], "E9E0")

        # 文档里给出的链头 16 字节（Pixel Editor Pro 的 init 段）
        head = bytes.fromhex("34610100F5D00100D203020046380100".replace(" ", ""))
        slots = [head[i:i + 4] for i in range(0, len(head), 4)]
        pc = slots[0][0] | (slots[0][1] << 8)
        csr = slots[0][2] & 0x0F
        self.assertEqual((csr << 16) | pc, gadgets["pop-xr0"])
        pc = slots[1][0] | (slots[1][1] << 8)
        # 第 2 槽是 pop-xr0 的数据：ER0=0xD0F5, ER2=0x0001
        self.assertEqual(slots[1], bytes.fromhex("F5D00100"))
        pc = slots[2][0] | (slots[2][1] << 8)
        csr = slots[2][2] & 0x0F
        self.assertEqual((csr << 16) | pc, gadgets["byte-set"])
        pc = slots[3][0] | (slots[3][1] << 8)
        csr = slots[3][2] & 0x0F
        self.assertEqual((csr << 16) | pc, gadgets["pop-xr8"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
