#!/usr/bin/env python3
"""CROP 第 4b 步自测：引导（launcher）字节与实测配方一致。"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from crop.launcher import (LAUNCHER_GADGET_REAL, launcher_bytes, parse_conf)  # noqa: E402

MODEL = os.path.expanduser("~/casioemu/models/fx991cnxfVirtual")
CONF = os.path.join(ROOT, "launcher.conf")


class TestLauncher(unittest.TestCase):
    def test_verified_golden_bytes(self):
        """真机实测通过的那 7 个字节：FD 24 D0 E9 8F 23 42（left_addr=E9E0）。"""
        self.assertEqual(launcher_bytes(0xE9E0), bytes.fromhex("FD24D0E98F2342"))

    def test_address_field_is_left_minus_10h(self):
        for left in (0xE9E0, 0xE000, 0xD710):
            b = launcher_bytes(left)
            self.assertEqual(b[:2], bytes([0xFD, 0x24]))
            self.assertEqual(b[2] | (b[3] << 8), (left - 0x10) & 0xFFFF)
            self.assertEqual(b[4:], bytes([0x8F, 0x23, 0x42]))

    def test_config_roundtrip(self):
        cfg = parse_conf(open(CONF).read())
        self.assertEqual(cfg.left_addr, 0xE9E0)
        self.assertEqual(cfg.launcher_addr, 0xD180)
        self.assertEqual(cfg.launcher(), launcher_bytes(0xE9E0))


@unittest.skipUnless(os.path.isdir(MODEL), "缺少参考机型")
class TestLauncherGadget(unittest.TestCase):
    def test_gadget_at_verified_entry(self):
        """0x2238E 必须正好是 MOV SP,ER14 ; POP QR8 ; POP QR0 ; POP PC。"""
        from crop.nxu16 import decode as _dec
        from crop.rom import RomImage

        rom = RomImage.auto(MODEL)
        want = ["MOV SP, ER14", "POP QR8", "POP QR0", "POP PC"]
        a = LAUNCHER_GADGET_REAL
        got = []
        for _ in range(len(want)):
            ins = _dec.decode_at(rom.space, a)
            self.assertIsNotNone(ins)
            got.append(ins.text)
            a += ins.size
        self.assertEqual(got, want)
        # 入口必须偶数（PC 按 2 字节对齐）；launcher 里写的 PC 值是它 +1
        self.assertEqual(LAUNCHER_GADGET_REAL % 2, 0)
        self.assertEqual(launcher_bytes(0xE9E0)[4:6], bytes([0x8F, 0x23]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
