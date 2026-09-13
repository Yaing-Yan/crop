"""RopIDE 的 `.rop` DSL 编译器（CROP 的输出格式之一）。

`.rop` 文件是一个 JSON：``{input, gadgets, leftStartAddress, rightStartAddress,
ideVersion}``，其中 ``input`` 是 RopIDE 的汇编 DSL。本模块按 RopIDE 的
``parser.js`` / ``compiler.py`` 规则把它编译成 ROP 链字节，规则已用真实产物
（Pixel Editor Pro v1.1 的前 64 字节，见 ``项目要求.md`` 里的十六进制清单）
逐字节对拍通过。

DSL 语法（只实现编译所需的子集，注释/花括号等一律忽略）：

===================  =====================================================
``// 注释``           到行尾
``$name = 0x1234;``   常量（10 进制/16 进制、可负）
``#gadget;``          右式 gadget（4 字节槽，CSR 段为 ``0X 00``）
``#-gadget;``         左式 gadget（CSR 段为 ``3X 30``，低字节 00→01）
``[expr]``            2 字节小端值；``$a + $b - 1F``，可前向引用
``<anchor>``          记录右基准地址（= right_base + 当前偏移）
``<-anchor>``         记录左基准地址（= left_base + 当前偏移）
``F5D0 01 00``        裸十六进制，直接进字节流
===================  =====================================================

所以 CROP 的解释器输出可以既是一份 ``Rop.bin``，又是一份可在 RopIDE 里打开
查看/继续编辑的 ``.rop`` 文件。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .chain import LEFT, RIGHT, ChainBuilder, ChainError

__all__ = ["RopGadget", "RopSource", "compile_rop_dsl", "load_rop_file", "save_rop_file"]

_HEX = set("0123456789abcdefABCDEF")
# RopIDE 的 otherStop：遇到这些字符就结束"其它字符"token
_OTHER_STOP = set("0123456789abcdefABCDEF/$#[<")


@dataclass
class RopGadget:
    name: str
    addr: int
    desc: str = ""
    tags: List[dict] = field(default_factory=list)


@dataclass
class RopSource:
    """一份 `.rop` 文件的内容（CROP 既读也写这个格式）。"""

    input: str = ""
    gadgets: List[RopGadget] = field(default_factory=list)
    left: int = 0xE9E0
    right: int = 0xD3C0
    ide_version: int = 100

    def gadget_map(self) -> Dict[str, RopGadget]:
        return {g.name: g for g in self.gadgets}


# ------------------------------------------------------------------ 读 / 写
def load_rop_file(path: str) -> RopSource:
    with open(path, encoding="utf-8") as fh:
        obj = json.load(fh)
    gadgets = []
    for g in obj.get("gadgets", []):
        gadgets.append(RopGadget(name=g.get("name", ""), addr=int(g.get("addr", "0"), 16),
                                 desc=g.get("desc", ""), tags=g.get("tags", [])))
    return RopSource(input=obj.get("input", ""), gadgets=gadgets,
                     left=int(obj.get("leftStartAddress", "E9E0"), 16),
                     right=int(obj.get("rightStartAddress", "D3C0"), 16),
                     ide_version=int(obj.get("ideVersion", 100)))


def save_rop_file(path: str, src: RopSource) -> None:
    obj = {
        "input": src.input,
        "leftStartAddress": "%04X" % src.left,
        "rightStartAddress": "%04X" % src.right,
        "gadgets": [{"name": g.name, "addr": "%05X" % g.addr, "desc": g.desc, "tags": g.tags}
                    for g in src.gadgets],
        "ideVersion": src.ide_version,
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


# ------------------------------------------------------------------ 编译
@dataclass
class _Tok:
    """一行 DSL 里的一个记号，保留位置信息以生成高亮/映射。"""

    kind: str          # 'const' | 'gadget' | 'value' | 'anchor' | 'hex' | 'other'
    text: str
    line: int
    col: int


def tokenize(input_text: str) -> List[_Tok]:
    toks: List[_Tok] = []
    for ln, line in enumerate(input_text.split("\n")):
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                break                                    # 注释到行尾
            if ch == "$":
                j = line.find(";", i)
                if j < 0:
                    toks.append(_Tok("other", line[i:], ln, i))
                    break
                toks.append(_Tok("const", line[i:j + 1], ln, i))
                i = j + 1
            elif ch == "#":
                j = i + 1
                while j < len(line) and line[j] not in "; ":
                    j += 1
                if j < len(line) and line[j] == ";":
                    toks.append(_Tok("gadget", line[i:j + 1], ln, i))
                    i = j + 1
                else:
                    toks.append(_Tok("other", line[i:j], ln, i))
                    i = j
            elif ch == "[":
                j = line.find("]", i)
                if j < 0:
                    toks.append(_Tok("other", line[i:], ln, i))
                    break
                toks.append(_Tok("value", line[i:j + 1], ln, i))
                i = j + 1
            elif ch == "<":
                j = i + 1
                while j < len(line) and line[j] not in "> ":
                    j += 1
                if j < len(line) and line[j] == ">":
                    toks.append(_Tok("anchor", line[i:j + 1], ln, i))
                    i = j + 1
                else:
                    toks.append(_Tok("other", line[i:j], ln, i))
                    i = j
            elif ch in _HEX:
                j = i
                while j < len(line) and (line[j] in _HEX or line[j].isspace()):
                    j += 1
                toks.append(_Tok("hex", line[i:j], ln, i))
                i = j
            else:
                j = i + 1
                while j < len(line) and line[j] not in _OTHER_STOP:
                    j += 1
                toks.append(_Tok("other", line[i:j], ln, i))
                i = j
    return toks


def compile_rop_dsl(src: RopSource, strict: bool = True) -> Tuple[bytes, List[str], Dict[str, int]]:
    """编译 `.rop` 的 ``input`` 字段，返回 ``(字节流, 警告, 常量表)``。"""
    cb = ChainBuilder(left_base=src.left, right_base=src.right)
    gmap = src.gadget_map()
    warnings: List[str] = []
    for t in tokenize(src.input):
        if t.kind == "const":
            body = t.text[1:-1]
            if "=" not in body:
                warnings.append("第 %d 行：常量缺少 '='" % (t.line + 1))
                continue
            name, _, val = body.partition("=")
            name, val = name.strip(), val.strip()
            try:
                cb.define(name, cb.eval_expr(val))
            except ChainError as e:
                warnings.append("第 %d 行：%s" % (t.line + 1, e))
        elif t.kind == "gadget":
            name = t.text[1:-1]
            left_form = name.startswith("-")
            if left_form:
                name = name[1:]
            g = gmap.get(name)
            if g is None:
                warnings.append("第 %d 行：未知 gadget #%s;" % (t.line + 1, name))
                continue
            cb.gadget(g.addr, LEFT if left_form else RIGHT)
        elif t.kind == "value":
            expr = t.text[1:-1]
            try:
                cb.value_expr(expr)
            except ChainError as e:
                warnings.append("第 %d 行：%s" % (t.line + 1, e))
        elif t.kind == "anchor":
            name = t.text[1:-1]
            side = RIGHT
            if name.startswith("-"):
                name = name[1:]
                side = LEFT
            cb.anchor(name, side)
        elif t.kind == "hex":
            h = "".join(c for c in t.text if c in _HEX)
            if len(h) % 2:
                warnings.append("第 %d 行：裸十六进制字符数为奇数" % (t.line + 1))
                continue
            cb.raw(h)
        # 'other' 一律忽略
    try:
        data = cb.finalize()
    except ChainError as e:
        if strict:
            raise
        warnings.append("回填失败：%s" % e)
        data = bytes(cb.buf)
    return data, warnings, dict(cb.constants)
