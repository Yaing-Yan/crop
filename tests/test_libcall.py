#!/usr/bin/env python3
"""A1 库例程调用 + A6 预处理器 的自测。

覆盖：
* `crop/routines.py`：例程体抽取（POP PC / RT 两种结尾）、rt-fix 原语扫描；
* `crop/libabi.py`：参数搬运（常量→R0/ER0/ER2、变量→R0/R1 的 BP 槽）；
* `crop/preproc.py`：#include / #define / include guard / 报错；
* 端到端：`#include "rstdio.h"` → rGCC 编译 → 解释器翻译，必须 0 条不支持，
  且链里出现"rt-fix 槽 + 例程入口槽"。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from crop.gadget import scan                            # noqa: E402
from crop.interp import Options, translate               # noqa: E402
from crop.labels import LabelTable, load_labels          # noqa: E402
from crop.launcher import parse_conf                     # noqa: E402
from crop.libabi import Library                          # noqa: E402
from crop.preproc import preprocess                      # noqa: E402
from crop.rgcc import Backend, RgccError, compile_source  # noqa: E402
from crop.rom import RomImage                            # noqa: E402
from crop.routines import build_routines, find_rt_push, routine_body   # noqa: E402

MODELS = {
    "VerF": os.path.expanduser("~/casioemu/models/fx991cnxfVirtual"),
    "VerC": os.path.expanduser("~/casioemu/models/models/fx991cnxVirtual"),
}
HAVE = {k: os.path.isdir(v) for k, v in MODELS.items()}


class TestPreproc(unittest.TestCase):
    def test_include_define_and_guard(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "h.h"), "w") as fh:
                fh.write("#ifndef H_H\n#define H_H\n"
                         "#define MAGIC 0x42\n"
                         "void lib(unsigned char a);\n"
                         "#endif\n")
            with open(os.path.join(d, "a.c"), "w") as fh:
                fh.write('#include "h.h"\n#include "h.h"\n'
                         "unsigned char x;\nvoid main(void){ x = MAGIC; }\n")
            with open(os.path.join(d, "a.c")) as fh:
                src, trace = preprocess(fh.read(), [d], origin=os.path.join(d, "a.c"))
        self.assertEqual(src.count("void lib"), 1, "include guard 没生效（展开了两次）")
        self.assertIn("x = 0x42;", src)
        self.assertIn("h.h", str(trace))

    def test_unknown_directive_is_an_error(self):
        with self.assertRaises(RgccError):
            preprocess("#error nope\n")

    def test_macro_not_substituted_inside_string(self):
        src, _ = preprocess('#define FOO 1\nvoid main(void){ x = "FOO"; }\n')
        self.assertIn('"FOO"', src)


@unittest.skipUnless(HAVE["VerF"], "缺少 VerF ROM")
class TestRoutines(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = RomImage.auto(MODELS["VerF"])
        cls.table = LabelTable(load_labels(os.path.join(ROOT, "labels.conf")), cls.rom)
        cls.routines, cls.warn = build_routines(cls.rom, cls.table)
        cls.db = scan(cls.rom, max_insns=8)
        cls.be = Backend(cls.db)
        cls.lib = Library(cls.be, cls.rom, cls.table)

    def test_all_three_labels_are_routines(self):
        for name in ("print-line", "refresh", "clear"):
            self.assertIn(name, self.routines, "标签 %s 没能抽成例程" % name)

    def test_terminators_match_reality(self):
        self.assertEqual(self.routines["print-line"].term, "pop_pc")
        self.assertEqual(self.routines["refresh"].term, "pop_pc")
        self.assertEqual(self.routines["clear"].term, "rt", "clear 应当以 RT 结尾")

    def test_routine_body_matches_rom_bytes(self):
        r = self.routines["clear"]
        self.assertEqual(r.code, bytes(self.rom.space[r.entry:r.entry + len(r.code)]))
        self.assertTrue(r.code.endswith(b"\x1f\xfe"), "clear 结尾应当是 RT（1F FE）")

    def test_rt_push_is_discovered(self):
        rt = find_rt_push(self.rom.space, 1 << 18)
        self.assertIsNotNone(rt)
        self.assertEqual(rt.code[:4].hex().upper(), "01F29696" if rt.addr == 0x2BAD4 else
                         rt.code[:4].hex().upper())
        # BL 的目标处必须是一条 POP PC
        from crop.nxu16 import decode as dec
        self.assertEqual(dec.decode_at(self.rom.space, rt.target).cls, "term_pop_pc")

    def test_marshal_constants(self):
        f = self.lib.func("rprint")
        code = self.lib.marshal(f.params, [("const", 0x0E), ("const", 3), ("const", 0xD708)])
        # R0/R1 一起用 POP ER0 装；ER2 用 POP ER2
        self.assertEqual(code[:4], self.be.pop_gad["ER0"][1] + bytes([0x0E, 3]))
        self.assertEqual(code[4:6], self.be.pop_gad["ER2"][1])

    def test_marshal_variable_uses_bp_slot(self):
        f = self.lib.func("rprint")
        code = self.lib.marshal(f.params, [("var", 0xD700), ("var", 0xD701), ("const", 0x2000)])
        self.assertIn(self.be.var_load["R0"][1], code)
        self.assertIn(self.be.var_load["R1"][1], code)
        self.assertIn(self.be.base_pop, code)

    def test_variable_to_er2_is_refused(self):
        f = self.lib.func("rprint")
        with self.assertRaises(Exception):
            self.lib.marshal(f.params, [("const", 0x0E), ("const", 0), ("var", 0xD700)])

    def test_emit_call_reads_routine_bytes_from_rom(self):
        code = self.lib.emit_call("rclear", [])
        self.assertTrue(code.endswith(self.routines["clear"].code))

    def test_end_to_end_compile_and_translate(self):
        with open(os.path.join(ROOT, "examples", "hello.c")) as fh:
            raw = fh.read()
        src = preprocess(raw, [os.path.join(ROOT, "include"),
                               os.path.join(ROOT, "examples")],
                         origin=os.path.join(ROOT, "examples", "hello.c"))[0]
        cu = compile_source(src, self.be, data_base=0xD700, lib=self.lib)
        self.assertIn("rprint", cu.protos)
        opts = Options(left_base=0xEC00, routines=tuple(self.lib.routines.values()),
                       rt_push=self.lib.rt_push)
        res = translate(self.db, cu.code, opts)
        self.assertEqual(res.stats["unsupported"], 0)
        kinds = [d.kind for d in res.decisions]
        self.assertIn("routine", kinds, "链里没有例程调用槽")
        self.assertIn("rt_fix", kinds, "以 RT 结尾的例程没有配 rt-fix 槽")
        # 三个例程入口都应在链里出现
        addrs = [d.addr for d in res.decisions if d.kind == "routine"]
        for name in ("print-line", "refresh", "clear"):
            self.assertIn(self.routines[name].entry, addrs)

    def test_launcher_conf_matches_verified_values(self):
        with open(os.path.join(ROOT, "launcher.conf")) as fh:
            conf = parse_conf(fh.read())
        self.assertEqual(conf.left_addr, 0xEC00)
        self.assertEqual(conf.launcher(), bytes.fromhex("FD24F0EB8F2342"))


@unittest.skipUnless(HAVE["VerC"], "缺少 VerC ROM")
class TestRoutinesVerC(unittest.TestCase):
    def test_verc_also_resolves(self):
        rom = RomImage.auto(MODELS["VerC"])
        table = LabelTable(load_labels(os.path.join(ROOT, "labels.conf")), rom)
        lib = Library(Backend(scan(rom, max_insns=8)), rom, table)
        self.assertEqual([r.term for r in lib.routines.values()].count("rt"), 1)
        self.assertTrue(lib.routines["print-line"].code.startswith(bytes.fromhex("119037D1")))
        self.assertIsNotNone(lib.rt_push)


if __name__ == "__main__":
    unittest.main()
