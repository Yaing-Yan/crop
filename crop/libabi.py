"""库函数 ABI 表（A1）—— C 里的 ``rprint(font, row, s)`` 怎么落到 ROM 例程上。

用户要求（原话）：

> 「显示文本的不能依赖寄存器了，而是用变量」「C 中体现为变量，rop 巧妙还回寄存器 / 函数参数」

所以分工是：

* **C 层**只写变量与函数参数 —— ``void rprint(unsigned char font, unsigned char row,
  const unsigned char *text);``
* **编译器/ROP 层**负责"把变量搬进例程要求的寄存器"：``font → R0``、``row → R1``、
  ``text → ER2``，然后进入例程。

地址一律来自标签表（``crop/labels.py``，按指令签名逐 Ver 解析），例程机器码从 ROM
现场读出（``crop/routines.py``），所以本文件里**没有任何裸地址**。

搬运原语（全部从 ROM 推导，见 ``Backend.var_load`` / ``Backend.pop_gad``）：

===========================  ==========================================================
从哪来                       生成的机器码
===========================  ==========================================================
常量 → ``R0``                 ``POP R0`` + 链上 2 字节（低字节 = 值）
常量 → ``R1``                 ``POP ER0`` + 链上 2 字节（= ``R0`` + ``R1`` 一次装完）
常量 → ``ER2``                ``POP ER2`` + 链上 2 字节（小端）
变量 → ``R0``                 ``POP ER12`` + 链上 2 字节（基址）→ ``L R0, 00h[BP]``
变量 → ``R1``                 ``POP ER12`` + 链上 2 字节（基址）→ ``L R1, 14h[BP]``
变量 → ``ER2``                **暂无**：ROM 里没有"（BP 相对）装 16 位进 ER2"的可内联槽
===========================  ==========================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .labels import LabelTable
from .rom import RomImage
from .routines import RtPush, Routine, build_routines, find_rt_push

__all__ = ["Param", "LibFunc", "LIBFUNCS", "Library", "LibError"]


class LibError(Exception):
    pass


@dataclass(frozen=True)
class Param:
    name: str
    reg: str            # 例程要求的寄存器：'R0' / 'R1' / 'ER2' …
    kind: str           # 'u8' / 'u16'
    ptr: bool = False   # 是指针（地址）而不是数值


@dataclass(frozen=True)
class LibFunc:
    name: str
    label: str                      # labels.conf 里的标签名（例程入口）
    params: Tuple[Param, ...]
    doc: str = ""


#: 库函数表。地址不在这里 —— 靠 ``label`` 现场解析。
LIBFUNCS: Tuple[LibFunc, ...] = (
    LibFunc("rprint", "print-line",
            (Param("font", "R0", "u8"), Param("row", "R1", "u8"),
             Param("text", "ER2", "u16", ptr=True)),
            "在屏幕缓冲区打印一行文字：font 字体(0x0E 正常/0x0A 小/0x08 表格小)、"
            "row 纵向像素行、text 字符串地址"),
    LibFunc("rprint_at", "print-0x1y",
            (Param("x", "R0", "u8"), Param("y", "R1", "u8"),
             Param("text", "ER2", "u16", ptr=True)),
            "在像素坐标 (x, y) 处打印一行文字：x 0..191、y 0..63（这个入口 x/y 独立，"
            "所以能真正居中）"),
    LibFunc("rrefresh", "refresh", (),
            "把屏幕缓冲区刷新到显存（并提交）"),
    LibFunc("rclear", "clear", (),
            "清空屏幕缓冲区"),
    LibFunc("rscreen_on", "screen-on", (),
            "开启屏幕显示（**不先调它的话，画进屏幕缓冲区的东西不会显示出来**）"),
)


def find_carrier(db, want: int = 16):
    """找一个"纯吃链字节"的 gadget 当内联数据载体。

    要求：inline_safe（结尾 POP PC、不碰 SP、不写内存）、体内只有 POP/MOV、
    且一次吃 ``want`` 字节（VerF 实测 ``0x22390: POP QR8 ; POP QR0 ; POP PC`` 吃 16 字节）。
    返回 ``(addr, insn_bytes_without_terminator, payload_len)``。
    """
    best = None
    for a in sorted(db.by_addr):
        g = db.by_addr[a]
        if not g.inline_safe or g.data_bytes < 8:
            continue
        ins = db.rebuild(a).insns
        ok = True
        for i in ins[:-1]:
            if i.cls == "pop_data":
                continue
            if i.mnemonic == "MOV" and "[EA" not in i.operands and "[" not in i.operands:
                continue
            ok = False
            break
        if not ok:
            continue
        cand = (g.data_bytes, a, b"".join(i.raw for i in ins[:-1]))
        if best is None or cand[0] > best[0]:
            best = cand
    if best is None:
        raise LibError("ROM 里没有可用的内联数据载体 gadget")
    return best[1], best[2], best[0]


class Library:
    """某个 ROM 上的库例程集合 + 参数搬运。"""

    def __init__(self, backend, rom: RomImage, table: LabelTable) -> None:
        self.backend = backend
        self.rom = rom
        self.table = table
        self.routines, self.warnings = build_routines(rom, table)
        self.rt_push: Optional[RtPush] = find_rt_push(rom.space, 1 << 18)
        if self.rt_push is None:
            self.warnings.append("ROM 里没找到 rt-fix 原语：以 RT 结尾的例程无法调用")
        self.funcs: Dict[str, LibFunc] = {f.name: f for f in LIBFUNCS}
        self.known = set(self.funcs)

    # ------------------------------------------------------------------ 查询
    def func(self, name: str) -> LibFunc:
        if name not in self.funcs:
            raise LibError("未知的库函数 %s（已知：%s）" % (name, ", ".join(sorted(self.funcs))))
        return self.funcs[name]

    def routine(self, name: str) -> Routine:
        f = self.func(name)
        r = self.routines.get(f.label)
        if r is None:
            raise LibError("库函数 %s 需要例程标签 %s，但该 ROM 里没解析出这个例程" % (name, f.label))
        return r

    def describe(self) -> str:
        out = ["库例程（地址来自标签表，机器码来自 ROM 现场读取）："]
        for f in LIBFUNCS:
            r = self.routines.get(f.label)
            if r is None:
                out.append("  %-9s 标签 %-11s **缺失**" % (f.name, f.label))
                continue
            out.append("  %-9s @%05X  %2d 条指令  结尾 %-6s  参数 %s" % (
                f.name, r.entry, r.ninsn, r.term,
                ", ".join("%s→%s" % (p.name, p.reg) for p in f.params) or "无"))
        if self.rt_push is not None:
            out.append("  rt-fix   @%05X（BL %05X，后者是 POP PC）" % (
                self.rt_push.addr, self.rt_push.target))
        return "\n".join(out)

    # ------------------------------------------------------------ 参数搬运
    def _const(self, reg: str, value: int) -> bytes:
        b = self.backend
        if reg not in b.pop_gad:
            raise LibError("ROM 里没有 ``POP %s`` 原语，装不进常量" % reg)
        raw = b.pop_gad[reg][1]
        if reg.startswith("ER"):
            if not 0 <= value <= 0xFFFF:
                raise LibError("常量 %X 装不进 %s" % (value, reg))
            return raw + bytes([value & 0xFF, (value >> 8) & 0xFF])
        if not 0 <= value <= 0xFF:
            raise LibError("常量 %X 装不进 %s" % (value, reg))
        return raw + bytes([value & 0xFF, 0x00])

    def _var(self, reg: str, addr: int) -> bytes:
        b = self.backend
        if reg not in b.var_load:
            raise LibError("ROM 里没有「L %s, off[%s]」可内联槽，装不进变量" % (reg, b.slot_base))
        off, raw = b.var_load[reg]
        base = (addr - off) & 0xFFFF
        return b.base_pop + bytes([base & 0xFF, (base >> 8) & 0xFF]) + raw

    def marshal(self, params: Sequence[Param], args: Sequence[Tuple[str, int]]) -> bytes:
        """``args`` 每项是 ``('const', 值)`` 或 ``('var', 变量地址)``。"""
        if len(args) != len(params):
            raise LibError("参数个数不对：要 %d 个，给了 %d 个" % (len(params), len(args)))
        out = bytearray()
        i = 0
        while i < len(params):
            p, (kind, val) = params[i], args[i]
            # R0/R1 都是常量时，一次 ``POP ER0`` 装两个字（ROM 里没有 ``POP R1``）
            if (p.reg == "R0" and kind == "const" and i + 1 < len(params)
                    and params[i + 1].reg == "R1" and args[i + 1][0] == "const"):
                v1 = args[i + 1][1]
                if not 0 <= val <= 0xFF or not 0 <= v1 <= 0xFF:
                    raise LibError("R0/R1 的常量必须在 0..255")
                if "ER0" not in self.backend.pop_gad:
                    raise LibError("ROM 里没有 ``POP ER0``，无法一次装 R0+R1")
                out += self.backend.pop_gad["ER0"][1] + bytes([val, v1])
                i += 2
                continue
            if kind == "const":
                out += self._const(p.reg, val)
            elif kind == "var":
                out += self._var(p.reg, val)
            else:                                   # pragma: no cover - 防御
                raise LibError("未知参数形式 %r" % (kind,))
            i += 1
        return bytes(out)

    # ------------------------------------------------------------ 生成调用
    def emit_call(self, name: str, args: Sequence[Tuple[str, int]]) -> bytes:
        """生成"搬运参数 + 进入例程"的机器码（例程机器码原样从 ROM 抄来）。"""
        f = self.func(name)
        r = self.routine(name)
        return self.marshal(f.params, args) + r.code
