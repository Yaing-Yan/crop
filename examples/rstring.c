/* rstring.c —— rstring.h / rstdlib.h 的用法示例
 *
 *   tools/rgcc --rom-dir $CROP_ROM_DIR -I include --data-base D700 --left-base EC00 \
 *              examples/rstring.c -o out/rstring.bin --asm \
 *              --rop out/rstring-Rop.bin
 *
 * 做的事：清零 msg → 搬 "CROP" 进去 → 覆写前两字节为 'A','B' → 打印并刷新。
 * 期望：msg = 41 42 4F 50 00 00 00 00（lo=65, hi=66），屏幕上出现 "ABOP"。
 */
#include "rstdio.h"
#include "rstring.h"
#include "rstdlib.h"

unsigned char msg[8];
unsigned char hi;
unsigned char lo;

void main(void) {
    rfill8(msg, 0);               /* 先清零：字符串要有 0 结尾 */
    rcopy4(msg, "CROP");          /* 前四个字节 ← "CROP" */
    rput16(msg, 65, 66);          /* msg[0..1] = 'A','B' ⇒ 屏幕上打成 "ABOP" */
    lo = msg[0];
    hi = msg[1];
    rclear();
    rprint(FONT_NORMAL, 0, msg);
    rrefresh();
    rhalt();
}
