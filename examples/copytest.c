/* copytest.c —— 真机上的"最小搬运探针"（用来判定变量读写这条路是否可用）
 *
 *   a = 7;   常量写（POP XR0 + ST R2,[ER0]）
 *   b = a;   运行时字节搬运（BP 槽：POP ER12 + L/ST R7,-10h[BP]）—— 本项目最容易出错的一环
 *   c = 9;   再一个常量写
 *
 * 注入后手按 [→] [=]，读 D700..D702 应为 07 07 09（a / b / c 的地址由 --data-base 决定，
 * 本样例按 --data-base D700 编）。
 *
 * 实测（VerF，真机）：07 07 09 ✅ —— copy_var 在真机上成立。
 */
unsigned char a;
unsigned char b;
unsigned char c;

void main(void) {
    a = 7;
    b = a;
    c = 9;
    while (1) {
    }
}
