/* 由 tools/crop-labels 生成 —— 请勿手改 */
#ifndef CROP_ROMLABELS_H
#define CROP_ROMLABELS_H

#define ROM_VER "verc"
#define ROM_PRINT_LINE     0x221B2u   /* ST R0, 0D137h ; BL * ; POP PC */
#define ROM_REFRESH        0x08706u   /* PUSH XR4 ; PUSH QR8 ; MOV R0, #212 ; MOV R1, #221 */
#define ROM_CLEAR          0x07F00u   /* PUSH QR8 ; MOV ER8, #0 ; MOV ER10, #0 ; MOV ER12, #0 */

#endif /* CROP_ROMLABELS_H */
