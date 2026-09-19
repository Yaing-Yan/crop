/* rstdlib.h —— CROP 的工具/运行控制（rGCC 专用；不含 libc）
 *
 * 可用：
 *     rscreen_on() —— 开启屏幕显示（**不先调它，屏幕不会有任何输出**）
 * 纯 C 的两个（不依赖 ROM 例程）：
 *     rhalt()  —— 结束程序：**冻结**（跳到 ROM 之外的不存在地址，手写 ROP 的 kill 手法）
 *     rspin64() —— 用展开出来的空转代码凑一点时间（固定 64 次）
 *
 * 下面这些 ROM 例程已经定位到（延时、屏幕初始化），但都要写硬件寄存器（0xF0xx），
 * 没法在离线的 lifted 模拟器里确证，**等真机验证记录补齐后再放进头文件**：
 *     rwait / rscreen_init
 * 宁可先不提供，也不给你一个没验证过的封装。
 */

#ifndef CROP_RSTDLIB_H
#define CROP_RSTDLIB_H

/*: 画完跳回 OS 的空闲主循环（**替代 while(1)**：不会霸占主循环，因此不会卡死/断电） */
void rexit(void);

/*: 关中断 —— 想在屏上**留住**画面时必须先调它：
 *  链跑完停在 while(1) 时，计算器的 OS 仍在中断里跑显示任务，会把屏幕重画回它自己的状态。 */
void rint_off(void);

/*: 开中断 */
void rint_on(void);

/*: 开启屏幕显示 —— **必须最先调用**：不打开显示的话，画进屏幕缓冲区的东西不会出现在屏上。
 *  对应 ROM 例程（VerF `0x0937C` / VerC `0x09310`，以 `RT` 结尾，编译器自动配 rt-fix）。 */
void rscreen_on(void);

/*: 返回 v*k（k 必须是编译期常量）：运行时缩放/乘法。 */
unsigned char rmul(unsigned char v, unsigned char k);

/*: 取一个 0..n 的随机数（结果 0..n）。 */
unsigned char rrand(unsigned char n);

/*: 延时：t/30 秒（rsleep(6) ≈ 0.2 秒）。 */
void rsleep(unsigned char z, unsigned char t);   /* rsleep(0, 6) ≈ 0.2 秒 */

/*: 运行时自增/自减（写成语句用：`rinc(a);` `rdec(a);`）—— 循环计数/递减用。 */
void rinc(unsigned char v);
void rdec(unsigned char v);
void radd8(unsigned char v, unsigned char d);      /* v += d（d 为编译期常量） */

/*: 比较：返回 a > b（0/1）。A7 条件分支的基础积木。 */
unsigned char rcmp_gt(unsigned char a, unsigned char b);

/*: 冻结 CPU（程序结束）：跳到一个不存在的地址（ROM 之外）—— 比 while(1) 体面，
 *  不会霸占 OS 主循环，因此不会触发自动关机。 */
void rhalt(void);

/*: 用展开出来的空转代码凑一点时间（固定 64 次；变长循环要等条件分支） */
void rspin64(void) {
    unsigned char acc;
    acc = 0;
    for (unsigned char i = 0; i < 64; i = i + 1) {   /* 编译期展开 64 条 */
        acc = i;
    }
}

#endif /* CROP_RSTDLIB_H */
