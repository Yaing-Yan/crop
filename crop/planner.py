"""路线 A：语义 gadget 规划器（允许无害副作用 / 组合）。

第 2 步的结论：**逐字节严格匹配只覆盖个位数~15%**，因为 ROM 里的指令序列绝大多数
不是"目标指令 + 紧跟 POP PC"。路线 A 放宽两档：

1. **无害副作用**：目标指令 ``T`` 可以落在某个 gadget 的中间，只要该 gadget 里
   *其它的* 指令只碰调用者声明的 **scratch 资源**（寄存器/内存），副作用就无害。
   这样一条 ``T`` 也能用 1 个链槽实现（``kind='equiv'``）。
2. **组合**（下一步）：一条 ``T`` 拆成"取数 → 运算 → 存回"多条 gadget。
   本 ROM 里能组合的形态要先由 ``capabilities()`` 报出来，再决定怎么做。

副作用模型来自 ``crop.nxu16.decode``：每条指令的 ``cls``/``sp_delta`` 已经能给出
"是否动 SP / 是否读写内存 / 写到哪个寄存器"，本模块把它聚合成 gadget 级的效果。

**安全判据（本项目采用）**：
* gadget 里不能有 ``POP*``（会从链上多取数据，超出解释器的槽约定）；
* 不能有 ``BC/B/BL/RT/SWI/BRK``（改变控制流）；
* 目标指令之外，写入的资源必须 ⊆ ``scratch``；且不得写 ``MEM`` 与 ``FLAGS``
  （除非调用者把 ``MEM``/``FLAGS`` 放进 scratch）。
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .gadget import Gadget, GadgetDB
from .nxu16 import decode as _dec

__all__ = ["Effect", "effect_of", "realize", "capabilities", "DEFAULT_SCRATCH"]

#: 默认可牺牲的 scratch：调用者不用它们做长期状态
DEFAULT_SCRATCH = frozenset({"R12", "R13", "R14", "R15", "BP", "FP", "EA"})


def _write_target(ins: _dec.Insn) -> Optional[str]:
    """这条指令"写"到的寄存器名（没有则 None）。"""
    ops = [o.strip() for o in ins.operands.split(",")] if ins.operands else []
    mn = ins.mnemonic
    if mn == "MOV" and ops:
        return ops[0] if not ops[0].startswith("[") else None
    if mn in ("L", "POP", "LEA", "INC", "DEC") and ops:
        return ops[0]
    if mn in ("ADD", "ADDC", "SUB", "SUBC", "AND", "OR", "XOR", "SLL", "SRL",
              "SRA", "SLLC", "SRLC", "MUL", "DIV", "NEG", "DAA", "DAS") and ops:
        return ops[0]
    return None


@dataclass
class Effect:
    """一个 gadget 的净效果（资源粒度，够用即可）。"""

    writes: Set[str] = field(default_factory=set)
    mem_write: bool = False
    flags: bool = False
    sp: int = 0
    chain_in: int = 0
    has_pop: bool = False
    has_flow: bool = False

    def clobbered(self) -> Set[str]:
        s = set(self.writes)
        if self.mem_write:
            s.add("MEM")
        if self.flags:
            s.add("FLAGS")
        if self.sp:
            s.add("SP")
        return s


def effect_of(g: Gadget) -> Effect:
    e = Effect()
    for i in g.insns:
        if i.cls == "term_pop_pc":
            continue
        if i.cls == "pop_data":
            e.has_pop = True
            continue
        if i.cls in ("branch", "jump", "call", "ret", "reti", "trap"):
            e.has_flow = True
            continue
        w = _write_target(i)
        if w:
            e.writes.add(w)
        if i.mnemonic == "ST" or (i.mnemonic == "MOV" and i.operands.startswith("[")):
            e.mem_write = True
        if i.mnemonic in ("ADD", "ADDC", "SUB", "SUBC", "CMP", "CMPC", "AND", "OR",
                          "XOR", "SLL", "SRL", "SRA", "SLLC", "SRLC", "NEG", "DAA",
                          "DAS", "MUL", "DIV", "INC", "DEC", "EXTBW", "T", "TB", "TE",
                          "DSR", "CPLC"):
            e.flags = True
        e.sp += i.sp_delta
    e.chain_in = g.chain_in
    return e


def _index_by_instruction(db: GadgetDB, max_body: int) -> Dict[str, List[int]]:
    """``指令文本 → [包含它的 gadget 地址…]``（含非内联型，交给判据筛）。"""
    idx: Dict[str, List[int]] = collections.defaultdict(list)
    for addr in sorted(db.by_addr):
        g = db.by_addr[addr]
        if g.ninsn == 0 or g.ninsn > max_body or g.term != "pop_pc":
            continue
        for ins in db.rebuild(addr).insns:
            if ins.cls == "term_pop_pc":
                continue
            idx[ins.text].append(addr)
    return dict(idx)


def realize(db: GadgetDB, target: _dec.Insn, scratch: Set[str] = DEFAULT_SCRATCH,
            max_body: int = 8, index: Optional[Dict[str, List[int]]] = None
            ) -> Optional[Tuple[int, Effect]]:
    """给一条目标指令找"1 个槽就能实现"的 gadget（允许无害副作用）。

    返回 ``(gadget 地址, 效果)``；找不到返回 None。
    """
    # MEM / SP 绝不能当 scratch：写了内存/动了栈就不是"无害副作用"。
    scratch = set(scratch) - {"MEM", "SP"}
    index = index if index is not None else _index_by_instruction(db, max_body)
    wildcard = "FLAGS" in scratch
    for addr in index.get(target.text, ()):
        g = db.rebuild(addr)
        e = effect_of(g)
        if e.has_pop or e.has_flow:
            continue                      # 会多取链上数据 / 改变控制流 → 不安全
        extra = e.clobbered() - scratch
        if extra and not wildcard:
            continue
        return addr, e
    return None


def capabilities(db: GadgetDB, targets: Sequence[str], scratch: Set[str] = DEFAULT_SCRATCH,
                 max_body: int = 8) -> List[Tuple[str, int, int]]:
    """统计一批目标指令各有多少可用实现（用于给 rGCC 划边界）。"""
    index = _index_by_instruction(db, max_body)
    out: List[Tuple[str, int, int]] = []
    for text in targets:
        cands = index.get(text, [])
        ok = 0
        for addr in cands:
            g = db.rebuild(addr)
            e = effect_of(g)
            if e.has_pop or e.has_flow:
                continue
            if e.clobbered() - scratch:
                continue
            ok += 1
        out.append((text, len(cands), ok))
    return out
