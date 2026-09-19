"""rGCC v0 —— 面向 ROP 的 C 子集编译器（`.c` → nX-U16 `.bin`）。

设计原则（第 1、2 步数据直接推导出来的）：

1. **rGCC 只生成"词表里有现成 gadget"的指令形态。** 生成后立刻用扫描器核对，
   任何一条指令没有对应 gadget 就直接报错（``--strict``），
   于是"解释器覆盖率 100%"是**构造性保证**，而不是碰运气。
2. **不用栈**：不生成 ``PUSH``/``POP``（除取常量的 ``POP XR0``）/``ADD SP``；
   SP 是 ROP 链指针，动了就散架。
3. **变量放 RAM，不放寄存器**：靠 ``POP XR0`` + ``ST R2, [ER0]`` 这类原语
   写内存（这正是 RopIDE 手工程序里 ``#pop-xr0; … #byte-set;`` 的写法）。
4. **常量必须经链上数据**：``POP <reg>`` 是唯一的"任意常量"来源。

当前支持的语言子集（v0，刻意做到能 100% 翻译为止）：

```c
unsigned char flag;      // 变量（自动分配 RAM 地址，或用 --at 指定）
void main(void) {
    flag = 1;            // 常量写内存
    flag = 0;
    while (1) {          // 无限循环（无条件跳转，L3 可翻译）
        flag = 1;
        flag = 0;
    }
}
```

**不支持**（会明确报错，不猜）：表达式、算术、`if`/`while(cond)`、函数调用。
原因见 ``docs/step-3-报告.md``：本 ROM 的 ALU 只有 48 种固定寄存器/立即数组合，
全片只有 1 个"既能 L 又能 ST"的变量槽，做通用 C 编译在数学上不成立。
"""

from __future__ import annotations

import collections
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .gadget import GadgetDB
from .nxu16 import decode as _dec
from .vocab import find_pop_gadgets, pick

__all__ = ["RgccError", "Backend", "CompileUnit", "compile_source",
           "verify_translatable", "Var", "Stmt"]

# ----------------------------------------------------------------- 错误
class RgccError(Exception):
    pass


def _parse_off(text: str) -> int:
    """``-10h`` / ``00h`` → 有符号整数（反汇编器把 BP/FP 偏移打印成十六进制+'h'）。"""
    t = text.strip()
    if t.startswith("-"):
        return -int(t[1:].rstrip("h"), 16)
    return int(t.rstrip("h"), 16)


# ----------------------------------------------------------------- 词表后端
@dataclass
class Backend:
    """把"要生成什么"翻译成"哪些 gadget + 什么字节"。

    **全部从 ROM 推导，零硬编码**（换 Ver / 换机型自动适配）：
      * 常量写内存：找 ``POP XRn/QRn`` + ``ST Rn+2, [ERn]`` 这对原语（哪个版有就用哪个）
      * 变量拷贝：找同时存在 ``L`` 与 ``ST`` 的 (寄存器,偏移,基址) 槽
      * 基址装载：该槽的基址是 BP 就找 ``POP ER12``，是 FP 就找 ``POP ER14``
    任何一项推导不出来 → 明确报错（绝不用错的常量兜底）。
    """

    db: GadgetDB
    const_pop: bytes = b""        # 常量装载指令的字节（POP XRn/QRn）
    const_pop_len: int = 0        # 它从链/内联数据取走的字节数
    const_value_reg: str = ""     # 其中承载"值"的寄存器
    const_store_addr: int = 0     # ST Rn+2,[ERn] gadget 地址
    const_store: bytes = b""
    blk_pop: bytes = b""          # 块写：值装载（POP QRm）
    blk_pop_len: int = 0
    blk_base: bytes = b""         # 块写：目标地址装载（POP ERk）
    blk_body: bytes = b""         # 块写 gadget 的本体（LEA + ST…，不含结尾 POP PC）
    blk_gadget_addr: int = 0
    blk_qr: int = -1
    blk_base_reg: str = ""
    slot_base: str = ""
    slot_off: str = ""
    slot_load_addr: int = 0
    slot_load: bytes = b""
    slot_store: bytes = b""
    base_pop: bytes = b""
    #: 取数原语：寄存器名 → (gadget 地址, 该条 POP 指令的字节)
    pop_gad: Dict[str, Tuple[int, bytes]] = field(default_factory=dict)
    #: 变量装载槽：寄存器名 → (偏移, ``L Rn, off[base]`` 的字节)。基址由 ``base_pop`` 装载。
    var_load: Dict[str, Tuple[int, bytes]] = field(default_factory=dict)
    #: 空操作间隔（``MOV Rn, Rn``，无副作用、以 POP PC 结尾）：用来把相邻的搬运动作隔开
    noop_ins: bytes = b""
    blk_pad: int = 0                  # 块写 gadget 尾巴里额外吃掉的链字节数
    #: 寄存器搬移槽（变量→ER2 用）：名字 → (指令, "MOV A, B" 的指令字节)
    reg_moves: Dict[str, bytes] = field(default_factory=dict)
    #: 内联数据载体：链上"只吃字节"的 gadget（字符串骑链用）
    carrier_addr: int = 0
    carrier_ins: bytes = b""
    carrier_len: int = 0

    def __post_init__(self) -> None:
        self._derive()

    # ---------------------------------------------------------------- 工具
    def _single(self, wanted: str):
        """找一条"单指令 gadget"，返回 (地址, 该指令字节, 指令对象)。"""
        for addr in sorted(self.db.by_addr):
            g = self.db.by_addr[addr]
            if not g.inline_safe or g.ninsn != 1:
                continue
            ins = self.db.rebuild(addr).insns[0]
            if ins.text == wanted:
                return addr, ins.raw, ins
        return None

    # ---------------------------------------------------------------- 推导
    def _derive(self) -> None:
        pops = find_pop_gadgets(self.db)
        for name in ("XR0", "XR4", "XR8", "XR12", "QR0", "QR8"):
            if name not in pops:
                continue
            n = int(name[2:])
            er, val = "ER%d" % n, "R%d" % (n + 2)
            st = self._single("ST %s, [%s]" % (val, er))
            pop = self._single("POP %s" % name)
            if not st or not pop:
                continue
            self.const_pop, self.const_value_reg = pop[1], val
            self.const_pop_len = pop[2].sp_delta
            self.const_store_addr, self.const_store = st[0], st[1]
            break
        if not self.const_pop:
            raise RgccError("ROM 里找不到「POP XRn/QRn + ST Rn+2,[ERn]」原语对，"
                            "无法生成常量写内存")

        # 块写：从 ROM 里扫"LEA [ERk] 紧跟 ST QRm,[EA+]"的 gadget（一次槽写多字节）。
        # ★按"代价 = pad 字节 + 一次写入不足 8 字节的惩罚"排序挑**最省**的那个：
        #   尾巴里的 POP 越多 ⇒ 链上要补的 0 越多（原来是固定取第一个，白付 12 字节）。
        cands = []
        for a in sorted(self.db.by_addr):
            g = self.db.by_addr[a]
            if g.term != "pop_pc" or g.ninsn == 0:
                continue
            ins = self.db.rebuild(a).insns
            for k in range(len(ins) - 1):
                t0, t1 = ins[k].text, ins[k + 1].text
                if not (t0.startswith("LEA [ER") and t1.startswith("ST QR")
                        and "[EA+]" in t1):
                    continue
                reg = int(t0.split("ER")[1].rstrip("]"))
                qr = int(t1.split("QR")[1].split(",")[0])
                preg, qreg = "ER%d" % reg, "QR%d" % qr
                if preg not in pops or qreg not in pops:
                    continue
                pad = sum(x.sp_delta for x in ins[k + 1:-1])   # 尾巴里额外吃掉的链字节
                stores = sum(1 for x in ins[k + 1:] if x.text.startswith("ST"))
                # ★安全优先：只有"写 8 字节 + 2 字节尾巴"这种**足迹可预测**的形态才低分；
                #   连写多段的 gadget（如 0x17E7E 会一路写 30 字节）虽然 pad=0，但会踩到
                #   后面 20 字节的数据 ⇒ 给很高的惩罚，避免悄悄改坏别的变量。
                cands.append((pad + (0 if stores == 2 else 40), pad, stores, g, ins, k,
                              qr, preg))
        if cands:
            cands.sort(key=lambda c: (c[0], c[1], c[3].addr))
            score, pad, stores, g, ins, k, qr, preg = cands[0]
            qreg = "QR%d" % qr
            self.blk_pop = self._single("POP %s" % qreg)[1]     # 值装载
            self.blk_pop_len = self._single("POP %s" % qreg)[2].sp_delta
            self.blk_base = self._single("POP %s" % preg)[1]    # 目标地址装载
            # ★入口必须是 "LEA [ERk]" 这条指令本身，而不是外层 gadget 的起点：
            #   起点之前的 L QR0,[EA+]/L ER8,[EA+] 会用垃圾 EA 读数据，
            #   把我们刚装进 R0..R9 的值冲掉（真机实测踩过这个坑）。
            start = ins[k].addr - g.addr
            self.blk_gadget_addr = ins[k].addr
            self.blk_body = g.code[start:-2]                    # 去掉结尾 POP PC
            self.blk_qr, self.blk_base_reg = qr, preg
            self.blk_pad = pad

        # 变量槽：同时有 L 与 ST 的 (寄存器,偏移,基址)
        slots = {}
        for addr in sorted(self.db.by_addr):
            g = self.db.by_addr[addr]
            if not g.inline_safe or g.ninsn != 1:
                continue
            ins = self.db.rebuild(addr).insns[0]
            m = re.match(r"^([A-Z0-9]+), (-?[0-9A-F]+h)\[(\w+)\]$", ins.operands)
            if not m:
                continue
            key = m.groups()
            slots.setdefault(key, {})[ins.mnemonic] = (addr, ins.raw)
        for (reg, off, base), forms in slots.items():
            if {"L", "ST"} <= set(forms):
                self.slot_base, self.slot_off = base, off
                self.slot_load_addr, self.slot_load = forms["L"]
                self.slot_store = forms["ST"][1]
                break
        if not self.slot_load:
            raise RgccError("ROM 里找不到同时具备 L 与 ST 的变量槽，无法生成变量拷贝")

        bp_reg = {"BP": "ER12", "FP": "ER14"}.get(self.slot_base)
        pop = self._single("POP %s" % bp_reg) if bp_reg else None
        if not pop:
            raise RgccError("变量槽基址是 %s，但 ROM 里没有 POP %s" % (self.slot_base, bp_reg))
        self.base_pop = pop[1]

        # ---- 取数原语表（常量 → 指定寄存器），给库函数参数搬运用 ----
        for reg in pops:
            got = self._single("POP %s" % reg)
            if got:
                self.pop_gad[reg] = (got[0], got[1])

        # ---- 寄存器搬移槽（给"变量 → ER2"用：L ER4,[BP] → MOV ER0,ER4 → MOV ER2,ER0）----
        for txt in ("MOV ER0, ER4", "MOV ER2, ER0", "MOV ER0, ER2", "MOV ER4, ER0"):
            got = self._single(txt)
            if got:
                self.reg_moves[txt] = got[1]

        # ---- 内联数据载体（字符串骑链）：纯吃链字节的 gadget ----
        from .libabi import find_carrier
        try:
            self.carrier_addr, self.carrier_ins, self.carrier_len = find_carrier(self.db)
            self.db.carrier_marker = (self.carrier_ins, self.carrier_len)   # 给 verify_translatable 用
        except Exception:
            pass

        # ---- 空操作 gadget（间隔用）：MOV Rn, Rn ----
        for cand in ("MOV R7, R7", "MOV R1, R1", "MOV R0, R0", "MOV R8, R8"):
            got = self._single(cand)
            if got:
                self.noop_ins = got[1]
                break

        # ---- 变量装载槽（变量 → 指定寄存器），基址与变量槽同一套（BP/FP）----
        pat = re.compile(r"^([A-Z0-9]+), (-?[0-9A-F]+h)\[%s\]$" % re.escape(self.slot_base))
        for addr in sorted(self.db.by_addr):
            g = self.db.by_addr[addr]
            if not g.inline_safe or g.ninsn != 1:
                continue
            ins = self.db.rebuild(addr).insns[0]
            if ins.mnemonic != "L":
                continue
            m = pat.match(ins.operands)
            if m and m.group(1) not in self.var_load:
                self.var_load[m.group(1)] = (_parse_off(m.group(2)), ins.raw)

    # ---------------------------------------------------------------- 生成
    def write_byte_imm(self, addr: int, value: int) -> bytes:
        """``*(u8*)addr = value``：POP <宽寄存器>(ERn=地址, Rn+2=值) + ST Rn+2,[ERn]。

        ``.bin`` 的**内联数据约定**：POP 后面紧跟它要弹走的字节，解释器把它们搬进链。
        """
        if not 0 <= addr <= 0xFFFF:
            raise RgccError("数据地址超出 16 位：%X" % addr)
        if self.const_pop_len < 3:
            raise RgccError("常量装载原语只取 %d 字节，装不下地址+值" % self.const_pop_len)
        data = bytes([addr & 0xFF, (addr >> 8) & 0xFF, value & 0xFF])
        data += b"\x00" * (self.const_pop_len - len(data))
        return self.const_pop + data + self.const_store

    def block_write(self, addr: int, values) -> bytes:
        """一次写连续 ``len(values)`` 个字节：``POP QRn`` + ``LEA [ERn]`` + ``ST QRn,[EA]``。

        QRn 是 R(n)…R(n+7)：前两个寄存器被 ``LEA`` 当作地址用，所以真正的写地址是
        ``addr``，而 **ERn = addr - 2**（那 2 个地址字节会被写到 ``addr-2``，属已知副作用）。
        """
        if not self.blk_pop:
            raise RgccError("ROM 里没有「LEA [ERk] + ST QRm,[EA+]」块写 gadget")
        if len(values) > self.blk_pop_len:
            raise RgccError("块写一次最多 %d 字节" % self.blk_pop_len)
        b = addr & 0xFFFF
        data = bytes(v & 0xFF for v in values) + b"\x00" * (self.blk_pop_len - len(values))
        # pad 由**解释器**在链上补（它知道 gadget 尾巴吃多少字节），这里不要重复补
        return (self.blk_base + bytes([b & 0xFF, (b >> 8) & 0xFF])
                + self.blk_pop + data + self.blk_body)

    def copy_var(self, dst_addr: int, src_addr: int) -> bytes:
        """``x = y;``：切基址指针 → L 槽 → 切基址指针 → ST 槽。"""

        def base(a: int) -> bytes:
            off = int(self.slot_off.rstrip("h"), 16)
            if self.slot_off.startswith("-"):
                off = -int(self.slot_off[1:].rstrip("h"), 16)
            b = (a - off) & 0xFFFF
            return bytes([b & 0xFF, (b >> 8) & 0xFF])

        # 两次搬运动作之间插一条空操作 gadget：真机实测"copy 紧跟 copy"时第一次会失效
        # （copytest 里 copy 后面跟常量写则没事），插开一格可规避。
        gap = self.noop_ins
        return (self.base_pop + base(src_addr) + self.slot_load + gap
                + self.base_pop + base(dst_addr) + self.slot_store + gap)

    def describe(self) -> str:
        return ("常量写内存: POP→%s + ST @%05X（内联 %d 字节）；"
                "变量槽: %s[%s] L@%05X；基址装载 %s；"
                "变量装载槽 %s" % (
                    self.const_value_reg, self.const_store_addr, self.const_pop_len,
                    self.slot_off, self.slot_base, self.slot_load_addr,
                    self.base_pop.hex().upper(),
                    " ".join(sorted(self.var_load))))

    @staticmethod
    def chain_data(addr: int, value: int) -> bytes:
        return bytes([addr & 0xFF, (addr >> 8) & 0xFF, value & 0xFF, 0])

    def op_slots(self, addr: int, value: int) -> List[Tuple[int, bytes]]:
        """返回这次写内存要放进 ROP 链的 (gadget 地址, 链上数据) 列表。"""
        return [(self.const_store_addr, b"")]


