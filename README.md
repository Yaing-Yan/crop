# CROP — 用 C 写 ROP

> **C** to **R**eturn-**O**riented **P**rogramming —— 把 C 程序编译、翻译成
> **CASIO fx-991 CN X**（nX-U16 / "ePS-16" 核，CY-239F）上可执行的 **ROP 链**。

```
  main.c ──[rgcc]──► main.bin ──[crop-rop]──► Rop.bin ──► 真机 / 模拟器
    C 源码       受限 C 子集编译器   nX-U16 机器码   块匹配+转义   4 字节槽 ROP 链
                        ▲                            ▲
                        └──────── ROM.bin（-r 传入）─┘   ← 不硬编码任何地址
```

* **零硬编码**：机型、ROM 布局、gadget、原语、launcher 全部从 `-r ROM.bin` 现场扫描推导。
  同一机型换 Ver（版本号）自动适配 —— 已用 4 份 ROM 回归验证。
* **不猜**：翻不动的指令/语法一律明确报错并说明原因，绝不生成错代码。
* **可对拍**：链编码与真机验证过的 RopIDE 产物**逐字节一致**；解释器内部跑"直接构造字节"与
  "生成 DSL 再回编译"两条路，不一致立即报错。

---

## ✅ 已达成：双版本真机闭环

同一份 C 程序、同一份 44 字节链，在 **VerC / VerF** 两个版本上各自推导 launcher 并真机跑通：

| 版本 | ROM 目录 | launcher（写 `0xD248`） | 结果 |
|---|---|---|---|
| **VerC** | `models/fx991cnxVirtual` | `FD 24 F0 EB 7B 23 42`（枢轴 `0x2237A`） | `0xD710..D715 = 11 45 14 19 19 81` ✅ |
| **VerF** | `fx991cnxfVirtual` | `FD 24 F0 EB 8F 23 42`（枢轴 `0x2238E`） | 同上 ✅，且 `PC=0x10742`（**正落在链的循环 gadget 上**） |

```c
/* six.c —— 在 0xD710 处写入 11 45 14 19 19 81 */
unsigned char b0; unsigned char b1; unsigned char b2;
unsigned char b3; unsigned char b4; unsigned char b5;
void main(void) {
    b0 = 0x11; b1 = 0x45; b2 = 0x14;
    b3 = 0x19; b4 = 0x19; b5 = 0x81;
    while (1) { }
}
```

```
$ tools/rgcc --rom-dir <机型目录> six.c --data-base D710 --left-base EC00 \
        -o six.bin --rop six.Rop.bin --dsl six.rop
.bin：28 字节   链总长 44 字节   不支持 0 条，警告 0 条

0xD710..D717 = 11 45 14 19 19 81 00 00     ← 真机（CasioEmuMsvc）实测
SP = 0xEC26（链内）  PC = 0x10742（链内循环 gadget）
```

---

## 原理：为什么"复用 ROM 里的字节"就能执行

nX-U16 的 `POP PC` 从栈上取 **PC(2 字节) + CSR(2 字节)**，CSR 只取低 4 位。所以栈上的
地址序列就是"程序"：

> **若目标程序里的字节段 `B` 在 ROM 地址 `A` 处逐字节相同，且 `A+|B|` 处的下一条指令
> 正好是 `POP PC`，那么"跳去执行 `A`"与"原地执行 `B` 再返回链上"语义完全等价。**

解释器就是把 `.bin` 切成尽量大的这种块（**L1 块复用**），没有现成块的单条指令走
**L2 等价转义**，控制流走**L3 栈枢轴**。`POP <reg>` 需要的"任意常量"由链上的**内联数据**提供。

### ROP 链的物理格式

```
槽 = 4 字节：[PC_lo][PC_hi][CSR][pad]          POP PC 消耗 4 字节
gadget 地址两种编码（与 RopIDE 完全一致）：
  右式  #name;   → h1h2 + ("0"+addr[0]) + "00"
  左式  #-name;  → h1h2 + ("3"+addr[0]) + "30"   （低字节 00→01 的历史怪癖）
```

---

## 目录结构

