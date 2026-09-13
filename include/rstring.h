/* rstring.h —— CROP 的字符串/内存小工具（rGCC 专用；不含 libc）
 *
 * ROP 链里**没有条件分支**（见 docs/step-7-…），所以这里没有"运行时变长"的循环。
 * 固定长度的搬运/填充用 `for` 写：rGCC 在编译期把固定次数的循环**展开**成直线代码
 * （循环变量按常量代入 ⇒ 数组下标是常量，正好是这台机器能做的事）。
 *
 * 指针形参在编译期做常量传播：实参给数组名或字符串常量时，函数体里的地址直接就是常量。
 *
 * 用法：
 *     #include "rstring.h"
 *     unsigned char msg[8];
 *     void main(void) { rcopy8(msg, "HI"); rfill4(msg, 32); while (1) {} }
 */

#ifndef CROP_RSTRING_H
#define CROP_RSTRING_H

/*: 把 s 的前 8 个字节搬到 d */
void rcopy8(unsigned char *d, const unsigned char *s) {
    for (unsigned char i = 0; i < 8; i = i + 1) {
        d[i] = s[i];
    }
}

/*: 把 s 的前 4 个字节搬到 d */
void rcopy4(unsigned char *d, const unsigned char *s) {
    for (unsigned char i = 0; i < 4; i = i + 1) {
        d[i] = s[i];
    }
}

/*: 把 d 的前 4 个字节填成 v */
void rfill4(unsigned char *d, unsigned char v) {
    for (unsigned char i = 0; i < 4; i = i + 1) {
        d[i] = v;
    }
}

/*: 把 d 的前 8 个字节填成 v */
void rfill8(unsigned char *d, unsigned char v) {
    for (unsigned char i = 0; i < 8; i = i + 1) {
        d[i] = v;
    }
}

/*: 写 16 位小端值 */
void rput16(unsigned char *d, unsigned char lo, unsigned char hi) {
    d[0] = lo;
    d[1] = hi;
}

#endif /* CROP_RSTRING_H */
