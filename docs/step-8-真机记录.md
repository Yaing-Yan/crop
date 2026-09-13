# 第 8 步：真机验证记录（rstdio / rstring）

环境：CasioEmuMsvc + McpPlugin（`127.0.0.1:3001`），机型 VerF。
启动方式（**McpPlugin 只在模型跑起来后才加载**）：

```bash
cd <EMU_DIR> && ./CasioEmuMsvc models/<VERF_MODEL_DIR>
```

## 1. `examples/hello.c` —— ✅ 通过（round 1）

```
链 128 字节 @0xEC00；按键 [→] [=]
链回读一致 ✓
[D137] 期望 0E 实际 0E ✓                        ← 参数搬运（R0=字体）生效
0xDDD4 读回 "HELLO CROP" 点阵（8% 非零字节）
0xF800 显存 F800[32:56] == DDD4[24:48] ✓        ← rrefresh 的 24→32 字节/行搬运正确
```

同一份源码换 `--rom-dir` 编 VerC（标签自动解析到 `0x221B2/0x08706/0x07F00`，rt-fix `0x2B948`），
在 VerC 机型上同样读到渲染出的文字与显存副本。

## 2. `examples/rstring.c` —— ⚠️ **本轮没能在真机上复现**

程序（`#include <rstring.h>` + `<rstdlib.h>`）：清零 `msg` → `rcopy4(msg,"CROP")` →
`rput16(msg,65,66)` → 打印 → 刷新 → `rhalt()`。
编译/翻译没问题（两版 ROM 都是 171 条指令 / 428 字节链 / **0 条不支持**）。

真机三次尝试的现象（每次都确认过链写对了）：

```
链回读一致 True（0 个字节不同）
按键后  PC=09216（OS 空闲循环）  SP=EE34（OS 栈）     ← 说明**触发没生效**，控制权还在 OS 里
[D700] = 01 D7 10 0E 0B 00 00 00                     ← 不是程序的 msg
屏幕缓冲区非零字节 = 15（只有 OS 自己的光标竖条）
```

`hello.c` 在同样的注入流程下是通过的，所以问题不在链本身，最可能是**机器状态**：

* 模拟器退出时会把 RAM 存进机型目录的 `ram.dmp`，下次启动恢复 —— 于是 OS 的输入行/
  历史（`0xD248` 那一片）不是干净的，AC/`→`/`=` 的语义和首次启动时不同；
  VerC 那次也观察到"按 `→` 之后 OS 会把自己的历史写回 `0xD248`"。
* 本轮顺手修了 `tools/crop-verify` 的一个顺序问题：**AC 必须在写内存之前按**
  （之前是写完再按 AC，会把刚写进 `0xD248` 的 launcher 冲掉）。

下次要复现的步骤（还没做）：
1. 启动前把机型目录里的 `ram.dmp` 移走（或先 `MCP reset` 再等机器回到空闲循环）；
2. 按 AC → 写 launcher/账本/链 → 按 `→` `=`；
3. 读 `0xEC00`、`PC/SP`、数据区与屏幕缓冲区，把结果补到本节。

## 3. `rstdlib.h` 的 ROM 例程

`rwait` / `rscreen_init` 这类要写 `0xF0xx` 硬件寄存器的例程**还没封装**：
离线 lifted 模拟器把 IO 窗口 stub 掉了（`memwr ≥ 0xF000` 直接丢弃），无法离线确证，
而真机这一环本轮卡在上面那个触发问题上。等真机记录补齐再放进头文件。
