# 第 9 步：例程词表导入 + 条件分支结论修正 + 压缩路线

数据来源（用户提供）：
* gadget 表：`~/latexcnx/991cn x/gadgets/VERF.json`（55 条，name/addr/desc）
* 排版合集：`~/latexcnx/991cn x/991CNX VerF 拼字合集/document.tex`

## 1. 已导入：53 条例程进入 `labels.conf`（**签名从 ROM 现场反汇编得到**）

工具：`tools/crop-import --rom-dir <机型> gadgets.json`

```
VerF: 解析成功 57 条，缺失 0
VerC: 解析成功 51 条，缺失 6（VerF 专属/签名不同，可后续单独补）
```

**关键新原语**（含寄存器契约）：

| 名字 | 地址 | 契约 / 作用 |
|---|---|---|
| `print-0x1y` | `0828A` | **R0=x 偏移、R1=y 偏移、ER2=文本** ⇒ 终于能**真正居中**（不必再用前置空格凑 x） |
| `print-0ynf` | `221AE` | R0=y、ER2=文本（固定字体） |
| `print-1ysf` | `222B4` | R1=y、ER2=文本（字体随设置） |
| `print-0f1y` | `221BE` | R0=字体、R1=y、ER2=文本（现在的 `rprint`） |
| `refresh-ddd4` | `08772` | 刷 DDD4 → 显存（`rrefresh`） |
| `clear-ddd4` | `07F6C` | 清 DDD4（`rclear`） |
| `screen-on` | `0937C` | 开显示（`rscreen_on`）；**不开就什么都不显示** |
| `sleep` | `091D8` | 延时 |
| `randint` | `13960` | 取随机数 |
| `swap` | `10F04` | 交换两个字 |
| `strcpy` | `203C8` | 字符串拷贝（**`rstring.h` 可以用它**） |
| `do-nothing` | `08300` | **就是一条 `POP PC`** ⇒ 链上"空操作一格"的完美原语（比 `MOV R7,R7` 更省） |
| `er0>er2?` | `0B61E` | `CMP ER0,ER2 ; BC GT,.. ; MOV R0,#1 ; RT` ⇒ **把比较结果物化成 0/1** |
| `switch-case` | `08F10` | 表驱动分派 |
| `wait-press` | `0F7CA` | 等按键 |
| `jump-q8` / `jump-e14` / `jmp-er6` | — | 计算跳转（跳转表基础） |

## 2. 结论修正：A7「条件分支不可能」的说法**过于悲观**

`docs/step-7-…md` 当时的结论是"缺少把条件物化成值的那一环，所以做不了 if/while"。
但新词表里有 **`er0>er2?`**（比较 → R0 = 0/1，`RT` 返回）以及 `switch-case`、
`jump-q8`/`jump-e14`（计算跳转）——这三样合起来**足以实现条件分支**：

```
条件（a > b） →  er0>er2?  →  R0 = 0/1
            →  用 R0 去索引跳转表（switch-case / jump 类原语）
            →  两条路径分别落到不同的链位置
```

**下一步（A7 重开）**：把这三个原语接进解释器（新的 L4 层），rGCC 端先支持
`if (a > b) { … }` 与 `while (a > b) { … }`（比较两侧都是单字节/常量）。这样
**冒泡排序**这种"逻辑明明很简单"的程序才有可能编译出来。

## 3. 压缩路线（用户要求：hello_world 主程序部分应能压到 ~42 字节）

现状 114 字节的账（见 `docs/问答-原理-语法-硬编码.md`）：字符串初值 68 + 例程/尾部 46。
可砍的地方，按收益排序：

1. **去掉每块 12 字节 pad**：块写 gadget 现在选 `0x17DE8`（尾巴 `POP QR8 ; POP XR4 ; POP PC`），
   而 ROM 里有 **pad=0** 的 `0x17E7E`（一口气从寄存器堆写 ~30 字节）。
   ⇒ `Backend` 改成**按"pad 最小、有效字节最多"排序**挑块写原语，并新增"寄存器堆批量写"发射路径；
