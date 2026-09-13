# CROP — Writing ROP in C

> License: **GPL-3.0-or-later** · targets the **CASIO fx-991 CN X** (nX-U16)

> **C** to **R**eturn-**O**riented **P**rogramming — compile and translate C programs into
> **ROP chains** that execute on the **CASIO fx-991 CN X** (nX-U16 / "ePS-16" core, CY-239F).

```
  main.c ──[rgcc]──► main.bin ──[crop-rop]──► Rop.bin ──► real hardware / emulator
    C source       restricted C subset    nX-U16 machine    block match + escape   4-byte-slot ROP chain
                        compiler              code
                          ▲                          ▲
                          └─── ROM.bin (passed with -r) ───┘   ← no address is hard-coded
```

* **Zero hard-coding**: the model, ROM layout, gadgets, primitives and launcher are all derived
  by scanning the ROM passed in with `-r ROM.bin`. Changing the Ver of the same model adapts
  automatically — regression-verified against 4 ROMs.
* **No guessing**: any instruction or syntax that cannot be translated raises an explicit error
  with the reason; wrong code is never emitted.
* **Cross-checkable**: the chain encoding is byte-for-byte identical to an on-device-verified
  RopIDE artifact; internally the interpreter runs two paths — "construct the bytes directly"
  and "generate DSL then recompile" — and reports an error immediately if they disagree.

---

## ✅ Achieved: dual-version on-device closed loop

The same C program and the same 44-byte chain run on both **VerC / VerF**, each deriving its own
launcher, verified on device:

