/* bubble3.c —— 可用版：3 个标量冒泡（骨架来自真机验证过的 644B 程序）+ 结尾画柱状图 */
#include "rstdio.h"
#include "rstdlib.h"

unsigned char a;
unsigned char b;
unsigned char c;
unsigned char t;
unsigned char y;

void main(void) {
    a = 9; b = 3; c = 7;

    /* 冒泡排序（3 个元素，全部展开；if 交换已在真机验证） */
    if (a > b) { t = a; a = b; b = t; }
    if (b > c) { t = b; b = c; c = t; }
    if (a > b) { t = a; a = b; b = t; }

    /* 排好后画三根柱子（列 0/1/2，宽 8 像素），再上屏并冻结 */
    rscreen_on();
    rclear();
    rplotcol(0, a);
    rplotcol(1, b);
    rplotcol(2, c);
    rrefresh();
    rhalt();
}
