# CROP — write C for the CASIO fx-991CN X

CROP compiles a small C subset to real nX-U16 machine code and then turns that code
into a **ROP chain** built entirely out of fragments of the calculator's own ROM.
No custom firmware, no hardcoded ROM addresses: everything is discovered by scanning
the ROM you point it at.

```
C source ──rGCC──► .bin (real nX-U16 code) ──interpreter──► ROP chain ──► calculator
```

## How it works

**1. The compiler only emits translatable code.**
`rGCC` compiles C to a genuine nX-U16 program, then verifies every instruction block can
be found in the ROM as a *gadget* (a byte-identical instruction sequence immediately
followed by `POP PC`). Anything it cannot translate is a compile error, never silent
breakage.

**2. The interpreter turns code into data.**
The derived ROM is cut at `POP PC`: each maximal matching block becomes **one 4-byte
chain slot** (`[PC_lo][PC_hi][CSR][pad]`). A block that does not exist in the ROM is
handled by an equivalent escape (for example `MOV Rn,#imm` → `POP Rn` plus inline data).
Jumps inside the program become **stack pivots** (`MOV SP,ERn`).

Because `POP PC` also advances the stack pointer, the chain pointer *is* the program
counter: the program is never executed as code — ROM fragments are glued together by SP,
and constants, strings and data ride **on the chain itself**.

**3. ROM routines become library functions.**
A routine's machine code is copied into the `.bin`; the interpreter emits exactly one
slot for it, and the compiler marshals arguments into `r0`/`r1`/`er2`. Routines that end
in `RT` (instead of `POP PC`) are handled by an automatically discovered **rt-fix**
trampoline, so they can return into the chain.

**4. Launcher.**
Seven bytes plus a length ledger are written into the input/replay area. They pivot SP
to the chain (for example `0xEC00`); the chain itself is just RAM content.

**5. Nothing about the ROM is hardcoded.**
Gadgets, pivots, block-write primitives and the rt-fix trampoline are **scanned** from the
model you supply (`--rom-dir`, or `crop/models.py`). Semantic routines (`print`, `refresh`,
`clear`, `screen-on`, …) are resolved by **instruction signature** from `labels.conf`, so
the same source rebuilds for other ROM versions (VerF/VerC) without edits.
Only *machine facts* are fixed, and they live in the headers: screen buffer `0xDDD4`,
VRAM `0xF800`, 192×64 pixels, font codes `0x0E/0x0A/0x08`, and the `r0 = font, r1 = row,
er2 = text` entry contract.

## Usage

```bash
# compile + translate to a ROP chain
tools/rgcc --rom-dir /path/to/model \
           -I include --data-base EA40 --left-base EC00 \
           examples/hello_world.c -o out/hello_world.bin --rop out/hello_world-Rop.bin

# run the test suite
python3 -m unittest discover -s tests
```

`tools/rgcc` flags: `-I <dir>` include path · `--labels labels.conf` routine table ·
`--data-base <hex>` where variables/initialisers live · `--left-base <hex>` where the
chain is written · `--max-insns <n>` gadget block limit · `--asm` listing ·
`--carrier` inline strings on the chain (experimental) · `--no-lib` compile only.

### Headers

| header | contents |
|---|---|
| `rstdio.h` | `rprint(font,row,text)`, `rprint_at(x,y,text)`, `rrefresh()`, `rclear()`, `FONT_NORMAL/SMALL/TABLE`, `SCREEN_BUF` |
| `rstdlib.h` | `rscreen_on()` (must be called first, or nothing appears), `rhalt()` (freeze), `rexit()` (return to the OS idle loop), `rint_off()` / `rint_on()` |
| `rstring.h` | small pure-C string helpers (`rcopy4/8`, `rfill4/8`, `rput16`) |
| `rchars.h` / `rchars2.h` | generated machine character sets (level-1 = input, level-2 = display; letters/digits match ASCII in level 2) |

### Running it on a calculator (or emulator)

1. Write your program with `rGCC`; you get a `.bin` and a ROP chain (`.bin` bytes).
2. Place the chain at `--left-base` (default `0xEC00`; keep clear of `0xE9E0` and below).
3. Write these three things into RAM:
   * the launcher (`FD 24 <base-0x10> <pivot+1> 0x40|csr`) at `0xD248`,
   * the length ledger `07 07 07 07` at `0xD244` (without it the import does not happen),
   * the chain itself at `--left-base`.
4. Press 【→】 then 【=】. The ledger/import machinery feeds the launcher to the OS, SP
   pivots into your chain, and the program runs.
5. End programs with `rhalt()` (deterministic freeze — same trick as hand-written ROP's
   jump past the ROM) or `rexit()` (hand control back to the OS).

Tested on a real fx-991CN X (VerF/VerC) and in CasioEmuMsvc: text is drawn into the
screen buffer and mirrored to VRAM, and `rhalt()` leaves the machine frozen with the
picture on screen.

## Limits

The C subset is deliberately strict — unsupported constructs are rejected with a clear
message instead of generating wrong code:

* compile-time constant expressions only (no runtime arithmetic on values);
* `if` / `while(cond)` are not wired yet (the ROM primitives exist: `er0>er2?` for
  materialising a comparison, `jump-e14` for pivoting);
* `for` loops must have a **compile-time constant** trip count (they are unrolled);
* pointer parameters must be bound to compile-time known addresses (no runtime indexing yet).

## License

GPL-3.0-or-later.
