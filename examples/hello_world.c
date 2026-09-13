/* hello_world.c —— 在屏幕正中显示 "hello world!"
 *
 *   tools/rgcc --rom-dir $CROP_ROM_DIR -I include --data-base D700 --left-base D400 \
 *              examples/hello_world.c -o out/hello_world.bin --asm \
 *              --rop out/hello_world-Rop.bin
 *
 * 三个要点：
 *   1. **先 rscreen_on()** —— 不开显示，往屏幕缓冲区画什么都不显示；
 *   2. rclear() 清屏幕缓冲区（1536 字节）；
 *   3. rprint(字体, 纵向像素, 文字)：这台机器的入口把 **R0 同时当"字体大小"和"起始 x 像素"**
 *      （0x0E=14），所以"居中"用**前置空格**微调 —— 每个字符宽 11 像素，
 *      屏幕 192 像素宽："hello world!" 共 12 字 ≈132 像素，前置 2 个空格后
 *      起点 ≈14+22=36，右边距 ≈24，视觉上居中。
 */
#include "rstdio.h"
#include "rstdlib.h"

void main(void) {
    rscreen_on();
    rclear();
    rprint(FONT_NORMAL, 20, "  hello world!");
    rrefresh();
    while (1) {
    }
}
