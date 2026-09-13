"""极简 C 预处理器（A6）—— 让 ``#include`` / ``#define`` 能用。

rGCC 是"面向 ROP 的 C 子集"编译器，不打算实现真正的 C 预处理器；这里只做头文件
真正需要的那点东西：

* ``#include "x.h"`` / ``#include <x.h>`` —— 按 ``-I`` 目录（以及当前文件所在目录）查找，
  内容原地展开；同一文件只展开一次（隐式的 ``#pragma once``）。
* ``#define NAME 值`` —— **对象式宏**，纯记号替换（不做带参宏）。
* ``#ifndef / #ifdef / #else / #endif`` —— 条件编译，够写头文件的 include guard。
* ``#pragma once`` / 空 ``#`` 行 —— 忽略。
* 其它 ``#`` 指令（``#if``、``#undef``、``#error``…）—— **明确报错**，不静默忽略。

替换和展开都逐行进行；``//`` 与 ``/* */`` 注释里的内容不参与替换。
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Sequence, Tuple

from .rgcc import RgccError

__all__ = ["preprocess", "PreprocTrace"]

_ID = re.compile(r"[A-Za-z_]\w*")
_STR = re.compile(r'"(?:[^"\\]|\\.)*"')
_DIRECTIVE = re.compile(r"^\s*#\s*([A-Za-z_]\w*)?\s*(.*)$")
_MAX_DEPTH = 16


class PreprocTrace:
    """记录 ``#include`` 展开路径（给 ``--asm``/调试看）。"""

    def __init__(self) -> None:
        self.files: List[str] = []

    def __str__(self) -> str:
        return " → ".join(self.files)


def _strip_comments(src: str) -> str:
    """去掉注释，但保留换行数（行号不乱）。"""
    out, i, n = [], 0, len(src)
    while i < n:
        two = src[i:i + 2]
        if two == "//":
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif two == "/*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("\n" * src.count("\n", i, j))
            i = j
        elif src[i] == '"':
            m = _STR.match(src, i) or re.compile(r'"').match(src, i)
            out.append(m.group())
            i = m.end()
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


def _subst(line: str, macros: Dict[str, str]) -> str:
    """对一行做对象式宏替换（字符串字面量里不替换）。"""
    parts = re.split(r'("(?:[^"\\]|\\.)*")', line)
    for k in range(0, len(parts), 2):
        for _ in range(_MAX_DEPTH):
            new = _ID.sub(lambda m: macros.get(m.group(), m.group()), parts[k])
            if new == parts[k]:
                break
            parts[k] = new
    return "".join(parts)


def _resolve(name: str, quoted: bool, here: str, dirs: Sequence[str]) -> Optional[str]:
    cands: List[str] = []
    if quoted and here:
        cands.append(os.path.join(here, name))
    cands += [os.path.join(d, name) for d in dirs]
    cands.append(name)
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def preprocess(src: str, include_dirs: Sequence[str] = (),
               origin: str = "<source>") -> Tuple[str, PreprocTrace]:
    """展开 ``src``，返回 ``(展开后的源码, 展开轨迹)``。"""
    trace = PreprocTrace()
    macros: Dict[str, str] = {}
    out = _expand(src, origin, list(include_dirs), macros, trace, {})
    return out, trace


def _expand(src: str, origin: str, dirs: List[str], macros: Dict[str, str],
            trace: PreprocTrace, done: Dict[str, bool]) -> str:
    real = os.path.realpath(origin) if origin != "<source>" else origin
    if real in done:
        return ""
    done[real] = True
    trace.files.append(os.path.basename(origin))

    here = os.path.dirname(origin)
    lines = _strip_comments(src).split("\n")
    out: List[str] = []
    # 条件编译栈：每层是 (当前是否输出, 本层是否已经取过真分支, 是否见过 else)
    stack: List[List[bool]] = []
    for no, line in enumerate(lines, 1):
        m = _DIRECTIVE.match(line)
        if not m:
            out.append(_subst(line, macros) if all(s[0] for s in stack) else "")
            continue
        op = (m.group(1) or "").lower()
        arg = m.group(2).strip()
        active = all(s[0] for s in stack)

        if op in ("ifdef", "ifndef"):
            name = arg.split()[0] if arg else ""
            if not name:
                raise RgccError("%s:%d：#%s 缺少宏名" % (origin, no, op))
            cond = (name in macros) if op == "ifdef" else (name not in macros)
            stack.append([active and cond, cond])
            continue
        if op == "else":
            if not stack:
                raise RgccError("%s:%d：#else 没有对应的 #if" % (origin, no))
            top = stack[-1]
            if top[2]:
                raise RgccError("%s:%d：重复的 #else" % (origin, no))
            top[0] = all(s[0] for s in stack[:-1]) and (not top[1])
            top[1] = top[1] or top[0]
            top[2] = True
            continue
        if op == "endif":
            if not stack:
                raise RgccError("%s:%d：#endif 没有对应的 #if" % (origin, no))
            stack.pop()
            continue
        if not active:
            continue
        if op == "include":
            quoted = arg.startswith('"')
            name = arg.strip('"<>')
            path = _resolve(name, quoted, here, dirs)
            if path is None:
                raise RgccError("%s:%d：找不到 #include %s（搜索目录：%s）" % (
                    origin, no, arg, ", ".join(dirs) or "无"))
            with open(path, encoding="utf-8") as fh:
                out.append(_expand(fh.read(), path, dirs, macros, trace, done))
            continue
        if op == "define":
            parts = arg.split(None, 1)
            if not parts:
                raise RgccError("%s:%d：#define 缺少宏名" % (origin, no))
            macros[parts[0]] = parts[1].strip() if len(parts) > 1 else "1"
            continue
        if op == "pragma":
            continue
        if op == "":
            continue
        raise RgccError("%s:%d：不支持的预处理指令 #%s" % (origin, no, op))
    if stack:
        raise RgccError("%s：有 %d 个 #if## 没有 #endif" % (origin, len(stack)))
    return "\n".join(out) + "\n"
