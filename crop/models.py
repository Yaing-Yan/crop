"""机型目录 / 模拟器的本地路径解析 —— **仓库里不写任何本机路径**。

公开仓库里出现 ``~/casioemu/models/...`` 这类路径，等于把使用者的目录结构公开出去。
所以本模块只按下面的顺序找，找不到就明确报错：

1. 环境变量 ``CROP_ROM_DIR``（最常用：指到某个机型的目录）；
2. 仓库根的 ``.crop-local.conf``（**本机私有**，已在 .gitignore 里）里的键：

       verf   = <VerF 机型目录>
       verc   = <VerC 机型目录>
       rom2   = <另一份 VerC dump>
       lifted = <可选：ROM lifted 模拟器 .py 路径>
       emu    = <可选：模拟器可执行文件所在目录>

3. 都没有 → 抛 ``LookupError``（测试会跳过）。
"""

from __future__ import annotations

import os
from typing import Dict, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_CONF = os.path.join(ROOT, ".crop-local.conf")

_cache: Dict[str, str] = {}
_loaded = False


def _local() -> Dict[str, str]:
    global _loaded
    if not _loaded:
        _loaded = True
        if os.path.isfile(LOCAL_CONF):
            with open(LOCAL_CONF, encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.split("#", 1)[0].strip()
                    if "=" in line:
                        k, _, v = line.partition("=")
                        _cache[k.strip()] = v.strip()
    return _cache


def local(key: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get("CROP_" + key.upper())
    if v:
        return v
    return _local().get(key, default)


def model_dir(which: str = "verf") -> str:
    """机型目录。``which``：``verf`` / ``verc`` / ``rom2``。"""
    v = local(which)
    if not v:
        raise LookupError("没有配置机型目录 %r：请设 $CROP_ROM_DIR 或在 .crop-local.conf 里写 %s = …"
                          % (which, which))
    return os.path.expanduser(v)


def lifted_path() -> str:
    """ROM 的 lifted 模拟器（可选依赖）。"""
    v = local("lifted")
    if not v:
        raise LookupError("没有配置 lifted 模拟器路径（.crop-local.conf 的 lifted = …）")
    return os.path.expanduser(v)


def has(which: str) -> bool:
    try:
        return os.path.isdir(model_dir(which))
    except LookupError:
        return False
