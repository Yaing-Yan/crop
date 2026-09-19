/* hello_world.c —— 在屏幕正中显示 "hello world!"
 *
 *   tools/rgcc --rom-dir $CROP_ROM_DIR -I include --data-base D700 --left-base D400 \
 *              examples/hello_world.c -o out/hello_world.bin --asm \
 *              --rop out/hello_world-Rop.bin
 *
 * 三个要点：
 *   1. **先 rscreen_on()** —— 不开显示，往屏幕缓冲区画什么都不显示；
 *   2. rclear() 清屏幕缓冲区（1536 字节）；
 *   3. rprint_at(x, y, 文字)：x/y 独立（这个入口 R0=x、R1=y）；
 *   4. **两张字符表，别搞混**（都用 tools/crop-chars 生成）：
 *        * `include/rchars2.h` ← **二级字符表 = 显示字库**：字母/数字与 ASCII 一致
 *          （0x68='h'、0x65='e'、0x6C='l'、0x6F='o'…），所以这里直接写 "hello world!"；
 *        * `include/rchars.h`  ← 一级字符表 = **输入/键盘**用的编码（0x68 是 "Abs("、0x65 是 "Neg("…），
 *          两者完全不同 —— 之前"乱码"就是因为拿一级表去当显示用。
 */
#include "rstdio.h"
#include "rstdlib.h"

void main(void) {
    rscreen_on();                        /* 开显示（不开的话屏幕不会有任何输出） */
    rclear();                            /* 清屏幕缓冲区 */
    rprint(FONT_NORMAL, 20, "hello world!");   /* 正常字体（0x0E）：11px/字，字母按二级表 */
    rrefresh();                          /* 刷到显存 */
    rhalt();                             /* 冻结（跳到 ROM 之外）：画面留住、不断电 */
}

