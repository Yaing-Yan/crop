#!/usr/bin/env python3
"""nX-U16 disassembler (Casio ClassWiz / CY-239F "ePS-16" core).

Usage:
  python3 nxu16_dis.py dump  <rom.bin> [start] [end] [--csr 0] > out.asm
  python3 nxu16_dis.py diff  <rom.bin> <reference.lst>
  python3 nxu16_dis.py word  <hexword> [--csr 0] [--ext hexword]

Linear-sweep disassembly with correct instruction lengths (2 or 4 bytes).
The `diff` mode compares against CasioEmu's own export and reports only real
mismatches (the reference disassembler does not know SWI/B ERn/BL ERn/RTICE/
ICESWI and a few NOP aliases - words we decode but it prints as
"Unrecognized command" are counted as reference gaps, not errors).
"""
import sys

try:
    from .isa import ENTRIES, KIND, F_EXT, F_SEXT, F_DIR
except ImportError:  # 允许作为脚本直接运行
    from isa import ENTRIES, KIND, F_EXT, F_SEXT, F_DIR

# entry index -> dispatch (first match wins, in table order)
def _build_dispatch():
    d = [None] * 65536
    for en in ENTRIES:
        i, base, mask = en[0], en[2], en[3]
        for v in range(65536):
            if (v & ~mask) == base and d[v] is None:
                d[v] = i
    return d

DISPATCH = _build_dispatch()

IDX = {en[0]: en for en in ENTRIES}

CONDS = ['GE', 'LT', 'GT', 'LE', 'GES', 'LTS', 'GTS', 'LES',
         'NE', 'EQ', 'NV', 'OV', 'PS', 'NS', 'AL', '<Unrecognized>']

# ALU mnemonic overrides (handler says SUB/SUBC, encoding says CMP/CMPC)
ALU_NAME = {0: 'ADD', 1: 'ADD', 4: 'ADDC', 5: 'ADDC', 6: 'AND', 7: 'AND',
            8: 'CMP', 9: 'CMP', 10: 'CMPC', 11: 'CMPC', 12: 'MOV16X', 13: 'MOV16X',
            14: 'MOV', 15: 'MOV', 16: 'OR', 17: 'OR', 18: 'XOR', 19: 'XOR',
            20: 'CMP16X', 21: 'SUB', 22: 'SUBC'}

def h2(v):  return '%02Xh' % v
def h2s(v):
    v = sext(v, 6)
    return ('-%02Xh' % -v) if v < 0 else ('%02Xh' % v)
def h3(v):  return '%03Xh' % v
def h4s(v):
    v &= 0xffff
    return ('-%04Xh' % ((-v) & 0xffff)) if v & 0x8000 else ('%04Xh' % v)
def h5(v):  return '%05Xh' % (v & 0xfffff)
def dec(v): return str(v)

def size_letter(cnt):
    return {1: 'R', 2: 'ER', 4: 'XR', 8: 'QR'}.get(cnt, 'R%d?' % cnt)

def sext(v, bits):
    v &= (1 << bits) - 1
    return v - (1 << bits) if v & (1 << (bits - 1)) else v

def entry_of(word):
    i = DISPATCH[word]
    return None if i is None else IDX[i]

def instr_size(word):
    en = entry_of(word)
    return 0 if en is None else (4 if en[10] & F_EXT else 2)

