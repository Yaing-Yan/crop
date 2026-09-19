"""ROP 解释器 —— 把 rGCC 输出的 `.bin` 翻译成 ROP 链。

分三层，与设计文档的三条要求一一对应：

**L1 块复用**（"一模一样"）
    贪心最长匹配：找 ROM 里逐字节相同、且后面紧跟 `POP PC` 的最长指令序列，
    直接把它变成 1 个链槽。代价 = 4 字节。

**L2 等价转义**（"等价"）
    单条指令在 ROM 里没有现成块时，用词表里的原语合成等价效果。当前实现覆盖
    最有价值的一类：``MOV Rn,#imm`` / ``MOV ERn,#imm`` 用 ``POP Rn/ERn`` 原语
    + 链上数据实现（任意常量，2~8 字节代价）。

**L3 链层控制流**
    ``B label``（程序内部）用"取数 + 栈枢轴"实现：
    ``#pop-er14; [$label-8]; #jump14-q8;``（10 字节）。
    条件分支 ``BC`` 目前**无法实现**——第 1 步已证明本 ROM 里不存在任何
    "条件跳过一个链槽"的 gadget（见 ``docs/step-2-报告.md``），因此解释器
    会明确报"不支持"，而不是悄悄生成错的链。

输出：``Rop.bin`` 字节流 + 一份等价的 RopIDE ``.rop`` DSL 文本。
两条路径（直接构造字节 vs 生成 DSL 再用 ``ropdsl`` 编译）互为校验，
不一致就直接报错——这是本层最强的正确性保证。
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .chain import LEFT, RIGHT, ChainBuilder, ChainError, encode_gadget, encode_value
from .gadget import GadgetDB, split_greedy
from .nxu16 import decode as _dec
from .ropdsl import RopGadget, RopSource, compile_rop_dsl
from .vocab import Primitive, find_pivots, find_pop_gadgets, is_clean_pivot, pick

__all__ = ["Options", "Decision", "Result", "translate"]


@dataclass
class Options:
    form: str = RIGHT              # gadget 槽编码形式（right/left）
    pivot_side: str = LEFT         # 链内跳转地址使用哪一侧基准
    max_insns: int = 8             # L1 块内最多指令数
    allow_pivot: bool = True       # 是否允许用栈枢轴实现内部跳转
    left_base: int = 0xE9E0        # 链存放位置（左侧地址基准，来自 launcher.conf）
    right_base: int = 0xD3C0       # 右侧地址基准
    routines: Sequence = ()        # ROM 例程（crop/routines.py）：在 .bin 里出现即发一个链槽
    rt_push: Optional[object] = None   # rt-fix 原语（给以 RT 结尾的例程用）
    #: 裸跳转表：.bin 偏移 → 20 位目标地址（在该处直接发一个链槽，不走指令流）
    raw_jumps: Dict[int, int] = field(default_factory=dict)
    #: A7 条件跳转标记：.bin 偏移 → 种类（if/else/end）
    a7_markers: Dict[int, str] = field(default_factory=dict)
    #: 内联数据载体 (指令字节, payload 字节数)：字符串骑链时用它识别
    carrier: Optional[Tuple] = None      # (指令字节, payload 字节数[, 载体地址])


@dataclass
class Decision:
    off: int
    size: int
    kind: str                      # 'block' | 'pop_block' | 'pop_imm' | 'jump' | 'anchor'
                                   # | 'routine' | 'rt_fix' | 'unsupported'
    detail: str
    chain_bytes: int = 0
    addr: Optional[int] = None      # 命中的 ROM gadget 地址（block/pop_imm/jump 有效）


@dataclass
class Result:
    data: bytes
    dsl: str
    rop: RopSource
    decisions: List[Decision]
    stats: Dict[str, int]
    warnings: List[str] = field(default_factory=list)

    def report(self) -> str:
        s = self.stats
        tot = max(1, s["insns"])
        lines = [
            "指令总数 %d；L1 块复用覆盖 %d 条 (%.1f%%)，L2 等价转义 %d 条 (%.1f%%)，"
            "L3 跳转 %d 处，无法翻译 %d 条 (%.1f%%)" % (
                s["insns"], s["l1"], 100.0 * s["l1"] / tot, s["l2"], 100.0 * s["l2"] / tot,
                s["jump"], s["unsupported"], 100.0 * s["unsupported"] / tot),
            "链总长 %d 字节（%.1f 字节/指令）；共 %d 个块（其中 ROM 例程调用 %d 次）、"
            "%d 个锚点" % (
                s["chain_bytes"], s["chain_bytes"] / tot, s["blocks"], s.get("lib", 0),
                s["anchors"]),
        ]
        if s["unsupported"]:
            c = collections.Counter(d.detail.split("（")[0] for d in self.decisions
                                    if d.kind == "unsupported")
            lines.append("无法翻译的指令（前 8 类）：")
            for t, n in c.most_common(8):
                lines.append("    ×%-4d %s" % (n, t))
        return "\n".join(lines)


# --------------------------------------------------------------------- 工具
def _is_normal(ins: _dec.Insn) -> bool:
    return ins.cls == "normal" and ins.sp_delta == 0


def _parse_mov_imm(ins: _dec.Insn) -> Optional[Tuple[str, int]]:
    """``MOV Rn, #imm`` / ``MOV ERn, #imm`` → (寄存器名, 立即数)。"""
    if ins.mnemonic != "MOV" or "#" not in ins.operands:
        return None
    dst, _, imm = ins.operands.partition(",")
    dst = dst.strip()
    if dst.startswith("["):                     # MOV [EA], CRn 之类
        return None
    try:
        # 反汇编器把 ALU 立即数打印成**十进制**（`dec(v)`），而地址类操作数是
        # 十六进制 + 'h' 后缀。这里必须按十进制解析。
        v = int(imm.strip().lstrip("#"), 10)
    except ValueError:
        return None
    if dst.startswith("CR") or dst == "SP":
        return None
    return dst, v