```
crop/
├── crop/
│   ├── nxu16/        nX-U16 ISA + 反汇编（复用 ~/nxu16-decompiler，已与模拟器反汇编对拍）
│   │   └── decode.py 结构化解码/分类层（4 字节槽等结论都在这里）
│   ├── rom.py        ROM 映像：多文件 → 20 位代码空间（按 model.lua 的 rom_path）
│   ├── gadget.py     gadget 扫描器（逐偏移，含奇数地址）+ 最长匹配分块
│   ├── chain.py      ROP 链编码（槽/值/锚点/前向引用回填）
│   ├── ropdsl.py     RopIDE `.rop` DSL 编译器（输出可被 IDE 直接打开）
│   ├── vocab.py      ROP 词表挖掘（这台机器能当什么指令集用）
│   ├── planner.py    路线 A：语义 gadget 规划器（允许无害副作用）
│   ├── interp.py     解释器：.bin → 链（L1/L2/L3 + 逐块校验 + DSL 交叉验证）
│   ├── rgcc.py       rGCC：受限 C 子集 → nX-U16 .bin
│   ├── launcher.py   引导字节生成（从扫描出的枢轴推导）
│   └── ropvm.py      ROP 模拟台（用 lifted 模拟器真跑链）
├── tools/            crop-gadgets / crop-rop / crop-eval / crop-plan / rgcc / mcp-call
├── docs/             报告与规程（step-1..4 报告、注入规程、左右分离设计）
├── tests/            42 项自测（含跨 4 Ver 可移植性回归）
└── out/              示例产物与实测数据
```

---

## 安装与依赖

* Python **3.9+**，**无第三方依赖**（标准库 + `urllib`）。
* 一个机型目录（含 `rom.bin` 与 `model.lua`）：
  ```bash
  MODEL=~/casioemu/models/fx991cnxfVirtual        # VerF
  MODEL=~/casioemu/models/models/fx991cnxVirtual  # VerC
  ```
* 可选：`~/nxu16-decompiler/rom991cnx_lifted.py`（ROP 模拟台用）、
  CasioEmuMsvc + McpPlugin（真机注入用，MCP 端口 3001）。

```bash
git clone https://github.com/Yaing-Yan/crop && cd crop
python3 -m unittest discover -s tests -v        # 42 项，约 30 秒
```

---

## 快速开始

```bash
# ① 看 ROM 里有什么可用原语
python3 tools/crop-gadgets --rom-dir $MODEL

# ② 写 C
cat > six.c <<'EOF'
unsigned char b0; unsigned char b1; unsigned char b2;
unsigned char b3; unsigned char b4; unsigned char b5;
void main(void) {
    b0 = 0x11; b1 = 0x45; b2 = 0x14;
    b3 = 0x19; b4 = 0x19; b5 = 0x81;
    while (1) { }
}
EOF

# ③ 一条命令：C → .bin → Rop.bin（+ RopIDE 可打开的 .rop）
python3 tools/rgcc --rom-dir $MODEL six.c --data-base D710 --left-base EC00 \
        -o six.bin --rop six.Rop.bin --dsl six.rop --asm
```

---

## CLI

### `tools/crop-gadgets` —— 扫描 ROM，建立 gadget 索引

| 选项 | 默认 | 说明 |
|---|---|---|
| `--rom-dir DIR` / `-r FILE`（可重复） | — | 机型目录（按 `model.lua` 的 `rom_path`）或直接给 ROM |
| `--max-insns N` | 8 | gadget 中 `POP PC` 之前最多几条指令 |
| `--rt` | 关 | 额外索引 `RT` 结尾的 gadget（需硬件返回栈配合） |
| `--demo N` / `--demo-functions` | 0 / 关 | 可行性评估（随机代码 / 真实函数体） |
| `--json OUT` | — | 导出 `{HEX:[addr…]}` 索引 |

### `tools/crop-rop` —— 解释器：`.bin` → `Rop.bin`

| 选项 | 默认 | 说明 |
|---|---|---|
| `-i FILE` / `-o FILE` / `--dsl FILE` | — | 输入 `.bin` / 输出链 / 同时导出 RopIDE `.rop` |
| `--max-insns N` | 8 | L1 块长上限 |
| `--form {right,left}` | right | gadget 槽编码形式 |
| `--pivot-side {left,right}` | left | 链内跳转地址用哪一侧基准 |
| `--vocab` / `--dump` | 关 | 打印词表 / 打印生成的 DSL |

### `tools/crop-eval` —— 用真实 ROM 函数体实测翻译覆盖率

```bash
$ tools/crop-eval --rom-dir $MODEL --limit 150
合计指令 24350 条：L1 块复用 2417 (9.9%)，L2 等价转义 612 (2.5%)，不支持 21321 (87.6%)
最常无法翻译：MOV R1,#0 ×348 | L ER0,-0040h[ER14] ×280 | MOV ER0,ER14 ×204 | PUSH LR ×202
```
> 注意解读：这 87.6% 里很大一部分是**栈/帧指针/外部调用**，而 rGCC 被要求根本不许生成它们。

### `tools/rgcc` —— 受限 C 子集 → 可 100% 翻译的 `.bin`

