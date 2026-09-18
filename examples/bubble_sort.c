/* bubble_sort.c —— 目标样例：屏幕显示随机数并冒泡排序
 *
 *   ⚠️ 现状：这份 C 逻辑没问题，但它需要三个**尚未实现**的编译器能力：
 *     1. 运行时比较（a > b）；
 *     2. 条件分支（if / while(cond)）—— 见 docs/step-7 与 step-9（结论已修正：
 *        ROM 里其实有把比较物化成 0/1 的原语 `er0>er2?`，接上计算跳转就能做）；
 *     3. 变量下标 a[i]（运行时地址算术，A4）。
 *   所以现在 `tools/rgcc` 会**明确报错**，而不是生成错的链。等 A7/A4 落地后它应当直接跑通。
 *
 * 编译（A7/A4 完成后）：
 *   tools/rgcc --rom-dir $CROP_ROM_DIR -I include --data-base D700 --left-base D400 \
 *              examples/bubble_sort.c -o out/bubble_sort.bin --rop out/bubble_sort-Rop.bin
 */
#include "rstdio.h"
#include "rstdlib.h"

#define N       8
#define ROW     20

unsigned char v[N];
unsigned char i;
unsigned char j;
unsigned char tmp;
unsigned char line[N + 1];

/* 画一行方块：数值越大，画得越长（用字符 '#' 表示长度） */
void draw(void) {
    for (unsigned char k = 0; k < N; k = k + 1) {
        line[k] = 48 + v[k];          /* 简化：直接把值画成数字字符 */
    }
    line[N] = 0;
    rprint_at(14, ROW, line);
}

void main(void) {
    rscreen_on();
    rclear();

    /* 随机初始化 */
    for (i = 0; i < N; i = i + 1) {
        v[i] = randint(90);
    }
    draw();
    rrefresh();

    /* 冒泡排序 */
    for (i = 0; i < N; i = i + 1) {
        for (j = 0; j < N - 1; j = j + 1) {
            if (v[j] > v[j + 1]) {
                tmp = v[j];
                v[j] = v[j + 1];
                v[j + 1] = tmp;
            }
        }
        draw();
        rrefresh();
    }
    rscreen_on();
    rclear();
    rprint_at(14, ROW, "sorted!");
    rrefresh();
    while (1) {
    }
}
