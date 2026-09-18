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
 *   4. **这台机器不是 ASCII**：字库是卡西欧自己的编码（见 include/rchars.h，
 *      由 tools/crop-chars 从"一级字符表.xlsx"生成）。`0x68` 是 "Abs(" 不是 'h'！
 *      所以这里用 `\xNN` 写机器真有的字符：0x31='1' 0xA7='+' 0x32='2' 0xA6='=' 0x33='3'。
 */
#include "rstdio.h"
#include "rstdlib.h"

void main(void) {
    rscreen_on();                        /* 开显示（不开的话屏幕不会有任何输出） */
    rclear();                            /* 清屏幕缓冲区 */
    rprint_at(30, 20, "\x31\xA7\x32\xA6\x33");   /* "1+2=3"（机器字库编码） */
    rrefresh();                          /* 刷到显存 */
    while (1) {
    }
}

