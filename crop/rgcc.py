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

        # 块写：从 ROM 里扫"LEA [ERk] 紧跟 ST QRm,[EA+]"的 gadget（一次槽写多字节）
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
                self.blk_pop = self._single("POP %s" % qreg)[1]     # 值装载
                self.blk_pop_len = self._single("POP %s" % qreg)[2].sp_delta
                self.blk_base = self._single("POP %s" % preg)[1]    # 目标地址装载
                # ★入口必须是 "LEA [ERk]" 这条指令本身，而不是外层 gadget 的起点：
                #   起点之前的 L QR0,[EA+]/L ER8,[EA+] 会用垃圾 EA 读数据，
                #   把我们刚装进 R0..R9 的值冲掉（真机实测踩过这个坑）。
                start = ins[k].addr - a
                self.blk_gadget_addr = ins[k].addr
                self.blk_body = g.code[start:-2]                   # 去掉结尾 POP PC
                self.blk_qr, self.blk_base_reg = qr, preg
                break
            if self.blk_pop:
                break

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

        return (self.base_pop + base(src_addr) + self.slot_load
                + self.base_pop + base(dst_addr) + self.slot_store)

    def describe(self) -> str:
        return ("常量写内存: POP→%s + ST @%05X（内联 %d 字节）；"
                "变量槽: %s[%s] L@%05X；基址装载 %s" % (
                    self.const_value_reg, self.const_store_addr, self.const_pop_len,
                    self.slot_off, self.slot_base, self.slot_load_addr,
                    self.base_pop.hex().upper()))

    @staticmethod
    def chain_data(addr: int, value: int) -> bytes:
        return bytes([addr & 0xFF, (addr >> 8) & 0xFF, value & 0xFF, 0])

    def op_slots(self, addr: int, value: int) -> List[Tuple[int, bytes]]:
        """返回这次写内存要放进 ROP 链的 (gadget 地址, 链上数据) 列表。"""
        return [(self.const_store_addr, b"")]


# ----------------------------------------------------------------- 前端
@dataclass
class Var:
    name: str
    addr: int


@dataclass
class Stmt:
    kind: str                  # 'assign' | 'loop'
    var: Optional[str] = None
    value: int = 0
    body: List["Stmt"] = field(default_factory=list)
    line: int = 0
    src: str = ""                      # kind == 'copy' 时的源变量


@dataclass
class CompileUnit:
    source: str
    vars: Dict[str, Var]
    body: List[Stmt]
    code: bytes
    listing: List[str]
    data_base: int = 0xD180


