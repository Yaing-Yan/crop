# 第 10 步：压缩实现规格（可直接照做）

目标：`examples/hello_world.c` 的链从 **114 字节** → **~70 字节**（用户给的"手写 42 字节"是极限参照，
本规格先拿到结构性收益），并且**对所有程序通用**。

---

## 优化 1：字符串骑链（省掉整块初值写入）

**现状的成本**：字符串先被写进数据区（块写 gadget），每 8 字节要付
`POP ER14(4+2) + POP QR0(4+8) + 块写 gadget(4) + pad(12)` = **34 字节**。
15 字节的 `"hello world!"` ⇒ 2 块 = **68 字节**（整条链 114 字节里的大头）。

**关键观察**：`POP QR0` 的 **payload 本身就是链上的字节**，而链就在 RAM 里
（`0xEC00`/`0xD400`…）。所以只要把 `ER2` 指向**那段 payload 的链地址**，
字符串就"已经躺在内存里"了 —— **完全不需要拷贝到数据区**。

**改法（两侧各一小步）**

1. **rGCC 侧**（`crop/rgcc.py`）：
   * 新增一种"只放数据、不落盘"的方式：给字符串常量发射
     `const_pop`（即 `POP QRn` 的指令字节）+ **8 字节 payload**（按 8 字节分块、末尾补 0），
     **后面不跟** `blk_body`（不写 RAM）；
   * 需要字符串地址时（`("str", idx)` 解析处）发射 `POP ER2` + **哨兵值 `FF FF`**
     （约定：`0xFFFF` 表示"取最近一次 payload 的链地址"）。

2. **解释器侧**（`crop/interp.py`）：
   * 在 `pop_block` 分支里记住**上一次 payload 在链上的偏移**（`cb` 的长度即可）；
   * 当遇到 `POP ER2` 且 payload == `FF FF` 时，用锚点把 payload 地址变成链上真地址：
     ```python
     name = "P%04X" % payload_off
     cb.anchor(name, opts.pivot_side)       # 或在发射 payload 前就 anchor
     gvalue("$%s" % name)
     ```
     （`ChainBuilder.anchor` + `gvalue` 的前向引用机制**已经现成**，见 `crop/chain.py`）

3. **数据区布局**：字符串不再占用 `--data-base` 之后的只读区 ⇒
   `compile_source` 里 `parser.strings` → 数据区那一段可以整体删掉（保留数组 `= "…"` 那种
   **变量初值**的块写，因为变量是要读写的）。

**验证**：`tools/rgcc … examples/hello_world.c` 链长应降到 ~70 字节；
在 lifted 模拟器里跑一遍，确认 `0xD7xx` 数据区**不再出现**那份拷贝、而屏幕缓冲区仍画出正确的字。

---

## 优化 2：块写 pad 归零 / 变短

现状固定选 `0x17DE8`（尾巴 `POP QR8 ; POP XR4 ; POP PC` = **白付 12 字节**）。
`Backend._derive` 里改成**按代价排序**：

```
代价 = pad 字节数 * 2 + (写入字节数 < 8 ? 惩罚 : 0)
```

候选（VerF 实测）：`0x17DE8`（pad=12，一次 8 字节）、`0x17E7E`（pad=0，但连写 5 段、
中途有 `MOV R0,#1` ⇒ 只有当 payload 能凑出它要的寄存器图案时才用）。
**先实现"评分 + 报告"，再决定是否启用 `0x17E7E`**；不确定时保持现 gadget（安全优先）。

---

## 优化 3：`--recommandly-simpler`（极致模式，显式开关）

参考用户给的 `~/Downloads/Pixel Editor Lite - v1.1.rop` 的手法，按风险从低到高：

1. **借字节**：把参数值塞进 gadget 自身的字节（例如 `POP ER14` 后面的 2 字节若恰好等于
   某个需要的常量，就可省一个槽）；
2. **共槽**：相邻的 `POP ERn` 合并（`POP QRn` 一次装 4 个寄存器，能同时满足多个参数）；
3. **尾调用**：`while(1)` 的尾跳转用 `jump-q8`/`do-nothing` 组合替代 10 字节的标准跳转；
4. **数据/代码共用**：字符串 payload 同时当作某个 gadget 的合法字节（"双解释"）。

默认关闭 ⇒ 保证默认产物的可读、可验证；开启时打印每一步省了多少字节（便于回归对比）。

---

## 优化 4：A7 条件分支接线（让 `bubble_sort.c` 能跑）

1. `labels.conf` 里已有 `er0>er2?`（`0x0B61E`）、`switch-case`（`0x08F10`）、
   `jump-q8`/`jump-e14`（计算跳转）；
2. 解释器加 **L4 层**：`.bin` 里的 `BC cond, L` 编译成
   `[比较原语槽] → [把 0/1 送进跳转表的索引] → [计算跳转]`；
3. rGCC：`if (a > b) { … }` / `while (a > b) { … }`（两侧允许单字节变量/常量），
   比较用 `er0>er2?` 物化成 0/1；
4. 验收：`examples/bubble_sort.c`（随机数 → 冒泡排序 → 逐步刷新）在真机上跑起来。

---

## 复现/验证命令

```bash
# 链长对比
tools/rgcc --rom-dir $CROP_ROM_DIR -I include --data-base D700 --left-base D400 \
           examples/hello_world.c -o out/hello_world.bin --rop out/hello_world-Rop.bin

# 语义对照（真 ROM 的 lifted 模拟器）
python3 -m unittest tests.test_rgcc_features -v

# 真机（注入后用 MCP 读回；用完记得 resume）
tools/crop-verify --rom-dir $CROP_ROM_DIR --bin out/hello_world.bin \
                  --src examples/hello_world.c --data-base D700 --screen DDD4
```