| Version | ROM dir | launcher (writes `0xD248`) | Result |
|---|---|---|---|
| **VerC** | `models/fx991cnxVirtual` | `FD 24 F0 EB 7B 23 42` (pivot `0x2237A`) | `0xD710..D715 = 11 45 14 19 19 81` ✅ |
| **VerF** | `fx991cnxfVirtual` | `FD 24 F0 EB 8F 23 42` (pivot `0x2238E`) | same ✅, and `PC=0x10742` (**lands exactly on the chain's looping gadget**) |

```c
/* six.c —— write 11 45 14 19 19 81 at 0xD710 */
unsigned char b0; unsigned char b1; unsigned char b2;
unsigned char b3; unsigned char b4; unsigned char b5;
void main(void) {
    b0 = 0x11; b1 = 0x45; b2 = 0x14;
    b3 = 0x19; b4 = 0x19; b5 = 0x81;
    while (1) { }
}
```

```
$ tools/rgcc --rom-dir <model dir> six.c --data-base D710 --left-base EC00 \
        -o six.bin --rop six.Rop.bin --dsl six.rop
.bin: 28 bytes   chain total 44 bytes   0 unsupported, 0 warnings

0xD710..D717 = 11 45 14 19 19 81 00 00     ← measured on device (CasioEmuMsvc)
SP = 0xEC26 (inside the chain)  PC = 0x10742 (chain's looping gadget)
```

---

## How it works: why "reusing bytes already in the ROM" can execute

The nX-U16's `POP PC` pops **PC (2 bytes) + CSR (2 bytes)** from the stack, and only the low 4 bits
of CSR are used. The sequence of addresses on the stack therefore *is* the program:

> **If a byte segment `B` of the target program is byte-for-byte identical at ROM address `A`,
> and the instruction right after `A+|B|` is exactly `POP PC`, then "jump to `A`" and "execute
> `B` in place and then return to the chain" are semantically equivalent.**

The interpreter chops the `.bin` into the largest such blocks (**L1 block reuse**); a single
instruction with no ready-made block takes an **L2 equivalent escape**; control flow uses an
**L3 stack pivot**. The "arbitrary constants" a `POP <reg>` needs are supplied as **inline data**
on the chain.

### Physical format of a ROP chain

```
slot = 4 bytes: [PC_lo][PC_hi][CSR][pad]          POP PC consumes 4 bytes
two gadget-address encodings (identical to RopIDE):
  right form  #name;   → h1h2 + ("0"+addr[0]) + "00"
  left form   #-name;  → h1h2 + ("3"+addr[0]) + "30"   (historical quirk: low byte 00→01)
```

---

## Repository layout

```
crop/
├── crop/
│   ├── nxu16/        nX-U16 ISA + disassembler (reused from ~/nxu16-decompiler,
│   │   │             cross-checked against the emulator's disassembly)
│   │   └── decode.py structured decoder/classifier (the 4-byte-slot and related
│   │                 conclusions live here)
│   ├── rom.py        ROM image: multiple files → 20-bit code space (via model.lua's rom_path)
│   ├── gadget.py     gadget scanner (every offset, including odd addresses) + longest-match splitting
│   ├── chain.py      ROP chain encoding (slots/values/anchors/forward-reference backpatching)
│   ├── ropdsl.py     RopIDE `.rop` DSL compiler (output opens directly in the IDE)
│   ├── vocab.py      ROP vocabulary mining (what instruction set this machine can act as)
│   ├── planner.py    route A: semantic gadget planner (harmless side effects allowed)
│   ├── interp.py     interpreter: .bin → chain (L1/L2/L3 + per-block verification + DSL cross-check)
│   ├── rgcc.py       rGCC: restricted C subset → nX-U16 .bin
│   ├── labels.py     label table: ROM routine names resolved per Ver by instruction signature
│   ├── routines.py   routine-body extraction + rt-fix primitive discovery
│   ├── libabi.py     library ABI: marshal C arguments into r0/r1/er2
│   ├── preproc.py    minimal C preprocessor (#include / #define / include guards)
│   ├── launcher.py   launcher byte generation (derived from the scanned pivot)
│   └── ropvm.py      ROP harness (runs the generated chain on a lifted emulator)
├── tools/            crop-gadgets / crop-rop / crop-eval / crop-plan / crop-portability /
│                     crop-labels / crop-verify / rgcc / mcp-call / xinput
├── include/          rstdio.h + generated romlabels_ver*.h
├── examples/         hello.c
├── docs/             reports and procedures (step-1..6 reports, injection procedure,
│                     left/right-split design)
├── tests/            59 self-tests (including cross-4-Ver portability regression)
├── out/              example artifacts and measured data
├── labels.conf       ROM routine labels (reference address + instruction signature)
└── launcher.conf     launcher / chain-placement configuration
```

---

## Documentation index

| Document | Contents |
|---|---|
| [`docs/step-1-报告.md`](docs/step-1-报告.md) | Step 1: ROM image + ISA decode + gadget scan + feasibility evaluation |
| [`docs/step-2-报告.md`](docs/step-2-报告.md) | Step 2: ROP interpreter (chain encoding + three-layer translation + coverage data) |
| [`docs/step-3-报告.md`](docs/step-3-报告.md) | Step 3: rGCC v0 (restricted C subset → 100%-translatable `.bin`) |
| [`docs/step-4-报告.md`](docs/step-4-报告.md) | Step 4: ROP harness and end-to-end execution verification on the real ROM |
| [`docs/step-5-设计-左右分离.md`](docs/step-5-设计-左右分离.md) | Step 5 design: left/right split (program storage area ↔ runtime area) |
| [`docs/step-6-报告-A1-库例程调用与头文件.md`](docs/step-6-报告-A1-库例程调用与头文件.md) | Step 6: library routine calls (A1) + headers (B1) + preprocessor (A6) |
| [`docs/注入规程.md`](docs/注入规程.md) | Injection procedure (device-verified) |
| [`docs/任务清单.md`](docs/任务清单.md) | Task list / persistent development plan |

---

## Installation and dependencies

* Python **3.9+**, **no third-party dependencies** (standard library + `urllib`).
* A model directory (containing `rom.bin` and `model.lua`):
  ```bash
  MODEL=~/casioemu/models/fx991cnxfVirtual        # VerF
  MODEL=~/casioemu/models/models/fx991cnxVirtual  # VerC
  ```
* Optional: `~/nxu16-decompiler/rom991cnx_lifted.py` (used by the ROP harness),
  CasioEmuMsvc + McpPlugin (for on-device injection, MCP port 3001).

```bash
git clone https://github.com/Yaing-Yan/crop && cd crop
python3 -m unittest discover -s tests -v        # 59 tests
```

---

## Quick start

```bash
# ① See which primitives the ROM offers
python3 tools/crop-gadgets --rom-dir $MODEL

# ② Write C
cat > six.c <<'EOF'
unsigned char b0; unsigned char b1; unsigned char b2;
unsigned char b3; unsigned char b4; unsigned char b5;
void main(void) {
    b0 = 0x11; b1 = 0x45; b2 = 0x14;
    b3 = 0x19; b4 = 0x19; b5 = 0x81;
    while (1) { }
}
EOF

# ③ One command: C → .bin → Rop.bin (+ a .rop that RopIDE can open)
python3 tools/rgcc --rom-dir $MODEL six.c --data-base D710 --left-base EC00 \
        -o six.bin --rop six.Rop.bin --dsl six.rop --asm

# ④ Or call ROM routines from C (step 6): clear → print → refresh
tools/rgcc --rom-dir <model dir> -I include --data-base D700 \
        examples/hello.c -o out/hello.bin --asm --rop out/hello-Rop.bin
```

---

## CLI

### `tools/crop-gadgets` — scan the ROM and build a gadget index

| Option | Default | Description |
|---|---|---|
| `--rom-dir DIR` / `-r FILE` (repeatable) | — | model directory (via `model.lua`'s `rom_path`) or a ROM file directly |
| `--all-roms` | off | also concatenate `rom2.bin`/`rom3.bin`/… into consecutive pages |
| `--max-insns N` | 8 | max instructions before `POP PC` inside a gadget |
| `--rt` | off | additionally index gadgets ending in `RT` (needs hardware return-stack support) |
| `--demo N` / `--demo-functions` | 0 / off | feasibility evaluation (random code / real function bodies) |
| `--demo-max-insns N` / `--seed N` | — | sampling controls for `--demo` |
| `--json OUT` | — | export the `{HEX:[addr…]}` index |

### `tools/crop-rop` — interpreter: `.bin` → `Rop.bin`

| Option | Default | Description |
|---|---|---|
| `-r FILE` (repeatable) / `--rom-dir DIR` | — | ROM file(s) or model directory |
| `-i FILE` / `-o FILE` / `--dsl FILE` | — | input `.bin` / output chain / also export a RopIDE `.rop` |
| `--max-insns N` | 8 | L1 block length limit |
| `--form {right,left}` | right | gadget slot encoding form |
| `--pivot-side {left,right}` | left | which side's base in-chain jump addresses use |
| `--vocab` / `--dump` | off | print the vocabulary / print the generated DSL |

### `tools/crop-eval` — measure translation coverage on real ROM function bodies

```bash
$ tools/crop-eval --rom-dir $MODEL --limit 150
Total 24350 instructions: L1 block reuse 2687 (11.0%), L2 equivalent escape 603 (2.5%),
jumps 0, unsupported 21006 (86.3%)
Most frequently untranslatable: MOV R1,#0 ×348 | L ER0,-0040h[ER14] ×280 |
MOV ER0,ER14 ×204 | PUSH LR ×202
```
> Reading note: a large part of that 86.3% is **stack / frame-pointer / external-call** code,
> which rGCC is required never to generate in the first place.

### `tools/rgcc` — restricted C subset → a `.bin` that is 100% translatable

| Option | Default | Description |
|---|---|---|
| `source` (positional) | required | C source file |
| `--rom-dir DIR` | required | take the ROM for the vocabulary (every primitive is derived from it) |
| `-o` / `--output FILE` | — | output `.bin` |
| `--asm` | off | print the assembly listing |
| `--data-base ADDR` | D180 | start of the variable area (hex) |
| `--left-base ADDR` | E000 | **where the chain is stored** (in-chain absolute addresses are recomputed accordingly) |
| `--rop` / `--dsl` | — | output the chain / export a RopIDE project |
| `-I DIR` / `--include DIR` | — | header search directory (`#include`), repeatable |
| `--labels FILE` | `labels.conf` | label table (routine entries resolved per Ver by signature) |
| `--no-lib` | off | do not use the library table (calling library functions then fails) |
| `--max-insns N` | 8 | L1 block length limit |

### `tools/crop-labels` — resolve / validate / generate the ROM label table (no hard-coded addresses)

```bash
tools/crop-labels --rom-dir $MODEL                       # print the resolution result
tools/crop-labels --rom-dir $MODEL --header include/romlabels.h --ver verf
```

### `tools/crop-verify` — one-command on-device verification over MCP

```bash
tools/crop-verify --rom-dir $MODEL --bin out/hello.bin --data-base D700 \
                  --expect D137=0E --screen DDD4 E3D4
```
`--dry-run` only prints the injection plan without touching the machine. One run performs the
whole injection procedure: press AC → clear `0xD180` → write the chain to `0xEC00` → write the
launcher to `0xD248` → write the ledger `0xD244=07` → long-press 【→】 and 【=】 → read back and
decode the screen buffer into a bitmap.

### `tools/crop-plan` — route-A capability measurement and byte-transfer synthesis material

```bash
tools/crop-plan --rom-dir $MODEL
tools/crop-plan --rom-dir $MODEL --targets "ADD R0, #1" "OR R0, R1"
```

### `tools/crop-portability` — cross-version portability audit

```bash
tools/crop-portability $MODEL ~/casioemu/models/fx991cnxVirtual \
                      ~/casioemu/models/models/fx991cnx \
                      ~/casioemu/models/models/fx991cnxVirtual
```

### `tools/mcp-call` — CasioEmuMsvc MCP debug interface

```bash
tools/mcp-call tools                                       # list available tools
tools/mcp-call call read_memory '{"address":"0xD710","size":8}'
tools/mcp-call call write_memory '{"address":"0xEC00","bytes":[66,7,1,0]}'
tools/mcp-call call keyboard_code '{"code":0x37,"pressed":true}'   # press Right (hold ≥0.9s)
```

### `tools/xinput` — XTest input for X11 (XWayland fallback)

```bash
tools/xinput info                      # show the X11 root size / find the CasioEmuMsvc window
tools/xinput click CasioEmuMsvc 94 257 # click at window-relative coordinates
tools/xinput key CasioEmuMsvc Return   # send a key to the window
```

---

## Calling ROM routines from C (step 6)

### `include/rstdio.h` — screen output (rGCC-specific, no libc)

```c
#include "rstdio.h"
unsigned char row;
void main(void) {
    row = 0;
    rclear();                                  /* 0x7F6C: zero 1536 bytes (ends in RT → rt-fix added) */
    rprint(FONT_NORMAL, row, "HELLO CROP");    /* 0x221BE: R1 comes from a variable */
    rrefresh();                                /* 0x08772: 0xDDD4 → 0xF800 + commit */
    while (1) { }
}
```

* `SCREEN_BUF 0xDDD4`, `SCREEN_BYTES 0x0600`, `FONT_NORMAL/SMALL/TABLE = 0x0E/0x0A/0x08`,
  `SCREEN_H 64`;
* the three prototypes live in the header (supported directly by A6's `#include`), and
  **not a single address is hard-coded in the C source**.

### How the call works: "C wrapper + compiler marshals the parameters"

* The C level only writes **variables and function parameters**:
  `void rprint(unsigned char font, unsigned char row, const unsigned char *text);`
* At compile time the routine entry comes from the **label table** (`labels.conf`, resolved per Ver
  by instruction signature); the routine's machine code is **read out of that ROM** and written
  into the `.bin` (so the `.bin` is still a semantically complete nX-U16 program).
* The interpreter recognizes that byte sequence inside the `.bin` ⇒ it emits **one chain slot**
  (routines ending in `RT` first get an rt-fix slot).
* "Move the arguments into r0/r1/er2" is the compiler's job.

Argument-marshalling primitives (all derived from the ROM):

| C side | Generated machine code |
|---|---|
| constant → `R0` (and the next argument is `R1`) | `POP ER0` + 2 bytes on the chain (there is no `POP R1` in the ROM) |
| constant → `ER2` | `POP ER2` + 2 bytes on the chain |
| variable → `R0` | `POP ER12` + base, then `L R0, 00h[BP]` |
| variable → `R1` | `POP ER12` + base, then `L R1, 14h[BP]` |
| variable → `ER2` | **not yet supported** (the ROM has no inlinable "BP-relative load of 16 bits into ER2" slot; see A4) |

String constants: the compiler places them in a read-only area after the variable area
(`data_base + variable count + 8`), writes them there with a block-write gadget at program start,
then loads the address into `ER2`.

### The three routines (VerF disassembly conclusions)

| Label | Entry (VerF) | Ends in | Semantics |
|---|---|---|---|
| `print-line` | `0x221BE` | `POP PC` | `R0` = font size (0x0E/0x0A/0x08, also used as the starting x pixel), `R1` = vertical pixel (renderer has `CMP R1,#64`, i.e. 0..63), `ER2` = NUL-terminated string address |
| `refresh` | `0x08772` | `POP PC` | copy `0xDDD4..0xE3D3` (1536 bytes) to display memory `0xF800`, then commit once via `BL 0A1DEh` |
| `clear` | `0x07F6C` | `RT` | zero `0xDDD4..0xE3D3` (192 × 8 = 1536 bytes) |

The renderer has **two drawing buffer pages**, selected by `[0xD139]`:

| `[0xD139]` | Drawing buffer | Relation to `rrefresh` (0x8772) |
|---|---|---|
| `= 0` (initial value after machine reset) | `0xDDD4 ~ 0xE3D3` | **0x8772 is exactly what flushes this range ⇒ the default path is self-consistent** |
| `≠ 0` | `0xE3D4 ~ 0xE9D3` | the twin routine `0x8764` in the ROM flushes this range |

(Measured with the lifted emulator: with `[0xD139]=0`, `rprint` writes its dot matrix to `0xDDD4`;
setting `[0xD139]=1` makes it write `0xE3D4` instead. On real hardware `[0xD139]` is decided by the
system UI, so both pages still need a look via `tools/crop-verify --screen DDD4 E3D4`.)

### rt-fix: calling routines that end in `RT`

`crop/routines.py` scans the ROM and automatically discovers the **rt-fix primitive**, whose shape is:

```
A:      BL  T          ; T holds a POP PC (push the return address into the hardware return stack,
A + 4:  BC  AL, U      ; U also holds a POP PC   then take a chain slot; after the routine's RT
                       ;                         returns, continue eating the chain from here)
```

Measured on VerF: `A = 0x2BAD4` (`BL 02h:09696h`, `T = 0x29696` is exactly `POP PC`),
`U = 0x2BA7E` is also `POP PC`. On VerC, `A = 0x2B948` (`BL 0FBFEh`).
**Both versions are discoverable automatically; there is no bare address in the code.**

Calling an `RT`-terminated routine (e.g. `clear`) is therefore **two ordinary slots** on the chain:

```
[rt-fix slot]        →  BL/POP PC eats the next slot → continue
[routine entry slot] →  routine runs, RT → 0x2BAD8(BC AL) → 0x2BA7E(POP PC) → continue
```

Measured offline trajectory (the `clear` run, with SP changes):

```
step  22: 2BAD4 sp=EC54   ← rt-fix
step  23: 29696 sp=EC54   ← POP PC eats the next slot
step  24: 07F6C sp=EC58   ← clear entry
step  25: 07F7C sp=EC50   ← after PUSH QR8, into the zeroing loop
step 216: 07F82 sp=EC50   ← loop finished (192 iterations)
step 217: 2BAD8 sp=EC58   ← RT returns to the trampoline
step 218: 2BA7E sp=EC58   ← POP PC
step 219: 1769C sp=EC5C   ← next chain slot (rprint's argument marshalling)
```

**The chain thus remains a uniform sequence of 4-byte slots**, and the interpreter needs no special
layout for rt-fix.

### Minimal C preprocessor (A6, `crop/preproc.py`)

`#include "x.h"` / `<x.h>` (searched via `-I` directories, each file expanded only once),
`#define NAME value` (object-like macros), `#ifndef/#ifdef/#else/#endif` (enough to write include
guards), `#pragma once`; any other directive (`#error`, `#undef`, …) is an **explicit error**,
never silently ignored.

---

## Bootstrapping / injection procedure (device-verified)

> Full version: [`docs/注入规程.md`](docs/注入规程.md). This section is what we settled on after
> tripping over a great many pitfalls.

| Address | Role | Key point |
|---|---|---|
| `0xEC00` | **chain storage (left address)** | must avoid the machine's own stack: measured stack at `0xE9xx~0xEBxx`; placing the chain at `0xE9E0` gets periodically trampled by pushes |
| `0xD248` | **playback area = input buffer (source)** | the launcher is injected here; injecting at `0xD180` gets overwritten by pressing Right |
| `0xD180` | **input area (destination)** | pressing 【→】 imports `0xD248` into it |
| `0xD244..0xD247` | **length/cursor ledger** | writing the content without the length means pressing Right imports nothing (it clears `D180`) |

```python
# injection (MCP)
write_memory 0xEC00 ← 44-byte chain
write_memory 0xD248 ← launcher: FD 24 <left_addr-10h, little-endian> <this Ver's pivot encoding>
                       VerC: FD 24 F0 EB 7B 23 42   (pivot 0x2237A)
                       VerF: FD 24 F0 EB 8F 23 42   (pivot 0x2238E)
write_memory 0xD244 ← 07 07 07 07
write_memory 0xD180 ← all 0 (clear)
# trigger: press 【→】 then 【=】
```

`launcher_bytes(left, skip, pivot)` generates these 7 bytes from the pivot (no address is
hard-coded); `launcher_for_db` derives that pivot by scanning the ROM and prefers the
device-verified shape (`MOV SP, ER14` + skipping ≥16 bytes). Changing `--left-base` moves the
chain and the launcher follows automatically.

---

## Design notes

1. **`POP PC` is a 4-byte slot.** In the lifted emulator `pop8()` was written as `SP += 1`, a typo —
   walking the chain in 3-byte steps derails at step 2, while 4-byte steps are perfectly correct
   (guarded by a control test).
2. **The `.bin` inline-data convention**: `POP <reg>` is immediately followed by the bytes it is to
   pop off, and the interpreter moves them into the chain — this is the only source of
   "arbitrary constants".
3. **Block-write downgrade** (squeezes the chain from 82 bytes down to 44): use the ROM's existing
   `LEA [ER14] ; ST QR0,[EA+] ; ST ER8,[EA+]`, which writes 8+2 bytes per slot.
   ⚠️ The entry must be the `LEA [ERn]` instruction (`0x17DE8`), **not** the outer gadget's start
   (`0x17DE2`) — the preceding `L QR0,[EA+]` clobbers the freshly loaded data with a garbage EA
   (measured on device).
4. **Every primitive is derived from the ROM**: `POP XRn/QRn`, `ST Rn+2,[ERn]`, a variable slot
   with both `L` and `ST`, base loads `POP ER12/ER14`, the block-write gadget, the launcher
   pivot — if any one of them cannot be derived, it is an explicit error, never a fallback to a
   wrong constant.

---

## Measured data (fx991cnxfVirtual)

```
ROM byte coverage: 253026/262144 bytes decode as instructions
Control flow: POP PC=765  RT=299  BC=13490  B=4235  BL=8412
Inlinable blocks: 720 kinds / 2174 instances; stack pivots 135
Across 4 Vers: common blocks 671 kinds / union 819 kinds = 81.9%; common blocks
cover 94.6%~97.4% of each version's gadgets
```

---

## Supported C subset (and why)

| Capability | Status | Notes |
|---|---|---|
| `unsigned char` global variables, constant assignment | ✅ | placed at the address given by `--data-base` |
| Constant expressions (`2*(3+4)`, `(1<<5)\|3`, `%`, `~`) | ✅ | evaluated at compile time |
| Variable copy `x = y;` | ✅ | BP switch + `L`/`ST` slot (the only slot in the whole ROM that has both) |
| `while (1) { … }` | ✅ | back-jump via stack pivot; verified on device, running in the loop |
| Runtime arithmetic `x = x + 1` / `x = y * z` | ❌ | **this ROM has no usable general ALU gadget** (clean ALU only has 48 fixed register/immediate combinations) |
| `if` / `while(cond)` | ❌ | no gadget anywhere in the ROM that conditionally skips one chain slot (routes A/B still to do) |
| Pointers (constant address / pointer-dereference assignment) | ⚠️ partial | `*(u8*)ADDR = v` already works; runtime pointers need byte-transfer synthesis |
| Headers (`#include` / `#define` / include guards) | ✅ | minimal preprocessor (A6, `crop/preproc.py`); other directives are explicit errors |
| Calling ROM library routines (`rprint`/`rrefresh`/`rclear`) | ✅ offline | prototypes in `include/rstdio.h`; C passes variables/parameters and the compiler marshals them into r0/r1/er2 (semantics of `rclear` proven in the lifted emulator, and the whole `hello.c` program proven on the real emulator by reading back RAM: font global, screen buffer bitmap and display-memory copy) |
| User-defined functions, arrays, structs | ❌ | still to do (A2/A3/A5) |

> In other words: **rGCC's boundary is "what can be translated 100%"**, not "supports full C".
> After compilation every block is checked against "this byte sequence exists in the ROM and is
> immediately followed by `POP PC`"; if not, it raises `RgccError`.

---

## Verification & tests

```bash
python3 -m unittest discover -s tests -v      # 59 tests
```

| Test | What it asserts |
|---|---|
| `test_real_pixel_editor_first_64_bytes` | our DSL compiler's output is **byte-for-byte identical in the first 64 bytes** to an on-device-verified `.rop` |
| `test_every_indexed_gadget_is_byte_exact_and_terminated` | every `(block, address)` in the index matches byte-for-byte and is immediately followed by `8E F2` |
| `test_pop_pc_slot_is_4_bytes` / `test_pop_pc_is_four_bytes` | the 4-byte slot model (including a 3-byte control experiment that must fail) |
| `test_launcher_follows_the_derivation_rule` | every launcher field is determined by that ROM's pivot; **different Vers must derive different values** |
| `test_backend_derives_everything` | the backend is zero-hard-coded; the derived `ST`/`L` really exist in that ROM |
| `test_straight_line_program_writes_expected_ram` | after execution on the real-ROM emulator, RAM equals the C semantics |
| `test_all_labels_resolve_in_every_version` | all labels resolve in every Ver; the addresses differ (proving they are derived, not constants) |
| `test_routine_body_matches_rom_bytes` | each routine body (entry → terminator) is byte-for-byte identical to the ROM |
| `test_rt_push_is_discovered` | the rt-fix primitive is found by scanning, and its `BL` target really is a `POP PC` |
| `test_terminators_match_reality` | `print-line` and `refresh` end in `POP PC`, `clear` ends in `RT` |
| `test_marshal_constants` / `test_marshal_variable_uses_bp_slot` | argument marshalling (constants/variables) produces the expected machine-code shape |
| `test_variable_to_er2_is_refused` | variable → `ER2` fails loudly instead of emitting wrong code |
| `test_include_define_and_guard` / `test_unknown_directive_is_an_error` | the preprocessor expands includes/defines, honors include guards, rejects unknown directives |
| `test_end_to_end_compile_and_translate` | `#include "rstdio.h"` → rGCC → interpreter yields `unsupported == 0` with routine and rt-fix slots in the chain |
| `test_verc_also_resolves` | VerC also resolves all three routines and finds an rt-fix primitive |
| `test_compile_and_translate_on_every_version` | the same C program compiles and translates on every available Ver |
| `test_verified_golden_bytes` / `test_config_roundtrip` | the launcher bytes and `launcher.conf` match the on-device-verified values |

### Honest verification status (step 6)

| Item | Means | Conclusion |
|---|---|---|
| Routine body extraction (entry → terminator) | byte-for-byte comparison with the ROM bytes | ✅ both Vers pass |
| rt-fix primitive discovery | found by scanning in both Vers, and the target really is `POP PC` | ✅ |
| Argument marshalling (constant/variable) | unit tests check the generated machine-code shape | ✅ |
| Whole-program translation | `unsupported == 0`, DSL and bytes cross-checked | ✅ 128-byte chain, 16 blocks, 4 routine calls |
| **`rclear` semantics** | lifted emulator on the real ROM: write `FF AA AA AA 55 55` → call `rclear` → read back `00 00 00 00 00 00`, chain halts cleanly | ✅ **semantically proven** (also proves the rt-fix loop is correct) |
| `rprint` / `rrefresh` pixel level | the lifted emulator treats `0xF000+` as an I/O window (`memwr` is stubbed), so the commit routine spins; deep inside the renderer it also runs off the rails (a lifted-emulator fidelity issue) | ⚠️ not provable *there* — proven on the real emulator instead (see below) |
| **On-device end-to-end (step 6)** | CasioEmuMsvc + McpPlugin, `examples/hello.c` → 128-byte chain injected at `0xEC00`, keys 【→】【=】 | ✅ **works**: `D137 == 0x0E`; `0xDDD4` holds the rendered "HELLO CROP" bitmap (8 % of bytes non-zero) read back *from the machine*; `0xF800` (display memory) holds the copy at the 32-byte stride (`F800[32:56] == DDD4[24:48]`); the uploaded chain reads back byte-identical |
| Pixel-level *on the emulator LCD* | MCP `request_screenshot` after the run shows the last frame the OS drew (its input line), not our text | ⚠️ expected: the chain runs `while(1)` in the main thread, so the ROM's display task — the thing that pushes `0xF800` to the LCD controller — never runs again. Memory-level read-back is the authoritative check; take over the display task if you want the LCD itself |

> **Practical note (how to get MCP at all):** the McpPlugin is only loaded once a model is
> running. Start the emulator with the model directory as a positional argument —
> `cd ~/casioemu && ./CasioEmuMsvc models/fx991cnxfVirtual` — then `127.0.0.1:3001` comes up.
> Starting it bare leaves you on the model-selection UI with no MCP server at all.

`tools/crop-verify` performs the whole injection procedure in one command (AC → clear `0xD180` →
write the chain to `0xEC00` → write the launcher to `0xD248` → write the ledger `0xD244=07` →
long-press 【→】【=】 → read back and decode the screen buffer into a bitmap):

```bash
tools/rgcc --rom-dir ~/casioemu/models/fx991cnxfVirtual -I include --data-base D700 \
           examples/hello.c -o out/hello.bin --rop out/hello-Rop.bin
tools/crop-verify --rom-dir ~/casioemu/models/fx991cnxfVirtual --bin out/hello.bin \
                  --data-base D700 --expect D137=0E --screen DDD4 E3D4
```

(`--dry-run` only prints the injection plan without touching the machine; drop it once MCP is up.)

---

## Pitfalls (all fixed in code/process)

| # | Pitfall | Symptom | Fix |
|---|---|---|---|
| 1 | launcher injected at the destination `0xD180` | pressing Right clears it; `=` runs the old ledger | inject into the source area `0xD248` |
| 2 | writing the content but not the length ledger | pressing Right imports nothing (`D180` all zero) | `0xD244..247 = 07` |
| 3 | wrong block-write gadget entry | `D710` becomes `C4 9C 00 70 E5 C8 00 7A` (deterministic garbage) | enter from `LEA [ER14]` |
| 4 | chain placed at `0xE9E0` | periodically trampled by the machine's stack (from byte 10 on it becomes `38 07 01 00`) | place it at `0xEC00` |
| 5 | MCP key press too short (0.08 s) | the keyboard scan simply misses it | hold for ≥0.9 s |
| 6 | injecting while the machine is stuck in an old ROP | the injected bytes are trampled within milliseconds | reset first (only `PC=0x91AA / SP=0xEE34` counts as clean) |

---

## Roadmap

- [x] **Step 1** ROM image + ISA decode + gadget scan + feasibility evaluation
- [x] **Step 2** chain encoding (byte-for-byte against RopIDE ground truth) + ROP vocabulary + interpreter L1/L2/L3
- [x] **Step 3** rGCC v0 (constants + expressions + variable copy + infinite loop) + end-to-end pipeline
- [x] **Step 4** ROP harness, on-device bootstrapping working, dual-version (VerC/VerF) on-device closed loop
- [ ] **Step 5** left/right split (program storage area ↔ runtime area, see `docs/step-5-设计-左右分离.md`)
- [x] **Step 6** library routine calls (A1) + headers (B1) + minimal preprocessor (A6); on-device verification tool is ready (see `docs/step-6-报告-A1-库例程调用与头文件.md`)
- [ ] **Step 7** conditional branches (jump table + 0/1-indexed pivot) → unlocks `if` / `while(cond)`
- [ ] **A2/A3/A5** user-defined functions (single-level inlining first), constant-index arrays, structs by constant offset
- [ ] **A4** byte-transfer synthesis (load a 16-bit address from a variable into `ER2`), which unlocks pointer variables for `rprint`
- [ ] **B2/B3** `rstring.h` (`rmemcpy` 0x0875C, `rstrcpy`, …) and `rstdlib.h` (`rsleep`, …), one signature at a time in `labels.conf`
- [x] **D1** README in English (this document)

---

## License and acknowledgements

* Project code: **GPL-3.0-or-later** (see `LICENSE`) — kept consistent with the RopIDE family of
  tools (also GPL-3.0).
* `crop/nxu16/isa.py` and `crop/nxu16/disasm.py` are reused from `nxu16-decompiler`
  (auto-generated from CasioEmuMsvc's `casioemu::CPU::opcode_sources`, and already cross-checked
  line-by-line against the emulator's own disassembly `_disas.txt`).
* The chain encoding rules and the `.rop` format align with
  [ropide-vscode-plugin](https://github.com/Yaing-Yan/ropide-vscode-plugin) and the RopIDE family;
  on-device injection uses **CasioEmuMsvc + McpPlugin**.
* Thanks to the Casio ClassWiz ROP community (wlyibo / Goodolls / fx-es(ms) and others) for the
  publicly shared gadgets and tutorials; they are important references for cross-checking and
  verification.
