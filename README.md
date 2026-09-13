# CROP — C → ROP for the CASIO fx-991 CN X

**C** to **R**eturn-**O**riented **P**rogramming: write C, get a ROP chain that runs on a
CASIO fx-991 CN X (nX-U16 / ePS-16, CY-239F).

```
main.c ─[rgcc]─► main.bin ─[crop-rop]─► Rop.bin ─► calculator
  C subset      nX-U16 code   gadget blocks   4-byte slots
```

* **No hard-coded addresses.** Everything (gadgets, pivots, ROM routines, the launcher) is
  derived from the ROM passed in with `--rom-dir`; the same source adapts to another Ver.
* **No guessing.** Anything the compiler cannot translate is a clear error, never wrong code.

## Usage

```bash
# 1. point at the model directory (kept out of the repo)
export CROP_ROM_DIR=/path/to/<model dir>          # or write it into .crop-local.conf

# 2. compile C → nX-U16 code → ROP chain
tools/rgcc --rom-dir $CROP_ROM_DIR -I include --data-base D700 --left-base EC00 \
           examples/hello.c -o out/hello.bin --asm \
           --rop out/hello-Rop.bin --dsl out/hello.rop

# 3. inject: chain → 0xEC00, launcher → 0xD248, ledger 0xD244 = 07, then press [→] [=]
tools/crop-verify --rom-dir $CROP_ROM_DIR --bin out/hello.bin \
                  --data-base D700 --expect D137=0E --screen DDD4 E3D4
tools/crop-verify ... --dry-run          # print the injection plan only, touch nothing
```

`tools/` also ships `crop-gadgets` (scan/report gadgets), `crop-rop` (translate an existing
`.bin`), `crop-labels` (resolve ROM routine addresses from `labels.conf`), `crop-eval`
(matching-rate stats), `crop-plan`, `crop-portability`, plus `mcp-call` / `xinput` helpers for
driving the emulator. Run any of them with `--help`.

## Headers (`include/`)

| Header | Contents |
|---|---|
| `rstdio.h` | screen output: `rprint(font, row, text)`, `rrefresh()`, `rclear()`, plus `SCREEN_BUF` and the `FONT_*` constants |
| `rstring.h` | constant-length copies/fills (`rcopy4/8`, `rfill4/8`, `rput16`) |
| `rstdlib.h` | `rhalt()`, `rspin64()`（ROM 的延时/屏幕例程待真机确证后再放进来） |

`romlabels_ver*.h` are **generated** per ROM by `tools/crop-labels` — never edited by hand.

## Progress

| Area | State |
|---|---|
| C → nX-U16 `.bin` → ROP chain pipeline, both ROM Vers | ✅ done, verified on the emulator |
| `#include` / `#define` preprocessor | ✅ done |
| Calling ROM routines from C (rt-fix, auto-discovered) | ✅ done (`rprint` / `rrefresh` / `rclear`) |
| Variables, constant expressions, `while(1)`, block writes | ✅ done |
| Functions (inlined), arrays (constant index), structs, compile-time pointers | ✅ done |
| Constant-trip `for` loops (compile-time unrolled) → `rstring.h` helpers | ✅ done |
| Runtime pointers (address held in a variable), conditional `if` / `while(cond)` | ❌ not yet — the ROM lacks the primitive this needs (`docs/step-7-A7条件分支调研.md`) |


Measurements, injection procedure and on-device records live in `docs/`
(`step-1 … step-7`, `注入规程.md`, `任务清单.md`).

## Verification

```bash
python3 -m unittest discover -s tests     # 70 tests; ROM-dependent ones skip when unset
export CROP_ROM_DIR=...                   # enables the ones that need a real ROM
```

Chains are executed in a lifted emulator of the real ROM and the RAM end-state is checked;
`examples/hello.c` and `examples/features.c` were additionally run on CasioEmuMsvc +
McpPlugin, reading the screen buffer and display memory back out of the machine.

## License

GPL-3.0-or-later. The nX-U16 ISA tables and disassembler are reused from a local decompiler fork.
