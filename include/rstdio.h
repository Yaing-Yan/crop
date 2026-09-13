/* rstdio.h —— CROP 的屏幕输出（rGCC 专用；不含 libc）
 *
 * 使用方式（见 examples/hello.c）：
 *     #include "rstdio.h"
 *     void main(void) {
 *         rclear();
 *         rprint(FONT_NORMAL, 0, "HELLO");
 *         rrefresh();
 *     }
 *
 * 这些函数**不是**直接在 C 里跳 ROM 地址：编译器按 labels.conf 里的**指令签名**
 * 逐 Ver 现场解析出例程入口（见 crop/labels.py），再把例程机器码从该 ROM 读出、
 * 连同"把变量搬进寄存器"的搬运槽一起编进 ROP 链（见 crop/libabi.py、crop/routines.py）。
 * 于是换 Ver / 换机型只要标签签名还匹配，C 源码一行都不用改。
 */

#ifndef CROP_RSTDIO_H
#define CROP_RSTDIO_H

/* ------------------------------------------------------------------ 常量 */

/*: 屏幕缓冲区地址（写这里 = 画像素；每字节 = 纵向 8 个像素，共 192×64 像素） */
#define SCREEN_BUF      0xDDD4

/*: 屏幕缓冲区大小（字节）：192 列 × 64 行 ÷ 8 像素/字节 = 1536 = 0x600 */
#define SCREEN_BYTES    0x0600

/*: 字体大小（rprint 的第一个参数，同时也是起始 x 坐标 —— ROM 例程的约定） */
#define FONT_NORMAL     0x0E    /* 正常字体 */
#define FONT_SMALL      0x0A    /* 小字体 */
#define FONT_TABLE      0x08    /* 表格小字 */

/*: 纵向像素范围（rprint 的第二个参数） */
#define SCREEN_H        64

/* ------------------------------------------------------------- 库例程声明 */
/* 参数一律是 C 变量/常量；"搬进 r0/r1/er2"是编译器的事（用户要求，见 README）。 */

/*: 在屏幕缓冲区打印一行文字。
 *  @param font 字体大小，取 FONT_NORMAL / FONT_SMALL / FONT_TABLE
 *  @param row  纵向像素位置（0..63）
 *  @param text 以 0 结尾的字符串地址（字符串常量由编译器放进只读数据区） */
void rprint(unsigned char font, unsigned char row, const unsigned char *text);

/*: 把屏幕缓冲区刷新到显存（并提交给显示控制器） */
void rrefresh(void);

/*: 清空屏幕缓冲区（0x600 字节全 0） */
void rclear(void);

#endif /* CROP_RSTDIO_H */