2. **字符串直接骑链**：`POP QRn` 的内联数据本身就躺在链上（RAM 里），
   只要**把 ER2 指向那段链地址**就不必再写一份到数据区（省掉整块初值写入）；
3. **例程调用只留必要槽**：`clear-ddd4`/`refresh-ddd4` 已经是 1 槽；
   `print-0x1y` 只需 `POP ER0`(x,y) + `POP ER2`(地址) + 1 槽 = 3 槽；
4. **链尾 `while(1)`**：可用 `jump-q8`/`do-nothing` 之类的更省形式（现在 ~10 字节）；
5. **`--recommandly-simpler`（极致模式）**：借字节（把参数塞进 gadget 自身字节、复用同一段
   链做数据/代码）、共槽、尾调用等 —— 参考用户给的作品
   `~/Downloads/Pixel Editor Lite - v1.1.rop`；
   目标：把常见"清屏→打印→刷新"三件套压到 **~42 字节**量级。

> 说明：C→ROP 的密度天然不如手写 ROP（编译器要保守、要可验证），
> 但上面 1~4 是**通用**优化（对所有程序生效），5 是显式的"极致模式"开关。

## 4. 冒泡排序（可视随机数）

`examples/bubble_sort.c` 的 C 逻辑没问题，但它需要**运行时比较 + 条件跳转 + 变量下标**，
这三样正是待补的 A7/A4 —— 所以现在编译会明确报错（而不是生成错链）。
按第 2 节把 `er0>er2?` + 计算跳转接上以后，它就能真正跑起来；
在那之前，仓库里保留这个样例作为**编译器能力的验收目标**。

---

## 5. 本轮已落地 & 下一步（交接说明）

**已落地**

* `tools/crop-import`：把 RopIDE 的 gadget 表（JSON）导入 `labels.conf`（签名现场反汇编）；
  VerF 57 条全中、VerC 51 条（6 条待补）。
* `rprint_at(x, y, text)` → **`print-0x1y`**（`0x0828A`，x/y 独立）⇒ 真正居中；
  `examples/hello_world.c` 改为 `rprint_at(30, 20, "hello world!")`（x=(192−12×11)/2）。
* 仓库内已写入 git 身份与 push 代理（`git config user.name/user.email/http(s).proxy`），
  以后直接 `git push` 即可（走 `127.0.0.1:1081`）。

**下一步（按收益排序，均已定位到实现点）**

1. **字符串骑链（省掉整块初值写入，114 → ~70 字节）**
   * 现状：字符串先放进数据区（`--data-base` 之后），再用块写 gadget 写进 RAM ⇒ 每 8 字节付 34 字节；
   * 目标：把字符串**直接作为链上的内联数据**（`POP QRn` 的 payload 本来就落在链里 = RAM），
     把 `ER2` 指向**那个链地址**即可，完全不用拷贝；
   * 实现：rGCC 侧对 `"..."` 参数发射一个新的标记块（例如 `POP QRn` + payload + 一个
     "取该 payload 地址"的伪指令）；解释器侧用 `ChainBuilder.anchor()` 记录 payload 的
     链地址，并把 ER2 的值编成**前向引用**（`cb.value_expr("$Lxxxx")`，机制现成）。
2. **块写 pad 归零**：`Backend` 改为按"pad 最小、有效字节最多"挑块写 gadget
   （现在固定选 `0x17DE8`，尾巴 `POP QR8 ; POP XR4 ; POP PC` = 白付 12 字节）。
3. **`--recommandly-simpler` 极致模式**：借字节 / 共槽 / 尾调用，参考用户给的
   `Pixel Editor Lite - v1.1.rop`；作为显式开关，默认关（保证可读与可验证）。
4. **A7 重开**：把 `er0>er2?`（比较→0/1，`0x0B61E`）、`switch-case`（`0x08F10`）、
   `jump-q8`/`jump-e14` 接成解释器 L4 条件分支；rGCC 端加 `if (a > b)` / `while (a > b)`，
   之后 `examples/bubble_sort.c` 才有望真正跑起来。
5. 补 VerC 缺的 6 条签名（`labels.conf` 里以 VerF 提示地址写的那些，逐条看 VerC 的等价形态）。
