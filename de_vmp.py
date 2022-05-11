import os
import sys

import capstone
import capstone as cs
import capstone.x86 as cs_x86
import lief

from entities import VMState, VMHandler
from architecture import VMPInstruction
from subroutines import VMEntryParser, VMHandlerParser
from utils import imatch, InstructionCollection, Mod2NInt, get_shared_md

vmp_bin_file_path = os.path.join(os.path.dirname(__file__), "vmtest.vmp.exe")


class VMP:

    def __init__(self, file_path):
        self.file_path = file_path
        self.binary = lief.PE.parse(file_path)
        self.sections = list(self.binary.sections)

    def _find_section(self, rva):
        sec = self.binary.section_from_rva(rva)
        off = self.binary.rva_to_offset(rva)
        assert sec and "Section not found"
        return sec

    def _is_vm_entry(self, rva):
        sec = self._find_section(rva)
        off = rva - sec.virtual_address
        # ----------------------------------------------------
        # push imm
        # call imm
        # ----------------------------------------------------
        return sec.content[off] == 0x68 and sec.content[off + 5] == 0xE8

    def _find_vm_entries(self):
        vm_entries = []
        md = get_shared_md()
        for sec in self.sections:
            if sec.name != ".text":
                continue

            rva = sec.virtual_address
            off = 0

            while off < sec.virtual_size:
                inst = next(md.disasm(sec.content[off:off + 15].tobytes(), rva))
                if imatch(inst, cs_x86.X86_INS_JMP, cs.CS_OP_IMM):
                    if inst.operands[0].type == cs.CS_OP_IMM:
                        jmp_rva = inst.operands[0].imm
                        if self._is_vm_entry(jmp_rva):
                            print(f"Found VMEntry at 0x{jmp_rva:x}")
                            vm_entries.append(jmp_rva)
                off += inst.size
                rva += inst.size
        return vm_entries

    def _deobfuscate(self, vm_handler_rva, rip_rva=None, debug=False):
        md = get_shared_md()
        sec = self._find_section(vm_handler_rva)

        insts = []
        off = vm_handler_rva - sec.virtual_address

        if rip_rva is None:
            rip_rva = vm_handler_rva
        while rip_rva < sec.virtual_address + sec.virtual_size:
            off2 = min(off + 15, sec.offset + sec.size)
            inst = next(md.disasm(sec.content[off:off2].tobytes(), rip_rva))

            if imatch(inst, cs_x86.X86_INS_JMP, cs.CS_OP_IMM) \
                    or imatch(inst, cs_x86.X86_INS_CALL, cs.CS_OP_IMM):
                rip_rva = inst.operands[0].imm
                off = rip_rva - sec.virtual_address
            elif inst.id in [cs_x86.X86_INS_JMP, cs_x86.X86_INS_RET]:
                break
            else:
                if inst.id != cs_x86.X86_INS_NOP:
                    insts.append(inst)
                off += inst.size
                rip_rva += inst.size

        if debug:
            for idx in range(len(insts)):
                inst = insts[idx]
                print(f"[{idx:04d}] 0x{inst.address:08x} {inst.mnemonic} {inst.op_str}")

        return InstructionCollection(insts)

    def _unroll(self, state: VMState, handler_rva: int):
        while True:
            ic = self._deobfuscate(handler_rva, debug=False)
            handler = VMHandlerParser.parse(state, ic)
            print(f"Unroll 0x{handler.rva:x}, next: 0x{handler.next_rva:x}")
            for param in handler.parameters:
                print(param)
            if handler.next_rva != VMHandler.INVALID_RVA:
                handler_rva = handler.next_rva
            else:
                break

    def _parse_vm_entry(self, vm_entry_rva):
        print(f"Processing VMEntry at 0x{vm_entry_rva:x}")
        ic = self._deobfuscate(vm_entry_rva)

        state, first_handler_rva = VMEntryParser.parse(self.binary, ic)
        self._unroll(state, first_handler_rva)

        # vmp_handler_rva = 0x6377d
        # state.vip_rva = 5766
        # state.rolling_key = 0xf6b39

        # vmp_handler_rva = 0xafaba
        # state.vip_rva = 0x5972
        # state.rolling_key = 0xfffffffffffe3ae2

        # self._unroll(state, vmp_handler_rva)

    def process(self):
        vm_entries = self._find_vm_entries()
        for vm_entry_rva in vm_entries:
            self._parse_vm_entry(vm_entry_rva)


VMP(vmp_bin_file_path).process()