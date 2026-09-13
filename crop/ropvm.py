"""ROP 模拟台 —— 用真 ROM 的 lifted 模拟器把生成的 ROP 链**真正跑一遍**。

第 3 步为止验证的都是"链的结构正确性"（编码对拍、块可复算、DSL 一致）。本模块补上
端到端执行验证：把链写进 RAM，把 `.bin` 放进一个空闲代码页，然后

* 走 **ROP 路径**：``SP = 链起址``，手工执行一次 ``POP PC``（等价于 launcher 做的事），
  之后让 CPU 自己按链执行 gadget；
* 走 **直译路径**：直接执行 `.bin`（同一台机器、同一份 RAM 初始状态）；
* 两条路径结束后比较目标 RAM 字节 —— 相同即说明"翻译"没改变语义。

模拟器来自 ``<LIFTED_PY>``（13 MB，导入较慢，只导一次）。
"""

from __future__ import annotations
from crop.models import lifted_path   # noqa: E402

import importlib.util
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .chain import encode_gadget, encode_value

__all__ = ["LIFTED_PATH", "lifted_module", "RopRun", "run_rop_chain", "run_linear",
           "BRK_ADDR"]

def _lifted_default() -> str:
    try:
        return lifted_path()
    except LookupError:
        return ""


LIFTED_PATH = _lifted_default()

#: ROM 里一个 ``FF FF``（BRK）地址，用作链尾哨兵：执行到它就干净停机。
BRK_ADDR = 0x95C

_MOD = None


