# 第 7 步（调研）：A7 条件分支 —— ROM 原语盘点与结论

> 目标：把 `if (cond) … else …` / `while (cond) …` 做进 rGCC。
> 用户给的线索：`<EMU_DIR>/models/nX-U16_ROP_Gadget_Mapping.md`
> 第 7 节 + "条件跳转的 ROP 实现（方法一：栈操作法）"。

## 一、把"栈操作法"翻译成本 ISA 的要求

用户文档里的思路（改写链）落到本 ROM 就是三步：

1. **条件选择**：`ER2 = cond ? A : B`（A/B 是两个分支的链上地址）；
2. **写链**：把 ER2 写进"下一个将被 `POP PC` 取走的那一格"；
3. 于是紧接着的 `POP PC` 就跳到 A 或 B —— 真正的条件跳转。

## 二、扫描结果（`crop/cond.py`，两个 Ver 都跑了）

### ✅ 第 2 步的"16 位写内存"有现成的

| VerF | gadget | 说明 |
|---|---|---|
| `0x08F94` | `ST ER2, [ER8] ; POP XR8 ; POP PC` | 把 ER2（链上来的值）写进 [ER8]（链上来的地址），**自带 4 字节 POP** |
| `0x0D0EC` | `ST ER0, 0008h[ER8] ; POP ER8 ; POP PC` | 同上，带 +8 偏移 |
| `0x21FF0` | `ST ER14, [ER12] ; POP XR4 ; POP QR8 ; POP PC` | 值是 ER14、地址是 ER12（BP） |

VerC 同款在 `0x08F28 / 0x0D068 / 0x21FF0`。**"改写链"这条路本身是通的。**

### ❌ 第 1 步的"条件选择"在本 ROM 里没有现成的

把可能实现"条件选择"的形态都搜了一遍（脚本可复现，见 `crop/cond.py` 与本文末命令）：

| 想要的形态 | 搜索结果 |
|---|---|
| `BC cond,T ; POP PC` 且 T 处代码正好多吃 4 字节（=跳过一个槽） | **0 个** |
| `BC cond,T ; POP PC` 且 T 处是"16 位寄存器 ← 16 位寄存器 ; POP PC" | **0 个** |
| `BC cond,T ; POP PC` 且 T 处**任意**不吃链、以 `POP PC` 收尾的代码 | **1 个**：`0x175BC: BC EQ, 175C4h` → `MOV R7,#4 ; MOV R2,#0` |
| `BC cond,T` 且 T 处代码里含 16 位写内存 | **0 个** |

那个唯一的条件副作用 gadget 只能给出"条件成立时 `R7=4, R2=0`"，
**不足以**表达 `ER2 = cond ? A : B`（除非把两个分支地址硬塞到同一高字节里，太脆）。

### 🔎 顺便扫出来的另一个有用原语：相对计算跳转

```
0x19222: MOV ER4, SP ; ADD ER4, ER2 ; MOV SP, ER4 ; ADD SP, #32h ; POP XR4 ; POP QR8 ; POP PC
```

`ER2` 从链上来 ⇒ **SP 可以按链上给的值相对平移** = "相对计算跳转"（jump table 的基础）。
VerC 同款在 `0x1921E/0x19220/0x19222` 一族（地址略有不同）。
它把"条件是 0..N 的枚举值"的情形（如 `switch`）变成可能，但**不能**替代比较。

## 三、结论（诚实版）

* 用户的 `nX-U16_ROP_Gadget_Mapping.md` 说"在允许副作用的前提下图灵完备"——
  就**本 ROM（fx-991CN X 的这两份 dump）**而言，**条件跳转的最后一环（条件选择）没找到现成原语**；
  要补齐只有三条路：
  1. 更宽松的口径再扫一遍（允许 T 处代码吃若干链字节、允许进入 ROM 深处，
     只要能证明它最终稳定回到链上）—— 这是**纯扫描工作**，风险是"能跑但副作用不可控"；
  2. 用 `0x175BC` 那个条件副作用 gadget + 链布局对齐（把某分支地址放在 `0x??00`）——
     可行但脆，换 Ver 就可能失效；
  3. 换思路：`MOV ER4,SP ; ADD ER4,ER2 ; MOV SP,ER4` 做**相对计算跳转**，
     把条件编译成"枚举值"（需要先把比较结果物化成 0/1 —— 还是缺同一环）。
* 因此本步**没有**给 rGCC 加 `if`/`while(cond)`：宁可明确报错，也不生成错的链
  （rGCC 现在对带条件的 `if`/`while` 会明确说"需要 A7 的条件分支原语"）。

## 四、复现命令

```bash
python3 - <<'PY'
import sys, os; sys.path.insert(0, '.')
from crop.rom import RomImage
from crop.cond import scan_cond
cp = scan_cond(RomImage.auto(model_dir("verf")).space)
print(cp.describe())
PY
```

（`scan_cond` 还会把"条件搬运"候选列出来；上面那张表里的 0 个就是这么来的。）
