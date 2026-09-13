/* rstdlib.h —— CROP 的工具/运行控制（rGCC 专用；不含 libc）
 *
 * 可用：
 *     rscreen_on() —— 开启屏幕显示（**不先调它，屏幕不会有任何输出**）
 * 纯 C 的两个（不依赖 ROM 例程）：
 *     rhalt()  —— 结束程序：把机器挂在这个空循环里（链的最后一定要有它）
 *     rspin64() —— 用展开出来的空转代码凑一点时间（固定 64 次）
 *
 * 下面这些 ROM 例程已经定位到（延时、屏幕初始化），但都要写硬件寄存器（0xF0xx），
 * 没法在离线的 lifted 模拟器里确证，**等真机验证记录补齐后再放进头文件**：
 *     rwait / rscreen_init
 * 宁可先不提供，也不给你一个没验证过的封装。
 */

#ifndef CROP_RSTDLIB_H
#define CROP_RSTDLIB_H

/*: 开启屏幕显示 —— **必须最先调用**：不打开显示的话，画进屏幕缓冲区的东西不会出现在屏上。
 *  对应 ROM 例程（VerF `0x0937C` / VerC `0x09310`，以 `RT` 结尾，编译器自动配 rt-fix）。 */
void rscreen_on(void);

/*: 程序结束：挂住（等价于 while (1) { }） */
void rhalt(void) {
    while (1) {
    }
}

/*: 用展开出来的空转代码凑一点时间（固定 64 次；变长循环要等条件分支） */
void rspin64(void) {
    unsigned char acc;
    acc = 0;
    for (unsigned char i = 0; i < 64; i = i + 1) {   /* 编译期展开 64 条 */
        acc = i;
    }
}

#endif /* CROP_RSTDLIB_H */
