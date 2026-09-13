"""ROP 链编码 —— 与 RopIDE 的 `.rop` 编译产物逐字节兼容。

链的物理模型（已在第 1 步用真实产物验证）：

* 一个槽 = 4 字节：``[PC_lo][PC_hi][CSR_lo][CSR_hi]``；``POP PC`` 取走 PC(2)+CSR(2)，
  CSR 只取低 4 位（``csr_mask = 0x000F``）。
* gadget 地址编码（与 ``ropide`` 的 ``parser.js`` / ``compiler.py`` 一致）::

      h1h2 + ("0"|"3")+addr[0] + ("00"|"30")
      h1 = addr 低字节, h2 = addr 次低字节

  - 右式（``#name;``）   ：``0X 00`` → CSR = X
  - 左式（``#-name;``）  ：``3X 30`` → CSR = 0x3X & 0x0F = X（高半字节被 csr_mask 抹掉），
    且地址低字节为 ``00`` 时会被换成 ``01``（RopIDE 的历史行为，用于避开不可输入的
    0x00）。
* ``[expr]`` 编码成小端 2 字节；可前向引用，最后回填。
* ``<anchor>`` / ``<-anchor>``：记录"当前字节流长度 + 基准地址"，分别用右/左基准。

链在 RAM 里同时有两套地址视图：**左 = 程序储存地址**（`left_base`，真实写入处），
**右 = 运行地址**（`right_base`，SP 所在侧）。同一份字节，两个基准。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

__all__ = ["encode_gadget", "encode_value", "ChainBuilder", "ChainError", "fmt_addr"]

RIGHT = "right"
LEFT = "left"


class ChainError(Exception):
    pass


def fmt_addr(addr: int) -> str:
    """gadget 地址的规范写法：5 位大写十六进制（RopIDE 的 ``addr`` 字段格式）。"""
    if not 0 <= addr <= 0xFFFFF:
        raise ChainError("地址超出 20 位空间：%X" % addr)
    return "%05X" % addr


def encode_gadget(addr: int, form: str = RIGHT) -> bytes:
    """把一个 ROM 地址编码成 ROP 链里的 4 字节槽。"""
    a = fmt_addr(addr)
    allow00 = (form == RIGHT)
    h1 = a[3:5]
    if h1 == "00" and not allow00:
        h1 = "01"
    h2 = a[1:3]
    h3 = ("0" if allow00 else "3") + a[0]
    h4 = "00" if allow00 else "30"
    return bytes.fromhex(h1 + h2 + h3 + h4)


def encode_value(v: int) -> bytes:
    """``[expr]`` 的值：小端 2 字节（负数按补码）。"""
    if v < 0:
        v = 0x10000 + v
    if not 0 <= v <= 0xFFFF:
        raise ChainError("值超出 16 位：%X" % v)
    return bytes((v & 0xFF, (v >> 8) & 0xFF))


@dataclass
class _Deferred:
    pos: int          # 在 buf 中的字节偏移（占位 2 字节）
    expr: str


@dataclass
class ChainBuilder:
    """ROP 链字节流构造器（同时维护左右两套地址）。"""

    left_base: int = 0xE9E0
    right_base: int = 0xD3C0
    buf: bytearray = field(default_factory=bytearray)
    constants: Dict[str, int] = field(default_factory=dict)
    constant_order: List[str] = field(default_factory=list)
    anchor_sides: Dict[str, str] = field(default_factory=dict)
    _deferred: List[_Deferred] = field(default_factory=list)
    _patched: bool = False

    # ------------------------------------------------------------ 位置
    @property
    def offset(self) -> int:
        """当前字节流长度（= 下一个字节的偏移）。"""
        return len(self.buf)

    def left_addr(self, off: Optional[int] = None) -> int:
        return self.left_base + (self.offset if off is None else off)

    def right_addr(self, off: Optional[int] = None) -> int:
        return self.right_base + (self.offset if off is None else off)

    # ------------------------------------------------------------ 写入
    def raw(self, data) -> int:
        """写入任意字节（bytes 或十六进制字符串）。返回写入起点偏移。"""
        if isinstance(data, str):
            h = "".join(c for c in data if c in "0123456789abcdefABCDEF")
            if len(h) % 2:
                raise ChainError("裸十六进制字符数为奇数：%r" % data)
            data = bytes.fromhex(h)
        pos = self.offset
        self.buf += data
        return pos

    def gadget(self, addr: int, form: str = RIGHT) -> int:
        return self.raw(encode_gadget(addr, form))

    def value(self, v: int) -> int:
        return self.raw(encode_value(v))

    def value_expr(self, expr: str) -> int:
        """写入一个 2 字节值；表达式中若有未定义常量则先占位、最后回填。"""
        pos = self.offset
        try:
            v = self.eval_expr(expr)
        except ChainError:
            self.raw(b"\x00\x00")
            self._deferred.append(_Deferred(pos, expr))
            return pos
        return self.raw(encode_value(v))

    def anchor(self, name: str, side: str = RIGHT) -> int:
        """记录锚点：值 = 该侧基准 + 当前偏移。"""
        base = self.left_base if side == LEFT else self.right_base
        addr = base + self.offset
        if name in self.constants and self.constants[name] != addr:
            # 重名锚点以最后一次为准（与 RopIDE 行为一致）
            pass
        self.constants[name] = addr
        self.anchor_sides[name] = side
        if name not in self.constant_order:
            self.constant_order.append(name)
        return addr

    def define(self, name: str, value: int) -> None:
        self.constants[name] = value
        if name not in self.constant_order:
            self.constant_order.append(name)

    # ------------------------------------------------------------ 表达式
    def eval_expr(self, expr: str, allow_undefined: bool = False) -> int:
        """``$a + $b - 1F`` 这种表达式求值（与 RopIDE 同规则）。"""
        value = 0
        symbol = "+"
        for part in expr.split():
            if part.startswith("$"):
                name = part[1:]
                if name in self.constants:
                    v = self.constants[name]
                elif allow_undefined:
                    symbol = ""
                    continue
                else:
                    raise ChainError("未定义常量 $%s" % name)
                if symbol == "+":
                    value += v
                elif symbol == "-":
                    value -= v
                elif symbol == "":
                    value += v
                else:
                    raise ChainError("表达式符号错乱：%r" % expr)
                symbol = ""
            elif part in ("+", "-"):
                if symbol:
                    raise ChainError("表达式符号错乱：%r" % expr)
                symbol = part
            else:
                try:
                    v = int(part, 16)
                except ValueError:
                    raise ChainError("表达式里的非法记号：%r" % part)
                if symbol == "-":
                    value -= v
                elif symbol in ("+", ""):
                    value += v
                else:
                    raise ChainError("表达式符号错乱：%r" % expr)
                symbol = ""
        if symbol:
            raise ChainError("表达式以运算符结尾：%r" % expr)
        if not -0x8000 <= value <= 0xFFFF:
            raise ChainError("表达式结果超出 16 位：%r" % expr)
        return value

    # ------------------------------------------------------------ 收尾
    def finalize(self) -> bytes:
        """回填所有前向引用的值，返回最终字节流。"""
        for d in self._deferred:
            v = self.eval_expr(d.expr)
            self.buf[d.pos:d.pos + 2] = encode_value(v)
        self._deferred.clear()
        self._patched = True
        return bytes(self.buf)

    def hex(self) -> str:
        return self.finalize().hex().upper()

    def hexdump(self, base: Optional[int] = None, width: int = 16) -> str:
        """带左侧地址的 hexdump（左侧 = 程序储存地址，注入模拟器用它）。"""
        b = self.finalize()
        base = self.left_base if base is None else base
        lines = []
        for i in range(0, len(b), width):
            chunk = b[i:i + width]
            lines.append("%05X  %-*s  %s" % (
                base + i, width * 3 - 1,
                " ".join("%02X" % c for c in chunk),
                "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)))
        return "\n".join(lines)
