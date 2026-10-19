#!/usr/bin/env python3
"""Apply the PEBBLE (Xiaomi SM8950) machine-config edits to an xnu tree.

Runs after darwin-xnu-build has applied its own patch set, so it edits by
anchor string instead of by line context.  Safe to re-run: every edit checks
for its marker first.

Usage: apply_pebble_all.py /path/to/xnu
"""
import os
import re
import sys

XNU = sys.argv[1] if len(sys.argv) > 1 else "xnu"

PEBBLE_H = r"""/*
 * PEBBLE.h - board configuration for the Xiaomi SM8950 ("pebble").
 *
 * Gunyah runs at EL2, so XNU is a guest.  TrustZone owns GIC Group 0, so timer
 * and IPI interrupts arrive as Group 1 IRQs.  Oryon implements PAC but XNU's
 * arm64e ABI assumes Apple keys, so this config stays plain arm64.
 */
#ifndef _PEXPERT_ARM64_PEBBLE_H
#define _PEXPERT_ARM64_PEBBLE_H

/* the machine-config symbol every #if defined(PEBBLE) keys off */
#define PEBBLE           1

/* arm64 builds leave __ARM_ARCH__ undefined; cpuid.c keys off it */
#ifndef __ARM_ARCH__
#define __ARM_ARCH__     8
#endif

#define NO_MONITOR       1
#define NO_ECORE         1
#define NO_APPLEARMSMP   1

#define PAGE_SIZE_FIXED  1

#define HAS_GIC_V3       1

/* from /proc/iomem: 17000000-1700ffff GICD, 17080000-1727ffff GICR */
#define PEBBLE_GICD_BASE        0x17000000UL
#define PEBBLE_GICD_SIZE        0x00010000UL
#define PEBBLE_GICR_BASE        0x17080000UL
#define PEBBLE_GICR_SIZE        0x00200000UL
#define PEBBLE_GICR_PE_SIZE     0x00020000UL

#define PEBBLE_GICR_IGROUPR0    (0x10000 + 0x080)
#define PEBBLE_GICR_ISENABLER0  (0x10000 + 0x100)
#define PEBBLE_GICR_IPRIORITYR  (0x10000 + 0x400)
#define PEBBLE_GICR_IGRPMODR0   (0x10000 + 0xD00)

#define PEBBLE_GICD_CTLR_ENABLEGRP1 (1u << 1)
#define PEBBLE_GICD_CTLR_ARE_NS     (1u << 4)

#define PEBBLE_VTIMER_INTID    27
#define PEBBLE_PTIMER_INTID    (27 - 1)

#define PEBBLE_CACHELINE_SHIFT 6

#endif /* _PEXPERT_ARM64_PEBBLE_H */
"""

BOARD_BLOCK = """#ifdef ARM64_BOARD_CONFIG_PEBBLE
#include <pexpert/arm64/PEBBLE.h>

#define MAX_L2_CLINE   6
#define MAX_CPUS       8
#define MAX_CPU_CLUSTERS 1

#define CORE_NCTRS     8 /* placeholder; KPC is not enabled here */

#endif  /* ARM64_BOARD_CONFIG_PEBBLE */

"""

SLEH_PEBBLE = """#if defined(PEBBLE)
	extern void cpu_signal_handler(void);

	/*
	 * Qualcomm GICv3: TrustZone owns Group 0, so timer and IPI interrupts
	 * arrive as Group 1 IRQs and must be acknowledged here.
	 */
	uint64_t iar = __builtin_arm_rsr64("ICC_IAR1_EL1");
	uint32_t intid = (uint32_t)(iar & 0xFFFFFF);

	if (intid >= 1020 && intid <= 1023) {
		return;
	}
	if (intid == PEBBLE_VTIMER_INTID || intid == PEBBLE_PTIMER_INTID) {
		sleh_interrupt_handler_prologue(state, DBG_INTR_TYPE_TIMER);
		cdp->cpu_decrementer = -1;
		rtclock_intr(TRUE);
		__builtin_arm_wsr64("ICC_EOIR1_EL1", iar);
	} else if (intid < 16) {
		sleh_interrupt_handler_prologue(state, DBG_INTR_TYPE_IPI);
		__builtin_arm_wsr64("ICC_EOIR1_EL1", iar);
		__builtin_arm_isb(ISB_SY);
		cpu_signal_handler();
	} else {
		sleh_interrupt_handler_prologue(state, DBG_INTR_TYPE_OTHER);
		if (cdp->interrupt_handler) {
			cdp->interrupt_handler(cdp->interrupt_target,
			    cdp->interrupt_refCon, cdp->interrupt_nub, intid);
		}
		__builtin_arm_wsr64("ICC_EOIR1_EL1", iar);
	}
	__builtin_arm_isb(ISB_SY);
#else
	sleh_interrupt_handler_prologue(state, DBG_INTR_TYPE_OTHER);"""