def lifted_module():
    """导入 lifted 模拟器（结果缓存）。"""
    global _MOD
    if _MOD is None:
        if not os.path.isfile(LIFTED_PATH):
            raise FileNotFoundError("找不到 lifted 模拟器：%s" % LIFTED_PATH)
        spec = importlib.util.spec_from_file_location("crop_lifted_rom", LIFTED_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["crop_lifted_rom"] = mod
        spec.loader.exec_module(mod)
        # 生成的 lifted 文件里 make_single_src() 用了入口解码函数，但没在模块里定义
        # （只有 --run 场景才会走到，所以是原工程的潜在遗漏）。这里从我们的
        # 反汇编器补齐这些名字，让"按需提升"这条路径可用。
        from .nxu16 import disasm as _dis

        def _fmt_text(en, w, ext):
            m, ops, _s, _ci = _dis.fmt(en, w, ext)
            return (m + (" " + ops if ops else "")).strip()

        mod.fmt_text = _fmt_text
        for name in ("entry_of", "fmt", "instr_size", "KIND", "ENTRIES", "sext",
                     "dec", "h2", "h2s", "h4s", "h5", "size_letter", "ALU_NAME",
                     "CONDS", "F_EXT", "F_SEXT", "F_DIR", "IDX", "DISPATCH",
                     "fmt_text", "text_of"):
            if not hasattr(mod, name) and hasattr(_dis, name):
                setattr(mod, name, getattr(_dis, name))
        _MOD = mod
    return _MOD


@dataclass
class RopRun:
    steps: int
    stopped: bool
    reason: str
    ram: Dict[int, int]
    io: List[Tuple[int, int]] = field(default_factory=list)   # 0xF000 以上的 IO 写（显存等）

    def ram_at(self, addr: int) -> int:
        return self.ram.get(addr, -1)

    def io_writes(self) -> Dict[int, int]:
        """IO 窗口的最终值（显存 0xF800 之类只能从这里看）。"""
        return {a: v for _s, a, v in self.io}

    def io_at(self, addr: int) -> int:
        return self.io_writes().get(addr, -1)


def _new_machine(prog: Optional[bytes] = None, prog_page: int = 5,
                 snapshot: Optional[List[int]] = None):
    mod = lifted_module()
    image = bytearray(mod.IMAGE)
    if prog:
        base = prog_page * 0x10000
        if base + len(prog) > len(image):
            raise ValueError("程序放不下")
        image[base:base + len(prog)] = prog
    m = mod.Machine(bytes(image), mod.RAM)
    if snapshot:
        for a, v in snapshot:
            m.data[0][a] = v
    return m


def _patch_pop8_quirk(m, pop_pc_bytes: int) -> None:
    """让 ``POP PC`` 消耗 ``pop_pc_bytes`` 字节（默认 4）。

    lifted 文件里 ``pop8()`` 是 ``SP += 1``；但同一份实现里 ``POP Rn``（单寄存器）
    是"读 1 字节、SP += 2"，``PUSH Rn`` 是"写 1 字节、SP -= 2"，单字节访问按 2 字节
    对齐才是自洽的。真实 .rop 产物也证明链是 4 字节槽。所以这里按需修正。
    """
    delta = pop_pc_bytes - 3
    if delta == 0:
        return
    orig = m.pop8

    def pop8():
        v = orig()
        m.sp = (m.sp + delta) & 0xffff
        return v

    m.pop8 = pop8


def _step_until(m, max_steps: int) -> Tuple[int, bool, str]:
    """执行直到停机/超步数。返回 (步数, 是否停机, 原因)。"""
    steps = 0
    while steps < max_steps:
        full = (m.csr << 16) | m.pc
        if full == ((0 << 16) | BRK_ADDR):        # 链尾哨兵
            return steps, True, "哨兵 BRK"
        fn = m.get(full)
        # 预提升的块自己写 m.pc；"按需单条提升"的块则**返回**下一条的完整地址
        # （`make_single_src` 的 `return 0x%05x`）。两种都要接住，否则 PC 会卡死。
        nxt = fn(m)
        if nxt:
            m.csr = (nxt >> 16) & 0xF
            m.pc = nxt & 0xFFFF
        m.steps += 1
        steps += 1
        if not m.running:
            return steps, True, "事件停机"
    return steps, False, "达到步数上限"


def run_rop_chain(chain: bytes, left_base: int = 0xE9E0, max_steps: int = 200000,
                  watch: Optional[List[int]] = None,
                  snapshot: Optional[List[int]] = None,
                  pop_pc_bytes: int = 4) -> RopRun:
    """把 ``chain`` 放进 RAM 的 ``left_base`` 处，按 ROP 方式执行。

    链尾建议自己接一个 ``BRK_ADDR`` 哨兵槽（``encode_gadget(BRK_ADDR)``），
    这样能干净停机而不是跑飞。
    """
    m = _new_machine(snapshot=snapshot)
    end = left_base + len(chain)
    if end > 0x10000:
        raise ValueError("链太长，超出 RAM")
    m.data[0][left_base:end] = chain
    m.sp = left_base
    # launcher 等价动作：从链首取第一个 PC/CSR 并跳过去。
    #
    # 关键：模拟器的 ``pop8()`` 里 SP 只加 1，而真实产物（RopIDE 生成的 .rop）
    # 是严格 4 字节槽，因此这里按 ``pop_pc_bytes`` 显式补齐差值。
    # ``pop_pc_bytes=4`` 能跑通就说明是 lifted 文件里 pop8 的转写笔误。
    _patch_pop8_quirk(m, pop_pc_bytes)
    m.pc = m.pop16()
    m.csr = m.pop8() & m.csr_mask
    steps, stopped, reason = _step_until(m, max_steps)
    watch = watch or []
    return RopRun(steps=steps, stopped=stopped, reason=reason,
                  ram={a: m.data[0][a] for a in watch}, io=list(m.io_log))


def run_linear(prog: bytes, entry: int = 0, prog_page: int = 5,
               max_steps: int = 200000, watch: Optional[List[int]] = None,
               snapshot: Optional[List[int]] = None) -> RopRun:
    """直接执行 ``.bin``（对照组）：把程序放进 ``prog_page``，从 ``entry`` 开始跑。"""
    m = _new_machine(prog=prog, prog_page=prog_page, snapshot=snapshot)
    m.csr = prog_page
    m.pc = entry
    m.sp = 0xF000                       # 直译执行时给一个无关的栈
    steps, stopped, reason = _step_until(m, max_steps)
    watch = watch or []
    return RopRun(steps=steps, stopped=stopped, reason=reason,
                  ram={a: m.data[0][a] for a in watch}, io=list(m.io_log))