# ----------------------------------------------------------------- 前端
U8 = "u8"


@dataclass
class Sym:
    """一个内存对象：变量 / 数组 / 结构体变量 / 指针。"""

    name: str
    addr: int
    kind: str = "byte"                  # 'byte' | 'array' | 'struct' | 'ptr'
    size: int = 1                       # 占多少字节
    elem: str = U8                      # 数组元素类型（'u8' 或 'struct:名字'）
    count: int = 1                      # 数组长度
    fields: Dict[str, int] = field(default_factory=dict)   # 结构体字段 → 字节偏移

    def describe(self) -> str:
        if self.kind == "array":
            return "%s[%d]@%04X" % (self.name, self.count, self.addr)
        if self.kind == "ptr":
            return "*%s@%04X" % (self.name, self.addr)
        return "%s@%04X" % (self.name, self.addr)


#: 兼容旧名字（外部脚本/测试里用过 ``Var``）
Var = Sym


@dataclass
class MemRef:
    """一个内存位置（左值/右值）。

    目前恒为**常量地址**；A4 会把 ``kind='var'`` 用起来（地址存在某个变量里）。
    """

    kind: str = "const"                 # 'const' | 'ptrparam' | 'runtime'（A4：基址+变量下标）
    value: int = 0                      # const：绝对地址
    var: str = ""                       # ptrparam：形参名；runtime：下标变量名
    offset: int = 0
    note: str = ""
    arr_base: int = 0                   # runtime：数组基址
    elem_size: int = 1                  # runtime：元素大小

    def label(self) -> str:
        if self.kind == "const":
            return "[%04X]" % self.value
        return "[*%s+%d]" % (self.var, self.offset)


@dataclass
class Stmt:
    kind: str = "assign"                # 'assign' | 'copy' | 'loop' | 'call' | 'return'
    dst: Optional[MemRef] = None
    value: int = 0
    src: Optional[MemRef] = None
    body: List["Stmt"] = field(default_factory=list)
    line: int = 0
    name: str = ""                      # kind == 'call'
    a: str = ""                         # kind == 'ifgt'：左操作数变量名
    cval: int = -1                      # kind == 'ifgt'/'whilegt'：右操作数是常量时的值
    b: str = ""                         # kind == 'ifgt'：右操作数变量名
    alt: List["Stmt"] = field(default_factory=list)   # kind == 'ifgt'：else 分支
    args: List[Tuple[str, object]] = field(default_factory=list)


@dataclass
class Func:
    name: str
    ret: str = "void"                   # 'u8' | 'void'
    params: List[Tuple[str, str]] = field(default_factory=list)   # (名字, 'u8'|'ptr')
    body: List[Stmt] = field(default_factory=list)
    scope: Dict[str, Sym] = field(default_factory=dict)
    ret_slot: int = 0
    line: int = 0


@dataclass
class CompileUnit:
    source: str
    vars: Dict[str, Sym]
    body: List[Stmt]
    code: bytes
    listing: List[str]
    data_base: int = 0xD180
    strings: Dict[int, bytes] = field(default_factory=dict)
    protos: Dict[str, int] = field(default_factory=dict)   # 声明过的库函数 → 参数个数
    funcs: Dict[str, Func] = field(default_factory=dict)
    structs: Dict[str, Dict[str, int]] = field(default_factory=dict)
    initials: Dict[int, bytes] = field(default_factory=dict)   # 静态初值（程序开头写入）
    #: 裸跳转表：.bin 偏移 → 20 位目标地址（解释器在该处直接发链槽，不走指令流）
    raw_jumps: Dict[int, int] = field(default_factory=dict)
    #: A7 条件跳转标记：.bin 偏移 → 种类（if/else/end）
    a7_markers: Dict[int, str] = field(default_factory=dict)


_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<comment>//[^\n]*)
  | (?P<str>"(?:[^"\\]|\\.)*")
  | (?P<num>0[xX][0-9a-fA-F]+|\d+)
  | (?P<id>[A-Za-z_]\w*)
  | (?P<op><<|>>|<=|>=|->|[-+*/%&|^~<>])
  | (?P<punct>[{}();=,.\][])
