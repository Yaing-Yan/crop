/* hello_world.c —— 在屏幕正中显示 "hello world!"
 *
 *   tools/rgcc --rom-dir $CROP_ROM_DIR -I include --data-base D700 --left-base D400 \
 *              examples/hello_world.c -o out/hello_world.bin --asm \
 *              --rop out/hello_world-Rop.bin
 *
 * 三个要点：
 *   1. **先 rscreen_on()** —— 不开显示，往屏幕缓冲区画什么都不显示；
 *   2. rclear() 清屏幕缓冲区（1536 字节）；
 *   3. rprint_at(x, y, 文字)：x/y 独立（这个入口 R0=x、R1=y），
 *      所以"居中"是算出来的：屏幕 192 像素、字体 0x0E 每字 11 像素，
 *      "hello world!" 12 字 ≈132 像素 ⇒ x=(192-132)/2=30。
 */
#include "rstdio.h"
#include "rstdlib.h"

void main(void) {
    rscreen_on();
    rclear();
    rprint_at(30, 20, "hello world!");   /* x=(192-12*11)/2=30：真正居中 */
    rrefresh();
    while (1) {
    }
}
