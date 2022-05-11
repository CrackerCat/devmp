import capstone as cs
import capstone.x86 as cs_x86

from utils import InstructionCollection, imatch, get_shared_md, get_shared_ks
from entities import VMEncryptedValue, VMState
from universal import X86Reg
import struct as st


class _DefUseChain:
    def __init__(self):
        self._uses = {}
        self._users = {}

    def add_use(self, i: int, use: int):
        if i not in self._uses:
            self._uses[i] = set()
        if use not in self._users:
            self._users[use] = set()

        self._uses[i].add(use)
        self._users[use].add(i)

    def remove_use(self, i: int, use: int):
        self._uses[i].remove(use)
        self._users[use].remove(i)

    def has_users(self, i: int):
        return i in self._users and len(self._users[i]) > 0

    def remove_all_uses(self, i: int):
        if i not in self._uses:
            return
        for use in self._uses[i]:
            self._users[use].remove(i)

        del self._uses[i]


class VMHandlerOptimizer:

    @classmethod
    def _asm_mov_reg_imm(cls, reg: X86Reg, imm: int):
        ks = get_shared_ks()
        code_bytes = bytes(ks.asm(f'mov {reg.name}, 0x{imm:x}')[0])
        return code_bytes

    @classmethod
    def _dead_code_elimination(cls, state: VMState, ic: InstructionCollection):
        o_ic = ic.duplicate()

        last_defs = {}
        def_use_chain = _DefUseChain()

        reversed_regs = set()
        reversed_regs.add(X86Reg.RSP)
        reversed_regs.add(state.vsp_reg)

        ja_idx = o_ic.next_index(0, cs_x86.X86_INS_JA)
        if ja_idx != -1:
            o_ic.resize(ja_idx)
            # print("Found JA")

        def _is_reversed_reg(r):
            return r != cs_x86.X86_REG_INVALID and X86Reg.from_capstone(r).extended in reversed_regs

        candidates = []
        for inst in o_ic:

            is_side_effect_inst = False
            if inst.id == cs_x86.X86_INS_PUSHFQ:
                is_side_effect_inst = True
            elif imatch(inst, cs_x86.X86_INS_LEA, cs_x86.X86_OP_REG, cs_x86.X86_OP_MEM) and \
                    inst.operands[1].mem.disp == -7 and \
                    inst.operands[1].men.scale == 1 and \
                    inst.operands[1].mem.base == cs_x86.X86_REG_RIP and \
                    inst.operands[1].men.index == cs_x86.X86_REG_INVALID:
                is_side_effect_inst = True
            else:
                for op in inst.operands:
                    if op.access & cs.CS_AC_WRITE:
                        if op.type == cs.CS_OP_REG:
                            if _is_reversed_reg(op.reg):
                                is_side_effect_inst = True
                        if op.type == cs.CS_OP_MEM:
                            if _is_reversed_reg(op.mem.base) or _is_reversed_reg(op.mem.index):
                                is_side_effect_inst = True
                        if is_side_effect_inst:
                            break

            if not is_side_effect_inst:
                candidates.append(inst)

            reg_uses, reg_defs = inst.regs_access()
            for reg in reg_uses:
                # TODO capstone_convertible
                if not X86Reg.capstone_convertible(reg):
                    continue
                u_reg = X86Reg.from_capstone(reg).extended
                if u_reg in last_defs:
                    def_i_address = last_defs[u_reg]
                    def_use_chain.add_use(inst, def_i_address)

            for reg in reg_defs:
                # TODO capstone_convertible
                if not X86Reg.capstone_convertible(reg):
                    continue
                d_reg = X86Reg.from_capstone(reg).extended
                last_defs[d_reg] = inst

        while True:
            dead_inst = None

            for inst in reversed(candidates):
                if not def_use_chain.has_users(inst):
                    dead_inst = inst
                    break
            if dead_inst:
                candidates.remove(dead_inst)
                o_ic.remove(dead_inst)
                def_use_chain.remove_all_uses(dead_inst)
            else:
                break

        print("reduced")
        for inst in o_ic:
            print(inst)

        print("After dead code elimination: ", len(o_ic), len(ic))
        return o_ic

    @classmethod
    def _lower_encryption_blocks(cls, state: VMState, values: [], ic: InstructionCollection):
        o_ic = ic.duplicate()
        md = get_shared_md()

        diff_sz = 0
        for value in values:  # type: VMEncryptedValue
            asm_code = cls._asm_mov_reg_imm(value.def_reg, value.decrypted_value)
            load_c_inst = next(md.disasm(asm_code, ic[value.blk_end - diff_sz].address))
            diff_sz += o_ic.replace_with(value.blk_start - diff_sz, value.blk_end - diff_sz, [load_c_inst])

        print("After lower encryption blocks: ", len(o_ic), len(ic))
        return o_ic

    @classmethod
    def process(cls, state: VMState, values: [], ic: InstructionCollection):
        o_ic = cls._lower_encryption_blocks(state, values, ic)
        o_ic = cls._dead_code_elimination(state, o_ic)
        return o_ic

        # reduced_insts = []
        # while inst_idx >= 0:
        #     inst = o_ic[inst_idx]
        #
        #     if inst.id in (cs_x86.X86_INS_CQO, cs_x86.X86_INS_CWD,
        #                    cs_x86.X86_INS_CBW, cs_x86.X86_INS_CWDE,
        #                    cs_x86.X86_INS_CDQ, cs_x86.X86_INS_CDQE,
        #                    cs_x86.X86_INS_LAHF, cs_x86.X86_INS_TEST,
        #                    cs_x86.X86_INS_CMP):
        #         inst_idx -= 1
        #         continue
        #
        #     if imatch(inst, cs_x86.X86_INS_LEA, cs_x86.X86_OP_REG, cs_x86.X86_OP_MEM) and \
        #             inst.operands[1].mem.disp == -7 and \
        #             inst.operands[1].scale == 1 and \
        #             inst.operands[1].mem.base == cs_x86.X86_REG_RIP and \
        #             inst.operands[1].mem.index == cs_x86.X86_REG_INVALID:
        #         reduced_insts.insert(0, inst)
        #         inst_idx -= 1
        #         continue
        #
        #     if inst.id == cs_x86.X86_INS_PUSHFQ:
        #         reduced_insts.insert(0, inst)
        #         inst_idx -= 1
        #         continue
        #
        #     reads = {}
        #     writes = {}
        #     mem_read = 0
        #     mem_write = 0
        #     for op in inst.operands:
        #         if op.type == cs_x86.X86_OP_REG:
        #             if op.access & cs.CS_AC_READ:
        #                 reads[X86Reg.from_capstone(op.reg).extended] = True
        #             if op.access & cs.CS_AC_WRITE:
        #                 writes[X86Reg.from_capstone(op.reg).extended] = True
        #         elif op.type == cs_x86.X86_OP_MEM:
        #             for reg in (op.mem.base, op.mem.index):
        #                 if reg == cs_x86.X86_REG_INVALID:
        #                     continue
        #                 reads[X86Reg.from_capstone(reg).extended] = True
        #             mem_read |= op.access & cs.CS_AC_READ
        #             mem_write |= op.access & cs.CS_AC_WRITE
        #
        #     reg_uses, reg_defs = inst.regs_access()
        #     for r in reg_uses:
        #         u_reg = X86Reg.from_capstone(r).extended
        #         if u_reg == X86Reg.EFLAGS or u_reg == X86Reg.RSP:
        #             continue
        #         if inst.id == cs_x86.X86_INS_CPUID and u_reg == X86Reg.RCX:
        #             continue
        #         reads[u_reg] = True
        #
        #     for r in reg_defs:
        #         u_reg = X86Reg.from_capstone(r).extended
        #         if r == cs_x86.X86_REG_EFLAGS or r == cs_x86.X86_REG_RSP:
        #             continue
        #         writes[u_reg] = True
        #
        #     should_be_tracked = mem_write
        #     for tr, tv in traced.items():
        #         if tr in writes:
        #             should_be_tracked |= True
        #
        #     if should_be_tracked:
        #         for wr, wv in writes.items():
        #             if wr not in traced:
        #                 traced[wr] = False
        #             traced[wr] &= not wv
        #
        #         for rd, rv in reads.items():
        #             if rd not in traced:
        #                 traced[rd] = False
        #             traced[rd] |= rv
        #
        #         reduced_insts.insert(0, inst)
        #
        #     inst_idx -= 1
        #
        # for i in reduced_insts:
        #     print(i)
        # return InstructionCollection(reduced_insts)