FIQ_PEBBLE = """#elif defined(PEBBLE)
/*
 * SM8950: Qualcomm GICv3.  TrustZone owns Group 0, so XNU drives the
 * controller in Group 1 (dispatch side lives in sleh_irq).
 */
#define PEBBLE_GICR_TYPER            0x0008ULL
#define PEBBLE_GICR_WAKER            0x0014ULL
#define PEBBLE_GICR_PROCESSORSLEEP   (1u << 1)
#define PEBBLE_GICR_CHILDRENASLEEP   (1u << 2)
#define PEBBLE_GICR_TYPER_LAST       (1ULL << 4)

void
pe_init_fiq(void)
{
	vm_offset_t gicd = ml_io_map(PEBBLE_GICD_BASE, PEBBLE_GICD_SIZE);
	vm_offset_t gicr = ml_io_map(PEBBLE_GICR_BASE, PEBBLE_GICR_SIZE);
	vm_offset_t pe = 0;
	vm_offset_t pe_aff0 = 0;
	uint64_t mpidr = __builtin_arm_rsr64("MPIDR_EL1");
	uint32_t want = (uint32_t)mpidr & 0xFFFFu;

	if (!gicd || !gicr) {
		panic("%s: cannot map GIC regions", __func__);
	}

	for (vm_offset_t off = 0; off < PEBBLE_GICR_SIZE; off += PEBBLE_GICR_PE_SIZE) {
		uint64_t typer = *(volatile uint64_t *)(gicr + off + PEBBLE_GICR_TYPER);
		uint32_t aff = (uint32_t)(typer >> 32) & 0xFFFFu;

		if (aff == want) {
			pe = gicr + off;
			break;
		}
		if (!pe_aff0 && ((aff & 0xFFu) == (want & 0xFFu))) {
			pe_aff0 = gicr + off;
		}
		if (typer & PEBBLE_GICR_TYPER_LAST) {
			break;
		}
	}
	/*
	 * Measured on hardware: MPIDR reports Aff1 = 3 but the first
	 * redistributor reports affinity 0, so an exact match can fail.
	 * Degrade to Aff0-only, then to the first frame, instead of
	 * panicking during early boot.
	 */
	if (!pe) {
		pe = pe_aff0 ? pe_aff0 : gicr;
	}

	uint32_t waker = *(volatile uint32_t *)(pe + PEBBLE_GICR_WAKER);
	if (waker & PEBBLE_GICR_CHILDRENASLEEP) {
		waker &= ~PEBBLE_GICR_PROCESSORSLEEP;
		*(volatile uint32_t *)(pe + PEBBLE_GICR_WAKER) = waker;
		while (*(volatile uint32_t *)(pe + PEBBLE_GICR_WAKER) &
		    PEBBLE_GICR_CHILDRENASLEEP) {
			/* spin */
		}
	}

	*(volatile uint32_t *)(pe + PEBBLE_GICR_IGROUPR0) = 0xFFFFFFFFu;
	*(volatile uint32_t *)(pe + PEBBLE_GICR_IGRPMODR0) = 0x00000000u;
	*(volatile uint32_t *)(pe + PEBBLE_GICR_IPRIORITYR) = 0xA0A0A0A0u;

	*(volatile uint32_t *)(pe + PEBBLE_GICR_ISENABLER0) =
	    (1u << PEBBLE_VTIMER_INTID) | 0x0000FFFFu;

	uint64_t sre = __builtin_arm_rsr64("ICC_SRE_EL1");
	sre |= 1ULL;
	__builtin_arm_wsr64("ICC_SRE_EL1", sre);
	__builtin_arm_isb(0xF);

	__builtin_arm_wsr64("ICC_BPR1_EL1", 0);
	__builtin_arm_wsr64("ICC_PMR_EL1", 0xFF);

	uint64_t ctlr = __builtin_arm_rsr64("ICC_CTLR_EL1");
	ctlr &= ~(1ULL << 1);
	__builtin_arm_wsr64("ICC_CTLR_EL1", ctlr);

	uint32_t gicd_ctlr = *(volatile uint32_t *)gicd;
	gicd_ctlr |= PEBBLE_GICD_CTLR_ENABLEGRP1 | PEBBLE_GICD_CTLR_ARE_NS;
	*(volatile uint32_t *)gicd = gicd_ctlr;

	__builtin_arm_wsr64("ICC_IGRPEN1_EL1", 1);
	__builtin_arm_isb(0xF);
}
"""