def fmt(entry, word, ext, csr=0, pc_after=0):
    """Return (mnemonic, operands, size, comment-info dict)."""
    i, handler, base, mask, av, ash, acnt, bv, bsh, bcnt, flags = entry
    a = (word >> ash) & av
    b = (word >> bsh) & bv
    ext = ext or 0
    kind = KIND[i]

    def RA(n):  return 'R%d' % n
    def RAB(n): return size_letter(acnt and acnt or 1) + ('%d' % (n & ~1) if acnt > 1 else '%d' % n)

    if kind == 'ALU8':
        name = ALU_NAME[i]
        dst = RA(a)
        src = ('R%d' % b) if bcnt == 1 else ('#%s' % dec(sext(b, 7) if flags & F_SEXT else b))
        return name, '%s, %s' % (dst, src), 2, None

    if kind == 'ALU16':
        name = {12: 'MOV', 13: 'MOV', 2: 'ADD', 3: 'ADD', 20: 'CMP'}[i]
        dst = 'ER%d' % (a & 0xe)
        if bcnt == 2:
            return name, '%s, ER%d' % (dst, b & 0xe), 2, None
        if name == 'ADD':   # ADD16 imm7 is displayed sign-extended
            v = sext(b, 7)
            return name, '%s, #%s' % (dst, dec(v)), 2, None
        return name, '%s, #%s' % (dst, dec(b)), 2, None

    if handler == 'OP_MUL':
        return 'MUL', 'ER%d, R%d' % (a & 0xe, b), 2, None
    if handler == 'OP_DIV':
        return 'DIV', 'ER%d, R%d' % (a & 0xe, b), 2, None
    if handler == 'OP_EXTBW':
        return 'EXTBW', 'ER%d' % ((word >> 4) & 0xe), 2, None

    if handler in ('OP_SLL', 'OP_SRL', 'OP_SRA', 'OP_SLLC', 'OP_SRLC'):
        name = handler[3:]
        if bcnt == 1:  # shift count in a register
            return name, '%s, R%d' % (RA(a), b), 2, None
        return name, '%s, #%s' % (RA(a), dec(b)), 2, None

    if handler in ('OP_DAA', 'OP_DAS', 'OP_NEG'):
        return handler[3:], RA(a), 2, None

    if kind == 'LS':
        # store flag: bit0 of the opcode for the 0x90xx/0xa0xx families,
        # bit7 for the 0xb0xx/0xd0xx BP/FP families
        store = bool(word & 0x80) if base >= 0xb000 else bool(word & 1)
        name = 'ST' if store else 'L'
        regs = size_letter(flags >> 8) + '%d' % a
        inc = '+' if (flags & 0x10 and handler == 'OP_LS_EA') else ''
        if handler == 'OP_LS_EA':
            addr = '[EA%s]' % inc
        elif handler == 'OP_LS_R':
            addr = '[ER%d]' % b
        elif handler == 'OP_LS_I_R':
            addr = '%s[ER%d]' % (h4s(ext), b)
        elif handler == 'OP_LS_BP':
            addr = '%s[BP]' % h2s(b)
        elif handler == 'OP_LS_FP':
            addr = '%s[FP]' % h2s(b)
        elif handler == 'OP_LS_I':
            addr = h5(ext)
        else:
            addr = '?'
        return name, '%s, %s' % (regs, addr), 4 if flags & F_EXT else 2, None

    if kind == 'LEA':
        if base == 0xf00a:
            return 'LEA', '[ER%d]' % (b & 0xe), 2, None
        if base == 0xf00b:
            return 'LEA', '%s[ER%d]' % (h4s(ext), b & 0xe), 4, None
        return 'LEA', h5(ext), 4, None

    if handler == 'OP_ADDSP':
        v = sext(a, 8)
        return 'ADD', 'SP, #%s' % (('-%02Xh' % -v) if v < 0 else h2(v)), 2, None

    if kind == 'PUSH':
        return 'PUSH', size_letter(bcnt) + ('%d' % (b & ~1) if bcnt > 1 else '%d' % b), 2, None
    if kind == 'POP':
        return 'POP', size_letter(acnt) + ('%d' % (a & ~1) if acnt > 1 else '%d' % a), 2, None
    if kind == 'PUSHL':
        n = b
        parts = []
        if n & 8: parts.append('LR')
        if n & 4: parts.append('EPSW')
        if n & 2: parts.append('ELR')
        if n & 1: parts.append('EA')
        return 'PUSH', ', '.join(parts) if parts else '???', 2, None
    if kind == 'POPL':
        n = a
        parts = []
        if n & 8: parts.append('LR')
        if n & 4: parts.append('PSW')
        if n & 2: parts.append('PC')
        if n & 1: parts.append('EA')
        return 'POP', ', '.join(parts) if parts else '???', 2, None

    if kind == 'CTRL':
        names = {70: ('MOV ECSR, R%d', 'b1'), 71: ('MOV ELR, ER%d', 'b2'),
                 72: ('MOV EPSW, R%d', 'b1'), 73: ('MOV ER%d, ELR', 'a2'),
                 74: ('MOV ER%d, SP', 'a2'), 75: ('MOV PSW, ER%d', 'b2'),
                 76: ('MOV PSW, #%s', 'bimm'), 77: ('MOV R%d, ECSR', 'a1'),
                 78: ('MOV R%d, EPSW', 'a1'), 79: ('MOV R%d, PSW', 'a1'),
                 80: ('MOV SP, ER%d', 'b2')}
        f, mode = names[i]
        if mode == 'b1':   arg = b
        elif mode == 'b2': arg = b & 0xe
        elif mode == 'a1': arg = a
        elif mode == 'a2': arg = a & 0xe
        else:              arg = dec(b)
        m, _, o = f.partition(' ')
        return m, o % arg, 2, None

    if kind == 'CR_R':
        if base == 0xa00e:
            return 'MOV', 'CR%d, R%d' % (a, b), 2, None
        return 'MOV', 'R%d, CR%d' % (a, b), 2, None

    if kind == 'CR_EA':
        n = b
        pre = {0x100: 'CR', 0x200: 'CER', 0x400: 'CXR', 0x800: 'CQR'}[flags & 0xf00]
        reg = pre + str(n)
        ea = '[EA+]' if word & 0x10 else '[EA]'
        if flags & F_DIR:
            return 'MOV', '%s, %s' % (ea, reg), 2, None
        return 'MOV', '%s, %s' % (reg, ea), 2, None

    if kind == 'BIT':
        op = {0: 'SB', 1: 'TB', 2: 'RB'}.get(word & 3, 'BIT?%d' % (word & 3))
        if flags & F_EXT:
            return op, '%s.%d' % (h5(ext), b), 4, None
        return op, 'R%d.%d' % (a, b), 2, None

    if kind == 'DSR':
        if base == 0xe300:
            return 'DSR<-', h3(a), 2, None
        if base == 0x900f:
            return 'DSR<-', 'R%d' % a, 2, None
        return 'DSR<-', 'DSR', 2, None

    if handler == 'OP_PSW_OR':
        return {0x08: 'EI', 0x80: 'SC'}.get(base & 0xff, 'PSW.OR'), '', 2, None  # EI/SC: psw |= word&0xff
    if handler == 'OP_PSW_AND':
        return {0xf7: 'DI', 0x7f: 'RC'}.get(base & 0xff, 'PSW.AND'), '', 2, None
    if handler == 'OP_CPLC':
        return 'CPLC', '', 2, None

    if kind == 'BC':
        disp = sext(a, 8)
        target = pc_after + disp * 2
        return 'BC', '%s, %s' % (CONDS[(word >> 8) & 0xf], h5(target)), 2, ('branch', target)

    if kind == 'B':
        if base == 0xf000:
            return 'B', '%s:%s' % (h2(b), h5(ext)), 4, ('jump', (b << 16) | ext)
        return 'B', 'ER%d' % ((word >> 4) & 0xe), 2, ('jump', None)

    if kind == 'BL':
        if base == 0xf001:
            return 'BL', '%s:%s' % (h2(b), h5(ext)), 4, ('call', (b << 16) | ext)
        return 'BL', 'ER%d' % ((word >> 4) & 0xe), 2, ('call', None)

    if handler == 'OP_SWI':
        return 'SWI', '#%s' % dec(a), 2, None
    if handler == 'OP_RT':
        return 'RT', '', 2, ('ret', None)
    if handler == 'OP_RTI':
        return 'RTI', '', 2, None
    if handler == 'OP_RTICE':
        return 'RTICE', '', 2, None
    if handler == 'OP_ICESWI':
        return 'ICESWI', '', 2, None
    if handler == 'OP_BRK':
        return 'BRK', '', 2, None
    if handler == 'OP_NOP':
        return 'NOP', '', 2, None
    if handler == 'OP_INC_EA':
        return 'INC', '[EA]', 2, None
    if handler == 'OP_DEC_EA':
        return 'DEC', '[EA]', 2, None

    return handler, '0x%04x' % word, 2, None


