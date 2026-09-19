/* barsort.c —— 96 个随机数（RanInt(1,48)）的冒泡排序 + 条形图实时刷新
 *
 *   tools/rgcc --rom-dir $CROP_ROM_DIR -I include --data-base D700 --left-base E400 \
 *              examples/barsort.c -o out/barsort.bin --rop out/barsort-Rop.bin
 *
 * 屏幕 192x64：96 根柱子 ⇒ 每根宽 2 像素（x = 2*i），高度 = 数据值（≤48）。
 */
#include "rstdio.h"
#include "rstdlib.h"

#define N 96

unsigned char v[N];
unsigned char i;
unsigned char j;
unsigned char k;
unsigned char a;
unsigned char b;
unsigned char t;

/* 画第 i 根柱子（用全局 i 当下标） */
void bar(void) {
    a = v[i];              /* A4：运行时下标读 */
    t = rmul(i, 2);        /* x = 2*i（运行时乘法） */
    r_xy(t, 0);            /* R0 = x, R1 = 0 */
    r_set2();              /* R2 = 2（柱宽） */
    r_set_h(a);            /* R3 = 柱高 */
    r_blockdraw();         /* 画反色框 */
}

/* 整张图重画一遍 */
void chart(void) {
    i = N;
    while (i > 0) {
        rdec(i);
        bar();
    }
}

void main(void) {
    /* 随机初始化 */
    i = N;
    while (i > 0) {
        rdec(i);
        t = rrand(48);
        v[i] = t;          /* A4：运行时下标写 */
    }

    rscreen_on();
    r_dim();
    rclear();
    chart();
    rrefresh();

    /* 冒泡排序：每交换一次就重画 */
    i = N;
    while (i > 0) {
        rdec(i);
        j = i;
        while (j > 0) {
            rdec(j);
            k = j;
            rinc(k);       /* k = j+1 */
            a = v[j];
            b = v[k];
            if (a > b) {
                t = a;
                v[j] = b;
                v[k] = t;
                rclear();
                chart();
                rrefresh();
            }
        }
    }
    rhalt();
}