report = []


def read(rel):
    p = os.path.join(XNU, rel)
    if not os.path.exists(p):
        report.append("MISSING  %s" % rel)
        return None
    return open(p, encoding="utf-8", errors="surrogateescape").read()


def write(rel, text):
    p = os.path.join(XNU, rel)
    d = os.path.dirname(p)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    open(p, "w", encoding="utf-8", errors="surrogateescape").write(text)


STUBS = {
    'osfmk/arm64/ppl/sart.h': "/*\n * pebble: Apple's SART (IOMMU for NVMe/ANS) header is not in the open\n * source drop. There is no SART on Qualcomm hardware; bootstrap is a no-op.\n */\n#ifndef _ARM64_PPL_SART_H_\n#define _ARM64_PPL_SART_H_\n\nstatic inline void\nsart_bootstrap(void)\n{\n}\n\n#endif /* _ARM64_PPL_SART_H_ */\n",
    'osfmk/arm64/ppl/uat.h': "/*\n * pebble: Apple's UAT (GPU unified address translator) header is not in the\n * open source drop. No UAT on Qualcomm hardware; bootstrap is a no-op.\n */\n#ifndef _ARM64_PPL_UAT_H_\n#define _ARM64_PPL_UAT_H_\n\nstatic inline void\nuat_bootstrap(void)\n{\n}\n\n#endif /* _ARM64_PPL_UAT_H_ */\n",
    'osfmk/arm64/tunables/tunables.s': "/*\n * pebble: Apple's per-SoC CPU tunables (implementation-defined HID/ACC\n * register settings applied in start.s) are not in the open source drop,\n * and none of them exist on Cortex-A76/A55. APPLY_TUNABLES does nothing.\n */\n.macro APPLY_TUNABLES midr, tmp1, tmp2\n.endmacro\n",
}


PPL_STUBS = "\n/* ---- PEBBLE_PPL_STUBS ----------------------------------------------------\n * Apple's closed Page Protection Layer supplies these helpers and their\n * declarations.  A Gunyah guest has no PPL, and the open drop never declares\n * them, but pmap.c calls several of them unconditionally.\n */\n#ifndef PEBBLE_PPL_STUBS\n#define PEBBLE_PPL_STUBS\n\nstatic inline void *\nptep_get_iommu(pte_t pte)\n{\n\t(void)pte;\n\treturn NULL;\n}\n\nstatic inline void\npmap_ppl_lockdown_pages(vm_address_t kva, vm_size_t size, int flags,\n    bool ppl_writable)\n{\n\t(void)kva;\n\t(void)size;\n\t(void)flags;\n\t(void)ppl_writable;\n}\n\nstatic inline void\npmap_ppl_unlockdown_pages(vm_address_t kva, vm_size_t size, int flags,\n    bool ppl_writable)\n{\n\t(void)kva;\n\t(void)size;\n\t(void)flags;\n\t(void)ppl_writable;\n}\n\nstatic inline void\npmap_ppl_lockdown_page(vm_address_t va, int flags, bool ppl_writable)\n{\n\t(void)va;\n\t(void)flags;\n\t(void)ppl_writable;\n}\n\nstatic void pmap_phys_write_disable(vm_address_t va);\n\n#endif /* PEBBLE_PPL_STUBS */\n"