| 选项 | 默认 | 说明 |
|---|---|---|
| `--rom-dir DIR` | 必填 | 取 ROM 做词表（全部原语推导自此） |
| `--data-base ADDR` | D180 | 变量区起始地址（十六进制） |
| `--left-base ADDR` | E000 | **链存放位置**（链内绝对地址随之重算） |
| `--rop` / `--dsl` / `--asm` | — | 输出链 / RopIDE 工程 / 汇编清单 |

### `tools/mcp-call` —— CasioEmuMsvc MCP 调试接口

```bash
tools/mcp-call tools                                       # 列出可用工具
tools/mcp-call call read_memory '{"address":"0xD710","size":8}'
tools/mcp-call call write_memory '{"address":"0xEC00","bytes":[66,7,1,0]}'
tools/mcp-call call keyboard_code '{"code":0x37,"pressed":true}'   # 按右（需长按 ≥0.9s）
```

---

## 引导 / 注入规程（真机验证版）

> 完整版见 [`docs/注入规程.md`](docs/注入规程.md)。这一节是踩了无数坑才定下来的。

| 地址 | 角色 | 要点 |
|---|---|---|
| `0xEC00` | **链存放处（左地址）** | 必须避开机器自己的栈：实测栈在 `0xE9xx~0xEBxx`，放 `0xE9E0` 会被压栈周期性踩烂 |
| `0xD248` | **回放区 = 输入缓冲区（源）** | launcher 注这里；注 `0xD180` 会被"按右"覆盖 |
| `0xD180` | **输入区（目的地）** | 按【→】把 `0xD248` 导进来 |
| `0xD244..0xD247` | **长度/光标账本** | 只写内容不写长度 → 按右什么都导不进来（会清空 `D180`） |

```python
# 注入（MCP）
write_memory 0xEC00 ← 44 字节链
write_memory 0xD248 ← launcher：FD 24 <左地址-10h，小端> <该 Ver 枢轴编码>
                       VerC: FD 24 F0 EB 7B 23 42   (枢轴 0x2237A)
                       VerF: FD 24 F0 EB 8F 23 42   (枢轴 0x2238E)
write_memory 0xD244 ← 07 07 07 07
write_memory 0xD180 ← 全 0（清空）
# 触发：按【→】再按【=】
```

`launcher_bytes(left, skip, pivot)` 会按扫描出的枢轴自动生成这 7 个字节
（**不写死任何地址**）；`--left-base` 改了链的位置，launcher 会自动跟着变。

---

## 设计要点

1. **`POP PC` 是 4 字节槽**。lifted 模拟器里 `pop8()` 写成 `SP += 1` 是笔误 ——
   按 3 字节走链在第 2 步就跑飞，按 4 字节走则完全正确（有对照测试守住）。
2. **`.bin` 的内联数据约定**：`POP <reg>` 之后紧跟它要弹走的字节，解释器把它们搬进链
   —— 这是"任意常量"的唯一来源。
3. **块写降级**（把链从 82 字节压到 44）：用 ROM 里现成的
   `LEA [ER14] ; ST QR0,[EA+] ; ST ER8,[EA+]`，一次槽写 8+2 字节。
   ⚠️ 入口必须是 `LEA [ERn]` 那条指令（`0x17DE8`），**不能**从外层 gadget 起点（`0x17DE2`）进
   —— 前面的 `L QR0,[EA+]` 会用垃圾 EA 把刚装好的数据冲掉（真机踩过）。
4. **全部原语从 ROM 推导**：`POP XRn/QRn`、`ST Rn+2,[ERn]`、同时具备 `L`/`ST` 的变量槽、
   基址装载 `POP ER12/ER14`、块写 gadget、launcher 枢轴 —— 一项推不出来就明确报错，
   绝不用错的常量兜底。

---

## 实测数据（fx991cnxfVirtual）

```
ROM 字节覆盖率：253026/262144 可解码为指令
控制流：POP PC=765  RT=299  BC=13490  B=4235  BL=8412
内联可用块：720 种 / 2174 个；栈枢轴 135 个
跨 4 个 Ver：共有块 671 种 / 并集 819 种 = 81.9%，共有块覆盖各版 94.6%~97.4% 的 gadget
```

---

## C 语言支持范围（以及为什么）

| 能力 | 状态 | 说明 |
|---|---|---|
| `unsigned char` 全局变量、常量赋值 | ✅ | 落在 `--data-base` 指定的地址 |
| 常量表达式（`2*(3+4)`、`(1<<5)\|3`、`%`、`~`） | ✅ | 编译期求值 |
| 变量拷贝 `x = y;` | ✅ | BP 切换 + `L`/`ST` 槽（全 ROM 唯一同时具备两者的槽） |
| `while (1) { … }` | ✅ | 回跳用栈枢轴，真机验证在循环 |
| 运行时算术 `x = x + 1` / `x = y * z` | ❌ | **本 ROM 没有可用的通用 ALU gadget**（干净 ALU 只有 48 种固定寄存器/立即数组合） |
| `if` / `while(cond)` | ❌ | 全 ROM 搜索不到"条件跳过一个链槽"的 gadget（路线 A/B 待做） |
| 指针（常量地址 / 指针解引用赋值） | ⚠️ 部分 | `*(u8*)ADDR = v` 已可用；运行时指针需字节传送合成 |
| 函数、数组、结构体、头文件 | ❌ | 待做 |