def disasm_one(rom, addr, csr=0):
    """Disassemble one instruction at rom offset addr. Returns text or None."""
    if addr + 2 > len(rom):
        return None
    w = rom[addr] | (rom[addr + 1] << 8)
    en = entry_of(w)
    if en is None:
        return None
    ext = 0
    if en[10] & F_EXT and addr + 4 <= len(rom):
        ext = rom[addr + 2] | (rom[addr + 3] << 8)
    size = 4 if en[10] & F_EXT else 2
    m, ops, _, _ = fmt(en, w, ext, csr, addr + size)
    byts = ' '.join('%02X' % b for b in rom[addr:addr + size])
    return '%06X   %-11s %-8s %s' % (addr, byts, m, ops)


def dump(rom, start=0, end=None, csr=0, out=sys.stdout):
    end = len(rom) if end is None else min(end, len(rom))
    a = start
    while a < end:
        w = rom[a] | (rom[a + 1] << 8) if a + 2 <= len(rom) else 0
        en = entry_of(w)
        if en is None:
            byts = ' '.join('%02X' % b for b in rom[a:a + 2])
            out.write('%06X   %-11s %-8s %s\n' % (a, byts, 'DCW', '0x%04x' % w))
            a += 2
            continue
        text = disasm_one(rom, a, csr)
        out.write(text + '\n')
        a += instr_size(w)