""", re.VERBOSE)


def _tokens(src: str, ) -> List[Tuple[str, str, int]]:
    out, pos, line = [], 0, 1
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m:
            raise RgccError("第 %d 行：无法识别的字符 %r" % (line, src[pos]))
        pos = m.end()
        kind = m.lastgroup
        text = m.group()
        if kind == "ws":
            line += text.count("\n")
        elif kind != "comment":
            out.append((kind, text, line))
    out.append(("eof", "", line))
    return out


class Parser:
    """rGCC 前端：作用域、数组、结构体、函数（A2/A3/A5）。

    内存模型：一个**线性分配器**把每个对象放进数据区（`data_base` 起）。
    只要所有下标/字段偏移都是常量，地址在编译期就能算出来 —— 这正是本 ROM
    能做的事（运行时算术要等 A4/A7 的字节传送与条件分支）。
    """
    #: 裸跳转表：.bin 偏移 → 20 位目标地址（解释器在该处直接发链槽，不走指令流）
    raw_jumps: Dict[int, int] = field(default_factory=dict)

    def __init__(self, src: str, data_base: int):
        self.toks = _tokens(src)
        self.i = 0
        self.globals: Dict[str, Sym] = {}
        self.next_addr = data_base
        self.protos: Dict[str, int] = {}
        self.strings: List[bytes] = []
        self._str_ids: Dict[bytes, int] = {}
        self.structs: Dict[str, Dict[str, int]] = {}
        self.struct_size: Dict[str, int] = {}
        self.funcs: Dict[str, Func] = {}
        self.main: Optional[Func] = None
        self.scope: Dict[str, Sym] = {}          # 当前函数作用域（全局时为空）
        self.initials: Dict[int, bytes] = {}     # 静态初值
        self.cur_fn: Optional[Func] = None
        self.const_env: Dict[str, int] = {}      # 编译期常量（for 展开的循环变量）

    def fold(self, e):
        return _fold(e, self.const_env)

    # ---------------------------------------------------------------- 基础
    def peek(self) -> Tuple[str, str, int]:
        return self.toks[self.i]

    def look(self, k: int = 1) -> Tuple[str, str, int]:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def next(self) -> Tuple[str, str, int]:
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, text: str) -> Tuple[str, str, int]:
        t = self.next()
        if t[1] != text:
            raise RgccError("第 %d 行：期望 %r，实际 %r" % (t[2], text, t[1]))
        return t

    def expect_kind(self, kind: str) -> Tuple[str, str, int]:
        t = self.next()
        if t[0] != kind:
            raise RgccError("第 %d 行：期望 %s，实际 %r" % (t[2], kind, t[1]))
        return t

    # ---------------------------------------------------------------- 分配
    def alloc(self, n: int, align: int = 1) -> int:
        a = self.next_addr
        if align > 1:
            a = (a + align - 1) & ~(align - 1)
        self.next_addr = a + n
        if self.next_addr > 0xFFFF:
            raise RgccError("数据区超出 16 位地址空间")
        return a

    def lookup(self, name: str, line: int) -> Sym:
        if name in self.scope:
            return self.scope[name]
        if name in self.globals:
            return self.globals[name]
        raise RgccError("第 %d 行：未声明的名字 %s" % (line, name))

    def define(self, sym: Sym) -> None:
        table = self.scope if self.cur_fn is not None else self.globals
        if sym.name in table or sym.name in self.protos or sym.name in self.funcs:
            raise RgccError("名字 %s 重复定义" % sym.name)
        table[sym.name] = sym

    def sizeof(self, tname: str) -> int:
        if tname == U8:
            return 1
        if tname.startswith("struct:"):
            return self.struct_size[tname.split(":", 1)[1]]
        if tname == "ptr":
            return 2
        raise RgccError("未知类型 %s" % tname)

    # ---------------------------------------------------------------- 类型
    def parse_type(self) -> Tuple[str, int]:
        """声明说明符 → ``(基类型, 指针层数)``。``const`` 只是说明，直接跳过。"""
        while self.peek()[1] == "const":
            self.next()
        t = self.peek()
        if t[1] == "unsigned":
            self.next()
            self.expect("char")
            base = U8
        elif t[1] == "char":
            self.next()
            base = U8
        elif t[1] == "void":
            self.next()
            base = "void"
        elif t[1] == "struct":
            self.next()
            name = self.expect_kind("id")[1]
            if name not in self.structs:
                raise RgccError("第 %d 行：未定义的结构体 %s" % (t[2], name))
            base = "struct:" + name
        else:
            raise RgccError("第 %d 行：只支持 unsigned char / char / void / struct，实际 %r"
                            % (t[2], t[1]))
        depth = 0
        while self.peek()[1] == "*":
            self.next()
            depth += 1
        return base, depth

    # ------------------------------------------------------------ 顶层解析
    def parse(self) -> Tuple[Dict[str, Sym], List[Stmt]]:
        while self.peek()[0] != "eof":
            self.parse_toplevel()
        if self.main is None:
            raise RgccError("没有 main()（程序入口）")
        return self.globals, self.main.body

    def parse_toplevel(self) -> None:
        kind, text, line = self.peek()
        if text == "struct" and self.look(1)[0] == "id" and self.look(2)[1] == "{":
            self.parse_struct_def()
            return
        base, depth = self.parse_type()
        if self.peek()[1] == ";":                    # `struct foo;` 之类的前向声明
            self.next()
            return
        name = self.expect_kind("id")
        if self.peek()[1] == "(":
            self.parse_function(base, depth, name[1], name[2])
            return
        self.parse_object(base, depth, name[1], name[2], global_scope=True)

    def parse_struct_def(self) -> None:
        self.expect("struct")
        name = self.expect_kind("id")[1]
        self.expect("{")
        fields: Dict[str, int] = {}
        off = 0
        while self.peek()[1] != "}":
            fbase, fdepth = self.parse_type()
            fname = self.expect_kind("id")[1]
            width = 2 if fdepth else self.sizeof(fbase)
            if fname in fields:
                raise RgccError("结构体 %s 里字段 %s 重复" % (name, fname))
            fields[fname] = off
            off += width
            self.expect(";")
        self.expect("}")
        self.expect(";")
        self.structs[name] = fields
        self.struct_size[name] = off
        if off == 0:
            raise RgccError("结构体 %s 没有字段" % name)

    def parse_object(self, base: str, depth: int, name: str, line: int,
                     global_scope: bool = False) -> None:
        """``unsigned char x;`` / ``unsigned char a[8];`` / ``unsigned char *p;`` / ``struct T v;``"""
        if depth:
            sym = Sym(name=name, addr=self.alloc(2, 2), kind="ptr", size=2,
                      elem=base if base != "void" else U8)
            self.define(sym)
            self.parse_init_scalar(sym)
            return
        if self.peek()[1] == "[":                    # 数组
            self.next()
            n = self.const_expr("数组长度")
            self.expect("]")
            elem_size = self.sizeof(base)
            sym = Sym(name=name, addr=self.alloc(n * elem_size, 2 if elem_size > 1 else 1),
                      kind="array", size=n * elem_size, elem=base, count=n,
                      fields=dict(self.structs.get(base.split(":", 1)[-1], {})))
            self.define(sym)
            self.parse_init_array(sym, line)
            return
        size = self.sizeof(base)
        kind = "struct" if base.startswith("struct:") else "byte"
        sym = Sym(name=name, addr=self.alloc(size, 2 if size > 1 else 1), kind=kind,
                  size=size, elem=base,
                  fields=dict(self.structs.get(base.split(":", 1)[-1], {})))
        self.define(sym)
        self.parse_init_scalar(sym)

    def parse_init_scalar(self, sym: Sym) -> None:
        if self.peek()[1] == "=":
            self.next()
            v = self.const_expr("初值")
            self.initials[sym.addr] = bytes([v & 0xFF])
        self.expect(";")

    def parse_init_array(self, sym: Sym, line: int) -> None:
        """支持 ``= "ABC"``（自动补 0）与 ``= {1,2,3}``。"""
        if self.peek()[1] != "=":
            self.expect(";")
            return
        self.next()
        if self.peek()[0] == "str":
            raw = self.next()[1][1:-1]
            data = raw.encode("utf-8").decode("unicode_escape").encode("latin-1")
            if len(data) + 1 > sym.size:
                raise RgccError("第 %d 行：字符串 %r 放不进 %s" % (line, raw, sym.name))
            self.initials[sym.addr] = data + b"\x00"
            self.expect(";")
            return
        self.expect("{")
        vals: List[int] = []
        if self.peek()[1] != "}":
            while True:
                vals.append(self.const_expr("数组初值"))
                if self.peek()[1] == ",":
                    self.next()
                    continue
                break
        self.expect("}")
        self.expect(";")
        if len(vals) > sym.size:
            raise RgccError("第 %d 行：初值太多（%d > %d）" % (line, len(vals), sym.size))
        if vals:
            self.initials[sym.addr] = bytes(v & 0xFF for v in vals)

    def parse_function(self, base: str, depth: int, name: str, line: int) -> None:
        self.expect("(")
        params: List[Tuple[str, str]] = []
        if self.peek()[1] == "void" and self.look(1)[1] == ")":
            self.next()                                  # `(void)` = 无参数
        elif self.peek()[1] != ")":
            while self.peek()[1] != ")":
                pbase, pdepth = self.parse_type()
                pname = self.expect_kind("id")[1]
                params.append((pname, "ptr" if pdepth else U8))
                if self.peek()[1] == ",":
                    self.next()
                    continue
                break
        self.expect(")")
        if depth:
            raise RgccError("第 %d 行：函数不能返回指针（先只支持 void / unsigned char）" % line)
        if self.peek()[1] == ";":                    # 原型（库函数用）
            self.next()
            self.protos[name] = len(params)
            return
        ret = "void" if base == "void" else U8
        fn = Func(name=name, ret=ret, params=params, line=line)
        self.funcs[name] = fn
        self.scope = fn.scope
        self.cur_fn = fn
        for pname, ptype in params:
            fn.scope[pname] = Sym(name=pname, addr=self.alloc(2 if ptype == "ptr" else 1,
                                                             2 if ptype == "ptr" else 1),
                                  kind="ptr" if ptype == "ptr" else "byte",
                                  size=2 if ptype == "ptr" else 1)
        if ret == U8:
            fn.ret_slot = self.alloc(1)
        fn.body = self.parse_block()
        self.scope = {}
        self.cur_fn = None
        if name == "main":
            if base != "void" or params:
                raise RgccError("第 %d 行：main 必须是 void main(void)" % line)
            self.main = fn

    # ------------------------------------------------------------ 语句解析
    def parse_block(self) -> List[Stmt]:
        self.expect("{")
        out: List[Stmt] = []
        while self.peek()[1] != "}":
            st = self.parse_stmt()
            if st is not None:
                out.append(st)
        self.expect("}")
        return out

    def parse_stmt(self) -> Optional[Stmt]:
        kind, text, line = self.peek()
        if text in ("unsigned", "char", "struct") and not (
                text == "struct" and self.look(1)[1] == "("):
            base, depth = self.parse_type()
            name = self.expect_kind("id")
            self.parse_object(base, depth, name[1], name[2])
            return None
        if text == "return":
            self.next()
            if self.peek()[1] == ";":
                self.next()
                return Stmt(kind="return", line=line)
            if self.cur_fn is not None and self.cur_fn.ret != U8:
                raise RgccError("第 %d 行：void 函数不能 return 值" % line)
            ref = self.parse_rhs()
            self.expect(";")
            return Stmt(kind="return", src=ref, line=line)
        if text in ("while", "for", "if", "do", "switch"):
            return self.parse_control(text, line)
        if kind == "id":
            name = self.next()[1]
            if self.peek()[1] == "(":                # 函数调用语句
                return self.parse_call(name, line)
            self.i -= 1                              # 回退到标识符
            dst = self.parse_lvalue()
            self.expect("=")
            if self.peek()[0] == "id" and self.look(1)[1] == "(":   # x = f(...);
                callee = self.next()[1]
                args = self.parse_args()
                self.expect(";")
                if callee in self.protos and self.protos[callee] != len(args):
                    raise RgccError("第 %d 行：%s 原型里有 %d 个参数，调用给了 %d 个"
                                    % (line, callee, self.protos[callee], len(args)))
                return Stmt(kind="call", name=callee, args=args, dst=dst, line=line)
            src = self.parse_rhs()
            self.expect(";")
            if src.kind == "imm":
                if not 0 <= src.value <= 255:
                    raise RgccError("第 %d 行：结果 %d 超出字节 0..255" % (line, src.value))
                return Stmt(kind="assign", dst=dst, value=src.value, line=line)
            return Stmt(kind="copy", dst=dst, src=src, line=line)
        raise RgccError("第 %d 行：无法解析的语句 %r" % (line, text))

    def parse_for(self, line: int) -> Stmt:
        """``for (i = 0; i < N; i = i + 1) { … }`` —— N 必须是常量，循环在编译期展开。

        （ROP 链里没有条件分支，所以运行时循环只能靠 `while(1)`；固定次数的循环
        直接展开成 N 份直线代码。展开时循环变量按常量代入，于是 `a[i]` 的下标是常量。）
        """
        self.expect("for")
        self.expect("(")
        if self.peek()[1] in ("unsigned", "char"):
            self.parse_type()
        var = self.expect_kind("id")[1]
        self.expect("=")
        start = self.const_expr("for 初值")
        self.expect(";")
        if self.expect_kind("id")[1] != var:
            raise RgccError("第 %d 行：for 的条件里必须是循环变量 %s" % (line, var))
        op = self.next()[1]
        if op not in ("<", "<=", "!="):
            raise RgccError("第 %d 行：for 条件只支持 < / <= / !=" % line)
        bound = self.const_expr("for 上界")
        self.expect(";")
        if self.expect_kind("id")[1] != var:
            raise RgccError("第 %d 行：for 的步进里必须是循环变量 %s" % (line, var))
        self.expect("=")
        if self.expect_kind("id")[1] != var:
            raise RgccError("第 %d 行：for 步进只支持 i = i ± 常量" % line)
        sop = self.next()[1]
        if sop not in ("+", "-"):
            raise RgccError("第 %d 行：for 步进只支持 i = i ± 常量" % line)
        step = self.const_expr("for 步长")
        self.expect(")")
        if step <= 0:
            raise RgccError("第 %d 行：for 步长必须为正" % line)
        brace = self.i
        self.expect("{")
        body_start = brace
        depth = 1
        while depth:
            t = self.next()[1]
            if t == "{":
                depth += 1
            elif t == "}":
                depth -= 1
        body_end = self.i
        vals, v = [], start
        while (v < bound) if op == "<" else (v <= bound) if op == "<=" else (v != bound):
            vals.append(v)
            v += step if sop == "+" else -step
            if len(vals) > 1024:
                raise RgccError("第 %d 行：for 展开超过 1024 次，太大" % line)
        out: List[Stmt] = []
        for k in vals:
            saved = self.const_env
            self.const_env = dict(saved)
            self.const_env[var] = k
            self.i = body_start
            out.extend(self.parse_block())
            self.const_env = saved
        self.i = body_end
        return Stmt(kind="block", body=out, line=line)

    def parse_control(self, text: str, line: int) -> Stmt:
        if text == "for":
            return self.parse_for(line)
        if text == "if":                             # A7：「变量 > 变量/常量」
            self.next()
            self.expect("(")
            a = self.expect_kind("id")[1]
            op = self.next()[1]
            if op != ">":
                raise RgccError("第 %d 行：目前 if 只支持 a > b / a > 常量" % line)
            if self.peek()[0] == "num":
                cv = int(self.next()[1], 0)
                self.expect(")")
                body = self.parse_block()
                alt: List[Stmt] = []
                if self.peek()[1] == "else":
                    self.next()
                    alt = self.parse_block()
                return Stmt(kind="ifgt", a=a, cval=cv, body=body, alt=alt, line=line)
            b = self.expect_kind("id")[1]
            self.expect(")")
            body = self.parse_block()
            alt: List[Stmt] = []
            if self.peek()[1] == "else":
                self.next()
                alt = self.parse_block()
            return Stmt(kind="ifgt", a=a, b=b, body=body, alt=alt, line=line)
        if text != "while":
            raise RgccError("第 %d 行：v0 只支持 while(1)（if/%s 需要条件分支，见 A7）" % (line, text))
        if (text == "while" and self.look(1)[1] == "(" and self.look(2)[0] == "id"
                and self.look(3)[1] == ">"):          # A7：while (a > b) / while (a > 0)
            self.next()
            self.expect("(")
            a = self.expect_kind("id")[1]
            self.next()                                  # '>'
            if self.peek()[0] == "num":
                cv = int(self.next()[1], 0)
                self.expect(")")
                return Stmt(kind="whilegt", a=a, cval=cv, body=self.parse_block(), line=line)
            b = self.expect_kind("id")[1]
            self.expect(")")
            return Stmt(kind="whilegt", a=a, b=b, body=self.parse_block(), line=line)
        self.next()
        self.expect("(")
        if self.peek()[0] != "num" or int(self.next()[1], 0) != 1:
            raise RgccError("第 %d 行：目前只支持 while(1)；带条件的循环需要"
                            "「条件分支原语」（A7，见 docs/step-2 报告）" % line)
        self.expect(")")
        return Stmt(kind="loop", body=self.parse_block(), line=line)

    def parse_args(self) -> List[Tuple[str, object]]:
        self.expect("(")
        args: List[Tuple[str, object]] = []
        if self.peek()[1] != ")":
            while True:
                args.append(self.parse_arg())
                if self.peek()[1] == ",":
                    self.next()
                    continue
                break
        self.expect(")")
        return args

    def parse_call(self, name: str, line: int) -> Stmt:
        args = self.parse_args()
        self.expect(";")
        if name in self.protos and self.protos[name] != len(args):
            raise RgccError("第 %d 行：%s 原型里有 %d 个参数，调用给了 %d 个" % (
                line, name, self.protos[name], len(args)))
        return Stmt(kind="call", name=name, args=args, line=line)

    def parse_arg(self) -> Tuple[str, object]:
        """实参：常量 / 变量（取它的值）/ 数组、结构体、字符串（取地址）。"""
        kind, text, line = self.peek()
        if kind == "str":
            self.next()
            raw = text[1:-1]
            data = raw.encode("utf-8").decode("unicode_escape").encode("latin-1")
            if data not in self._str_ids:
                self._str_ids[data] = len(self.strings)
                self.strings.append(data)
            return ("str", self._str_ids[data])
        if kind == "num":
            self.next()
            return ("const", int(text, 0))
        if kind == "id":
            if self.look(1)[1] in ("[", "."):
                ref = self.parse_lvalue()
                if ref.kind != "const":
                    raise RgccError("第 %d 行：还不支持把指针指向的东西当实参（A4）" % line)
                return ("var", ref.value)
            self.next()
            sym = self.lookup(text, line)
            if sym.kind in ("array", "struct"):
                return ("const", sym.addr)           # 取地址
            if sym.kind == "ptr":
                if self.cur_fn is not None and text in [n for n, _ in self.cur_fn.params]:
                    return ("ptrparam", text)        # 编译期可解析（内联时绑定）
                return ("ptrvar", text)              # 运行时指针：需要 A4
            return ("var", sym.addr)                 # 取该字节的值
        raise RgccError("第 %d 行：实参只支持常量/变量/数组名/字符串，实际 %r" % (line, text))

    # ------------------------------------------------------------ 左值/右值
    def parse_lvalue(self) -> MemRef:
        kind, text, line = self.peek()
        if text == "*":                                   # *p
            self.next()
            inner = self.parse_lvalue()
            if inner.kind == "ptrparam":
                return inner
            raise RgccError("第 %d 行：解引用需要编译期已知的指针（A4 的运行时指针还没做）" % line)
        if kind != "id":
            raise RgccError("第 %d 行：左值必须是变量/数组元素/结构体字段，实际 %r" % (line, text))
        self.next()
        sym = self.lookup(text, line)
        if sym.kind == "ptr":
            if not (self.cur_fn is not None and text in [n for n, _ in self.cur_fn.params]):
                raise RgccError("第 %d 行：指针变量 %s 的解引用需要 A4 的字节传送" % (line, text))
            ref = MemRef(kind="ptrparam", var=text, note="*" + text)
            cur = Sym(name="(*%s)" % text, addr=0, kind="byte", size=1)
            while self.peek()[1] == "[":
                self.next()
                idx = self.const_expr("指针下标")
                self.expect("]")
                ref.offset += idx
                ref.note = "%s[%d]" % (text, idx)
            while self.peek()[1] in (".", "->"):
                self.next()
                fname = self.expect_kind("id")[1]
                if fname not in sym.fields:
                    raise RgccError("第 %d 行：%s 没有字段 %s" % (line, ref.note, fname))
                ref.offset += sym.fields[fname]
                ref.note += "." + fname
            return ref
        ref = MemRef(kind="const", value=sym.addr, note=text)
        cur = sym
        while True:
            t = self.peek()[1]
            if t == "[":
                if cur.kind != "array":
                    raise RgccError("第 %d 行：%s 不是数组" % (line, ref.note))
                self.next()
                # 先当常量算（for 展开时循环变量已被绑定为常量）；算不动再按 A4 运行时下标
                _save = self.i                      # const_expr 失败时要把词法位置退回来
                try:
                    idx = self.const_expr("数组下标")
                except RgccError:
                    self.i = _save
                    ivar = self.expect_kind("id")[1]
                    self.expect("]")
                    elem_size = self.sizeof(cur.elem)
                    ref = MemRef(kind="runtime", arr_base=cur.addr, elem_size=elem_size,
                                 var=ivar, note="%s[%s]" % (cur.name, ivar))
                    cur = Sym(name=ref.note, addr=0, kind="byte", size=elem_size, elem=cur.elem)
                    continue
                self.expect("]")
                if not 0 <= idx < cur.count:
                    raise RgccError("第 %d 行：下标 %d 越界（%s 长 %d）" % (line, idx, cur.name, cur.count))
                elem_size = self.sizeof(cur.elem)
                ref.value = cur.addr + idx * elem_size
                ref.note = "%s[%d]" % (cur.name, idx)
                cur = Sym(name=ref.note, addr=ref.value,
                          kind="struct" if cur.elem.startswith("struct:") else "byte",
                          size=elem_size, elem=cur.elem,
                          fields=dict(self.structs.get(cur.elem.split(":", 1)[-1], {})))
                continue
            if t == "." or t == "->":
                if t == "->":
                    raise RgccError("第 %d 行：'->' 需要指针（A4）" % line)
                self.next()
                fname = self.expect_kind("id")[1]
                if cur.kind != "struct" or fname not in cur.fields:
                    raise RgccError("第 %d 行：%s 没有字段 %s" % (line, ref.note, fname))
                ref.value = cur.addr + cur.fields[fname]
                ref.note = ref.note + "." + fname
                cur = Sym(name=ref.note, addr=ref.value, kind="byte", size=1)
                continue
            break
        return ref

    def parse_rhs(self) -> MemRef:
        """右值：``a[2]`` / ``p.x`` 这类内存位置，或常量/单变量的表达式。"""
        if self.peek()[1] == "*" or (self.peek()[0] == "id" and self.look(1)[1] in ("[", ".")):
            return self.parse_lvalue()
        ep = _ExprParser(self.toks[self.i:])
        e = ep.parse()
        self.i += ep.i                                   # parse 不消费 ';'
        got = self.fold(e)
        if got.kind == "const":
            return MemRef(kind="imm", value=got.value, note=str(got.value))
        sym = self.lookup(got.var, self.peek()[2])
        if sym.kind != "byte":
            raise RgccError("第 %d 行：%s 不是单字节变量（数组/结构体要先写下标）"
                            % (self.peek()[2], got.var))
        return MemRef(kind="const", value=sym.addr, note=got.var)

    def const_expr(self, what: str) -> int:
        """常量表达式（下标/长度/初值）——必须是编译期可算的。"""
        ep = _ExprParser(self.toks[self.i:])
        e = ep.parse(stops=("]", ")", ",", "}", ";"))
        self.i += ep.i
        got = self.fold(e)
        if got.kind != "const":
            raise RgccError("%s 必须是编译期常量（运行时算术/索引见 A4/A7）" % what)
        return got.value


# ----------------------------------------------------------------- 表达式
_BIN_PREC = {("|",): 1, ("^",): 2, ("&",): 3, ("<<", ">>"): 4,
             ("+", "-"): 5, ("*", "/", "%"): 6}


@dataclass
class Expr:
    """表达式节点：``op`` 为 None 时 ``value`` 是常量或变量名。"""

    op: Optional[str] = None
    value: object = None
    left: Optional["Expr"] = None
    right: Optional["Expr"] = None


@dataclass
class Assign:
    """``dst = expr;`` 的语义化结果：常量或单变量引用。"""

    kind: str          # 'const' | 'var'
    value: int = 0
    var: str = ""


def _fold(e: Expr, env: Optional[Dict[str, int]] = None) -> Assign:
    """把表达式折叠成"能落地"的形式，否则报错说明缺什么。

    本 ROM 的可内联 gadget 里没有通用算术（第 2 步实测：干净 ALU 只有 48 种
    固定的寄存器/立即数组合），所以 rGCC 只能把
      * **常量表达式** 在编译期算出来（`x = 2*(3+4);`）
      * **单变量引用** 变成运行时内存搬运（`x = y;`，用 BP 切换 + L/ST）
    其它一律明确报错，不生成错代码。
    """
    if e.op is None:
        if isinstance(e.value, Assign):
            v = e.value
            if v.kind == "var" and env and v.var in env:      # 循环变量在展开后是常量
                return Assign("const", env[v.var])
            return v
        return Assign("const", int(e.value))
    if e.op == "u-":
        a = _fold(e.left, env)
        if a.kind == "const":
            return Assign("const", -a.value)
        raise RgccError("一元 '-' 只支持常量操作数（本 ROM 无通用算术 gadget）")
    if e.op == "u~":
        a = _fold(e.left, env)
        if a.kind == "const":
            return Assign("const", ~a.value)
        raise RgccError("一元 '~' 只支持常量操作数（本 ROM 无通用算术 gadget）")
    a, b = _fold(e.left, env), _fold(e.right, env)
    if a.kind == "const" and b.kind == "const":
        x, y = a.value, b.value
        try:
            v = {"+": x + y, "-": x - y, "*": x * y,
                 "&": x & y, "|": x | y, "^": x ^ y,
                 "<<": x << y, ">>": x >> y,
                 "/": x // y, "%": x % y}[e.op]
        except ZeroDivisionError:
            raise RgccError("常量表达式里除以 0")
        return Assign("const", v)
    raise RgccError("运行时算术运算 '%s' 暂不支持：本 ROM 没有可用的通用 "
                    "ALU gadget（见 docs/step-2 报告）。可直接写常量表达式，"
                    "或改用单变量赋值 `x = y;`" % e.op)


class _ExprParser:
    """优先级爬升的表达式解析（只做语法，语义交给 ``_fold``）。"""

    def __init__(self, toks):
        self.t = toks
        self.i = 0

    def peek(self):
        return self.t[self.i]

    def take(self):
        tok = self.t[self.i]
        self.i += 1
        return tok

    def parse(self, stops=(";",)) -> Expr:
        e = self.binary(1)
        if self.peek()[1] not in stops:
            raise RgccError("第 %d 行：表达式里多余的记号 %r（期望 %s）"
                            % (self.peek()[2], self.peek()[1], "/".join(stops)))
        return e

    def binary(self, min_prec: int) -> Expr:
        left = self.unary()
        while True:
            kind, text, _line = self.peek()
            if kind != "op":
                break
            prec = next((p for ops, p in _BIN_PREC.items() if text in ops), None)
            if prec is None or prec < min_prec:
                break
            self.take()
            right = self.binary(prec + 1)
            left = Expr(op=text, left=left, right=right)
        return left

    def unary(self) -> Expr:
        kind, text, line = self.take()
        if kind == "op" and text in ("-", "~", "+"):
            operand = self.unary()
            return operand if text == "+" else Expr(op="u" + text, left=operand)
        if kind == "num":
            return Expr(value=int(text, 0))
        if kind == "id":
            return Expr(value=Assign("var", var=text))
        if text == "(":
            e = self.binary(1)
            if self.take()[1] != ")":
                raise RgccError("第 %d 行：括号不匹配" % line)
            return e
        raise RgccError("第 %d 行：表达式里出现无法解析的 %r" % (line, text))


# ----------------------------------------------------------------- 代码生成
def compile_source(src: str, backend: Backend, data_base: int = 0xD180,
                   lib: Optional[object] = None) -> CompileUnit:
    """把 C 子集源码编译成 nX-U16 机器码，并核对每条指令都有 gadget。

    ``lib``：``crop/libabi.py`` 的 ``Library``；给了才允许调用 ROM 库例程。
    用户函数用**内联**实现（ROP 链没有真正的调用栈）：调用点先把实参写进形参槽，
    再把函数体展开一遍；``return`` 编成一条无条件跳转（L3 已有）。
    """
    parser = Parser(src, data_base)
    globals_, main_body = parser.parse()

    # ---- 初值/字符串常量：放在所有变量之后（留 8 字节避开块写的 2 字节溢出）----
    def _strs_for_userfuncs(stmts, out):
        for st in stmts:
            if st.kind == "call" and st.name in parser.funcs:
                if st.name in parser.funcs:
                    for k, v in st.args:
                        if k == "str":
                            out.add(v)
            if st.body:
                _strs_for_userfuncs(st.body, out)
        return out

    need_data: set = set()
    for fn in list(parser.funcs.values()):
        _strs_for_userfuncs(fn.body, need_data)

    # 说明：字符串常量现在**骑在链上**（用内联数据载体 gadget），不再写进数据区；
    # 只有"放不下载体"的长字符串才回退到旧的"数据区 + 块写"路径。
    str_addr: Dict[int, int] = {}
    data: Dict[int, bytes] = dict(parser.initials)
    fallback: Dict[int, int] = {}
    at = (parser.next_addr + 8) & ~1
    for idx, raw in enumerate(parser.strings):
        if (idx not in need_data) and backend.carrier_len and len(raw) + 1 <= backend.carrier_len:
            continue                                   # 走载体（骑链）
        fallback[idx] = at
        data[at] = raw + b"\x00"
        at = (at + len(raw) + 2) & ~1
    str_addr = fallback

    code = bytearray()
    listing: List[str] = []
    label_off: Dict[int, int] = {}
    label_name: Dict[int, str] = {}
    fixups: List[Tuple[int, int]] = []
    label_chain: Dict[int, int] = {}
    pad_total = [0]        # pad 标记在链上会展开（2→4 / 2→8）：标签偏移必须补这个增量
    node = [0]
    inline_stack: List[str] = []
    ptr_stack: List[Dict[str, int]] = []      # 指针形参 → 编译期已知地址（内联时绑定）

    def emit(b: bytes, text: str) -> int:
        off = len(code)
        code.extend(b)
        listing.append("%04X  %-11s %s" % (off, b.hex(" ").upper(), text))
        return off

    def new_label(tag: str) -> int:
        node[0] += 1
        label_off[node[0]] = -1
        label_name[node[0]] = "L%d_%s" % (node[0], tag)
        return node[0]

    def place(lid: int) -> None:
        label_off[lid] = len(code)                     # .bin 偏移：解释器靠它打锚点
        label_chain[lid] = len(code) + pad_total[0]    # 链偏移：跳转目标要用这个
        listing.append("      %-22s // = %04X (链 %04X)"
                       % (label_name[lid] + ":", len(code), label_chain[lid]))

    def jump(lid: int, text: str) -> None:
        at2 = emit(bytes([0x00, 0xF0, 0x00, 0x00]), text)
        fixups.append((at2, lid))

    def ptr_addr(name: str, line: int) -> int:
        for frame in reversed(ptr_stack):
            if name in frame:
                return frame[name]
        raise RgccError("第 %d 行：指针 %s 没有绑定到编译期已知的地址"
                        "（运行时指针需要 A4）" % (line, name))

    def addr(ref: MemRef, line: int) -> int:
        if ref.kind == "const":
            return ref.value
        if ref.kind == "ptrparam":
            return ptr_addr(ref.var, line) + ref.offset
        raise RgccError("第 %d 行：地址要在运行时算（A4 的字节传送还没做）" % line)

    def written_slots(stmts) -> set:
        out = set()
        for st in stmts:
            if st.dst is not None and st.kind in ("assign", "copy"):
                out.add(st.dst.value)
            if st.body:
                out |= written_slots(st.body)
        return out

    def subst_consts(stmts, const_slots: Dict[int, int]) -> List[Stmt]:
        """把"源是已知常量槽"的搬运改写成常量赋值 —— 这样 codegen 的块写归并就能接手。"""
        out: List[Stmt] = []
        for st in stmts:
            if (st.kind == "copy" and st.src is not None and st.src.kind == "const"
                    and st.src.value in const_slots):
                out.append(Stmt(kind="assign", dst=st.dst,
                                value=const_slots[st.src.value], line=st.line))
            elif st.kind in ("block", "loop") and st.body:
                out.append(Stmt(kind=st.kind, body=subst_consts(st.body, const_slots),
                                line=st.line))
            else:
                out.append(st)
        return out

    def idx_fits(idx: int) -> bool:
        return len(parser.strings[idx]) + 1 <= backend.carrier_len

    raw_jumps: Dict[int, int] = {}
    a7_markers: Dict[int, str] = {}
    verbatim: List[bytes] = []
    pending_carriers: List[int] = []
    # A7 用：一个恒为 0 的 1 字节变量（把 16 位寄存器的高字节清零）
    zero_addr = parser.alloc(1)
    scratch_addr = parser.alloc(8, 2)          # A4：把寄存器 R0..R7 落到这里的临时区
    tmp16_addr = parser.alloc(2, 2)            # 16 位地址临时单元
    parser.initials[zero_addr] = b"\x00"
    if lib is not None:
        lib.zero_addr = zero_addr
        lib.scratch_addr = scratch_addr
        lib.tmp16_addr = tmp16_addr

    def emit_carriers() -> None:
        """把"骑链字符串"统一放在**程序末尾**：ROM 例程的栈帧只占 SP 以下的已走链段，
        链尾在 SP 之上，不会被踩。"""
        for idx in pending_carriers:
            raw = parser.strings[idx] + b"\x00"
            payload = raw + b"\x00" * (backend.carrier_len - len(raw))
            emit(backend.carrier_ins + payload,
                 "字符串骑链 #%d @载体%05X ← %r" % (idx, backend.carrier_addr, raw[:-1]))

    def resolve(args, line: int = 0, allow_carrier: bool = True) -> List[Tuple[str, int]]:
        out: List[Tuple[str, int]] = []
        for k, v in args:
            if k == "str":
                if allow_carrier and backend.carrier_len and idx_fits(v):
                    if v not in pending_carriers:
                        pending_carriers.append(v)
                    out.append(("const", 0x8000 | pending_carriers.index(v)))   # 哨兵
                    continue
                out.append(("const", str_addr[v]))
            elif k == "ptrparam":
                out.append(("const", ptr_addr(v, line)))
            elif k == "ptrvar":
                raise RgccError("第 %d 行：把指针变量当实参需要 A4 的字节传送"
                                "（编译期已知的指针请用形参或数组名）" % line)
            else:
                out.append((k, v))
        return out

    # ---- 初值/字符串写入：程序一开头做（块写 gadget，6 字节一组，地址递增）----
    chunk_max = backend.blk_pop_len or 6          # 一个 POP QRn 能装多少就写多少（VerF=8）
    for daddr in sorted(data):
        blob = data[daddr]
        for off in range(0, len(blob), chunk_max):
            chunk = blob[off:off + chunk_max]
            emit(backend.block_write(daddr + off, chunk),
                 "初值 [%04X] ← %s" % (daddr + off, " ".join("%02X" % c for c in chunk)))

    all_objs = list(globals_.values()) + [s for f in parser.funcs.values()
                                          for s in f.scope.values()]

    def gen(stmts: List[Stmt], scope: Dict[str, Sym], cur: Func,
            end_label: Optional[int]) -> None:
        from .libabi import Param as _PR      # 本函数内多处用到

        def _emit_padded_routine(nm, note, pad):
            """发一条"内部有 POP 的例程"：例程字节 + 链填充标记（pad2/pad4/pad8）。"""
            rb = lib.routine(nm).code
            verbatim.append(rb)
            emit(rb, note)
            if pad:
                a7_markers[len(code)] = "pad%d" % pad
                emit(bytes([0xDE, {2: 0xB4, 4: 0xB2, 8: 0xB3}[pad]]),
                     "填充 %d 字节（例程内部 POP）" % pad)
                pad_total[0] += {2: 0, 4: 2, 8: 6}[pad]   # 展开后比标记多这么多字节

        def _vaddr_of(nm):
            sym = scope.get(nm)
            return sym.addr if hasattr(sym, "addr") else (sym or 0)

        i = 0
        while i < len(stmts):
            st = stmts[i]

            # ---------------- 函数调用 ----------------
            if st.kind == "call":
                fn = parser.funcs.get(st.name)
                if fn is not None:                       # 用户函数 → 内联
                    if st.name in inline_stack:
                        raise RgccError("第 %d 行：递归调用 %s（ROP 链没有真正的调用栈）"
                                        % (st.line, st.name))
                    if len(inline_stack) >= 6:
                        raise RgccError("第 %d 行：内联层数太深（>6）" % st.line)
                    args = resolve(st.args, st.line, allow_carrier=False)
                    if len(args) != len(fn.params):
                        raise RgccError("第 %d 行：%s 要 %d 个参数，给了 %d 个"
                                        % (st.line, st.name, len(fn.params), len(args)))
                    bind: Dict[str, int] = {}
                    const_slots: Dict[int, int] = {}
                    body_writes = written_slots(fn.body)
                    for (pname, ptype), (ak, av) in zip(fn.params, args):
                        slot = fn.scope[pname]
                        if ptype == "ptr":
                            # 指针形参在编译期就绑定成具体地址（内联展开 → 常量传播）
                            if ak == "const":
                                bind[pname] = av
                            elif ak == "ptrparam":
                                bind[pname] = ptr_addr(av, st.line)
                            else:
                                raise RgccError("第 %d 行：%s 的指针实参要在运行时算地址"
                                                "（A4 的字节传送还没做）" % (st.line, st.name))
                            continue
                        if ak == "const":
                            emit(backend.write_byte_imm(slot.addr, av),
                                 "%s ← %d（实参）" % (pname, av))
                            if slot.addr not in body_writes:      # 体内没改过 → 可以常量代入
                                const_slots[slot.addr] = av
                        else:
                            emit(backend.copy_var(slot.addr, av),
                                 "%s ← [%04X]（实参）" % (pname, av))
                    end = new_label("ret_" + fn.name)
                    listing.append("      // ---- 内联 %s()%s ----" % (
                        fn.name, "".join("  %s=%04X" % (k, v) for k, v in bind.items())))
                    inline_stack.append(st.name)
                    ptr_stack.append(bind)
                    gen(subst_consts(fn.body, const_slots), fn.scope, fn, end)
                    ptr_stack.pop()
                    inline_stack.pop()
                    place(end)
                    if st.dst is not None:               # `x = f(...)`
                        if fn.ret != "u8":
                            raise RgccError("第 %d 行：%s 没有返回值" % (st.line, st.name))
                        emit(backend.copy_var(addr(st.dst, st.line), fn.ret_slot),
                             "%s = [%04X]（返回值）" % (st.dst.note, fn.ret_slot))
                    i += 1
                    continue
                if lib is None:
                    raise RgccError("第 %d 行：调用了 %s，但编译时没给库表"
                                    "（tools/rgcc --labels labels.conf）" % (st.line, st.name))
                _fc = lib.funcs.get(st.name)
                if st.dst is not None and not getattr(_fc, "returns", False):
                    raise RgccError("第 %d 行：库函数 %s 没有返回值" % (st.line, st.name))
                try:
                    # RMUL：dst = src * K（K 是编译期常量）—— 用 er0-table 例程
                    if st.name == "rmul" and st.args and st.dst is not None:
                        from .libabi import Param as _PM
                        _mk, _mv = st.args[0]
                        _ma = _mv if _mk == "var" else _vaddr_of(_mv)
                        _K = int(st.args[1][1]) if len(st.args) > 1 else 1
                        emit(lib._var("ER0", _ma), "R0 = %s" % st.args[0][1])
                        emit(lib.marshal((_PM("s", "ER2", "u16"),), [("const", _K & 0xFF)]),
                             "R2 = %d" % _K)
                        emit(lib.marshal((_PM("b", "ER4", "u16"),), [("const", 0)]), "ER4 = 0")
                        verbatim.append(lib.routine("rt_er0_table").code)
                        emit(lib.routine("rt_er0_table").code, "ER0 = R0 * %d" % _K)
                        _s2 = lib.scratch_addr
                        emit(backend.blk_base + bytes([_s2 & 0xFF, (_s2 >> 8) & 0xFF])
                             + backend.blk_body, "R0..R7 → 临时区 %04X" % _s2)
                        _md = addr(st.dst, st.line)
                        emit(backend.copy_var(_md, _s2), "%s = %s * %d（经临时区）"
                             % (st.dst.note, st.args[0][1], _K))
                        i += 1
                        continue
                    # RPLOT：屏幕缓冲区画图两颗助手（16 位地址 + 写字节）
                    if st.name == "rplotcol" and len(st.args) >= 2:
                        # 画一整根柱子：地址只算一次，然后逐行 +24（省掉每行的地址链）
                        from .libabi import Param as _PC2
                        _ok, _ov = st.args[0]          # off（字节列，通常是变量 i）
                        _hk, _hv = st.args[1]          # h（高度）
                        _oa = [("var", _ov) if _ok == "var" else ("const", _ov)]
                        _ha = [("var", _hv) if _hk == "var" else ("const", _hv)]
                        _t = lib.tmp16_addr
                        off4, raw4 = lib.backend.var_load["ER4"]
                        # 列首字节地址 = 0xDDD4 + off（用 mem-add，比 er0-table 少 4 个槽）
                        emit(lib.marshal((_PC2("a", "ER8", "u16"),), [("const", _t)]), "ER8 = &tmp16")
                        emit(lib.marshal((_PC2("v", "ER2", "u16"),), [("const", 0xE3BC)]),
                             "ER2 = 0xDDD4 + 24*63（屏幕最后一行）")
                        _emit_padded_routine("rt_st_er2_er8", "tmp16 = 底部起画", 4)
                        emit(lib.marshal((_PC2("a", "ER8", "u16"),), [("const", _t)]), "ER8 = &tmp16")
                        emit(lib.marshal((_PC2("o", "ER2", "u16"),), _oa), "ER2 = off")
                        _emit_padded_routine("rt_mem_add", "tmp16 += off（列地址）", 4)
                        # 内部行循环：y = h; while (y > 0) { rdec(y); 写一行(0xFF) + tmp16 += 24 }
                        _yy = scope.get("y") or parser.lookup("y", st.line)
                        _ya2 = _yy.addr if hasattr(_yy, "addr") else _yy
                        emit(lib.marshal((_PC2("p", "R0", "u8"),), _ha), "R0 = h")
                        emit(backend.blk_base + bytes([_ya2 & 0xFF, (_ya2 >> 8) & 0xFF])
                             + backend.blk_body, "y = h（块写 R0..R7）")
                        gen([Stmt(kind="whilegt", a="y", cval=0, body=[
                            Stmt(kind="call", name="rdec", args=[("var", _ya2)], line=st.line),
                            Stmt(kind="call", name="rplotnext", args=[], line=st.line),
                        ], line=st.line)], scope, cur, end_label)
                        i += 1
                        continue
                    if st.name == "rplotnext":
                        from .libabi import Param as _PN
                        _t = lib.tmp16_addr
                        off4, raw4 = lib.backend.var_load["ER4"]
                        # ★纯 POP-PC 取列地址（mem-add 变体 ⇒ ER10 旧值 ⇒ MOV ER2,ER10）
                        emit(lib._read16_er2(_t), "ER2 = 列地址（纯 POP-PC）")
                        emit(lib.marshal((_PN("p", "R0", "u8"),), [("const", 255)]), "R0 = 0xFF")
                        verbatim.append(lib.rt_store())
                        emit(lib.rt_store(), "写 0xFF（一行 8 像素）")
                        # 列地址 += 24（下一行）
                        emit(lib.marshal((_PN("a", "ER8", "u16"),), [("const", _t)]), "ER8 = &tmp16")
                        emit(lib.marshal((_PN("o", "ER2", "u16"),), [("const", 0xFFE8)]), "ER2 = -24")
                        _emit_padded_routine("rt_mem_add", "tmp16 -= 24（往上长一行）", 4)
                        i += 1
                        continue
                    # RCHART：画图助手（寄存器装载都用现成槽）
                    if st.name == "r_dim":
                        emit(backend.write_byte_imm(0xD138, 0x03), "r_dim：0xD138 = 3")
                        i += 1
                        continue
                    if st.name == "r_xy" and len(st.args) >= 2:
                        _ak, _xv = st.args[0]
                        _ax = _xv if _ak == "var" else _vaddr_of(_xv)
                        emit(lib._var("ER0", _ax), "R0 = x, R1 = 0（零变量）")
                        i += 1
                        continue
                    if st.name == "r_set2":
                        _rb = lib.routine("r_set2").code
                        verbatim.append(_rb); emit(_rb, "R2 = 2")
                        i += 1
                        continue
                    if st.name == "r_set_h" and st.args:
                        _ak, _hv = st.args[0]
                        _ha = _hv if _ak == "var" else _vaddr_of(_hv)
                        emit(lib._var("R9", _ha), "R9 = h")
                        emit(lib.routine("r_set_h").code, "R3 = R9")
                        i += 1
                        continue
                    if st.name == "r_blockdraw":
                        _rb = lib.routine("r_blockdraw").code
                        verbatim.append(_rb); emit(_rb, "画反色框")
                        # 经 rt-fix 进入（needs_lr）：LR 指向链上续接，不再需要 SP 补偿
                        i += 1
                        continue
                    # RINC：rinc(v) / rdec(v) → mem-add（地址与增量都是编译期常量）
                    if st.name in ("rinc", "rdec", "radd8") and st.args:
                        _ak, _v = st.args[0]
                        if _ak == "var":                       # 实参已经是地址
                            _va = _v
                        else:
                            _sym = scope.get(_v)
                            _va = _sym.addr if hasattr(_sym, "addr") else (_sym or 0)
                        _delta = 1 if st.name == "rinc" else (0xFFFF if st.name == "rdec"
                                                              else (st.args[1][1] if len(st.args) > 1 else 1))
                        emit(lib.marshal(lib.funcs[st.name].params,
                                         [("const", _va), ("const", _delta)]),
                             "%s(%s)：地址 %04X、增量 %d" % (st.name, _v, _va,
                                                            _delta if _delta < 0x8000 else _delta - 0x10000))
                        verbatim.append(lib.routine(st.name).code)
                        emit(lib.routine(st.name).code, "mem-add 例程")
                        a7_markers[len(code)] = "pad4"
                        emit(bytes([0xDE, 0xB2]), "填充：例程内部 POP XR8 吃 4 字节")
                        i += 1
                        continue
                    _f = lib.funcs.get(st.name)
                    if _f is not None and getattr(_f, "raw_jump", False):
                        _a = lib.target_addr(st.name)
                        # 5 字节自证明标记：DE AD <lo> <hi> <csr>（含完整 20 位目标）
                        pad_total[0] += 2
                        emit(bytes([0xDE, 0xAD, _a & 0xFF, (_a >> 8) & 0xFF,
                                    (_a >> 16) & 0xF, 0x00]),      # 6 字节（偶数）⇒ 后续偏移保持对齐
                             "%s → %05X（裸跳转标记）" % (st.name, _a))
                        i += 1                      # ★gen 里是 while 循环，必须自己推进
                        continue
                    body_bytes = lib.emit_call(st.name, resolve(st.args, st.line))
                except RgccError:
                    raise
                except Exception as e:                   # LibError 等 → 编译错误
                    raise RgccError("第 %d 行：调用 %s 失败：%s" % (st.line, st.name, e))
                r = lib.routine(st.name)
                emit(body_bytes, "%s(...);   // 搬运参数 + 例程 @%05X（%d 条指令，%s 结尾）"
                     % (st.name, r.entry, r.ninsn, r.term))
                if st.dst is not None:
                    # 返回值在 R0（以及 R1..R7 的垃圾）⇒ 用块写族把寄存器堆写到目标变量
                    _da = addr(st.dst, st.line)
                    emit(backend.blk_base + bytes([_da & 0xFF, (_da >> 8) & 0xFF])
                         + backend.blk_body,
                         "%s = R0（库返回值，连带 R1..R7 一起写，布局需留 8 字节余量）" % st.dst.note)
                i += 1
                continue

            # ---------------- while (a > b) { … }（A7 循环）----------------
            if st.kind == "whilegt":
                if lib is None:
                    raise RgccError("第 %d 行：while 条件需要库表（--labels）" % st.line)
                def _wv(nm):
                    sym = scope.get(nm) or parser.lookup(nm, st.line)
                    return sym.addr if hasattr(sym, "addr") else sym
                lid_cond = new_label("whcond")
                place(lid_cond)
                _rhs_w = (("const", st.cval) if st.cval >= 0 else ("var", _wv(st.b)))
                emit(lib.emit_call("rcmp_gt", [_rhs_w, ("var", _wv(st.a))]),
                     "R0 = 1 ⟺ (%s ≤ %s)" % (st.a, st.b if st.cval < 0 else st.cval))
                a7_markers[len(code)] = "if"
                emit(bytes([0xDE, 0xAF]), "循环条件（行 %d）" % st.line)
                gen(st.body, scope, cur, end_label)              # block1 = 循环体（cond=0 时进入）
                jump(lid_cond, "B %s   // 回跳：再判条件" % label_name[lid_cond])
                a7_markers[len(code)] = "else"
                emit(bytes([0xDE, 0xB0]), "循环出口（cond=1 跳过循环体）")
                a7_markers[len(code)] = "end"
                emit(bytes([0xDE, 0xB1]), "while 结束")
                i += 1
                continue

            # ---------------- if (a > b) { } else { }（A7）----------------
            if st.kind == "ifgt":
                if lib is None:
                    raise RgccError("第 %d 行：if 需要库表（--labels）" % st.line)
                from .libabi import Param as _P
                def _vaddr(nm):
                    sym = scope.get(nm) or parser.lookup(nm, st.line)
                    return sym.addr if hasattr(sym, "addr") else sym
                # ★cmp-gt 的真实语义：R0 = 1 ⟺ (ER0 ≤ ER2)（见 ROM 0B61E 反汇编）
                #   所以：block1 = else（无 else 时 = 跳到末尾）、block2 = then
                # 直接用已验证的 ABI：rcmp_gt(b, a) ⇒ R0 = 1 ⟺ (a ≤ b)
                _rhs = (("const", st.cval) if st.cval >= 0 else ("var", _vaddr(st.b)))
                _cmp_bytes = lib.emit_call("rcmp_gt", [_rhs, ("var", _vaddr(st.a))])
                verbatim.append(_cmp_bytes)
                emit(_cmp_bytes, "R0 = 1 ⟺ (%s ≤ %s)"
                     % (st.a, st.b if st.cval < 0 else st.cval))
                a7_markers[len(code)] = "if"
                emit(bytes([0xDE, 0xAF]), "条件跳转（行 %d）" % st.line)
                # cond = 0（a > b）→ block1 = then；cond = 1（a ≤ b）→ block2 = else（或跳到末尾）
                gen(st.body, scope, cur, end_label)              # block1 = then
                lid = None
                if st.alt is None:
                    lid = new_label("ifend")
                    jump(lid, "B %s   // 无 else：a ≤ b 时跳过 then" % label_name[lid])
                a7_markers[len(code)] = "else"
                emit(bytes([0xDE, 0xB0]), "第二段起点（else）")
                if st.alt:
                    gen(st.alt, scope, cur, end_label)           # block2 = else
                if lid is not None:
                    place(lid)
                a7_markers[len(code)] = "end"
                emit(bytes([0xDE, 0xB1]), "if 结束")
                i += 1
                continue

            # ---------------- return ----------------
            if st.kind == "return":
                if end_label is None:
                    raise RgccError("第 %d 行：main 里不能 return（用 while(1) 挂住）" % st.line)
                if st.src is not None:
                    if cur.ret != "u8":
                        raise RgccError("第 %d 行：void 函数不能 return 值" % st.line)
                    if st.src.kind == "imm":
                        emit(backend.write_byte_imm(cur.ret_slot, st.src.value),
                             "返回值 ← %d" % st.src.value)
                    else:
                        emit(backend.copy_var(cur.ret_slot, addr(st.src, st.line)),
                             "返回值 ← %s" % st.src.label())
                jump(end_label, "B %s   // return" % label_name[end_label])
                i += 1
                continue

            # ---------------- 归并常量赋值（块写 gadget 一次 6 字节）----------------
            if st.kind == "assign" and backend.blk_pop:
                run = [st]
                j = i + 1
                while (j < len(stmts) and stmts[j].kind == "assign"
                       and addr(stmts[j].dst, stmts[j].line) == addr(run[-1].dst, run[-1].line) + 1
                       and len(run) < backend.blk_pop_len):
                    run.append(stmts[j])
                    j += 1
                a0 = addr(run[0].dst, run[0].line)
                a_end = addr(run[-1].dst, run[-1].line)
                busy = any(a0 <= o.addr <= a0 + backend.blk_pop_len + 1 for o in all_objs
                           if not (a0 <= o.addr <= a_end))
                if len(run) >= 3 and not busy:
                    blk = backend.block_write(a0, [x.value for x in run])
                    off = emit(blk, "块写 %04X..%04X = %s" % (
                        a0, a_end, " ".join("%02X" % x.value for x in run)))
                    listing.append("%04X  %-11s （POP QR%d 的内联 8 字节）" % (
                        off + 2,
                        " ".join("%02X" % b for b in blk[len(backend.blk_pop):len(backend.blk_pop) + 8]),
                        backend.blk_qr))
                    i = j
                    continue

            if st.kind == "assign":
                if st.dst.kind == "runtime":                    # A4：v[i] = 常量
                    from .libabi import Param as _PA
                    emit(lib.rt_element(st.dst.arr_base, st.dst.elem_size,
                                        _vaddr_of(st.dst.var)), "&%s → ER0" % st.dst.note)
                    # 设备已验证的路线：ER0 = &v[i]（er0-table），再补一次 ER2 = ER0
                    _e2 = lib.routine("er2_from_er0").code
                    verbatim.append(_e2); emit(_e2, "ER2 = ER0（目标地址）")
                    emit(lib.marshal((_PA("t", "R0", "u8"),), [("const", st.value)]),
                         "R0 ← %d" % st.value)
                    verbatim.append(lib.rt_store())
                    emit(lib.rt_store(), "%s = %d（A4 写）" % (st.dst.note, st.value))
                    i += 1
                    continue
                da = addr(st.dst, st.line)
                emit(backend.write_byte_imm(da, st.value),
                     "%s = %d;            // [%04X] ← %02X" % (st.dst.note, st.value, da, st.value))
            elif st.kind == "copy":
                from .libabi import Param as _PC
                if st.src.kind == "runtime":                    # A4：x = v[i]
                    emit(lib.rt_element(st.src.arr_base, st.src.elem_size,
                                        _vaddr_of(st.src.var)), "&%s → ER0" % st.src.note)
                    verbatim.append(lib.rt_load())
                    emit(lib.rt_load(), "R0 ← %s（A4 读）" % st.src.note)
                    _sx = lib.scratch_addr
                    emit(backend.blk_base + bytes([_sx & 0xFF, (_sx >> 8) & 0xFF])
                         + backend.blk_body, "R0..R7 → 临时区 %04X" % _sx)
                    da = addr(st.dst, st.line)
                    emit(backend.copy_var(da, _sx),
                         "%s = %s;            // [%04X] ← [%04X]（经临时区）"
                         % (st.dst.note, st.src.note, da, _sx))
                    i += 1
                    continue
                if st.dst.kind == "runtime":                    # A4：v[i] = x
                    emit(lib.rt_element(st.dst.arr_base, st.dst.elem_size,
                                        _vaddr_of(st.dst.var)), "&%s → ER0" % st.dst.note)
                    # 设备已验证的路线：ER0 = &v[i]（er0-table），再补一次 ER2 = ER0
                    _e2 = lib.routine("er2_from_er0").code
                    verbatim.append(_e2); emit(_e2, "ER2 = ER0（目标地址）")
                    emit(lib.marshal((_PC("t", "R0", "u8"),), [("var", addr(st.src, st.line))]),
                         "R0 ← %s" % st.src.note)
                    verbatim.append(lib.rt_store())
                    emit(lib.rt_store(), "%s = %s（A4 写）" % (st.dst.note, st.src.note))
                    i += 1
                    continue
                da, sa = addr(st.dst, st.line), addr(st.src, st.line)
                emit(backend.copy_var(da, sa),
                     "%s = %s;            // [%04X] ← [%04X]" % (st.dst.note, st.src.note, da, sa))
            elif st.kind == "block":                     # for 展开出来的直线代码
                gen(st.body, scope, cur, end_label)
            elif st.kind == "loop":
                lid = new_label("loop")
                listing.append("      // while(1) ----")
                place(lid)
                gen(st.body, scope, cur, end_label)
                jump(lid, "B %s   // 无条件回跳" % label_name[lid])
            else:                                        # pragma: no cover - 防御
                raise RgccError("未知语句 %s" % st.kind)
            i += 1

    gen(main_body, globals_, parser.main, None)
    emit_carriers()                                  # 骑链字符串放在最后

    # 回填跳转（B 是 4 字节绝对地址：低字节在先）
    for at2, lid in fixups:
        target = label_chain.get(lid, label_off[lid])
        if target < 0:                                   # pragma: no cover - 防御
            raise RgccError("内部错误：标签 %s 没有落地" % label_name[lid])
        code[at2 + 2] = target & 0xFF
        code[at2 + 3] = (target >> 8) & 0xFF

    # ---- 核对：每条指令都必须有 gadget（否则"解释器覆盖 100%"不成立）
    routines = tuple(getattr(lib, "routines", {}).values()) if lib is not None else ()
    verify_translatable(bytes(code), backend.db, routines=routines, verbatim=verbatim)

    allvars: Dict[str, Sym] = {}
    for f in list(parser.funcs.values()) + []:
        for k, v in f.scope.items():
            allvars.setdefault(k, v)
    allvars.update(globals_)
    return CompileUnit(a7_markers=a7_markers, source=src, vars=allvars, body=main_body, code=bytes(code),
                       listing=listing, data_base=data_base, strings=data,
                       protos=dict(parser.protos), funcs=dict(parser.funcs),
                       structs=dict(parser.structs), initials=dict(parser.initials))


def _label_offset(listing: Sequence[str], label: str) -> int:
    for ln in listing:
        if ln.strip().startswith(label + ":"):
            return int(ln.split("起点 =")[1].strip(), 16)
    raise RgccError("内部错误：找不到标签 %s" % label)


def verify_translatable(code: bytes, db: GadgetDB, routines: Sequence = (),
                        verbatim: Sequence[bytes] = ()) -> None:
    """逐**块**核对：ROM 里存在"同字节 + 后面紧跟 POP PC"的 gadget。

    与解释器的 L1 完全同构：贪心取最长可匹配块；取数原语（POP）按其 ``sp_delta``
    跳过内联数据；块内允许含 POP（解释器会补零）。

    ``routines``：ROM 例程（``crop/routines.py``）。它们在解释器里各占一个链槽，
    ``.bin`` 里出现的是"入口到结尾"的整段字节，所以这里也要按整段识别。
    """
    by_code = {r.code: r for r in routines}
    carrier = getattr(db, "carrier_marker", b"")
    a = 0
    while a + 2 <= len(code):
        _vb = next((b for b in verbatim if code.startswith(b, a)), None)
        if _vb is not None:
            a += len(_vb)                     # 编译器直接发射的例程/gadget 字节：整块跳过
            continue
        if code[a] == 0xDE and code[a + 1] in (0xAF, 0xB0, 0xB1, 0xB2, 0xB3, 0xB4):
            a += 2                                        # A7 条件跳转标记：整块跳过
            continue
        if code[a] == 0xDE and code[a + 1] == 0xAD and len(code) >= a + 5:
            a += 6                                        # 裸跳转标记：整块跳过（6 字节，保持偶数对齐）
            continue
        if carrier and code.startswith(carrier[0], a) and len(code) >= a + len(carrier[0]) + carrier[1]:
            a += len(carrier[0]) + carrier[1]          # 内联数据载体：整块跳过
            continue
        hit = None
        for c, r in by_code.items():
            if code.startswith(c, a):
                hit = r
                break
        if hit is not None:
            a += len(hit.code)
            continue
        ins = _dec.decode_at(code, a)
        if ins is None:
            raise RgccError("@%04X 字节 %02X %02X 不是合法指令" % (a, code[a], code[a + 1]))
        if ins.cls == "pop_data":
            if not db.lookup(ins.raw):
                raise RgccError("@%04X %s：ROM 里没有这个取数原语" % (a, ins.text))
            a += ins.size + ins.sp_delta
            continue
        if ins.cls == "jump":
            a += ins.size
            continue
        # 贪心最长块（与 interp 的 L1 同构）
        best = 0
        end = a
        b = a
        n = 0
        while n <= db.max_insns and b + 2 <= len(code):
            cur = _dec.decode_at(code, b)
            if cur is None or cur.cls not in ("normal", "pop_data"):
                break
            b += cur.size
            n += 1
            if db.lookup(bytes(code[a:b])):
                best, end = n, b
        if not best:
            raise RgccError("@%04X %s：ROM 里没有等价 gadget" % (a, ins.text))
        a = end
