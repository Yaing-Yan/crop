"""Gadget 扫描器 —— CROP 的地基。

定义（本项目统一口径）：

* **Gadget**：ROM 中从地址 ``A`` 开始的一段**直线**指令序列，其最后一条指令是
  ``POP PC``（或 ``RT``）。从 ``A`` 进入后，指令顺序执行到结尾，由 ``POP PC``
  从 ROP 链（SP 指向处）取出下一个 ``PC``+``CSR``，从而回到链上。
* 因此，如果待翻译程序里的某一段字节 ``B`` 在 ROM 的地址 ``A`` 处**逐字节相同**，
  且 ``A+|B|`` 处的下一条指令正好是 ``POP PC``，那么"调用 ``A`` 这个 gadget"
  与"原地执行 ``B``"语义完全等价（后面接一个链上跳转）。
  这正是"把 .bin 映射成 ROP 链"的全部原理。
* 扫描时会跨过 ``POP Rn`` 这类"从链上取数据"的指令（它们消耗链上的字节，
  数据由链提供，与 .bin 中的同字节指令自洽），但**不会**跨过任何会破坏链条的
  指令：``PUSH*``、``ADD SP``、``MOV SP, ERn``、``BC``/``B``/``BL``、``RT``、
  ``SWI``/``BRK`` 等。

``RT``（``POPL`` 之外的另一类返回）：它弹的是**硬件返回栈**（``BL`` 压入），
不是 ROP 链，所以 RT 结尾的 gadget 只有在硬件返回栈已被安排好时才能续链，
单独统计、默认不参与自动匹配（``include_rt=False``）。
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .nxu16 import decode as _dec
from .rom import RomImage, SPACE

__all__ = ["GadgetDB", "Gadget", "scan", "split_greedy", "Block"]

# 类别 → 单字节编码（见 decode.py 的 cls 说明）
_CLS_CODE = {
    None: 0,
    "normal": 1,
    "pop_data": 2,
    "push": 3,
    "sp_add": 4,
    "sp_set": 5,
    "sp_get": 6,
    "branch": 7,
    "jump": 8,
    "call": 9,
    "term_pop_pc": 10,
    "ret": 11,
    "reti": 12,
    "trap": 13,
    "pop_pc_extra": 14,
}
_C_TERM = 10
_C_RET = 11
_C_RETI = 12
_C_NORMAL = 1
_C_POP_DATA = 2
#: 指令类别 → gadget 标志位（会改变 SP / 链 / 硬件返回栈）
_FLAG_OF = {
    "push": {"push"},
    "sp_add": {"sp_add"},
    "sp_set": {"pivot"},
    "sp_get": {"sp_get"},
    "pop_data": {"pop_data"},
}
#: 会打断"直线执行"、不能作为 gadget 一部分的类别
_C_STOP = frozenset({7, 8, 9, 13, 14})
#: 类别编码 → 名称
_CLS_NAME = {v: k for k, v in _CLS_CODE.items()}


@dataclass
class Gadget:
    """一个已解出的 gadget（按需重建，不常驻内存）。"""

    addr: int
    size: int
    ninsn: int
    term: str                    # 'pop_pc' | 'rt' | 'reti'
    code: bytes
    data_bytes: int              # 结尾 POP PC 之前，从链上取走的字节数
    insns: Tuple[_dec.Insn, ...] = ()
    flags: frozenset = frozenset()   # {'pivot','push','sp_add','sp_get','pop_data'}

    @property
    def chain_in(self) -> int:
        """执行本 gadget 一共从 ROP 链上取走的字节数。"""
        return self.data_bytes + (4 if self.term == "pop_pc" else 0)

    @property
    def inline_safe(self) -> bool:
        """能否把本 gadget 当作"目标块的等价物"直接内联进链。

        要求：结尾是 POP PC、且中途不碰 SP / 不写链 / 不动硬件返回栈。
        """
        return self.term == "pop_pc" and not (self.flags & {"pivot", "push", "sp_add", "sp_get"})

    def render(self) -> str:
        head = "%05X  %-11s %s" % (self.addr, self.code.hex(" ").upper(),
                                   self.insns[0].text if self.insns else "")
        rest = "".join("\n              %-11s %s" % (i.raw.hex(" ").upper(), i.text)
                       for i in self.insns[1:])
        return head + rest


@dataclass
class GadgetDB:
    """gadget 索引：``机器码字节串 -> [起始地址…]``。"""

    index: Dict[bytes, List[int]] = field(default_factory=dict)
    index_rt: Dict[bytes, List[int]] = field(default_factory=dict)
    #: 所有已解出的 gadget（含栈枢轴/函数入口等"非内联"型），供第 2 阶段做原语库
    by_addr: Dict[int, "Gadget"] = field(default_factory=dict)
    max_insns: int = 8
    stats: Dict[str, int] = field(default_factory=dict)
    hist: Dict[int, int] = field(default_factory=dict)      # 指令条数 → 去重后的字节串数
    hist_all: Dict[int, int] = field(default_factory=dict)  # 指令条数 → gadget 个数（含重复）
    rom: Optional[RomImage] = None

    # ------------------------------------------------------------------ 查询
    def lookup(self, code: bytes) -> Optional[List[int]]:
        """返回所有"以 ``code`` 为内容、后面紧跟 POP PC"的 ROM 地址。"""
        return self.index.get(code)

    def __len__(self) -> int:
        return sum(len(v) for v in self.index.values())

    def __contains__(self, code: bytes) -> bool:
        return code in self.index

    # ------------------------------------------------------------------ 还原
    def rebuild(self, addr: int, max_insns: Optional[int] = None) -> Gadget:
        """按地址重新解码出 gadget 详情。"""
        if self.rom is None:
            raise RuntimeError("GadgetDB 未绑定 ROM")
        rom = self.rom
        max_insns = self.max_insns if max_insns is None else max_insns
        insns: List[_dec.Insn] = []
        flags = set()
        a = addr
        data = 0
        for _ in range(max_insns + 1):
            ins = _dec.decode_at(rom.space, a)
            if ins is None:
                break
            insns.append(ins)
            if ins.cls == "term_pop_pc":
                return Gadget(addr=addr, size=a + ins.size - addr, ninsn=len(insns) - 1,
                              term="pop_pc", code=bytes(rom.space[addr:a + ins.size]),
                              data_bytes=data, insns=tuple(insns), flags=frozenset(flags))
            if ins.cls in ("ret", "reti"):
                return Gadget(addr=addr, size=a + ins.size - addr, ninsn=len(insns) - 1,
                              term="rt" if ins.cls == "ret" else "reti",
                              code=bytes(rom.space[addr:a + ins.size]),
                              data_bytes=data, insns=tuple(insns), flags=frozenset(flags))
            if ins.cls in ("branch", "jump", "call", "trap"):
                break
            flags |= _FLAG_OF.get(ins.cls, set())
            data += ins.sp_delta
            a += ins.size
        raise ValueError("地址 %05X 处不是 gadget" % addr)

    # ------------------------------------------------------------------ 报告
    def report(self, top: int = 12) -> str:
        s = self.stats
        out = []
        out.append("ROM 字节覆盖率　　：%d/%d 字节可解码为指令" % (s.get("decodable", 0), s.get("bytes", 0)))
        out.append("控制流统计　　　　：POP PC=%d  RT=%d  RTI/RTICE=%d  BC=%d  B=%d  BL=%d  SWI/BRK=%d" % (
            s.get("pop_pc", 0), s.get("rt", 0), s.get("reti", 0),
            s.get("branch", 0), s.get("jump", 0), s.get("call", 0), s.get("trap", 0)))
        out.append("会破坏 ROP 链的指令：PUSH=%d  ADD SP=%d  MOV SP=%d" % (
            s.get("push", 0), s.get("sp_add", 0), s.get("sp_set", 0)))
        out.append("Gadget（POP PC 结尾）：%d 个，去重后 %d 种字节串" % (
            len(self), len(self.index)))
        piv = [g for g in self.by_addr.values() if "pivot" in g.flags]
        nt = [g for g in self.by_addr.values() if not g.inline_safe and not ("pivot" in g.flags)]
        out.append("非内联 gadget（第 2 阶段的原语库）：栈枢轴 %d 个，其它（含 PUSH/改 SP/RT）%d 个" % (
            len(piv), len(nt)))
        if self.index_rt:
            n = sum(len(v) for v in self.index_rt.values())
            out.append("Gadget（RT 结尾，需硬件返回栈配合）：%d 个，去重后 %d 种" % (n, len(self.index_rt)))
        out.append("按块内指令条数分布（去重字节串数 / gadget 总数）：")
        for k in sorted(self.hist):
            out.append("    %2d 条指令：%7d 种 / %7d 个" % (k, self.hist[k], self.hist_all.get(k, 0)))
        out.append("最常见的可复用块（前 %d）：" % top)
        for code, addrs in sorted(self.index.items(), key=lambda kv: -len(kv[1]))[:top]:
            ins = self.rebuild(addrs[0])
            out.append("    ×%-6d %-26s %s" % (len(addrs), code.hex(" ").upper(),
                                               " ; ".join(i.text for i in ins.insns)))
        return "\n".join(out)


# ---------------------------------------------------------------------- 扫描
def scan(rom: RomImage, max_insns: int = 8, include_rt: bool = False) -> GadgetDB:
    """扫描整张 ROM 映像，建立 gadget 索引。

    ``max_insns`` 是"gadget 里 POP PC 之前最多允许几条指令"。逐偏移扫描，
    所以奇数地址（+1/+2 进入）也在索引里。
    """
    db = GadgetDB(max_insns=max_insns, rom=rom)
    cls = bytearray(SPACE)
    size = bytearray(SPACE)
    spd = array("h", bytes(2 * SPACE))

    stats = db.stats
    stats["bytes"] = 0
    stats["decodable"] = 0
    for key in ("pop_pc", "pop_pc_extra", "rt", "reti", "branch", "jump", "call",
                "trap", "push", "sp_add", "sp_set", "sp_get", "pop_data", "normal"):
        stats[key] = 0
    _cls_stat = {1: "normal", 2: "pop_data", 3: "push", 4: "sp_add",
                 5: "sp_set", 6: "sp_get", 7: "branch", 8: "jump", 9: "call",
                 10: "pop_pc", 11: "rt", 12: "reti", 13: "trap", 14: "pop_pc_extra"}

    # ---- 1) 逐偏移分类
    for rf in rom.files:
        base, end = rf.base_addr, rf.base_addr + len(rf.data)
        stats["bytes"] += len(rf.data)
        space = rom.space
        for a in range(base, end - 1):
            w = space[a] | (space[a + 1] << 8)
            info = _dec.classify_word(w)
            if info is None:
                continue
            if info.size == 4 and a + 4 > end:
                continue
            c = _CLS_CODE[info.cls]
            cls[a] = c
            size[a] = info.size
            spd[a] = info.sp_delta
            stats["decodable"] += 1
            stats[_cls_stat[c]] += 1

    # ---- 2) 从每个偏移向后走，收集所有以 POP PC/RT 结尾的直线序列
    index = db.index
    index_rt = db.index_rt
    by_addr = db.by_addr
    hist, hist_all = db.hist, db.hist_all
    for rf in rom.files:
        base, end = rf.base_addr, rf.base_addr + len(rf.data)
        space = rom.space
        for a in range(base, end - 1):
            if cls[a] == 0:
                continue
            b = a
            n = 0
            data = 0
            flags: set = set()
            while n <= max_insns:
                c = cls[b]
                if c == 0:
                    break
                if c == _C_TERM or c in (_C_RET, _C_RETI):
                    term = "pop_pc" if c == _C_TERM else ("rt" if c == _C_RET else "reti")
                    code = bytes(space[a:b])
                    g = Gadget(addr=a, size=b + size[b] - a, ninsn=n, term=term,
                               code=bytes(space[a:b + size[b]]), data_bytes=data,
                               flags=frozenset(flags))
                    by_addr[a] = g
                    if g.inline_safe:
                        # 索引键 = **结尾 POP PC 之前**的那段字节（"可复用块"本身）。
                        # 匹配时直接拿块字节查表；命中即说明 ROM 中该字节串后面
                        # 紧跟 POP PC（于是"跳该地址" ≡ "原地执行该块 + 返回链上"）。
                        addrs = index.get(code)
                        if addrs is None:
                            index[code] = [a]
                            hist[n] = hist.get(n, 0) + 1
                        else:
                            addrs.append(a)
                        hist_all[n] = hist_all.get(n, 0) + 1
                    elif include_rt and term != "pop_pc":
                        addrs = index_rt.get(code)
                        if addrs is None:
                            index_rt[code] = [a]
                        else:
                            addrs.append(a)
                    break
                if c in _C_STOP:
                    break
                flags |= _FLAG_OF.get(_CLS_NAME[c], set())
                data += spd[b]
                b += size[b]
                n += 1
    return db


# ---------------------------------------------------------------------- 分块
@dataclass
class Block:
    """待翻译程序里的一段：``off`` 起、``size`` 字节、``ninsn`` 条指令。"""

    off: int
    size: int
    ninsn: int
    code: bytes
    addrs: Optional[List[int]] = None   # 可用 gadget 地址（None = 找不到，需要转义）
    insns: Tuple[_dec.Insn, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.addrs)


def split_greedy(db: GadgetDB, code: bytes,
                 max_insns: Optional[int] = None) -> Tuple[List[Block], List[_dec.Insn]]:
    """贪心最长匹配：把 ``code`` 切成尽量少的、ROM 里现成的 ROP gadget。

    返回 ``(blocks, leftover)``；``leftover`` 是完全找不到等价 gadget 的指令，
    需要第 2 阶段用"逐条转义"处理。
    """
    max_insns = db.max_insns if max_insns is None else max_insns
    # 先解出所有指令边界（线性）
    insns: List[_dec.Insn] = []
    a = 0
    n = len(code)
    while a + 2 <= n:
        ins = _dec.decode_at(code, a)
        if ins is None or a + ins.size > n:
            ins = _dec.Insn(addr=a, word=code[a] | (code[a + 1] << 8), ext=0, size=2,
                            kind="?", handler="?", cls="raw", sp_delta=0,
                            mnemonic="DCW", operands="0x%04X" % (code[a] | (code[a + 1] << 8)),
                            raw=bytes(code[a:a + 2]))
        insns.append(ins)
        a += ins.size

    blocks: List[Block] = []
    leftover: List[_dec.Insn] = []
    i = 0
    while i < len(insns):
        best_n = 0
        best_code: Optional[bytes] = None
        best_addrs: Optional[List[int]] = None
        off = insns[i].addr
        end = off
        for k in range(1, max_insns + 2):
            if i + k > len(insns):
                break
            end = insns[i + k - 1].addr + insns[i + k - 1].size
            cand = bytes(code[off:end])
            addrs = db.lookup(cand)
            if addrs:
                best_n = k
                best_code = cand
                best_addrs = addrs
        if best_n:
            blocks.append(Block(off=off, size=len(best_code), ninsn=best_n, code=best_code,
                                addrs=best_addrs, insns=tuple(insns[i:i + best_n])))
            i += best_n
        else:
            leftover.append(insns[i])
            i += 1
    return blocks, leftover
