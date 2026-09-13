/* hello.c —— CROP 最小示例：清屏 → 打印一行 → 刷新
 *
 * 编译（VerF）：
 *   tools/rgcc --rom-dir ~/casioemu/models/fx991cnxfVirtual \
 *              -I include --data-base D700 \
 *              examples/hello.c -o out/hello.bin --asm \
 *              --rop out/hello-Rop.bin --dsl out/hello.rop
 */
#include "rstdio.h"

unsigned char row;

void main(void) {
    row = 0;
    rclear();
    rprint(FONT_NORMAL, row, "HELLO CROP");
    rrefresh();
    while (1) {
    }
}
