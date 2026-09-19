"""ROM 例程 = 链原语（A1：调用 ROM 例程）。

标签表（``crop/labels.py``）把"例程名 → 地址"按签名逐 Ver 解析出来。本模块再往前
走一步：把每个例程**从入口到结尾**的整段机器码抽出来，成为"逐字节可识别"的块。
于是：

* rGCC 生成 ``.bin`` 时，把这些字节原样写进去（``.bin`` 仍是一段合法的 nX-U16 程序）；
* 解释器在 ``.bin`` 里看到这段字节，就知道"这里要调用那个 ROM 例程"，
  于是发**一个链槽**（4 字节）跳过去。

例程的结尾有两种（由 ``nxu16_lift.py`` 的语义确定，实机可验证）：

``POP PC``
    从 **ROP 链**取 PC+CSR 返回。链槽序列与控制流天然一致，**不需要额外处理**。

``RT``
    从**硬件返回栈**（``rstack``）返回，与 ROP 链无关。必须先让 ``rstack`` 里有一条
    "返回后用 ``POP PC`` 继续吃链"的记录 —— 这就是 RopIDE 说的 ``rt-fix``。
    本模块**扫描 ROM 自动发现**该原语：形如

        A:      BL  T          ; T 处是 ``POP PC``（先把返回地址压进 rstack，再吃一个链槽）
        A + 4:  BC  AL, U      ; U 处也是 ``POP PC``（例程 ``RT`` 回来后从这里继续吃链）

    的指令对。整段（``BL`` + ``BC``）作为一个"原语块"登记，地址取 ``A``。
    由于 `BL` 会把 ``A + 4`` 压进 rstack，例程 ``RT`` 之后回到 ``A + 4`` → ``POP PC``
    → 链上紧接着的下一个槽。整条链因此**仍然是均匀的 4 字节槽序列**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .labels import LabelTable
from .nxu16 import decode as _dec
from .rom import RomImage

__all__ = ["Routine", "RtPush", "routine_body", "find_rt_push", "build_routines"]

MAX_BODY_INSNS = 400


@dataclass
class Routine:
    """一个 ROM 例程（标签表解析出来的）。"""

    name: str
    entry: int
    code: bytes            # .bin 里应出现的字节（入口 → 结尾指令，含结尾指令）
    term: str              # 'pop_pc' | 'rt'
    ninsn: int
    #: True 表示这条例程内部会 "返回 LR"（PUSH LR … POP PC）⇒ 必须像 RT 例程那样
    #: **经 rt-fix 进入**（让 LR 指向链上续接位置），否则直跳进去会跳到垃圾地址。
    needs_lr: bool = False

    @property
    def needs_push(self) -> bool:
        return self.term == "rt" or self.needs_lr


#: 内部"返回 LR"的例程（需要经 rt-fix 进入）——按标签名
LR_ROUTINES = {"blk-draw", "render-bitmap"}


@dataclass
class RtPush:
    """``rt-fix`` 原语：往硬件返回栈里放一条"回来后继续吃链"的记录。"""

    addr: int
    code: bytes
    target: int                        # BL 的目标（那里应当是 POP PC）


def routine_body(space, entry: int, max_insns: int = MAX_BODY_INSNS) -> Tuple[bytes, str, int]:
    """从 ``entry`` 线性扫到第一个结尾指令（``POP PC`` / ``RT``）。

    返回 ``(字节, 结尾类型, 指令条数)``。扫不到结尾就报错 —— 绝不猜一个长度。
    """
    b, n = entry, 0
    while n < max_insns:
        ins = _dec.decode_at(space, b)
        if ins is None:
            break
        b += ins.size
        n += 1
        if ins.cls == "term_pop_pc":
            return bytes(space[entry:b]), "pop_pc", n
        if ins.cls == "ret":
            return bytes(space[entry:b]), "rt", n
    raise ValueError("从 %05X 起 %d 条指令内没找到 POP PC / RT 结尾" % (entry, max_insns))


def _abs_target(ins: _dec.Insn) -> Optional[int]:
    """取跳转目标的绝对地址（20 位）。``B`` 是 ``seg:off``，``BC`` 是页内偏移。"""
    ops = ins.operands
    if ":" in ops:
        seg, _, off = ops.partition(":")
        try:
            return (int(seg.rstrip("h"), 16) << 16) | int(off.strip().rstrip("h"), 16)
        except ValueError:
            return None
    tail = ops.split(",")[-1].strip().rstrip("h")
    try:
        return int(tail, 16)
    except ValueError:
        return None


def find_rt_push(space, max_scan: Optional[int] = None) -> Optional[RtPush]:
    """扫描 ROM，找 ``rt-fix`` 原语（见模块文档）。找不到返回 None。"""
    limit = len(space) if max_scan is None else min(len(space), max_scan)
    for a in range(0, limit - 6, 2):
        i0 = _dec.decode_at(space, a)
        if i0 is None or i0.cls != "call" or i0.size != 4:
            continue
        t = _abs_target(i0)
        if t is None:
            continue
        hit = _dec.decode_at(space, t)
        if hit is None or hit.cls != "term_pop_pc":
            continue
        i1 = _dec.decode_at(space, a + i0.size)
        if i1 is None or i1.cls != "branch" or not i1.operands.startswith("AL"):
            continue
        u = _abs_target(i1)
        if u is None:
            continue
        back = _dec.decode_at(space, u)
        if back is None or back.cls != "term_pop_pc":
            continue
        end = a + i0.size + i1.size
        return RtPush(addr=a, code=bytes(space[a:end]), target=t)
    return None


def build_routines(rom: RomImage, table: LabelTable,
                   max_insns: int = MAX_BODY_INSNS) -> Tuple[Dict[str, Routine], List[str]]:
    """把标签表里所有标签抽成例程。返回 ``({名字: Routine}, 警告)``。"""
    out: Dict[str, Routine] = {}
    warn: List[str] = []
    for r in table.rows:
        try:
            code, term, n = routine_body(rom.space, r.addr, max_insns)
        except ValueError as e:
            warn.append("标签 %s：%s（跳过，不作为例程）" % (r.label.name, e))
            continue
        out[r.label.name] = Routine(name=r.label.name, needs_lr=r.label.name in LR_ROUTINES,
                                    entry=r.addr, code=code,
                                    term=term, ninsn=n)
    return out, warn
