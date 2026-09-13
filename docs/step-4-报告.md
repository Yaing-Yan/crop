# 第 4a 步报告：ROP 模拟台与端到端执行验证

> 产物：`crop/ropvm.py`、`tests/test_ropvm.py`（自测总数 28 项，全绿）。
> 实测数据：`out/step4-exec.txt`。

## 一、做了什么

`crop/ropvm.py` 接入你自己的 `<LIFTED_PY>`（真 ROM 的
lifted 模拟器），提供：

* `run_rop_chain(chain, left_base, watch, pop_pc_bytes)`：把链写进 RAM，按 launcher
  的等价动作起跳（`SP = 链起址`，手工取一次 PC/CSR），然后让 CPU 自己按链执行；
  链尾接一个 `BRK` 哨兵槽即可干净停机。
* `run_linear(...)`：对照组，把 `.bin` 放进空闲代码页直接执行。

顺手修了 lifted 生成文件里两处只有"按需提升"路径才会踩到的遗漏：
`make_single_src()` 用到的 `entry_of` 与 `fmt_text` 在模块里没有定义。

## 二、实测结果（真 ROM 执行）

| C 程序 | `.bin` | 链 | 执行 |
|---|---|---|---|
| `aa = 7; bb = 9;` | 16 字节 | 28 字节 | 4 步后停在哨兵，**`D180=7 D181=9`** ✅ |
| `flag=1;dot=0; while(1){flag=0;dot=255;}` | 36 字节 | 62 字节 | 跑到 2000 步上限仍不停机（回跳枢轴真在循环），`D180=0 D181=255` ✅ |

也就是说：**rgcc 编译 → 解释器翻译 → 链在真 ROM 上执行 → RAM 终态等于 C 程序语义**，
整条链路第一次被端到端证明。

## 三、关键发现：`POP PC` 是 4 字节，lifted 文件的 `pop8` 有笔误

对照实验（`tests/test_ropvm.py::test_pop_pc_is_four_bytes`）：

```
pop_pc_bytes = 3  →  第 2 步就跑飞（事件停机），D180 没被写
pop_pc_bytes = 4  →  4 步后干净停机，D180=7 D181=9
```

这与第 1 步从**真实 `.rop` 产物**（Pixel Editor Pro / testing.rop）推出的结论完全一致，
也符合同一份 lifted 实现里"单字节访问按 2 字节对齐"的自洽规则
（`POP Rn` 读 1 字节 SP+=2，`PUSH Rn` 写 1 字节 SP-=2）。因此
`rom991cnx_lifted.py` 的

```python
def pop8(self):
    v = self.data[0][self.sp & 0xffff]
    self.sp = (self.sp + 1) & 0xffff     # ← 应为 + 2
    return v
```

几乎可以确定是转写笔误，建议回馈到 `<NXU16_DECOMPILER>`。
本仓库用 `run_rop_chain(..., pop_pc_bytes=4)` 显式修正，并有对照测试守住这个约定。

## 四、第 4b 步：引导（launcher）—— ✅ 已在真模拟器上跑通

按用户给的配方实测（**CasioEmuMsvc + fx-991cnxfVirtual**，用 computer-use 点模拟器界面 +
MCP 调试接口注入/读内存）：

```
write_memory 0xE9E0 ← 链（34 字节）
write_memory 0xD180 ← FD 24 D0 E9 8F 23 42        # FD 24 <左侧地址-10h 小端> 8F 23 42
在计算器窗口点 "→"，再点 "="
read_memory 0xD31C = [7, 9]                        # ✅ 正是 C 程序 aa=7; bb=9 的语义
```

### 机制（用真模拟器断点钉死，不是猜的）

* `8F 23 42` → PC 字节对 `8F 23` = `0x238F`、CSR = `0x42 & 0x0F` = 2；
  **PC 取指按 2 字节对齐**，所以实际执行 **`2:2238E`**：

  ```
  02238E  EA A1   MOV SP, ER14
  022390  3E F8   POP QR8
  022392  3E F0   POP QR0
  022394  8E F2   POP PC
  ```

* payload 里的 `<左侧地址 - 10h>` 就是**供给 ER14 的值**：`MOV SP,ER14` 把 SP 置成
  `L-0x10`，两个 POP 吃掉 `L-0x10 … L-1`，最后的 `POP PC` 从 `L … L+3` 取到**链首槽**
  → 链从 `L` 开始跑。
* **断点证据**：`add_execution_breakpoint 0x2238F` 被拒绝（"Code debugger is
  unavailable"，奇数 PC 不可执行），`0x2238E` 被接受且实测命中
  （`list_execution_breakpoints` → `[0, 140174]`，140174 = 0x2238E）；
  执行后 `PC=0x93C0`（正好是链首 gadget `POP XR0`）。
* 链跑起来后会被计算器**自身的中断**接管（观测到 `SP/PC` 被改），
  但目标 RAM 已写入 —— 与 RopIDE 手工程序表现一致。

产物：`crop/launcher.py`（`launcher_bytes()` + 配置解析）、`launcher.conf`、
`tests/test_launcher.py`（把实测的 7 字节与 gadget 语义锁进回归）。

## 五、剩余（第 4c 步）

1. **条件分支原语实测**：ROM `0x08DD6`（`testing.rop` 注释里的 `D68D3030`）是
   "ER0 == ER2 → R0 = 1，否则 0"。用真模拟器/模拟台实测它的链契约，
   再配 `0/1 索引 × 2 + 表基址` 与枢轴，就能做跳转表式条件分支，从而解锁
   `if` / `while(cond)`。
2. **中断问题**：链被计算器自身中断打断（SP/PC 被改）。要么在链首 `DI`（禁中断），
   要么把主循环放在能被中断安全恢复的位置 —— 这决定了长驻程序的写法。
3. 把 `launcher.conf` 接进 `tools/rgcc` / `tools/crop-rop`（自动打印 launcher 字节）。
