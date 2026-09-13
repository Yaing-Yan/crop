/* features.c —— A2 函数 / A3 数组 / A5 结构体 的最小演示（配合 include/rstdio.h）
 *
 * 编译：
 *   tools/rgcc --rom-dir <VERF_MODEL_DIR> -I include \
 *              --data-base D700 --left-base EC00 examples/features.c \
 *              -o out/features.bin --asm --rop out/features-Rop.bin
 *
 * 它做的事：往屏幕缓冲区里画两行字（"*ELLO" 与 "CROP A2/A3/A5"），刷新到显存。
 * 函数是内联展开的（ROP 链没有真正的调用栈），数组/结构体地址在编译期算出来。
 */
#include "rstdio.h"

struct rowinfo {                 /* A5：结构体 */
    unsigned char row;
    unsigned char len;
};
struct rowinfo head;

unsigned char banner[14] = "CROP A2/A3/A5";   /* A3：数组 + 初值 */
unsigned char text[6] = "HELLO";
unsigned char first;

unsigned char star(void) {       /* A2：函数（返回值） */
    return 42;                   /* '*' */
}

void show(unsigned char y, const unsigned char *s) {   /* A2：带参数的函数 */
    rprint(FONT_NORMAL, y, s);
}

void main(void) {
    first = star();
    text[0] = first;             /* 函数返回值 → 数组元素 */
    head.row = 0;
    head.len = 5;
    rclear();
    show(head.row, text);
    show(2, banner);
    rrefresh();
    while (1) {
    }
}
