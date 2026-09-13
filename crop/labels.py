"""标签表：ROM 例程名 → 地址，**按每个 ROM 现场解析**（不硬编码地址）。

`labels.conf` 里每条标签给一个"参考地址"和一段**指令签名**。解析时：

1. 先在参考地址处验证签名（多数 Ver 相同，最快）；
2. 不匹配就**全 ROM 按签名扫描**，找到就说明该 Ver 把这段代码搬到了别处；
3. 都找不到 → 明确报错（绝不猜地址）。

于是"同机型不同 Ver"自动适配，C 代码里也不会出现任何裸地址。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .nxu16 import decode as _dec
from .rom import RomImage

__all__ = ["Label", "LabelTable", "load_labels", "gen_header"]


@dataclass
class Label:
    name: str
    hint: int
    sig: Tuple[str, ...]
    desc: str = ""


@dataclass
class Resolved:
    label: Label
    addr: int
    how: str          # 'hint' | 'scan'
    snippet: str


def load_labels(path: str) -> List[Label]:
    out: List[Label] = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("//", 1)[0].strip()   # 注释用 //（签名里有 #212 这种立即数）
            if not line or "=" not in line:
                continue
            name, _, rest = line.partition("=")
            hint_s, _, sig_s = rest.partition("|")
            sig = tuple(s.strip() for s in sig_s.split(";") if s.strip())
            out.append(Label(name=name.strip(), hint=int(hint_s.strip(), 16),
                             sig=sig, desc=""))
    return out


def _match(rom: RomImage, addr: int, sig: Sequence[str]) -> bool:
    """签名匹配；``xxx *`` 表示前缀匹配（用于"目标地址随 Ver 变化"的 BL/B）。

    例：``BL *`` 只要求是指令是 BL，不比较跳转目标 —— 因为不同 Ver 里
    被调用例程的位置会变，但指令形态相同。
    """
    space = rom.space
    b = addr
    for want in sig:
        ins = _dec.decode_at(space, b)
        if ins is None:
            return False
        if want.endswith("*"):
            if not ins.text.startswith(want[:-1].strip()):
                return False
        elif ins.text != want:
            return False
        b += ins.size
    return True


def _snippet(rom: RomImage, addr: int, n: int = 4) -> str:
    out, b = [], addr
    space = rom.space
    for _ in range(n):
        ins = _dec.decode_at(space, b)
        if ins is None:
            break
        out.append(ins.text)
        b += ins.size
        if ins.cls in ("term_pop_pc", "ret"):
            break
    return " ; ".join(out)


class LabelTable:
    """一批标签在某个具体 ROM 上的解析结果。"""

    def __init__(self, labels: Sequence[Label], rom: RomImage):
        self.rom = rom
        self.rows: List[Resolved] = []
        self.missing: List[Label] = []
        for lab in labels:
            if _match(rom, lab.hint, lab.sig):
                self.rows.append(Resolved(lab, lab.hint, "hint", _snippet(rom, lab.hint)))
                continue
            found = None
            for a in range(0, len(rom.space) - 1, 2):
                if rom.valid[a] and _match(rom, a, lab.sig):
                    found = a
                    break
            if found is None:
                self.missing.append(lab)
            else:
                self.rows.append(Resolved(lab, found, "scan", _snippet(rom, found)))

    def addr(self, name: str) -> int:
        for r in self.rows:
            if r.label.name == name:
                return r.addr
        raise KeyError("标签 %r 在该 ROM 里没找到（见 missing）" % name)

    def report(self) -> str:
        out = ["标签解析结果（%d 条，%d 条缺失）" % (len(self.rows), len(self.missing))]
        for r in self.rows:
            out.append("  %-11s @%05X  [%s]  %s" % (r.label.name, r.addr, r.how, r.snippet))
        for lab in self.missing:
            out.append("  %-11s **缺失**：签名 %s" % (lab.name, " ; ".join(lab.sig)))
        return "\n".join(out)


def gen_header(table: LabelTable, ver: str, guard: str = "CROP_ROMLABELS_H") -> str:
    """生成 include/romlabels.h（该 Ver 的地址常量）。"""
    lines = ["/* 由 tools/crop-labels 生成 —— 请勿手改 */",
             "#ifndef %s" % guard, "#define %s" % guard, "",
             "#define ROM_VER \"%s\"" % ver]
    for r in table.rows:
        macro = "ROM_" + r.label.name.upper().replace("-", "_")
        lines.append("#define %-18s 0x%05Xu   /* %s */" % (macro, r.addr,
                                                        " ; ".join(r.label.sig)))
    if table.missing:
        lines.append("")
        lines.append("/* !! 以下标签在该 ROM 里没找到：%s */" %
                     ", ".join(l.name for l in table.missing))
    lines.append("")
    lines.append("#endif /* %s */" % guard)
    return "\n".join(lines) + "\n"