def diff(rom, ref_path):
    import re
    from collections import Counter
    pat = re.compile(r'^([0-9A-F]{6})\s+((?:[0-9A-F]{2}[ ]?)+)[ ]{2,}(.+?)\s*$')
    bad = []
    stats = Counter()
    csr = 0
    for line in open(ref_path):
        m = pat.match(line)
        if not m:
            continue
        addr = int(m.group(1), 16)
        rest = m.group(3)
        rest = rest.replace('Wrong format - ', '')
        p = rest.split(' ', 1)
        rmn = p[0]
        rops = p[1].strip() if len(p) > 1 else ''
        if addr + 2 > len(rom):
            continue
        w = rom[addr] | (rom[addr + 1] << 8)
        en = entry_of(w)
        if en is None:
            if rmn == 'Unrecognized':
                stats['unrec'] += 1
            elif rmn in ('EXTBW', 'BC'):
                stats['ref-extra(%s on word outside CPU table)' % rmn] += 1
            else:
                stats['ERR:we-unrec-ref-decodes'] += 1
                bad.append((addr, w, 'DCW', rmn + ' ' + rops))
            continue
        ext = 0
        if en[10] & F_EXT and addr + 4 <= len(rom):
            ext = rom[addr + 2] | (rom[addr + 3] << 8)
        size = 4 if en[10] & F_EXT else 2
        m2, ops2, _, _ = fmt(en, w, ext, csr, addr + size)
        if (en[1], en[2]) in (('OP_B', 0xf000), ('OP_BLE', 0xf001)):
            csr = (w >> 8) & 0xf
        # reference prints B/BL ERn only when bits 11-8 are zero; and does not
        # know SWI/RTICE/ICESWI/some NOPs at all
        en_abs_b = (en[1], en[2]) in (('OP_B', 0xf000), ('OP_BL', 0xf001))
        ref_ignorant = (rmn == 'Unrecognized' and ((m2, ops2) in (
            ('B', 'ER%d' % ((w >> 4) & 0xe)), ('BL', 'ER%d' % ((w >> 4) & 0xe)),
            ('SWI', '#%d' % (w & 0xff)), ('RTICE', ''), ('ICESWI', ''), ('NOP', ''))
            or (en_abs_b and (w & 0xf0) != 0)))
        if ref_ignorant:
            stats['ref-gap(we decode, ref does not)'] += 1
            continue
        if rmn == 'Unrecognized':
            stats['ERR:ref-unrec-we-decode'] += 1
            bad.append((addr, w, m2 + ' ' + ops2, 'Unrecognized'))
            continue
        if (m2, ops2) != (rmn, rops):
            stats['ERR:text-mismatch'] += 1
            bad.append((addr, w, m2 + ' ' + ops2, rmn + ' ' + rops))
        else:
            stats['match'] += 1
    return stats, bad


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    if cmd == 'dump':
        rom = open(sys.argv[2], 'rb').read()
        start = int(sys.argv[3], 0) if len(sys.argv) > 3 and not sys.argv[3].startswith('-') else 0
        end = int(sys.argv[4], 0) if len(sys.argv) > 4 else None
        dump(rom, start, end)
    elif cmd == 'diff':
        rom = open(sys.argv[2], 'rb').read()
        stats, bad = diff(rom, sys.argv[3])
        total = sum(stats.values())
        print('compared %d lines' % total)
        for k, v in sorted(stats.items()):
            print('  %-40s %d' % (k, v))
        for addr, w, ours, ref in bad[:30]:
            print('  @%06X %04X  ours=%-28s ref=%s' % (addr, w, ours, ref))
        if len(bad) > 30:
            print('  ... %d more' % (len(bad) - 30))
    elif cmd == 'word':
        w = int(sys.argv[2].replace('h', ''), 16)
        ext = int(sys.argv[4].replace('h', ''), 16) if len(sys.argv) > 4 else 0
        en = entry_of(w)
        if en is None:
            print('unrecognized')
        else:
            m, ops, size, _ = fmt(en, w, ext, 0, 0)
            print('%04X: %s %s   (size=%d, entry=%d %s)' % (w, m, ops, size, en[0], en[1]))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