_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<comment>//[^\n]*)
  | (?P<num>0[xX][0-9a-fA-F]+|\d+)
  | (?P<id>[A-Za-z_]\w*)
  | (?P<op><<|>>|[-+*/%&|^~])
  | (?P<punct>[{}();=])
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
    def __init__(self, src: str, data_base: int):
        self.toks = _tokens(src)
        self.i = 0
        self.vars: Dict[str, Var] = {}
        self.next_addr = data_base

    # ---- 基础
    def peek(self) -> Tuple[str, str, int]:
        return self.toks[self.i]

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

    # ---- 语法
    def parse(self) -> Tuple[Dict[str, Var], List[Stmt]]:
        while self.peek()[1] != "void":
            self.parse_decl()
        self.expect("void")
        fn = self.expect_kind("id")
        if fn[1] != "main":
            raise RgccError("第 %d 行：v0 只支持 main()" % fn[2])
        self.expect("(")
        if self.peek()[1] == "void":
            self.next()
        self.expect(")")
        body = self.parse_block()
        if self.peek()[0] != "eof":
            raise RgccError("第 %d 行：main() 之后还有内容" % self.peek()[2])
        return self.vars, body

    def parse_decl(self) -> None:
        line = self.peek()[2]
        if self.next()[1] != "unsigned":
            raise RgccError("第 %d 行：只支持 unsigned char 声明" % line)
        self.expect("char")
        name = self.expect_kind("id")[1]
        if name in self.vars:
            raise RgccError("第 %d 行：变量 %s 重复声明" % (line, name))
        self.vars[name] = Var(name=name, addr=self.next_addr)
        self.next_addr += 1
        self.expect(";")

    def parse_block(self) -> List[Stmt]:
        self.expect("{")
        out: List[Stmt] = []
        while self.peek()[1] != "}":
            out.append(self.parse_stmt())
        self.expect("}")
        return out

    def parse_stmt(self) -> Stmt:
        kind, text, line = self.peek()
        if text in ("while", "for", "if", "do", "switch"):
            if text != "while":
                raise RgccError("第 %d 行：v0 只支持 while(1)（条件分支待第 4 步）" % line)
            self.next()
            self.expect("(")
            if self.peek()[0] != "num":
                raise RgccError("第 %d 行：v0 只支持 while(1)（带条件的循环需要"
                                "「条件分支原语」，本 ROM 暂无，见 step-2 报告）" % line)
            v = self.next()[1]
            if int(v, 0) != 1:
                raise RgccError("第 %d 行：v0 只支持 while(1)；带条件的循环需要"
                                "「条件分支原语」，本 ROM 暂无（见 step-2 报告）" % line)
            self.expect(")")
            return Stmt(kind="loop", body=self.parse_block(), line=line)
        if kind == "id":
            name = self.next()[1]
            if name not in self.vars:
                raise RgccError("第 %d 行：未声明的变量 %s" % (line, name))
            self.expect("=")
            ep = _ExprParser(self.toks[self.i:])
            e = ep.parse()
            self.i += ep.i                          # 同步游标（parse 不消费 ';'）
            self.expect(";")
            got = _fold(e)
            if got.kind == "const":
                if not 0 <= got.value <= 255:
                    raise RgccError("第 %d 行：结果 %d 超出字节 0..255" % (line, got.value))
                return Stmt(kind="assign", var=name, value=got.value, line=line)
            if got.var not in self.vars:
                raise RgccError("第 %d 行：未声明的变量 %s" % (line, got.var))
            return Stmt(kind="copy", var=name, value=0, src=got.var, line=line)
        raise RgccError("第 %d 行：无法解析的语句 %r" % (line, text))


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


