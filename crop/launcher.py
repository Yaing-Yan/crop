"""引导（launcher）—— 把 CPU 交给 ROP 链的那一步。

## 实测确认的配方（2026-09，CasioEmuMsvc + fx-991cnxfVirtual）

1. 把解释器产出的链写进 RAM 的**左地址** `L`（默认 `0xE9E0`）；
2. 把 launcher 写进输入区 `0xD180`：

       FD 24 <L - 10h，小端 2 字节> 8F 23 42

3. 在计算器窗口按 **→** 再按 **=**，链就跑起来了。

## 为什么是这几个字节（已用真模拟器断点证实）

* `8F 23 42` → PC 字节对是 `8F 23` = `0x238F`，CSR = `0x42 & 0x0F` = 2。
  但 **PC 会按 2 字节对齐（`pc & 0xFFFE`）**，所以实际执行的是 **`2:2238E`**：

      02238E  EA A1   MOV SP, ER14
      022390  3E F8   POP QR8
      022392  3E F0   POP QR0
      022394  8E F2   POP PC

  也就是说用的是 RopIDE 里那个完整的 `jump14-q8q0` 型 gadget：**先把 SP 设成 ER14，
  再顺着 SP 吃掉 16 字节数据，最后 POP PC 取到链首槽**。
* `<L - 10h>` 这 2 个字节就是**供给 ER14 的值**（= `MOV SP, ER14` 要设的 SP）。
  于是 `MOV SP,ER14` 把 SP 置为 `L-0x10`，`POP QR8` + `POP QR0` 吃掉
  `L-0x10 … L-1`，紧跟的 `POP PC` 从 `L … L+3` 取出**链的第一个槽** → 链从 `L` 开始跑。
* `FD 24` 是触发这次复制/引导所需的 2 字节字符前缀。
* **断点证据**（真模拟器）：`add_execution_breakpoint 0x2238F` 被拒绝
  （"Code debugger is unavailable"，奇数 PC 不可执行），而 `0x2238E` 被接受，
  并且实测中被命中（`list_execution_breakpoints` → `[0, 140174]`，140174 = 0x2238E）。

## 复现证据

```
write_memory 0xE9E0 ← 链（34 字节）
write_memory 0xD180 ← FD 24 D0 E9 8F 23 42
点 → 再点 =   ⇒   read_memory 0xD31C = [7, 9]      ← C 程序 aa=7; bb=9 的语义
                  SP=0x000E PC=0x8CF10（随后被计算器自身中断接管）
```

注意：链跑完/循环中会被计算器自身的**中断**打断（SP/PC 被改），
但目标 RAM 已经被写入 —— 这与 RopIDE 手工程序的表现一致。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["launcher_bytes", "LauncherConfig", "parse_conf", "DEFAULT_LAUNCHER_ADDR",
           "LAUNCHER_GADGET_PC", "LAUNCHER_GADGET_REAL", "LAUNCHER_GADGET_CSR", "SP_SKIP"]

#: launcher 写入的地址（输入区）
DEFAULT_LAUNCHER_ADDR = 0xD180
#: `8F 23 42` 里写的 PC 值（奇数；CPU 取指时按 2 字节对齐 → 真实入口 0x2238E）
LAUNCHER_GADGET_PC = 0x238F
#: 实际执行的 gadget 入口（= LAUNCHER_GADGET_PC & 0xFFFE）
LAUNCHER_GADGET_REAL = 0x2238E
LAUNCHER_GADGET_CSR = 2
#: gadget 在 POP PC 之前会吃掉 16 字节（POP QR8 + POP QR0）
SP_SKIP = 0x10
#: 前两个字节：触发复制/引导所需的 2 字节字符
PREFIX = bytes([0xFD, 0x24])
#: 后三个字节：PC 低/高 + CSR 段
TAIL = bytes([0x8F, 0x23, 0x42])


def launcher_bytes(left_addr: int, skip: int = SP_SKIP,
                   pivot_addr: int = LAUNCHER_GADGET_REAL) -> bytes:
    """生成要写进 `0xD180` 的 launcher 字节（7 字节）。

    `pivot_addr` 是**从该 ROM 扫描出来的**枢轴入口（见 `launcher_for_db`），
    所以换 Ver 只换参数，不改代码。
    """
    v = (left_addr - skip) & 0xFFFF
    pc = (pivot_addr & 0xFFFF) + 1          # CPU 取指按 2 字节对齐
    csr = (pivot_addr >> 16) & 0x0F
    return PREFIX + bytes([v & 0xFF, (v >> 8) & 0xFF,
                           pc & 0xFF, (pc >> 8) & 0xFF, 0x40 | csr])


def launcher_for_db(db, left_addr: int, prefer_verified: bool = True):
    """按 ROM 扫描结果推导 launcher：返回 ``(bytes, pivot)``。

    优先用与真机实测同形的枢轴（``MOV SP, ER14`` + 跳过 ≥16 字节），
    否则退到"最省的干净枢轴"。两者都是扫描出来的，没有写死地址。
    """
    from .vocab import find_pivots, is_clean_pivot

    pivots = [p for p in find_pivots(db) if is_clean_pivot(p)]
    if not pivots:
        raise ValueError("ROM 里没有可续链的干净枢轴，无法生成 launcher")
    best = None
    if prefer_verified:
        cand = [p for p in pivots if "MOV SP, ER14" in p.text and p.skip >= 16]
        best = cand[0] if cand else None
    best = best or pivots[0]
    return launcher_bytes(left_addr, best.skip, best.addr), best


@dataclass
class LauncherConfig:
    """`launcher.conf` 的内容。"""

    left_addr: int = 0xE9E0
    right_addr: int = 0xD3C0
    data_base: int = 0xD180
    launcher_addr: int = 0xD248          # 回放区＝输入缓冲区（源）；写 0xD180 会被"按右"覆盖
    len_field: int = 0xD244              # 输入长度/光标账本（不写它，按右不会导入）
    len_value: int = 7                   # 账本值 = launcher 字节数
    history_addr: int = 0xFC00          # 链在左地址处可能被覆盖时用的备用区
    keys: str = "→ 然后 ="              # 触发按键序列

    def launcher(self) -> bytes:
        return launcher_bytes(self.left_addr)


def parse_conf(text: str) -> LauncherConfig:
    """读 `launcher.conf`（`key = value`，`#` 注释，值按十六进制/字符串解析）。"""
    cfg = LauncherConfig()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k in ("left_addr", "right_addr", "data_base", "launcher_addr",
                 "history_addr", "len_field"):
            setattr(cfg, k, int(v, 16))
        elif k == "len_value":
            setattr(cfg, k, int(v, 16))
        elif k == "keys":
            cfg.keys = v
    return cfg
