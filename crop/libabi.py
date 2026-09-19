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
    #: True 表示"不是调用例程，而是发射一条**裸绝对跳转**"（如 rexit：跳回 OS 空闲循环）
    raw_jump: bool = False
    #: True 表示该库函数把结果留在 R0（可以写 `c = f(...)`）
    returns: bool = False


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
    LibFunc("rhalt", "@frozen", (), "冻结 CPU：跳到一个不存在的地址（ROM 之外）"
            "—— 手写 ROP 里的 kill/0x40000 就是这个手法；比 while(1) 更体面（不霸占主循环）",
            raw_jump=True),
    LibFunc("rexit", "os-idle", (), "画完跳回 OS 空闲主循环（替代 while(1)，避免卡死/断电）",
            raw_jump=True),
    LibFunc("er2_from_er0", "er2-from-er0", (), "ER2 ← ER0（A7 内部用）"),
    LibFunc("er2_from_er0b", "er2-from-er0b", (), "ER2 ← ER0（带 8 字节填充，A7 内部用）"),
    LibFunc("rt_er0_table", "er0-table", (),
            "ER0 ← R0*R2 + ER4（A7 内部用：按 0/1 选链地址）"),
    LibFunc("rt_er0_er14", "er0-er14", (), "ER14 ← ER0（A7 内部用）"),
    LibFunc("rcmp_gt", "cmp-gt", (Param("b", "ER2", "u8"), Param("a", "ER0", "u8")),
            "比较：R0 = 1 ⟺ (a ≤ b)（ROM 0B61E 的真实语义；A7 基础）", returns=True),
    LibFunc("rinc", "mem-add", (Param("dst", "ER8", "u16"), Param("delta", "ER2", "u16")),
            "v += 1（codegen 直接填变量地址常量）"),
    LibFunc("rdec", "mem-add", (Param("dst", "ER8", "u16"), Param("delta", "ER2", "u16")),
            "v -= 1（codegen 直接填变量地址常量）"),
    LibFunc("radd8", "mem-add", (Param("dst", "ER8", "u16"), Param("delta", "ER2", "u16")),
            "v += delta（codegen 直接填变量地址常量）"),
    LibFunc("rt_st_er2_er8", "st-er2-er8", (), "ST ER2,[ER8] 组（A4 内部）"),
    LibFunc("rt_er2_from_er10", "er2-from-er10", (), "MOV ER2,ER10 ; POP QR8 ; POP PC（A4 内部）"),
    LibFunc("rt_mem_add", "mem-add", (), "[ER8] += ER2（A4 内部）"),
    LibFunc("rplot16", "mem-add", (Param("pat", "R0", "u8"),), "写字节到 tmp16 指向的地址"),
    LibFunc("rplotcol", "er0-table", (Param("off", "R0", "u8"), Param("h", "R0", "u8")),
            "画一整根柱子（列地址只算一次）"),
    LibFunc("rplotnext", "er0-table", (), "写一行并把列地址前进 24 字节"),
    LibFunc("rplotrow", "er0-table",
            (Param("y", "ER0", "u8"), Param("off", "ER2", "u16"), Param("pat", "R0", "u8")),
            "屏幕缓冲区画一行两像素"),
    LibFunc("raddr24", "er0-table", (Param("y", "ER0", "u8"), Param("off", "ER2", "u16")),
            "tmp16 = 0xDDD4 + 24*y + off"),
    LibFunc("rmul", "er0-table", (Param("v", "ER0", "u8"), Param("k", "ER2", "u16")),
            "返回 v*k（k 为编译期常量；codegen 直接发 er0-table 序列）", returns=True),
    LibFunc("r_blockdraw", "blk-draw", (), "画反色框（参数先用 r_xy/r_w2/r_h3 摆好）"),
    LibFunc("r_set2", "set-r2-2", (), "R2 = 2（柱宽）"),
    LibFunc("r_set_h", "r3-from-r9", (Param("h", "R9", "u8"),),
            "R9 = h 且 R3 = h（柱高）"),
    LibFunc("rrand", "randint", (Param("n", "ER0", "u16"),),
            "取一个 0..n 的随机数（结果留在 R0）", returns=True),
    LibFunc("rsleep", "sleep", (Param("z", "R0", "u8"), Param("t", "R1", "u8")),
            "延时 t/30 秒；常量要成对给：rsleep(0, 6) ≈ 0.2 秒"),
    LibFunc("rint_off", "int-off", (),
            "关中断：之后 OS 的显示任务不会再重画屏幕，画进去的东西才留得住"),
    LibFunc("rint_on", "int-on", (), "开中断"),
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

    #: 「零变量」的地址（编译期分配、初值 0）：用来把寄存器高字节清零
    zero_addr: int = 0
    #: A4 的 8 字节临时区：寄存器堆 R0..R7 落盘用
    scratch_addr: int = 0
    #: 2 字节临时单元（16 位地址运算用）
    tmp16_addr: int = 0
    #: BP(ER12) 当前装载的基址（None = 未知）
    _bp_loaded: Optional[int] = None

    # ---------------------------------------------------------------- A4 运行时下标
    def _var(self, reg: str, addr: int) -> bytes:
        """把**变量**的值装进寄存器（BP 槽 + 基址装载）。"""
        b = self.backend
        if reg == "ER0":
            # 变量 → ER0：L R0,[BP]（低字节）+ L R1,[BP]（零变量 ⇒ 高字节 0，保证无符号比较）
            if not self.zero_addr:
                raise LibError("编译期没有分配「零变量」，变量 → ER0 不可用")
            if "R0" not in b.var_load or "R1" not in b.var_load:
                raise LibError("ROM 里缺少 L R0 / L R1 的 BP 槽")
            off0, raw0 = b.var_load["R0"]
            base0 = (addr - off0) & 0xFFFF
            off1, raw1 = b.var_load["R1"]
            base1 = (self.zero_addr - off1) & 0xFFFF
            if self._bp_loaded == base0 == base1:
                return raw0 + raw1
            self._bp_loaded = base0
            return (b.base_pop + bytes([base0 & 0xFF, (base0 >> 8) & 0xFF]) + raw0
                    + b.base_pop + bytes([base1 & 0xFF, (base1 >> 8) & 0xFF]) + raw1)
        if reg == "ER2":
            # 变量 → ER2：先 ER0（含高字节清零），再用例程 MOV ER2,ER0
            return self._var("ER0", addr) + self.routine("er2_from_er0").code
        if reg not in b.var_load:
            raise LibError("ROM 里没有「L %s, off[%s]」可内联槽，装不进变量"
                           % (reg, b.slot_base))
        off, raw = b.var_load[reg]
        base = (addr - off) & 0xFFFF
        if self._bp_loaded == base:
            return raw
        self._bp_loaded = base
        return b.base_pop + bytes([base & 0xFF, (base >> 8) & 0xFF]) + raw

    def rt_element(self, arr_base: int, elem_size: int, idx_addr: int) -> bytes:
        self._bp_loaded = None
        """把 ``&arr[idx]`` 算进 ER0：R0=idx、R2=elem_size、ER4=arr_base。"""
        b = self.backend
        code = bytearray()
        code += self.marshal((Param("i", "ER0", "u8"),), [("var", idx_addr)])
        code += self.marshal((Param("s", "ER2", "u16"),), [("const", elem_size & 0xFF)])
        code += self.marshal((Param("b", "ER4", "u16"),), [("const", arr_base)])
        code += self.routine("rt_er0_table").code        # ER0 = R0*R2 + ER4
        return bytes(code)

    def rt_load(self) -> bytes:
        """ER0 指向元素 ⇒ 读一个字节进 R0（``L R0,[ER0]`` @0x1810E，紧跟 POP PC）。"""
        return self._gadget_insns(0x1810E)

    def rt_store(self) -> bytes:
        """把 R0 写到 [ER2]（``ST R0,[ER2] ; MOV R0,#0`` @0x1651A，紧跟 POP PC）。"""
        return self._gadget_insns(0x1651A)

    def rt_addr_to_er2(self) -> bytes:
        """ER0 → ER2（``MOV ER2,ER0 ; MOV ER0,ER2 ; POP ER8 ; RT`` @0x0D572；吃 8 字节链填充）。"""
        return self.routine("er2_from_er0b").code

    # ---- 纯 POP-PC 的"值/地址 → ER2"（避开 RT 例程 ⇒ 循环体不泄漏硬件返回栈）----
    def _read16_er2(self, cell: int) -> bytes:
        """**纯 POP-PC**：ER2 = [cell]（16 位）。
        原理：mem-add 的 ER2=0 变体把旧值留在 ER10 ⇒ MOV ER2,ER10 ; POP QR8 ; POP PC。
        pad：mem-add 内 POP XR8 吃 4、MOV…POP QR8 吃 8。"""
        b = self.backend
        out = bytearray()
        out += self.marshal((Param("a", "ER8", "u16"),), [("const", cell)])
        out += self.marshal((Param("z", "ER2", "u16"),), [("const", 0)])
        out += self.routine("rt_mem_add").code + bytes([0xDE, 0xB2])      # 4 字节填充
        out += self.routine("rt_er2_from_er10").code + bytes([0xDE, 0xB3])   # 8 字节填充
        return bytes(out)

    def a4_write(self, arr_base: int, elem_size: int, idx_addr: int) -> bytes:
        """纯 POP-PC 的"元素地址 → ER2"（元素类型 u8、elem_size==1）：
        tmp16 = arr_base + [idx]；然后 ER2 = [tmp16]。返回后调用方只需 R0=值 + ST R0,[ER2]。"""
        if elem_size != 1:
            raise LibError("a4_write 目前只支持 u8 元素（elem_size==1）")
        t = self.tmp16_addr
        out = bytearray()
        out += self.marshal((Param("a", "ER8", "u16"),), [("const", t)])
        out += self.marshal((Param("v", "ER2", "u16"),), [("const", arr_base)])
        out += self.routine("rt_st_er2_er8").code + bytes([0xDE, 0xB2])   # tmp16 = arr_base
        out += self._read16_er2(idx_addr)                                # ER2 = [idx]
        out += self.marshal((Param("a", "ER8", "u16"),), [("const", t)])
        out += self.routine("rt_mem_add").code + bytes([0xDE, 0xB2])      # tmp16 += idx
        out += self._read16_er2(t)                                       # ER2 = 元素地址
        return bytes(out)

    def _var16_er2(self, addr: int) -> bytes:
        """**16 位**变量 → ER2：L ER4,[BP] → (R0=0,R2=1) → er0-table（ER0=ER4）→ MOV ER2,ER0。"""
        b = self.backend
        out = bytearray()
        if "ER4" not in b.var_load:
            raise LibError("ROM 里没有 16 位变量装载槽 L ER4,[BP]")
        off, raw = b.var_load["ER4"]
        base = (addr - off) & 0xFFFF
        out += b.base_pop + bytes([base & 0xFF, (base >> 8) & 0xFF]) + raw   # ER4 = [addr]
        out += self._var("ER0", self.zero_addr)      # R0 = 0（零变量）⇒ er0-table 得 ER0 = ER4
        out += self.marshal((Param("s", "ER2", "u16"),), [("const", 1)])     # R2 = 1
        out += self.routine("rt_er0_table").code
        out += self.routine("er2_from_er0").code     # MOV ER2,ER0
        return bytes(out)

    def _gadget_insns(self, addr: int) -> bytes:
        self._bp_loaded = None
        ins = self.backend.db.rebuild(addr).insns
        return b"".join(i.raw for i in ins[:-1])          # 去掉结尾的 POP PC

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
    def target_addr(self, name: str) -> int:
        """``raw_jump`` 型库函数的目标地址。

        ``@frozen`` = **ROM 末尾之后**（由 ROM 大小推导，绝不写死）：跳到那里 CPU 会冻住，
        这就是手写 ROP 里"用不存在的 gadget 卡死"的手法。
        """
        f = self.func(name)
        if f.label == "@frozen":
            # 冻结地址 = **已装载 ROM 的末尾之后**（由 ROM 文件表推导，绝不写死；O(文件数)）
            end = 0
            for rf in self.rom.files:
                end = max(end, rf.base_page * 0x10000 + len(rf.data))
            return (end + 4) & ~1
        r = self.routines.get(f.label)
        return r.entry if r else self.table.addr(f.label)

    def emit_call(self, name: str, args: Sequence[Tuple[str, int]]) -> bytes:
        """生成"搬运参数 + 进入例程"的机器码（例程机器码原样从 ROM 抄来）。"""
        f = self.func(name)
        r = self.routine(name)
        return self.marshal(f.params, args) + r.code