def sub(rel, old, new, note):
    src = read(rel)
    if src is None:
        return
    added = [ln for ln in new.splitlines() if ln.strip() and ln not in old.splitlines()]
    if added and added[0].strip() and added[0] in src and "PEBBLE" in new:
        report.append("SKIP     %s (%s)" % (rel, note))
        return
    if old not in src:
        report.append("NOANCHOR %s (%s)" % (rel, note))
        return
    write(rel, src.replace(old, new, 1))
    report.append("PATCHED  %s (%s)" % (rel, note))


def main():
    # 1. machine config list
    sub("makedefs/MakeInc.def",
        "SUPPORTED_ARM64_MACHINE_CONFIGS = BCM2837 T6000 T8101 T8103 VMAPPLE",
        "SUPPORTED_ARM64_MACHINE_CONFIGS = BCM2837 T6000 T8101 T8103 VMAPPLE PEBBLE",
        "machine config list")

    # 2. per-config flags
    sub("makedefs/MakeInc.def",
        "MACHINE_FLAGS_ARM64_BCM2837 = -DARM64_BOARD_CONFIG_BCM2837",
        "MACHINE_FLAGS_ARM64_BCM2837 = -DARM64_BOARD_CONFIG_BCM2837\n"
        "MACHINE_FLAGS_ARM64_PEBBLE = -DARM64_BOARD_CONFIG_PEBBLE -march=armv8.2-a -D__ARM_VMSA__=8 -D__ARM_16K_PG__",
        "machine flags")

    # 3. keep arm64 (not arm64e) for PEBBLE
    src = read("makedefs/MakeInc.def")
    if src and "CURRENT_MACHINE_CONFIG),PEBBLE" not in src:
        pat = re.compile(
            r"(ifneq \(\$\(filter arm64,\$\(ARCH_STRING_FOR_CURRENT_MACHINE_CONFIG\)\),\)\n)"
            r"(export ARCH_STRING_FOR_CURRENT_MACHINE_CONFIG := arm64e\n)")
        new_src, n = pat.subn(
            r"\1ifneq ($(CURRENT_MACHINE_CONFIG),PEBBLE)\n\2endif\n", src, count=1)
        if n:
            write("makedefs/MakeInc.def", new_src)
            report.append("PATCHED  makedefs/MakeInc.def (arm64e guard)")
        else:
            report.append("NOANCHOR makedefs/MakeInc.def (arm64e guard)")

    # 4. board config hook
    sub("pexpert/pexpert/arm64/board_config.h",
        "#ifdef ARM64_BOARD_CONFIG_VMAPPLE",
        BOARD_BLOCK + "#ifdef ARM64_BOARD_CONFIG_VMAPPLE",
        "board config block")

    # 5. new header
    hdr = os.path.join(XNU, "pexpert/pexpert/arm64/PEBBLE.h")
    if os.path.exists(hdr) and "PEBBLE_GICD_BASE" in open(hdr).read():
        report.append("SKIP     pexpert/pexpert/arm64/PEBBLE.h (exists)")
    else:
        open(hdr, "w").write(PEBBLE_H)
        report.append("WROTE    pexpert/pexpert/arm64/PEBBLE.h")

    # 6. GIC dispatch in sleh.c
    sub("osfmk/arm64/sleh.c",
        "\tsleh_interrupt_handler_prologue(state, DBG_INTR_TYPE_OTHER);",
        SLEH_PEBBLE,
        "group-1 dispatch")
    sub("osfmk/arm64/sleh.c",
        "\tentropy_collect();",
        "#endif /* PEBBLE */\n\n\tentropy_collect();",
        "close dispatch guard")

    # 7. start.s SCTLR check
    sub("osfmk/arm64/start.s",
        "\tMOV64   x1, SCTLR_EL1_DEFAULT\n\tcmp\t\tx0, x1\n\tbne\t\t.",
        "#if defined(PEBBLE)\n"
        "\t/* Gunyah may sanitise SCTLR_EL1; skip the Apple readback check */\n"
        "#else\n"
        "\tMOV64   x1, SCTLR_EL1_DEFAULT\n\tcmp\t\tx0, x1\n\tbne\t\t.\n"
        "#endif",
        "sctlr readback")

    # 8. cache geometry (proc_reg.h #errors out without a board branch)
    sub("osfmk/arm64/proc_reg.h",
        "#elif defined (VMAPPLE)",
        "#elif defined (PEBBLE) /* Qualcomm Oryon, 64-byte cache lines */\n"
        "\n"
        "/* I-Cache. */\n"
        "#define MMU_I_CLINE 6\n"
        "\n"
        "/* D-Cache. */\n"
        "#define MMU_CLINE   6\n"
        "\n"
        "#elif defined (VMAPPLE)",
        "proc_reg board branch")

    # 8b. pmap.h declares 'reserved10' twice when neither Rosetta nor PAC is
    # configured (upstream only builds Apple boards), which breaks PEBBLE.
    sub("osfmk/arm/pmap/pmap.h",
        "#else\n\tbool reserved10;\n#endif /* HAS_APPLE_PAC */",
        "#else\n\tbool reserved10_reserved;\n#endif /* HAS_APPLE_PAC */",
        "pmap duplicate field")

    # 9. export the new header so #include <pexpert/arm64/PEBBLE.h> resolves
    sub("pexpert/pexpert/arm64/Makefile",
        "\tBCM2837.h",
        "\tBCM2837.h \\\n\tPEBBLE.h",
        "export PEBBLE.h")

    # 10. GIC bring-up
    sub("pexpert/arm/pe_fiq.c",
        "#if HAS_GIC_V3\n#define GICR_WAKE_TIMEOUT_NS",
        "#if HAS_GIC_V3 && !defined(PEBBLE)\n#define GICR_WAKE_TIMEOUT_NS",
        "exclude apple gic path")
    sub("pexpert/arm/pe_fiq.c",
        "#else\nvoid\npe_init_fiq()\n{\n}\n#endif",
        FIQ_PEBBLE + "#else\nvoid\npe_init_fiq()\n{\n}\n#endif",
        "pebble gic init")

    # 13. make the open-source arm64 assembly and pmap visible to the file
    #     list generator.  files.arm64 gates start.s, cswitch.s, locore.s and
    #     the pmap on "nos_arm_asm" / "nos_arm_pmap", options this board never
    #     defines, so those objects were silently dropped and the kernel
    #     linked with __start and _call_continuation undefined.
    src = read("osfmk/conf/files.arm64")
    if src is not None:
        out = []
        n = 0
        for ln in src.splitlines(True):
            if ("nos_arm_asm" in ln or "nos_arm_pmap" in ln) and "optional" in ln:
                if "kpc.c" in ln:
                    out.append(ln)
                    continue
                out.append(ln.split("optional")[0] + "standard\n")
                n += 1
            else:
                out.append(ln)
        write("osfmk/conf/files.arm64", "".join(out))
        report.append("PATCHED  osfmk/conf/files.arm64 (%d gates removed)" % n)


    # 14. Apple-internal headers/asm that the open-source pmap and start.s
    #     include; the tree does not ship them.
    for path, body in STUBS.items():
        if read(path) is None:
            write(path, body)
            report.append("CREATED  %s (stub)" % path)


    # 15. PPL/IOMMU helpers that live only in Apple's closed sources.
    #     pmap.c is the only file that calls them, and it has pte_t in scope
    #     by this point in its include list.
    anchor = ("#if __ARM_VMSA__ != 8\n"
              "#error Unknown __ARM_VMSA__\n"
              "#endif\n")
    sub("osfmk/arm/pmap/pmap.c", anchor, anchor + "\n" + PPL_STUBS, "ppl stubs")

    print("\n".join(report))
    bad = [r for r in report if r.startswith(("NOANCHOR", "MISSING"))]
    print("\n%d edits, %d problems" % (len(report), len(bad)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