def _fold(e: Expr) -> Assign:
    """把表达式折叠成"能落地"的形式，否则报错说明缺什么。

    本 ROM 的可内联 gadget 里没有通用算术（第 2 步实测：干净 ALU 只有 48 种
    固定的寄存器/立即数组合），所以 rGCC 只能把
      * **常量表达式** 在编译期算出来（`x = 2*(3+4);`）
      * **单变量引用** 变成运行时内存搬运（`x = y;`，用 BP 切换 + L/ST）
    其它一律明确报错，不生成错代码。
    """
    if e.op is None:
        return e.value if isinstance(e.value, Assign) else Assign("const", int(e.value))
    if e.op == "u-":
        a = _fold(e.left)
        if a.kind == "const":
            return Assign("const", -a.value)
        raise RgccError("一元 '-' 只支持常量操作数（本 ROM 无通用算术 gadget）")
    if e.op == "u~":
        a = _fold(e.left)
        if a.kind == "const":
            return Assign("const", ~a.value)
        raise RgccError("一元 '~' 只支持常量操作数（本 ROM 无通用算术 gadget）")
    a, b = _fold(e.left), _fold(e.right)
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

    def parse(self) -> Expr:
        e = self.binary(1)
        if self.peek()[1] != ";":
            raise RgccError("第 %d 行：表达式里多余的记号 %r" % (self.peek()[2], self.peek()[1]))
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
def compile_source(src: str, backend: Backend, data_base: int = 0xD180) -> CompileUnit:
    """把 C 子集源码编译成 nX-U16 机器码，并核对每条指令都有 gadget。"""
    parser = Parser(src, data_base)
    vars_, body = parser.parse()

    code = bytearray()
    listing: List[str] = []
    labels: Dict[int, str] = {}      # 结点 id → label
    fixups: List[Tuple[int, int]] = []   # (B 指令所在偏移, 目标结点 id)
    node_id = [0]

    def emit(b: bytes, text: str) -> int:
        off = len(code)
        code.extend(b)
        listing.append("%04X  %-11s %s" % (off, b.hex(" ").upper(), text))
        return off

    def gen(stmts: List[Stmt]) -> None:
        i = 0
        while i < len(stmts):
            st = stmts[i]
            # --- 块写：连续的「地址相邻的常量赋值」合并成 1 次 8 字节写 ---
            if st.kind == "assign" and backend.blk_pop:
                run = [st]
                j = i + 1
                while (j < len(stmts) and stmts[j].kind == "assign"
                       and vars_[stmts[j].var].addr == vars_[run[-1].var].addr + 1
                       and len(run) < backend.blk_pop_len - 2):
                    run.append(stmts[j])
                    j += 1
                if len(run) >= 3:
                    a0 = vars_[run[0].var].addr
                    off = emit(backend.block_write(a0, [x.value for x in run]),
                               "块写 %04X..%04X = %s" % (a0, a0 + len(run) - 1,
                                                       " ".join("%02X" % x.value for x in run)))
                    listing.append("%04X  %-11s （POP QR%d 的内联 8 字节）" % (
                        off + 2, " ".join("%02X" % b for b in backend.block_write(
                            a0, [x.value for x in run])[len(backend.blk_pop):len(backend.blk_pop) + 8]),
                        backend.blk_qr))
                    i = j
                    continue
            if st.kind == "assign":
                v = vars_[st.var]
                off = len(code)
                emit(backend.write_byte_imm(v.addr, st.value),
                     "%s = %d;            // [%04X] ← %02X" % (st.var, st.value, v.addr, st.value))
                listing.append("%04X  %-11s （POP XR0 的链数据）" % (off + 2, backend.chain_data(v.addr, st.value).hex(" ").upper()))
            elif st.kind == "copy":
                d, sr = vars_[st.var], vars_[st.src]
                emit(backend.copy_var(d.addr, sr.addr),
                     "%s = %s;            // [%04X] ← [%04X]" % (st.var, st.src, d.addr, sr.addr))
            elif st.kind == "loop":
                node_id[0] += 1
                lid = node_id[0]
                labels[lid] = "L%d" % lid
                start = len(code)
                listing.append("      %s:                       // while(1) 起点 = %04X" % (labels[lid], start))
                gen(st.body)
                # `B csr:addr` 4 字节：[0x00][0xF0][addr_lo][addr_hi]
                at = emit(bytes([0x00, 0xF0, 0x00, 0x00]),
                          "B %s                  // 无条件回跳" % labels[lid])
                fixups.append((at, lid))
            else:                                  # pragma: no cover - 防御
                raise RgccError("未知语句 %s" % st.kind)
            i += 1

    gen(body)
    # 回填跳转（B 是 4 字节绝对地址：低字节在先，段号在最后）
    for at, lid in fixups:
        # 目标必须落在自身代码里；段号固定 0（解释器只按 .bin 内偏移解析标签）
        target = _label_offset(listing, labels[lid])
        code[at + 2] = target & 0xFF
        code[at + 3] = (target >> 8) & 0xFF

    # ---- 核对：每条指令都必须有 gadget（否则"解释器覆盖 100%"不成立）
    verify_translatable(bytes(code), backend.db)
    return CompileUnit(source=src, vars=vars_, body=body, code=bytes(code),
                       listing=listing, data_base=data_base)


def _label_offset(listing: Sequence[str], label: str) -> int:
    for ln in listing:
        if ln.strip().startswith(label + ":"):
            return int(ln.split("起点 =")[1].strip(), 16)
    raise RgccError("内部错误：找不到标签 %s" % label)


def verify_translatable(code: bytes, db: GadgetDB) -> None:
    """逐**块**核对：ROM 里存在"同字节 + 后面紧跟 POP PC"的 gadget。

    与解释器的 L1 完全同构：贪心取最长可匹配块；取数原语（POP）按其 ``sp_delta``
    跳过内联数据；块内允许含 POP（解释器会补零）。
    """
    a = 0
    while a + 2 <= len(code):
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
