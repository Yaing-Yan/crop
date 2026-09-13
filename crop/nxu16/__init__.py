"""nX-U16（Casio ePS-16 / CY-239F）架构支持层。

本子包内的 ``isa.py`` / ``disasm.py`` 来自用户自己的反编译器工程
``~/nxu16-decompiler``（由 CasioEmuMsvc 的 ``casioemu::CPU::opcode_sources``
自动生成、并与模拟器自带反汇编 ``_disas.txt`` 逐行对拍通过），
CROP 直接复用它们作为唯一的 ISA 真值来源，见 ``decode.py``。

对外主要入口：
    decode.decode_at(bytes, addr) -> Insn | None
    decode.classify_word(word)    -> CodeInfo | None
"""

from . import isa, disasm  # noqa: F401
from .decode import Insn, CodeInfo, decode_at, classify_word  # noqa: F401

__all__ = ["isa", "disasm", "Insn", "CodeInfo", "decode_at", "classify_word"]
