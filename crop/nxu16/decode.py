"""nX-U16 结构化解码 / 指令分类层。

在 ``isa.py`` + ``disasm.py``（vendored）之上补两件事：

1. ``Insn`` —— 结构化的指令对象，供分块匹配与 ROP 翻译使用（不再靠解析文本）。
2. ``CodeInfo`` —— 只做分类、不做字符串格式化的快速路径，供全 ROM 扫描使用
   （256 KB × 逐偏移扫描需要它足够快）。

指令分类 ``cls`` 的含义（ROP 视角）：

===============  ==========================================================
cls              含义
===============  ==========================================================
``normal``       纯数据流指令（算术/逻辑/传送/L/ST/LEA/…）。可以原样内联进 ROP 链
``pop_data``     ``POP Rn/ERn/XRn/QRn``：从 ROP 链上取数据（改变 SP）
``push``         ``PUSH*``：往 ROP 链上写数据（改变 SP，破坏链条）→ 禁止内联
``sp_add``       ``ADD SP, #imm``：直接移动 ROP 链指针 → 禁止内联
``sp_set``       ``MOV SP, ERn``：栈枢轴（pivot），条件跳转的实现基础
``sp_get``       ``MOV ERn, SP``：读取链指针
``branch``       ``BC cond, disp`` 条件分支
``jump``         ``B label`` / ``B ERn``
``call``         ``BL label`` / ``BL ERn``（写硬件返回栈）
``term_pop_pc``  ``POP PC``：从 ROP 链取 PC+CSR 并跳转 —— gadget 的合法结尾
``ret``          ``RT``：弹出**硬件返回栈**（不是 ROP 链）返回
``reti``         ``RTI`` / ``RTICE``
``trap``         ``SWI`` / ``BRK`` / ``ICESWI``
===============  ==========================================================

关于 ``POP PC`` 消耗的字节数（重要，本项目采用的模型）：

* nX-U16 的 ``POP PC`` = ``POPL PC``：先 ``pop16`` 取 PC（SP += 2），
  再 ``pop8`` 取 CSR（低 4 位有效）。
* vendored 的 ``nxu16_lift.py`` 里 ``pop8`` 实现为 ``SP += 1``，但同一份实现里
  ``POP Rn`` 单寄存器是"读 1 字节、SP += 2"，``PUSH Rn`` 也是"写 1 字节、
  SP -= 2"（README 明确称其为硬件怪癖）。单字节访问按 2 字节对齐才是自洽的。
* 经验证据：真实可用的 ``.rop`` 程序（Pixel Editor / testing.rop）的链是严格的
  **4 字节槽**（``[PC_lo][PC_hi][CSR][pad]``）。若 ``POP PC`` 只消耗 3 字节，
  槽会立刻错位、语义全错；按 4 字节解释则逐槽完全自洽。

因此本项目统一按 **POP PC 消耗 4 字节**（PC 2 + CSR 2）建模。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Optional, Tuple

from . import disasm as _d
from .isa import KIND, F_EXT

__all__ = [
    "CodeInfo",
    "Insn",
    "classify_word",
    "decode_at",
    "POP_PC_BYTES",
    "SAFE_CLASSES",
]

#: ``POP PC``（= POPL PC）从 ROP 链上消耗的字节数：PC(2) + CSR(2)。
POP_PC_BYTES = 4

#: 允许被"原样内联"到 ROP 链里的指令类别。
SAFE_CLASSES = frozenset({"normal"})


@dataclass(frozen=True)
class CodeInfo:
    """一条指令的编码/分类信息（无文本，快）。"""

    word: int
    size: int
    entry: int
    kind: str
    handler: str
    cls: str
    sp_delta: int          # SP 变化（字节，正=弹栈/链变短方向）；0 = 不影响 SP
    ext: int = 0           # 4 字节指令的扩展字（读 ROM 后才能填）

    @property
    def is_ext(self) -> bool:
        return bool(_d.IDX[self.entry][10] & F_EXT)


def _popl_words(a: int) -> int:
    """POPL 选择位 a（bit0=EA, bit1=PC, bit2=PSW, bit3=LR）压/弹的 16 位字数。"""
    return (((a & 1) and 1 or 0)
            + (2 if (a & 2) else 0)
            + ((a & 4) and 1 or 0)
            + ((a & 8) and 1 or 0))


@lru_cache(maxsize=1 << 17)
def classify_word(word: int) -> Optional[CodeInfo]:
    """按指令字分类；无法识别返回 None（对应 ROM 中的数据/未定义字）。

    全 ROM 逐偏移扫描会调用上百万次，所以这里做 65536 项的 memo。
    """
    en = _d.entry_of(word)
    if en is None:
        return None
    i, handler, base, mask, av, ash, acnt, bv, bsh, bcnt, flags = en
    kind = KIND[i]
    a = (word >> ash) & av
    b = (word >> bsh) & bv
    size = 4 if flags & F_EXT else 2
    cls = "normal"
    sp_delta = 0

    if kind == "POPL":
        sp_delta = 2 * _popl_words(a)
        if a == 2:
            # 只有"纯 POP PC"（0xF28E）才是干净的链式返回：消耗 4 字节、只改 PC/CSR。
            cls = "term_pop_pc"
        elif a & 2:
            # POP PC 与其它字段混在一起（如 0xFF8E = POP LR,PSW,PC,EA）：
            # 虽然最后也会跳走，但之前会多消耗链上的字节 → 不能当作干净结尾。
            cls = "pop_pc_extra"
        else:
            cls = "pop_data"
            if sp_delta == 0:
                cls = "normal"          # POPL 选择位全 0：无操作
    elif kind == "POP":
        cls = "pop_data"
        sp_delta = 2 if acnt == 1 else acnt
    elif kind in ("PUSH", "PUSHL"):
        cls = "push"
        if kind == "PUSHL":
            words = 1 if (b & 0xE) == 0 else \
                (2 if (b & 2) else 0) + (1 if (b & 4) else 0) + (1 if (b & 8) else 0)
        else:
            words = (bcnt if bcnt != 1 else 2) // 2
        sp_delta = -2 * words
    elif kind == "BC":
        cls = "branch"
    elif kind == "B":
        cls = "jump"
    elif kind == "BL":
        cls = "call"
    elif handler == "OP_RT":
        cls = "ret"
    elif handler in ("OP_RTI", "OP_RTICE"):
        cls = "reti"
    elif handler in ("OP_SWI", "OP_BRK", "OP_ICESWI"):
        cls = "trap"
    elif handler == "OP_ADDSP":
        cls = "sp_add"
        sp_delta = _d.sext(a, 8)
    elif kind == "CTRL":
        if i == 80:          # MOV SP, ERn
            cls = "sp_set"
        elif i == 74:        # MOV ERn, SP
            cls = "sp_get"
    return CodeInfo(word=word, size=size, entry=i, kind=kind, handler=handler,
                    cls=cls, sp_delta=sp_delta)


@dataclass(frozen=True)
class Insn:
    """一条已从 ROM 解出的完整指令（含文本）。"""

    addr: int
    word: int
    ext: int
    size: int
    kind: str
    handler: str
    cls: str
    sp_delta: int
    mnemonic: str
    operands: str
    raw: bytes

    @property
    def text(self) -> str:
        return (self.mnemonic + (" " + self.operands if self.operands else "")).strip()

    def __str__(self) -> str:  # pragma: no cover - 仅调试用
        return "%05X  %-11s %s" % (self.addr, self.raw.hex(" ").upper(), self.text)


def decode_at(rom, addr: int) -> Optional[Insn]:
    """解码 ``rom`` 中 ``addr`` 处的一条指令；非法/越界返回 None。"""
    if addr < 0 or addr + 2 > len(rom):
        return None
    w = rom[addr] | (rom[addr + 1] << 8)
    info = classify_word(w)
    if info is None:
        return None
    en = _d.IDX[info.entry]
    ext = 0
    if info.size == 4:
        if addr + 4 > len(rom):
            return None
        ext = rom[addr + 2] | (rom[addr + 3] << 8)
    mnem, ops, _size, _ci = _d.fmt(en, w, ext, 0, addr + info.size)
    return Insn(addr=addr, word=w, ext=ext, size=info.size, kind=info.kind,
                handler=info.handler, cls=info.cls, sp_delta=info.sp_delta,
                mnemonic=mnem, operands=ops, raw=bytes(rom[addr:addr + info.size]))
