"""ROM 映像：把若干个 ROM 文件按 64 KiB 页拼进 nX-U16 的 20 位代码空间。

卡西欧 ClassWiz 机型的固件由多片 ROM 组成，模拟器按 ``rom.bin``、``rom2.bin``…
顺序装入连续的 64 KiB 页（``rom.bin`` 4 页 → ``rom2.bin`` 就从第 4 页起）。
``fx991cnxfVirtual`` 的实测：``rom.bin``=页 0-3、``rom2``=4-7、…、``rom7``=24-38。

本模块不做任何机型硬编码：页号由文件顺序与文件大小推导，也允许显式指定。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence

PAGE = 0x10000
SPACE = 0x100000  # 20 位代码空间


@dataclass
class RomFile:
    path: str
    data: bytes
    base_page: int

    @property
    def base_addr(self) -> int:
        return self.base_page * PAGE

    @property
    def pages(self) -> int:
        return (len(self.data) + PAGE - 1) // PAGE


@dataclass
class RomImage:
    """由一到多个 ROM 文件拼成的 20 位代码空间映像。"""

    files: List[RomFile] = field(default_factory=list)
    space: bytearray = field(default_factory=lambda: bytearray(SPACE))
    valid: bytearray = field(default_factory=lambda: bytearray(SPACE))

    # ---------------------------------------------------------------- 构造
    @classmethod
    def from_paths(cls, paths: Sequence[str], first_page: int = 0) -> "RomImage":
        img = cls()
        page = first_page
        for p in paths:
            with open(p, "rb") as fh:
                data = fh.read()
            rf = RomFile(path=p, data=data, base_page=page)
            img.add(rf)
            page += rf.pages
        return img

    @classmethod
    def auto(cls, directory: str, first_page: int = 0, primary_only: bool = True) -> "RomImage":
        """载入一个机型目录里的 ROM。

        模拟器（``model.lua``）实际只加载 ``rom_path``（默认 ``rom.bin``）：
        它就是该机型**当前生效**的那张映像，可能横跨多个 64 KiB 页。
        同一目录里其余的 ``rom2.bin``… 往往是同机型其它版本/其它映像，
        默认不拼进来（``primary_only=False`` 时才按顺序拼）。
        """
        primary = cls._model_rom_path(directory) or "rom.bin"
        paths = [cls._find(directory, primary)]
        if not primary_only:
            for name in tuple("rom%d.bin" % i for i in range(2, 32)):
                p = cls._find(directory, name, required=False)
                if p:
                    paths.append(p)
        return cls.from_paths(paths, first_page=first_page)

    @staticmethod
    def _find(directory: str, name: str, required: bool = True) -> Optional[str]:
        for cand in (name, name.upper(), name.lower(), name.capitalize()):
            full = os.path.join(directory, cand)
            if os.path.isfile(full):
                return full
        if required:
            raise FileNotFoundError("找不到 ROM 文件：%s" % os.path.join(directory, name))
        return None

    @staticmethod
    def _model_rom_path(directory: str) -> Optional[str]:
        """从 ``model.lua`` 里读出 ``rom_path = "..."``（不引入 Lua 依赖）。"""
        import re

        for cand in ("model.lua", "MODEL.LUA"):
            full = os.path.join(directory, cand)
            if os.path.isfile(full):
                with open(full, "r", errors="replace") as fh:
                    text = fh.read()
                m = re.search(r'rom_path\s*=\s*"([^"]+)"', text)
                if m:
                    return m.group(1)
        return None

    def add(self, rf: RomFile) -> None:
        base = rf.base_addr
        end = base + len(rf.data)
        if end > SPACE:
            raise ValueError("%s 超出 20 位空间（%05X+%X）" % (rf.path, base, len(rf.data)))
        self.space[base:end] = rf.data
        self.valid[base:end] = b"\x01" * len(rf.data)
        self.files.append(rf)

    # ---------------------------------------------------------------- 访问
    def __len__(self) -> int:
        return SPACE

    def read(self, addr: int, n: int) -> bytes:
        return bytes(self.space[addr:addr + n])

    def is_valid(self, addr: int, n: int = 1) -> bool:
        if addr < 0 or addr + n > SPACE:
            return False
        return self.valid[addr:addr + n] == b"\x01" * n

    def page(self, addr: int) -> int:
        return addr >> 16

    def file_of(self, addr: int) -> Optional[RomFile]:
        for rf in self.files:
            if rf.base_addr <= addr < rf.base_addr + len(rf.data):
                return rf
        return None

    def loaded_pages(self) -> List[int]:
        return [rf.base_page + i for rf in self.files for i in range(rf.pages)]

    # ---------------------------------------------------------------- 统计
    def summary(self) -> str:
        lines = []
        total = 0
        for rf in self.files:
            total += len(rf.data)
            lines.append("  %-14s 页 %2d-%2d  %8d 字节" % (
                os.path.basename(rf.path), rf.base_page, rf.base_page + rf.pages - 1, len(rf.data)))
        lines.append("  合计 %d 文件 / %d 字节 / 已装入 %d 页（%d 个 64 KiB 页空）" % (
            len(self.files), total, len(self.loaded_pages()), 16 - len(self.loaded_pages())))
        return "\n".join(lines)


def iter_words(rom: bytes) -> Iterable[tuple]:
    """线性遍历：产出 (addr, word, size)。无法识别的字按 2 字节数据跳过。"""
    from .nxu16 import decode as _dec

    a = 0
    n = len(rom) - 1
    while a < n:
        w = rom[a] | (rom[a + 1] << 8)
        info = _dec.classify_word(w)
        size = info.size if info else 2
        yield a, w, size
        a += size