> 换句话说：**rGCC 的边界是"能 100% 翻译"**，而不是"支持完整 C"。编译完会逐块核对
> "该字节在 ROM 里存在且后面紧跟 `POP PC`"，不满足就 `RgccError`。

---

## 测试

```bash
python3 -m unittest discover -s tests -v      # 42 项
```

| 测试 | 断言什么 |
|---|---|
| `test_real_pixel_editor_first_64_bytes` | 我们的 DSL 编译器产物与真机验证过的 `.rop` **前 64 字节逐字节相同** |
| `test_every_indexed_gadget_is_byte_exact_and_terminated` | 索引里每个 `(块,地址)` 逐字节对得上且后面紧跟 `8E F2` |
| `test_pop_pc_slot_is_4_bytes` / `test_pop_pc_is_four_bytes` | 4 字节槽模型（含 3 字节对照实验必须失败） |
| `test_launcher_follows_the_derivation_rule` | launcher 每个字段都由该 ROM 的枢轴决定；**各 Ver 推出的必须不同** |
| `test_backend_derives_everything` | 后端零硬编码，推出来的 `ST`/`L` 在该 ROM 里真实存在 |
| `test_straight_line_program_writes_expected_ram` | 真 ROM 模拟器执行后 RAM 等于 C 语义 |

---

## 踩过的坑（都已在代码/流程里修掉）

| # | 坑 | 现象 | 修法 |
|---|---|---|---|
| 1 | launcher 注在目的地 `0xD180` | 按右被清空，`=` 跑旧账本 | 注到源区 `0xD248` |
| 2 | 只写内容、不写长度账本 | 按右导不进来（`D180` 全 0） | `0xD244..247 = 07` |
| 3 | 块写 gadget 入口选错 | `D710` 被写成 `C4 9C 00 70 E5 C8 00 7A`（确定性垃圾） | 改从 `LEA [ER14]` 进入 |
| 4 | 链放 `0xE9E0` | 被机器栈周期性踩（第 10 字节起变 `38 07 01 00`） | 放 `0xEC00` |
| 5 | MCP 按键太短（0.08s） | 键盘扫描直接漏掉 | 长按 ≥0.9s |
| 6 | 机器卡死在旧 ROP 时继续注入 | 注入的字节毫秒级被踩 | 先复位（`PC=0x91AA / SP=0xEE34` 才算干净） |

---

## 路线图

- [x] **第 1 步** ROM 映像 + ISA 解码 + gadget 扫描 + 可行性评估
- [x] **第 2 步** 链编码（与 RopIDE 真值逐字节对拍）+ ROP 词表 + 解释器 L1/L2/L3
- [x] **第 3 步** rGCC v0（常量 + 表达式 + 变量拷贝 + 死循环）+ 端到端流水线
- [x] **第 4 步** ROP 模拟台、真机引导打通、双版本（VerC/VerF）真机闭环
- [ ] **第 5 步** 左右分离（程序存储区 ↔ 运行区，见 `docs/step-5-设计-左右分离.md`）
- [ ] **第 6 步** 路线 A：语义 gadget 合成（放开额外 POP → 字节传送 → 指针/算术）
- [ ] **第 7 步** 条件分支（跳转表 + 0/1 索引枢轴）→ 解锁 `if` / `while(cond)`

---

## 许可与致谢

* 本项目代码：**MIT**（见 `LICENSE`）。
* `crop/nxu16/isa.py`、`crop/nxu16/disasm.py` 复用自 `nxu16-decompiler`
  （由 CasioEmuMsvc 的 `casioemu::CPU::opcode_sources` 自动生成，并已与模拟器自带反汇编
  `_disas.txt` 逐行对拍通过）。
* 链编码规则与 `.rop` 格式对齐 [ropide-vscode-plugin](https://github.com/Yaing-Yan/ropide-vscode-plugin)
  与 RopIDE 系列工具；真机注入借助 **CasioEmuMsvc + McpPlugin**。
* 感谢 Casio ClassWiz ROP 社区（wlyibo / Goodolls / fx-es(ms) 等）公开的 gadget 与教程，
  它们是对拍与验证的重要参照。
