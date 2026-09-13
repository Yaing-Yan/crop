/* rstring.c —— rstring.h / rstdlib.h 的用法示例
 *
 *   tools/rgcc --rom-dir $CROP_ROM_DIR -I include --data-base D700 --left-base EC00 \
 *              examples/rstring.c -o out/rstring.bin --asm \
 *              --rop out/rstring-Rop.bin
 *
 * 做的事：把 "CROP" 搬进 msg、填 4 个空格、写一个 16 位值，然后打印并刷新。
 */
#include "rstdio.h"
#include "rstring.h"
#include "rstdlib.h"

unsigned char msg[8];
unsigned char hi;
unsigned char lo;

void main(void) {
    rcopy4(msg, "CROP");
    rfill4(msg, 32);              /* 后四个字节填成空格（rfill 的 4 展开） */
    rcopy4(msg, "CROP");          /* 再把前四个写回，演示两次搬运 */
    rput16(msg, 65, 66);          /* msg[0..1] = 'A','B' */
    lo = msg[0];
    hi = msg[1];
    rclear();
    rprint(FONT_NORMAL, 0, msg);
    rrefresh();
    rhalt();
}
