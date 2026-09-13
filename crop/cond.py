"""A7：条件分支的 ROM 原语（"栈操作法"）—— 全部扫描得到，零硬编码。

背景（见 `docs/step-2-报告.md` 与用户给的 `nX-U16_ROP_Gadget_Mapping.md`）：
本 ROM 里**不存在**"条件跳过一个链槽"的现成 gadget（`BC cond,T` 后面跟 `POP PC`，
且 T 处正好多吃 4 字节的候选：0 个 —— `tools/` 里的搜索可以复现）。
所以条件分支要靠**改写链**：链就在 RAM 里，地址在翻译期已知。

本模块扫描出两件东西，组合起来就能实现真正的条件跳转：

``CondMove``（条件搬运）
    形如 ``BC cond, T`` 的指令，其下落路径直接 ``POP PC``（**只吃 1 个槽**），
    而 T 处是一段"把某个 16 位寄存器搬到另一个"的代码（也以 ``POP PC`` 结尾）。
    于是两条路径**都**回到同一个槽，但寄存器状态不同 —— 一次"条件选择"：

        POP ER12 + A ; POP ER2 + B
        BC cond,T            ; 条件成立 → 执行 T：ER2 ← ER12 = A；否则 ER2 保持 B
        ⇒ ER2 = cond ? A : B

``StoreWord``（16 位写内存）
    形如 ``ST ERa, [ERb] ; … ; POP PC`` 的 gadget：把 16 位寄存器写进指定地址。
    用它把"选好的地址"写进链上**将要被 POP PC 取走的那一格**，
    紧接着的 ``POP PC`` 就跳过去了 ⇒ 真正的条件跳转。

VerF 实测（自动扫描结果，`describe()` 会打印）：
``CondMove`` = ``0x1125A: BC NE, 112BCh`` + ``T=0x112BC: MOV ER2, ER12``；
``StoreWord`` = ``0x08F94: ST ER2, [ER8] ; POP XR8 ; POP PC``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .nxu16 import decode as _dec

__all__ = ["CondMove", "StoreWord", "scan_cond", "CondPrimitives"]


@dataclass
class CondMove:
    """``BC cond, T`` + ``T`` 处"16 位寄存器搬运"，两条路径都只吃 1 个槽。"""

    addr: int                 # 链上要跳进来的地址（BC 那条指令）
    cond: str                 # 'EQ' / 'NE' / ...
    target: int               # T
    dst: str                  # 被改写的寄存器（'ER2'）
    src: str                  # 取值来源寄存器（'ER12'）
    code: bytes               # BC 那 2 字节


@dataclass
class StoreWord:
    """``ST ERa, [ERb]`` … ``POP PC``：把 16 位值写进 [ERb]。"""

    addr: int
    value_reg: str            # 要写的值（'ER2'）
    addr_reg: str            # 写入地址（'ER8'）
    code: bytes               # 从入口到 POP PC 的整段
    ninsn: int
    pops: int                 # 结尾 POP PC 之前额外吃掉的链字节数


@dataclass
class CondPrimitives:
    moves: List[CondMove]
    stores: List[StoreWord]

    def pick_move(self, cond: str, dst: str = "ER2", src: str = "ER12") -> Optional[CondMove]:
        for m in self.moves:
            if m.cond == cond and m.dst == dst and m.src == src:
                return m
        return None

    def pick_store(self, value_reg: str, addr_reg: str) -> Optional[StoreWord]:
        for s in self.stores:
            if s.value_reg == value_reg and s.addr_reg == addr_reg:
                return s
        return None

    def describe(self) -> str:
        out = ["A7 条件原语（扫描结果）："]
        for m in self.moves[:4]:
            out.append("  条件搬运 @%05X: %s → %s ← %s（T=%05X）"
                       % (m.addr, m.cond, m.dst, m.src, m.target))
        for s in self.stores[:4]:
            out.append("  16 位写内存 @%05X: ST %s, [%s]（自带 %d 字节 POP）"
                       % (s.addr, s.value_reg, s.addr_reg, s.pops))
        if not self.moves:
            out.append("  **没有**可用的条件搬运 gadget")
        if not self.stores:
            out.append("  **没有**可用的 16 位写内存 gadget")
        return "\n".join(out)


def _target(ins: _dec.Insn) -> Optional[int]:
    ops = ins.operands
    try:
        if ":" in ops:
            seg, _, off = ops.partition(":")
            return (int(seg.rstrip("h"), 16) << 16) | int(off.strip().rstrip("h"), 16)
        return int(ops.split(",")[-1].strip().rstrip("h"), 16)
    except ValueError:
        return None


def scan_cond(space, max_addr: int = 1 << 20) -> CondPrimitives:
    """扫描 ROM，找出条件搬运与 16 位写内存两类原语。"""
    # ---- (1) 16 位写内存：ST ERx,[ERy] … POP PC（不碰 SP、不碰链指针）
    stores: List[StoreWord] = []
    for a in range(0, max_addr - 6, 2):
        i0 = _dec.decode_at(space, a)
        if i0 is None or i0.mnemonic != "ST":
            continue
        ops = i0.operands.strip()
        if not ops.startswith("ER") or "[ER" not in ops or "EA" in ops:
            continue
        val = ops.split(",")[0].strip()
        addr_reg = ops.split("[")[1].rstrip("]").split("+")[0].strip()
        seq, b, pops, ok = [i0], a + i0.size, 0, False
        for _ in range(6):
            i = _dec.decode_at(space, b)
            if i is None:
                break
            if i.cls == "term_pop_pc":
                ok = True
                break
            if i.cls == "pop_data":
                o = i.operands.strip()
                pops += 8 if o.startswith("QR") else 4 if o.startswith("XR") else 2
                seq.append(i)
                b += i.size
                continue
            break
        if ok:
            stores.append(StoreWord(addr=a, value_reg=val, addr_reg=addr_reg,
                                    code=bytes(space[a:b + 2]), ninsn=len(seq) + 1, pops=pops))

    # ---- (2) 条件搬运：BC cond,T ; POP PC，T 处是"16 位寄存器 ← 16 位寄存器 ; POP PC"
    moves: List[CondMove] = []
    for a in range(0, max_addr - 8, 2):
        i0 = _dec.decode_at(space, a)
        if i0 is None or i0.cls != "branch" or i0.size != 2:
            continue
        i1 = _dec.decode_at(space, a + 2)
        if i1 is None or i1.cls != "term_pop_pc":
            continue
        t = _target(i0)
        if t is None:
            continue
        i2 = _dec.decode_at(space, t)
        if i2 is None or i2.mnemonic != "MOV" or not i2.operands.strip().startswith("ER"):
            continue
        dst, _, src = i2.operands.partition(",")
        src = src.strip()
        if not src.startswith("ER"):
            continue
        nxt = _dec.decode_at(space, t + i2.size)
        if nxt is None or nxt.cls != "term_pop_pc":
            continue
        moves.append(CondMove(addr=a, cond=i0.operands.split(",")[0].strip(), target=t,
                              dst=dst.strip(), src=src, code=bytes(space[a:a + 2])))
    return CondPrimitives(moves=moves, stores=stores)
