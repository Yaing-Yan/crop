"""ROP 词表（"这个 ROM 到底能当什么指令集用"）。

第 1 步的可行性评估给出一个硬结论：**逐字节块复用只能覆盖个位数~15%**。
所以解释器必须有一层"等价"实现，而"等价"能做什么，完全取决于 ROM 里
**实际存在**的原语。本模块从扫描结果里自动挖掘这些原语并给出语义分类，
这就是 rGCC（第 3 步）唯一被允许使用的"ROP 指令集"。

每条原语的分类（``Primitive.kind``）：

=================  ==========================================================
kind               含义 / 链上的契约
=================  ==========================================================
``pop``            ``POP <reg>; POP PC``：把链上的 n 字节装进寄存器（**任意常量**）
``mov_imm``        ``MOV <reg>, #imm; POP PC``：只能产生 ROM 里那个确定的 imm
``mov_reg``        ``MOV <reg>, <reg>; POP PC``
``load``           ``L <reg>, ...; POP PC``（BP/FP/ER 相对或绝对地址）
``store``          ``ST <reg>, ...; POP PC``
``alu``            算术/逻辑/移位/比较（注意是否改标志位）
``pivot``          ``MOV SP, ERn; …; POP PC``：栈枢轴，链层控制流的基础
``other``          其它
=================  ==========================================================

每个原语都带 ``clobbers``（被写到的寄存器/内存/标志），供上层做"副作用是否
可接受"的判断；链层契约 ``chain_in`` 给出它从链上取走的字节数。
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .gadget import Gadget, GadgetDB
from .nxu16 import decode as _dec

__all__ = ["Primitive", "mine_vocabulary", "vocab_report", "find_pop_gadgets",
           "find_pivots", "is_clean_pivot", "pick"]

_ALU_FLAG = {"ADD", "ADDC", "SUB", "SUBC", "CMP", "CMPC", "AND", "OR", "XOR",
             "SLL", "SRL", "SRA", "SLLC", "SRLC", "NEG", "DAA", "DAS", "T", "TB",
             "TE", "INC", "DEC", "EXTBW", "MUL", "DIV", "DSR"}
_NOFLAG = {"MOV", "L", "ST", "LEA", "PUSH", "POP"}


@dataclass
class Primitive:
    addr: int
    insns: Tuple[_dec.Insn, ...]
    kind: str
    detail: str
    chain_in: int                     # 从链上取走的字节总数
    clobbers: Tuple[str, ...] = ()    # 被写到的资源
    flags: frozenset = frozenset()    # gadget 的 pivot/push/... 标记
    skip: int = 0                     # pivot 专用：枢轴后 POP 掉的字节数

    @property
    def text(self) -> str:
        return " ; ".join(i.text for i in self.insns)

    @property
    def payload(self) -> int:
        """链上跟着本原语的"数据"字节数（不含结尾 POP PC 的 4 字节）。"""
        return self.chain_in - (4 if self.insns and self.insns[-1].cls == "term_pop_pc" else 0)


def _regs_written(ins: _dec.Insn) -> List[str]:
    """粗略地列出某条指令写到的寄存器/内存（ROP 视角，够用即可）。"""
    out: List[str] = []
    t = ins.text
    mn = ins.mnemonic
    if mn == "POP":
        out.append(ins.operands.strip())
    elif mn in ("MOV", "L", "LEA"):
        dst = ins.operands.split(",")[0].strip()
        out.append(dst)
        if mn == "MOV" and ins.operands.count(",") == 1 and dst.startswith("["):
            out.append("MEM")
    elif mn == "ST":
        out.append("MEM")
    elif mn == "INC" or mn == "DEC":
        out.append(ins.operands.strip())
    elif mn in _ALU_FLAG:
        out.append(ins.operands.split(",")[0].strip())
        if mn in ("ST",):
            out.append("MEM")
    if mn in _ALU_FLAG:
        out.append("FLAGS")
    if mn == "POP":
        out.append("SP")
    return out


def classify(g: Gadget) -> Primitive:
    """把 gadget 归入 ROP 词表的一类。"""
    insns = tuple(g.insns)
    body = insns[:-1] if insns and insns[-1].cls == "term_pop_pc" else insns
    clob: List[str] = []
    for i in body:
        clob += _regs_written(i)
    kind, detail = "other", g.insns[0].text if g.insns else ""
    skip = 0
    if "pivot" in g.flags:
        piv = next((i for i in body if i.cls == "sp_set"), None)
        skip = 0
        seen = False
        for i in body:
            if i is piv:
                seen = True
                continue
            if seen and i.sp_delta > 0:
                skip += i.sp_delta
        kind, detail = "pivot", "%s（枢轴后跳过 %d 字节）" % (piv.text if piv else "?", skip)
    elif len(body) == 1:
        i = body[0]
        if i.mnemonic == "POP":
            kind, detail = "pop", i.operands.strip()
        elif i.mnemonic == "MOV" and "#" in i.operands:
            kind, detail = "mov_imm", i.operands
        elif i.mnemonic == "MOV":
            kind, detail = "mov_reg", i.operands
        elif i.mnemonic in ("L", "LEA"):
            kind, detail = "load", i.operands
        elif i.mnemonic == "ST":
            kind, detail = "store", i.operands
        elif i.mnemonic in _ALU_FLAG:
            kind, detail = "alu", i.text
    return Primitive(addr=g.addr, insns=insns, kind=kind, detail=detail,
                     chain_in=g.chain_in, clobbers=tuple(dict.fromkeys(clob)),
                     flags=g.flags, skip=skip)


def mine_vocabulary(db: GadgetDB, max_body: int = 3) -> List[Primitive]:
    """从扫描结果里挖出"干净可内联"的原语（按地址排序）。"""
    out: List[Primitive] = []
    for addr in sorted(db.by_addr):
        g = db.by_addr[addr]
        if not g.inline_safe or g.ninsn > max_body or g.ninsn == 0:
            continue
        out.append(classify(db.rebuild(addr)))   # scan 阶段不保留指令文本，这里重建
    return out


def find_pop_gadgets(db: GadgetDB) -> Dict[str, List[int]]:
    """``POP Rn/ERn/XRn/QRn`` 单指令 gadget：链上取值原语（值任意）。

    返回 ``{寄存器名: [地址…]}``。这是 L2"等价转义"最有力的工具：
    `MOV Rn,#imm` / `MOV ERn,#imm` 都能用它 + 链上的数据实现。
    """
    res: Dict[str, List[int]] = collections.defaultdict(list)
    for addr in sorted(db.by_addr):
        g = db.by_addr[addr]
        if not g.inline_safe or g.ninsn != 1:
            continue
        i = db.rebuild(addr).insns[0]
        if i.cls == "pop_data" and i.mnemonic == "POP":
            res[i.operands.strip()].append(addr)
    return dict(res)


def find_pivots(db: GadgetDB) -> List[Primitive]:
    """所有栈枢轴原语（``MOV SP, ERn`` 型），按"枢轴后跳过字节数"排序。"""
    # 只保留"能续链"的枢轴：结尾必须是 POP PC。
    # 以 RT 结尾的枢轴（如 ``MOV SP,ER14; POP ER14; RT``）弹出的是硬件返回栈，
    # 单独使用会跳到未知地址，必须配合 rt-fix 机制，不能当普通跳转用。
    out = [classify(db.rebuild(a)) for a, g in sorted(db.by_addr.items())
           if "pivot" in g.flags and g.term == "pop_pc"]
    # 排序：越简单（指令越少）、跳过越短、地址越小越优先 —— 副作用越小，
    # rGCC 的寄存器分配越好做。
    out.sort(key=lambda p: (len(p.insns), p.skip, p.addr))
    return out


def is_clean_pivot(p: Primitive) -> bool:
    """枢轴的"干净"判据：体内除 ``MOV SP,ERn`` 外只允许 POP（不写内存、不动其它寄存器）。"""
    insns = [i for i in p.insns if i.cls != "term_pop_pc"]
    for i in insns:
        if i.cls == "sp_set":
            continue
        if i.cls == "pop_data":
            continue
        return False
    return any(i.cls == "sp_set" for i in insns)


def pick(addrs: Sequence[int], avoid_low00: bool = False) -> int:
    """在多个等价地址里挑一个：优先偶数地址，其次地址最小（结果确定，便于回归）。

    ``avoid_low00``：左式编码（``#-name;``）会把地址低字节 ``00`` 改成 ``01``，
    于是实际跳转地址会偏 1。所以左式必须避开低字节为 0 的 gadget。
    """
    cand = [a for a in addrs if not (avoid_low00 and (a & 0xFF) == 0)]
    if not cand:
        raise ValueError("没有可用候选地址（都被低字节 00 过滤掉了）")
    even = [a for a in cand if a % 2 == 0]
    return min(even or cand)


def vocab_report(db: GadgetDB, prims: Optional[Sequence[Primitive]] = None, top: int = 12) -> str:
    prims = list(prims if prims is not None else mine_vocabulary(db))
    by_kind: Dict[str, List[Primitive]] = collections.defaultdict(list)
    for p in prims:
        by_kind[p.kind].append(p)
    out = ["ROP 词表（本 ROM 里「干净可内联 + 指令数 ≤3」的原语，共 %d 条）" % len(prims)]
    for kind in ("pop", "mov_imm", "mov_reg", "load", "store", "alu", "pivot", "other"):
        lst = by_kind.get(kind)
        if not lst:
            continue
        cnt = collections.Counter(p.text for p in lst)
        out.append("  [%s] %d 条 / %d 种；覆盖的寄存器或形态：" % (kind, len(lst), len(cnt)))
        seen = collections.Counter()
        for p in lst:
            if p.kind in ("pop", "mov_reg", "load", "store"):
                seen[p.detail] += 1
            elif p.kind in ("mov_imm", "alu"):
                seen[p.detail.split(",")[0] if "," in p.detail else p.detail] += 1
        for d, n in seen.most_common(top):
            out.append("      %-28s ×%d" % (d, n))
        if kind == "pivot":
            for p in lst[:6]:
                out.append("      @%05X  %s" % (p.addr, p.text))
    return "\n".join(out)