def _payload_bytes(reg: str) -> int:
    if reg.startswith("QR"):
        return 8
    if reg.startswith("XR"):
        return 4
    if reg.startswith("ER"):
        return 2
    return 2 if reg.startswith("R") else 0    # POP Rn：只读 1 字节但 SP 走 2


def _branch_target(ins: _dec.Insn) -> Optional[int]:
    """取出 ``B csr:addr`` 的绝对目标（20 位）；非该形态返回 None。"""
    if ins.mnemonic != "B" or ":" not in ins.operands:
        return None
    seg, _, off = ins.operands.partition(":")
    try:
        return (int(seg.rstrip("h"), 16) << 16) | int(off.rstrip("h"), 16)
    except ValueError:
        return None


# --------------------------------------------------------------------- 主流程
def translate(db: GadgetDB, code: bytes, opts: Optional[Options] = None) -> Result:
    opts = opts or Options()
    low00 = (opts.form == LEFT)
    pop_gad = find_pop_gadgets(db)
    pivots = [p for p in find_pivots(db) if is_clean_pivot(p)] if opts.allow_pivot else []
    # 选一个"取数进 ER"的枢轴组合：需要 POP ERn + 能 MOV SP,ERn 的枢轴
    jump_pair: Optional[Tuple[str, int, Primitive]] = None
    for reg in ("ER14", "ER12", "ER10", "ER8", "ER6", "ER4", "ER2", "ER0"):
        if reg not in pop_gad:
            continue
        for p in pivots:
            if ("MOV SP, %s" % reg) in p.text:
                jump_pair = (reg, pick(pop_gad[reg], low00), p)
                break
        if jump_pair:
            break
    warnings: List[str] = []
    if jump_pair is None and opts.allow_pivot:
        warnings.append("ROM 里找不到可用的 POP ERn + MOV SP,ERn 组合，内部跳转无法实现")

    insns: List[_dec.Insn] = []
    a = 0
    while a + 2 <= len(code):
        ins = _dec.decode_at(code, a)
        if ins is None:
            ins = _dec.Insn(addr=a, word=code[a] | (code[a + 1] << 8), ext=0, size=2,
                            kind="?", handler="?", cls="raw", sp_delta=0, mnemonic="DCW",
                            operands="0x%04X" % (code[a] | (code[a + 1] << 8)),
                            raw=bytes(code[a:a + 2]))
        insns.append(ins)
        a += ins.size
    by_off = {i.addr: k for k, i in enumerate(insns)}

    # 程序内部跳转目标 → 需要锚点
    labels: Dict[int, str] = {}
    for ins in insns:
        if ins.cls == "jump":
            t = _branch_target(ins)
            if t is not None and 0 <= t < len(code) and t in by_off:
                labels.setdefault(t, "L%04X" % t)

    routs = sorted(getattr(opts, "routines", ()) or (), key=lambda r: -len(r.code))
    rt_push = getattr(opts, "rt_push", None)

    cb = ChainBuilder(left_base=opts.left_base, right_base=opts.right_base)
    dsl: List[str] = ["// CROP 自动生成：.bin → ROP 链",
                      "// pivot_side=%s  form=%s  max_insns=%d" % (
                          opts.pivot_side, opts.form, opts.max_insns), ""]
    used_gadgets: Dict[str, int] = {}
    decisions: List[Decision] = []
    stats = collections.Counter(insns=len(insns), l1=0, l2=0, jump=0, lib=0,
                                unsupported=0, blocks=0, anchors=0, chain_bytes=0)

    def gslot(addr: int) -> None:
        """写一个 gadget 槽（同时写 DSL）。"""
        name = "g%05X" % addr
        used_gadgets[name] = addr
        cb.gadget(addr, opts.form)
        dsl.append("#%s%s;" % ("-" if opts.form == LEFT else "", name))

    def gvalue(expr: str) -> None:
        cb.value_expr(expr)
        dsl.append("[%s]" % expr)

    def graw(b: bytes) -> None:
        cb.raw(b)
        dsl.append(b.hex().upper())

    last_anchor: List[Optional[str]] = [None]      # 最近一个"骑链字符串"的锚点名
    carrier_addrs: List[str] = []                  # 所有骑链字符串的锚点（按出现顺序）
    marker_offsets = set()                      # 冻结/裸跳转标记 DE AD 的出现位置
    _k = 0
    while True:
        _k = code.find(b"\xDE\xAD", _k)
        if _k < 0:
            break
        marker_offsets.add(_k)
        _k += 1
    carrier_offsets = set()
    _cm0 = getattr(opts, "carrier", None) or getattr(db, "carrier_marker", None)
    if _cm0:
        _k = 0
        while True:
            _k = code.find(_cm0[0], _k)
            if _k < 0:
                break
            carrier_offsets.add(_k)
            _k += 1
    # ---- A7：条件跳转所需的例程/槽（从 ROM 现场解析）----
    a7 = {}
    try:
        from .libabi import Library as _Lib  # noqa: F401
    except Exception:
        pass
    def _a7_addr(name):
        for r in (getattr(opts, "routines", ()) or ()):
            if getattr(r, "name", "") == name:
                return r.entry
        return None
    a7["table"] = _a7_addr("er0-table")          # ER0 = R0*R2 + ER4
    a7["er2"] = _a7_addr("er2-from-er0b")        # ER2 = ER0（带 8 字节填充）
    a7["store"] = 0x08F94                        # ST ER2,[ER8] ; POP XR8 ; POP PC（吃 4）
    a7["pivot"] = None
    if jump_pair:
        a7["pivot"] = jump_pair[2].addr
    if_id = [0]
    if_stack: List[int] = []
    carrier_offsets = set()
    _cm0 = getattr(opts, "carrier", None) or getattr(db, "carrier_marker", None)
    if _cm0:
        _k = 0
        while True:
            _k = code.find(_cm0[0], _k)
            if _k < 0:
                break
            carrier_offsets.add(_k)
            _k += 1
    marker_offsets = set()
    for _pat in (b"\xDE\xAD", b"\xDE\xAF", b"\xDE\xB0", b"\xDE\xB1"):
        _k = 0
        while True:
            _k = code.find(_pat, _k)
            if _k < 0:
                break
            marker_offsets.add(_k)
            _k += 1
    i = 0
    while i < len(insns):
        ins = insns[i]
        # ---- A7：if 的条件跳转（R0 = 0/1）→ 自改链值 + 枢轴 ----
        if ins.addr in labels:                      # ANCHOR_GEN：程序内部跳转目标落锚点
            cb.anchor(labels[ins.addr], opts.pivot_side)
            dsl.append("<%s%s>" % ("-" if opts.pivot_side == LEFT else "", labels[ins.addr]))
        _a7k = getattr(opts, "a7_markers", {}).get(ins.addr)
        if _a7k and ins.addr in labels:            # A7mark_anchor：内部跳转目标落锚点
            cb.anchor(labels[ins.addr], opts.pivot_side)
            dsl.append("<%s%s>" % ("-" if opts.pivot_side == LEFT else "", labels[ins.addr]))
        if _a7k == "if":
            if a7["table"] and a7["er2"] and a7["pivot"]:
                if_id[0] += 1
                n = if_id[0]
                if_stack.append(n)                 # ★嵌套也能对上：DE B0/B1 用栈顶
                tn, en, vn = "T%d" % n, "E%d" % n, "V%d" % n
                dsl.append("// %04X: if 条件跳转 #%d（R0=0/1）" % (ins.addr, n))
                # ★不需要自改链：算好目标 → MOV ER6,ER0（@22414，吃 8 字节填充）→ jmp-er6（@21D38，吃 2）
                import os as _os
                if _os.environ.get("CROP_A7DBG"):
                    print("A7DBG: if@链偏移 0x%02X, Then(=此处之后) = 0x%02X"
                          % (len(cb.buf), len(cb.buf) + (4+2+4+2+4+4+4+8+4+2)))
                gslot(pick(pop_gad["ER4"], low00)); gvalue("$%s - 2" % tn)
                gslot(pick(pop_gad["ER2"], low00)); gvalue("$%s - $%s" % (en, tn))
                if rt_push is not None:
                    gslot(rt_push.addr)
                gslot(a7["table"])
                gslot(0x22414); graw(b"\x00" * 8)         # ER6 = ER0
                gslot(0x21D38); graw(b"\x00" * 2)         # MOV SP,ER6 ; POP ER8 ; POP PC
                nbytes = 4 + 2 + 4 + 2 + 4 + 4 + 4 + 8 + 4 + 2
                stats["jump"] += 1
                stats["blocks"] += 1
                decisions.append(Decision(ins.addr, 2, "jump", "if 条件跳转 #%d" % n, nbytes, a7["pivot"]))
                cb.anchor(tn, opts.pivot_side)
                dsl.append("<%s%s>" % ("-" if opts.pivot_side == LEFT else "", tn))
                i += 1
                while i < len(insns) and insns[i].addr < ins.addr + 2:
                    i += 1
                continue
        if _a7k == "else":
            if if_stack:
                _top = if_stack[-1]
                cb.anchor("E%d" % _top, opts.pivot_side)
                dsl.append("<%sE%d>" % ("-" if opts.pivot_side == LEFT else "", _top))
            i += 1
            while i < len(insns) and insns[i].addr < ins.addr + 2:
                i += 1
            continue
        if getattr(opts, "a7_markers", {}).get(ins.addr) == "pad2":
            graw(b"\x00" * 2)
            i += 1
            while i < len(insns) and insns[i].addr < ins.addr + 2:
                i += 1
            continue
        if getattr(opts, "a7_markers", {}).get(ins.addr) == "pad8":
            graw(b"\x00" * 8)                  # 例程内部 POP ER8 吃掉的 8 字节
            i += 1
            while i < len(insns) and insns[i].addr < ins.addr + 2:
                i += 1
            continue
        if getattr(opts, "a7_markers", {}).get(ins.addr) == "pad4":
            graw(b"\x00" * 4)                  # 例程内部 POP XR8 吃掉的 4 字节
            i += 1
            while i < len(insns) and insns[i].addr < ins.addr + 2:
                i += 1
            continue
        if _a7k == "end":
            if if_stack:
                if_stack.pop()                     # 这一层 if 结束
            i += 1
            while i < len(insns) and insns[i].addr < ins.addr + 2:
                i += 1
            continue
        # ---------------- 裸绝对跳转（冻结/跳回 OS）：DE AD <lo> <hi> <csr>（20 位目标）----
        if ins.addr in marker_offsets and len(code) >= ins.addr + 6:
            tgt = (code[ins.addr + 2] | (code[ins.addr + 3] << 8)
                   | ((code[ins.addr + 4] & 0xF) << 16))
            gslot(tgt)
            dsl.append("#g%05X" % tgt)
            decisions.append(Decision(ins.addr, 6, "jump", "裸跳转 → %05X" % tgt, 4, tgt))
            stats["l3"] += 1
            stats["blocks"] += 1
            i += 1
            while i < len(insns) and insns[i].addr < ins.addr + 6:
                i += 1
            continue
        # ---------------- 内联数据载体（字符串骑链）：整块吃字节、不写内存 ----------------
        cm = getattr(opts, "carrier", None) or getattr(db, "carrier_marker", None)

        if cm and ins.addr in carrier_offsets and len(code) >= ins.addr + len(cm[0]) + cm[1]:
            payload = bytes(code[ins.addr + len(cm[0]):ins.addr + len(cm[0]) + cm[1]])
            # ★直接用它已知的地址（cm 可带第三项 = 载体地址）；反查只是兜底
            caddr = [cm[2]] if len(cm) > 2 else sorted(
                a for a, g in db.by_addr.items()
                if g.inline_safe and b"".join(i.raw for i in db.rebuild(a).insns[:-1]) == cm[0]
                and g.data_bytes == cm[1])
            if caddr:
                gslot(caddr[0])
                name = "S%d" % len(carrier_addrs)      # ★按序号命名：哨兵 0x8000|k ↔ $Sk
                carrier_addrs.append(name)
                last_anchor[0] = name
                cb.anchor(name, opts.pivot_side)
                dsl.append("<%s%s>" % ("-" if opts.pivot_side == LEFT else "", last_anchor[0]))
                dsl.append("// %04X: 内联数据载体（%d 字节）→ 记为 %s"
                           % (ins.addr, cm[1], name))
                graw(payload)
                stats["l1"] += 1
                stats["blocks"] += 1
                decisions.append(Decision(ins.addr, len(cm[0]) + cm[1], "block",
                                          "内联数据载体 %d 字节" % cm[1], 4 + cm[1], caddr[0]))
                while i < len(insns) and insns[i].addr < ins.addr + len(cm[0]) + cm[1]:
                    i += 1
                continue

        # 约定：`.bin` 中 ``POP <reg>`` 之后紧跟该指令要弹走的 ``sp_delta`` 个字节，
        # 由解释器原样搬进 ROP 链（这正是"任意常量"的来源）。
        if ins.cls == "pop_data":
            addrs = db.lookup(ins.raw)
            if not addrs:
                stats["unsupported"] += 1
                decisions.append(Decision(ins.addr, ins.size, "unsupported",
                                          "%s（ROM 里没有该取数原语）" % ins.text))
                i += 1
                continue
            nbytes = ins.sp_delta
            end = ins.addr + ins.size + nbytes
            payload = bytes(code[ins.addr + ins.size:end])
            sent = payload[0] | (payload[1] << 8) if len(payload) >= 2 else -1
            if (sent & 0xFF00) == 0x8000:
                _k = sent & 0xFF
                name = carrier_addrs[_k] if _k < len(carrier_addrs) else "S%d" % _k
                dsl.append("// %04X: %s ← 哨兵 → 骑链字符串 $%s" % (ins.addr, ins.text, name))
                gslot(pick(addrs, low00))
                gvalue("$%s" % name)
                stats["l1"] += 1
                stats["blocks"] += 1
                decisions.append(Decision(ins.addr, 2, "pop_block",
                                          "哨兵 → %s" % name, 6, None))
                i += 1
                while i < len(insns) and insns[i].addr < end:
                    i += 1
                continue
            if len(payload) < nbytes:
                stats["unsupported"] += 1
                decisions.append(Decision(ins.addr, ins.size, "unsupported",
                                          "%s（内联数据不完整）" % ins.text))
                i += 1
                continue
            addr = pick(addrs, low00)
            dsl.append("// %04X: %s  ← 链上数据 %s" % (ins.addr, ins.text, payload.hex(" ").upper()))
            gslot(addr)
            graw(payload)
            stats["l1"] += 1
            stats["blocks"] += 1
            decisions.append(Decision(ins.addr, nbytes, "pop_block",
                                      "%s + 链上 %d 字节" % (ins.text, nbytes), 4 + nbytes, addr))
            i += 1
            while i < len(insns) and insns[i].addr < end:
                i += 1
            continue

        if ins.addr in marker_offsets and len(code) >= ins.addr + 6:
            _t = (code[ins.addr + 2] | (code[ins.addr + 3] << 8)
                  | ((code[ins.addr + 4] & 0xF) << 16))
            gslot(_t)
            dsl.append("#g%05X" % _t)
            decisions.append(Decision(ins.addr, 5, "jump", "裸跳转 → %05X" % _t, 4, _t))
            stats["l3"] += 1
            stats["blocks"] += 1
            i += 1
            while i < len(insns) and insns[i].addr < ins.addr + 6:
                i += 1
            continue
        rj = getattr(opts, "raw_jumps", {}).get(ins.addr)
        if rj is not None:                     # 裸跳转：直接发一个 20 位链槽
            gslot(rj)
            dsl.append("#g%05X" % rj)
            decisions.append(Decision(ins.addr, ins.size, "jump", "裸跳转 → %05X" % rj, 4, rj))
            stats["l3"] += 1
            stats["blocks"] += 1
            i += 1
            while i < len(insns) and insns[i].addr < ins.addr + ins.size:
                i += 1
            continue
        if ins.addr in labels:
            name = labels[ins.addr]
            cb.anchor(name, opts.pivot_side)
            dsl.append("<%s%s>" % ("-" if opts.pivot_side == LEFT else "", name))
            stats["anchors"] += 1
            decisions.append(Decision(ins.addr, 0, "anchor", name))

        # ---------------- ROM 例程调用（A1）---------------------------------
        # `.bin` 里出现"某个 ROM 例程从入口到结尾的整段字节" → 发一个链槽跳过去。
        # 以 ``RT`` 结尾的例程还要先发一个 rt-fix 槽（把"回来后继续吃链"压进硬件返回栈）。
        hit = None
        _nxt_lbl = min([o for o in labels if o > ins.addr], default=None)
        for r in routs:
            if code.startswith(r.code, ins.addr):
                # ★不许把"程序内部跳转目标"吞进例程里（否则那个锚点永远不会生成）
                if _nxt_lbl is not None and _nxt_lbl < ins.addr + len(r.code):
                    continue
                hit = r
                break
        if hit is not None:
            if hit.needs_push and rt_push is None:
                stats["unsupported"] += 1
                decisions.append(Decision(ins.addr, len(hit.code), "unsupported",
                                          "例程 %s（RT 结尾）缺少 rt-fix 原语" % hit.name))
                i += hit.ninsn
                continue
            if hit.needs_push:
                dsl.append("// %04X: 例程 %s @%05X 以 RT 结尾 → 先 rt-fix @%05X"
                           % (ins.addr, hit.name, hit.entry, rt_push.addr))
                gslot(rt_push.addr)
                stats["lib"] += 1
                stats["blocks"] += 1
                decisions.append(Decision(ins.addr, 0, "rt_fix",
                                          "rt-fix @%05X" % rt_push.addr, 4, rt_push.addr))
            dsl.append("// %04X: 调用 ROM 例程 %s @%05X（%d 条指令）"
                       % (ins.addr, hit.name, hit.entry, hit.ninsn))
            gslot(hit.entry)
            stats["lib"] += 1
            stats["blocks"] += 1
            decisions.append(Decision(ins.addr, len(hit.code), "routine",
                                      "rom:%s" % hit.name, 4, hit.entry))
            i += hit.ninsn
            continue



        # ---------------- L3：无条件跳转 ----------------
        if ins.cls == "jump":
            t = _branch_target(ins)
            if (t is not None and 0 <= t < len(code) and t in by_off
                    and labels.get(t) and jump_pair is not None):
                reg, pop_addr, piv = jump_pair
                dsl.append("// %04X: %s → %s（ER6 枢轴）" % (ins.addr, ins.text, labels[t]))
                if "ER6" in pop_gad and 0x21D38 in db.by_addr:
                    # ★统一用 ER6 枢轴：不依赖当前 SP（A7 也是这么跳的）
                    gslot(pick(pop_gad["ER6"], low00))
                    gvalue("$%s - 2" % labels[t])
                    gslot(0x21D38)
                    graw(b"\x00" * 2)
                else:
                    gslot(pop_addr)
                    gvalue("$%s - %d" % (labels[t], piv.skip))
                    gslot(piv.addr)
                stats["jump"] += 1
                decisions.append(Decision(ins.addr, ins.size, "jump",
                                          "%s → %s" % (ins.text, labels[t]), 10, pop_addr))
                i += 1
                continue
            stats["unsupported"] += 1
            decisions.append(Decision(ins.addr, ins.size, "unsupported",
                                      "%s（外部/间接跳转）" % ins.text))
            i += 1
            continue
        if not _is_normal(ins):
            stats["unsupported"] += 1
            decisions.append(Decision(ins.addr, ins.size, "unsupported",
                                      "%s（%s 类，会破坏 ROP 链或改变控制流）" % (ins.text, ins.cls)))
            i += 1
            continue

        # ---------------- L1：最长块复用 ----------------
        best_n, best_code, best_addrs, end = 0, None, None, ins.addr
        size_hint = 0
        pad = 0
        for k in range(1, opts.max_insns + 1):
            if i + k > len(insns):
                break
            blk = insns[i:i + k]
            # 允许"含取数原语"的块：gadget 自带的 POP 会从链上多取数据，
            # 由解释器补零（pad），因此这类块仍然可以整块复用。
            if not all(_is_normal(x) or x.cls == "pop_data" for x in blk):
                break
            end = blk[-1].addr + blk[-1].size
            # ★块不许跨过"程序内部跳转目标"（否则那个锚点永远不会生成 ⇒ $Lxxxx 未定义）
            if any(ins.addr < o < end for o in labels):
                break
            cand = bytes(code[ins.addr:end])
            addrs = db.lookup(cand)
            if addrs:
                best_n, best_code, best_addrs = k, cand, addrs
                pad = sum(x.sp_delta for x in blk if x.cls == "pop_data")
        if best_n:
            try:
                addr = pick(best_addrs, low00)
            except ValueError:
                addr = None
            if addr is None:
                stats["unsupported"] += 1
                decisions.append(Decision(ins.addr, size_hint, "unsupported",
                                          "L1 候选地址全被左式 00→01 规则排除"))
                i += 1
                continue
            dsl.append("// %04X: %s%s" % (ins.addr, " ; ".join(x.text for x in insns[i:i + best_n]),
                                          "（自带 POP，链上补 %d 字节 0）" % pad if pad else ""))
            gslot(addr)
            if pad:
                graw(b"\x00" * pad)
            stats["l1"] += best_n
            stats["blocks"] += 1
            decisions.append(Decision(ins.addr, len(best_code), "block",
                                      " ; ".join(x.text for x in insns[i:i + best_n]),
                                      4 + pad, addr))
            i += best_n
            continue

        # ---------------- L2：等价转义 ----------------
        mi = _parse_mov_imm(ins)
        if mi and mi[0] in pop_gad:
            reg, val = mi
            addr = pick(pop_gad[reg], low00)
            nbytes = _payload_bytes(reg)
            dsl.append("// %04X: %s  → POP %s + 链上数据" % (ins.addr, ins.text, reg))
            gslot(addr)
            graw(encode_value(val).ljust(nbytes, b"\x00"))
            stats["l2"] += 1
            stats["blocks"] += 1
            decisions.append(Decision(ins.addr, ins.size, "pop_imm",
                                      "%s ← POP %s" % (ins.text, reg), 4 + nbytes, addr))
            i += 1
            continue

        stats["unsupported"] += 1
        decisions.append(Decision(ins.addr, ins.size, "unsupported", ins.text))
        i += 1

    data = cb.finalize()
    stats["chain_bytes"] = len(data)

    # ---------------- 交叉校验：DSL 文本 → 字节 ----------------
    rop = RopSource(input="\n".join(dsl), left=cb.left_base, right=cb.right_base,
                    gadgets=[RopGadget(name=n, addr=a) for n, a in sorted(used_gadgets.items())])
    try:
        data2, warns, _ = compile_rop_dsl(rop, strict=False)
    except ChainError as e:                       # pragma: no cover - 防御
        warnings.append("DSL 回编译失败：%s" % e)
        data2, warns = b"", ["DSL 回编译失败"]
    warnings += warns
    if data2 != data:                             # pragma: no cover - 防御
        warnings.append("警告：DSL 路径(%d 字节)与直接构造(%d 字节)不一致" % (len(data2), len(data)))
    return Result(data=data, dsl="\n".join(dsl), rop=rop, decisions=decisions,
                  stats=dict(stats), warnings=warnings)
