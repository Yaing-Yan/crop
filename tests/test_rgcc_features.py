#!/usr/bin/env python3
"""A2/A3/A5 自测：函数（内联）、数组、结构体。

两条腿走路：
* **结构**：编出来的机器码必须每条都有等价 gadget（`verify_translatable`）；
* **语义**：把链放进真 ROM 的 lifted 模拟器跑一遍，检查 RAM 终态等于 C 程序的语义
  （`crop/ropvm.py`；缺 ROM/模拟器就跳过）。
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

from crop.chain import encode_gadget                       # noqa: E402
from crop.gadget import scan                               # noqa: E402
from crop.interp import Options, translate                  # noqa: E402
from crop.labels import LabelTable, load_labels             # noqa: E402
from crop.libabi import Library                             # noqa: E402
from crop.preproc import preprocess                         # noqa: E402
from crop.rgcc import Backend, RgccError, compile_source     # noqa: E402
from crop.rom import RomImage                               # noqa: E402
from crop.ropvm import BRK_ADDR, LIFTED_PATH, run_rop_chain  # noqa: E402

def _m(which):
    try:
        return model_dir(which)
    except LookupError:
        return ""


MODEL = _m("verf")
HAVE_VM = os.path.isdir(MODEL) and os.path.isfile(LIFTED_PATH)


@unittest.skipUnless(os.path.isdir(MODEL), "缺少 VerF ROM")
class TestFeatures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = RomImage.auto(MODEL)
        cls.db = scan(cls.rom, max_insns=8)
        cls.be = Backend(cls.db)
        cls.lib = Library(cls.be, cls.rom,
                          LabelTable(load_labels(os.path.join(ROOT, "labels.conf")), cls.rom))

    def c(self, body: str, base: int = 0xD700):
        src = preprocess(body, [os.path.join(ROOT, "include")], origin="<test>")[0]
        return compile_source(src, self.be, data_base=base, lib=self.lib)

    # ------------------------------------------------------------- A3 数组
    def test_array_constant_index(self):
        cu = self.c("unsigned char a[4] = {1,2,3,4};\nunsigned char t;\n"
                    "void main(void){ t = a[2]; a[3] = 7; while(1){} }\n")
        a = cu.vars["a"]
        self.assertEqual(a.kind, "array")
        self.assertEqual(a.count, 4)
        self.assertEqual(cu.initials[a.addr], bytes([1, 2, 3, 4]))
        self.assertIn(7, cu.code)                       # a[3] = 7 编进去了

    def test_array_string_initializer(self):
        cu = self.c('unsigned char msg[8] = "HELLO";\nvoid main(void){ while(1){} }\n')
        self.assertEqual(cu.initials[cu.vars["msg"].addr], b"HELLO\x00")

    def test_variable_index_is_supported_now(self):
        """A4：v[i]（i 是变量）现在可以编译。"""
        cu = self.c("unsigned char v[4];\nunsigned char i;\nunsigned char t;\n"
                    "void main(void){ i = 2; t = v[i]; v[1] = t; }\n")
        self.assertGreater(len(cu.code), 0)

    def test_index_out_of_range(self):
        with self.assertRaises(RgccError):
            self.c("unsigned char a[2];\nvoid main(void){ a[5] = 1; while(1){} }\n")

    # ----------------------------------------------------------- A5 结构体
    def test_struct_fields(self):
        cu = self.c("struct point { unsigned char x; unsigned char y; };\n"
                    "struct point p;\nunsigned char t;\n"
                    "void main(void){ p.x = 5; p.y = p.x; t = p.y; while(1){} }\n")
        p = cu.vars["p"]
        self.assertEqual(p.kind, "struct")
        self.assertEqual(p.fields, {"x": 0, "y": 1})
        self.assertEqual(cu.structs["point"], {"x": 0, "y": 1})

    def test_struct_array(self):
        cu = self.c("struct pair { unsigned char a; unsigned char b; };\n"
                    "struct pair v[3];\nunsigned char t;\n"
                    "void main(void){ v[2].b = 9; t = v[2].b; while(1){} }\n")
        v = cu.vars["v"]
        self.assertEqual(v.size, 6)

    # --------------------------------------------------------------- A2 函数
    def test_function_with_params_and_return(self):
        cu = self.c("unsigned char f(unsigned char v) { return 7; }\n"
                    "unsigned char t;\n"
                    "void main(void){ t = f(3); while(1){} }\n")
        f = cu.funcs["f"]
        self.assertEqual(f.ret, "u8")
        self.assertEqual([n for n, _ in f.params], ["v"])
        # 调用点：先写形参槽，再展开函数体，最后把返回值拷到 t
        self.assertIn(f.ret_slot, range(0xD700, 0xD720))
        self.assertNotEqual(f.ret_slot, cu.vars["t"].addr)
        self.assertLess(f.ret_slot, cu.vars["t"].addr, "返回值槽应当在 t 之前分配")

    def test_void_function_calling_a_library_routine(self):
        cu = self.c('#include <rstdio.h>\nunsigned char msg[6] = "HI";\n'
                    "void row(unsigned char y) { rprint(FONT_SMALL, y, msg); }\n"
                    "void main(void){ row(3); while(1){} }\n")
        self.assertEqual(cu.funcs["row"].params, [("y", "u8")])
        res = translate(self.db, cu.code, Options(
            left_base=0xEC00, routines=tuple(self.lib.routines.values()), rt_push=self.lib.rt_push))
        self.assertEqual(res.stats["unsupported"], 0)
        self.assertIn("routine", [d.kind for d in res.decisions])

    def test_recursion_is_refused(self):
        with self.assertRaises(RgccError) as cm:
            self.c("void f(void) { f(); }\nvoid main(void){ f(); while(1){} }\n")
        self.assertIn("递归", str(cm.exception))

    def test_for_loop_is_unrolled_with_constant_index(self):
        cu = self.c("unsigned char s[4] = {1,2,3,4};\nunsigned char d[4];\n"
                    "void main(void){ for (unsigned char i = 0; i < 4; i = i + 1) { d[i] = s[i]; }"
                    " while(1){} }\n")
        d, src_ = cu.vars["d"], cu.vars["s"]
        self.assertEqual(self.be.copy_var(d.addr, src_.addr)[:2], cu.code[
            cu.code.index(self.be.copy_var(d.addr, src_.addr)):][:2])

    def test_rstring_header_compiles(self):
        cu = self.c('#include <rstring.h>\n#include <rstdlib.h>\n'
                    'unsigned char msg[8];\nunsigned char t;\n'
                    'void main(void){ rcopy4(msg, "CROP"); rfill4(msg, 32);'
                    ' rput16(msg, 65, 66); t = msg[0]; rhalt(); }\n')
        self.assertIn("rcopy4", cu.funcs)
        self.assertIn("rfill4", cu.funcs)
        # rhalt 现在是**库例程**（冻结到 ROM 之外），不再是被内联的 C 函数
        self.assertNotIn("rhalt", cu.funcs)
        res = translate(self.db, cu.code, Options(left_base=0xEC00))
        self.assertEqual(res.stats["unsupported"], 0, res.report())

    def test_for_with_nonconstant_bound_is_refused(self):
        with self.assertRaises(RgccError):
            self.c("unsigned char n;\nunsigned char d[4];\n"
                   "void main(void){ for (unsigned char i = 0; i < n; i = i + 1) { d[i] = 1; }"
                   " while(1){} }\n")

    def test_nonconstant_expression_still_refused(self):
        with self.assertRaises(RgccError):
            self.c("unsigned char a;\nunsigned char b;\n"
                   "void main(void){ a = b + 1; while(1){} }\n")


@unittest.skipUnless(HAVE_VM, "缺少机型 ROM 或 lifted 模拟器")
class TestFeatureSemantics(unittest.TestCase):
    """在真 ROM 的 lifted 模拟器里跑链，检查 RAM 终态。"""

    @classmethod
    def setUpClass(cls):
        cls.rom = RomImage.auto(MODEL)
        cls.db = scan(cls.rom, max_insns=8)
        cls.be = Backend(cls.db)

    def _run(self, src: str, watch, base: int = 0xD700):
        cu = compile_source(preprocess(src, [os.path.join(ROOT, "include")],
                                       origin="<test>")[0],
                            self.be, data_base=base)
        res = translate(self.db, cu.code, Options(left_base=0xEC00))
        self.assertEqual(res.stats["unsupported"], 0, res.report())
        chain = res.data + encode_gadget(BRK_ADDR)
        run = run_rop_chain(chain, left_base=0xEC00, watch=watch, max_steps=400000)
        return cu, run

    def test_arrays_structs_and_functions_end_to_end(self):
        src = ("struct point { unsigned char x; unsigned char y; };\n"
               "struct point p;\n"
               "unsigned char a[4] = {1,2,3,4};\n"
               "unsigned char t;\n"
               "unsigned char f(unsigned char v) { return 9; }\n"
               "void main(void){ t = a[2]; p.x = 5; p.y = t; t = f(1);\n"
               "                 a[0] = t; while(1){} }\n")
        probe = {"a": 0xD700, "p": 0xD704, "t": 0xD706}
        # 直接用名字取地址：先编一遍拿布局
        cu = compile_source(preprocess(src, [os.path.join(ROOT, "include")],
                                       origin="<test>")[0], self.be, data_base=0xD700)
        addrs = {k: cu.vars[k].addr for k in ("a", "p", "t")}
        watch = [addrs["a"] + i for i in range(4)] + [addrs["p"], addrs["p"] + 1, addrs["t"]]
        _cu, run = self._run(src, watch)
        self.assertEqual([run.ram_at(addrs["a"] + i) for i in range(4)], [9, 2, 3, 4],
                         "a[0] 应当被函数返回值 9 覆盖，其余保持初值")
        self.assertEqual(run.ram_at(addrs["p"]), 5, "p.x = 5")
        self.assertEqual(run.ram_at(addrs["p"] + 1), 3, "p.y = a[2] = 3")
        self.assertEqual(run.ram_at(addrs["t"]), 9, "t = f(1) = 9")


if __name__ == "__main__":
    unittest.main()
