/* bubble_sort.c —— 冒泡排序（3 个数，展开）+ 上屏
 *
 *   tools/rgcc --rom-dir $CROP_ROM_DIR -I include --data-base EA40 --left-base EC00 \
 *              examples/bubble_sort.c -o out/bubble_sort.bin --rop out/bubble_sort-Rop.bin
 *
 * 当前编译器支持 `if (a > b) { … }` 与 `while (a > b) { … }`；
 * 运行时数组下标（v[j]）还没接（A4），所以用 3 个独立变量 + 展开的冒泡排序。
 * 排序结果在变量 a/b/c 里；屏幕上打一行固定文本作为"跑完了"的提示。
 */
#include "rstdio.h"
#include "rstdlib.h"

unsigned char a;
unsigned char b;
unsigned char c;
unsigned char t;

void main(void) {
    a = 9;
    b = 3;
    c = 7;

    /* 冒泡排序（3 个数，两轮足够） */
    if (a > b) { t = a; a = b; b = t; }
    if (b > c) { t = b; b = c; c = t; }
    if (a > b) { t = a; a = b; b = t; }

    /* 上屏 */
    rscreen_on();
    rclear();
    rprint_at(24, 20, "sorted!");
    rrefresh();
    rhalt();
}
