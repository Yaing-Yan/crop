/* barplot.c —— 24 根宽 8 像素竖条 + 每轮重画的选择排序（极致精简版）
 *   柱 i 占字节列 i（8 像素宽），位模式恒 0xFF ⇒ 无查表、无移位
 */
#include "rstdio.h"
#include "rstdlib.h"

#define N 24

unsigned char v[N];
unsigned char i;   /* 未排序区末尾下标 */
unsigned char y;   /* 行计数 */
unsigned char h;   /* 柱高 */
unsigned char j;   /* 扫描下标 */
unsigned char k;   /* 最大值下标 */
unsigned char a;   /* v[j] */
unsigned char b;   /* v[k] */
unsigned char t;   /* 临时 */
unsigned char c;   /* chart 自己的列计数 */

void chart(void) {
    c = N;
    while (c > 0) {
        rdec(c);
        h = v[c];
        rplotcol(c, h);        /* 内部自带行循环（用全局 y） */
    }
}

void main(void) {
    i = N; t = N;
    while (i > 0) { rdec(i); rdec(t); v[i] = t; }     /* 倒序初值 */
    rscreen_on();
    i = N;
    while (i > 0) {
        rdec(i);
        k = 0;
        j = i; rinc(j);
        while (j > 0) {
            rdec(j);
            a = v[j]; b = v[k];
            if (a > b) { k = j; }
        }
        a = v[i]; b = v[k];
        v[i] = b; v[k] = a;                            /* 唯一的两个 A4 写点 */
        rclear(); chart(); rrefresh();                  /* 每轮重画 → 动画 */
    }
    rhalt();
}
