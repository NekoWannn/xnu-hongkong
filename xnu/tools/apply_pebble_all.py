#!/usr/bin/env python3
"""Apply the PEBBLE (Xiaomi SM8950) machine-config edits to an xnu tree.

Runs after darwin-xnu-build has applied its own patch set, so it edits by
anchor string instead of by line context.  Safe to re-run: every edit checks
for its marker first.

Usage: apply_pebble_all.py /path/to/xnu
"""
import os

# set to "arm_init" to hang there and test reachability
STALL_SITE = ""
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


STUBS_ASM = "/* PEBBLE: stubs for Apple-only symbols that have no open implementation.\n * kpc programs Apple's PMU; the cache helper and monitor hook are not\n * used on this platform.  Assembly is used so no prototypes are needed.\n */\n\t.text\n\t.p2align 2\n\n\t.globl _kpc_arch_init\n_kpc_arch_init:\n\tret\n\n\t.globl _kpc_get_all_cpus_counters\n_kpc_get_all_cpus_counters:\n\tret\n\n\t.globl _kpc_get_classes\n_kpc_get_classes:\n\tret\n\n\t.globl _kpc_get_configurable_config\n_kpc_get_configurable_config:\n\tret\n\n\t.globl _kpc_get_configurable_counters\n_kpc_get_configurable_counters:\n\tret\n\n\t.globl _kpc_get_fixed_config\n_kpc_get_fixed_config:\n\tret\n\n\t.globl _kpc_get_fixed_counters\n_kpc_get_fixed_counters:\n\tret\n\n\t.globl _kpc_get_pmu_version\n_kpc_get_pmu_version:\n\tret\n\n\t.globl _kpc_get_rawpmu_config\n_kpc_get_rawpmu_config:\n\tret\n\n\t.globl _kpc_idle\n_kpc_idle:\n\tret\n\n\t.globl _kpc_idle_exit\n_kpc_idle_exit:\n\tret\n\n\t.globl _kpc_is_running_configurable\n_kpc_is_running_configurable:\n\tret\n\n\t.globl _kpc_is_running_fixed\n_kpc_is_running_fixed:\n\tret\n\n\t.globl _kpc_pc_capture\n_kpc_pc_capture:\n\tret\n\n\t.globl _kpc_set_config_arch\n_kpc_set_config_arch:\n\tret\n\n\t.globl _kpc_set_period_arch\n_kpc_set_period_arch:\n\tret\n\n\t.globl _kpc_set_running_arch\n_kpc_set_running_arch:\n\tret\n\n\t.globl _kpc_set_sw_inc\n_kpc_set_sw_inc:\n\tret\n\n\t.globl _ml_enable_monitor\n_ml_enable_monitor:\n\tret\n\n\t.globl _CleanPoC_DcacheRegion_Force_nopreempt_nohid\n_CleanPoC_DcacheRegion_Force_nopreempt_nohid:\n\tret\n\n\t.data\n\t.p2align 2\n\n\t.globl _kpc_configurable_config_count\n_kpc_configurable_config_count:\n\t.long 0\n\n\t.globl _kpc_configurable_count\n_kpc_configurable_count:\n\t.long 0\n\n\t.globl _kpc_configurable_max\n_kpc_configurable_max:\n\t.long 0\n\n\t.globl _kpc_fixed_config_count\n_kpc_fixed_config_count:\n\t.long 0\n\n\t.globl _kpc_fixed_count\n_kpc_fixed_count:\n\t.long 0\n\n\t.globl _kpc_fixed_max\n_kpc_fixed_max:\n\t.long 0\n\n\t.globl _kpc_rawpmu_config_count\n_kpc_rawpmu_config_count:\n\t.long 0\n\n\t.globl _pmap_pending_preemption\n_pmap_pending_preemption:\n\t.long 0\n\n"

FBCON_C = '\n/*****************************************************************************/\n\n/*\n * pebble: framebuffer "serial" console.  There is no usable UART on this\n * board, so kprintf output is drawn straight onto the boot framebuffer.\n * Geometry measured on hardware.\n *\n * The mapping is created lazily on the first character printed: serial_init()\n * runs before the pmap can service ml_io_map(), so mapping at probe time fails\n * and the console would stay silent forever.\n */\n\nextern unsigned char iso_font[256 * 16];\n\n#define PEBBLE_FB_BASE    0xf8c60000UL\n#define PEBBLE_FB_WIDTH   1120\n#define PEBBLE_FB_HEIGHT  2436\n#define PEBBLE_FB_STRIDE  4480\n\n#define FBCON_SCALE     2\n#define FBCON_FG        0xffffffffu\n#define FBCON_BG        0xff000000u\n\nstatic volatile uint32_t *fbcon_base;\nstatic uint32_t fbcon_cols = PEBBLE_FB_WIDTH / (8 * FBCON_SCALE);\nstatic uint32_t fbcon_rows = PEBBLE_FB_HEIGHT / (16 * FBCON_SCALE);\nstatic uint32_t fbcon_col, fbcon_row;\n\nstatic int\nfbcon_ensure_mapped(void)\n{\n\tif (fbcon_base != NULL) {\n\t\treturn 1;\n\t}\n\tfbcon_base = (volatile uint32_t *)ml_io_map(PEBBLE_FB_BASE,\n\t    PEBBLE_FB_STRIDE * PEBBLE_FB_HEIGHT);\n\treturn fbcon_base != NULL;\n}\n\nstatic void\nfbcon_clear_row(uint32_t row)\n{\n\tfor (uint32_t y = row * 16 * FBCON_SCALE; y < (row + 1) * 16 * FBCON_SCALE; y++) {\n\t\tvolatile uint32_t *line = fbcon_base + y * (PEBBLE_FB_STRIDE / 4);\n\t\tfor (uint32_t x = 0; x < PEBBLE_FB_WIDTH; x++) {\n\t\t\tline[x] = FBCON_BG;\n\t\t}\n\t}\n}\n\nstatic void\nfbcon_newline(void)\n{\n\tfbcon_col = 0;\n\tfbcon_row = (fbcon_row + 1) % fbcon_rows;\n\tfbcon_clear_row(fbcon_row);\n\tfbcon_clear_row((fbcon_row + 1) % fbcon_rows);\n}\n\nstatic void\nfbcon_init(void)\n{\n\tfbcon_col = fbcon_row = 0;\n\tif (fbcon_ensure_mapped()) {\n\t\tfbcon_clear_row(0);\n\t}\n}\n\nstatic unsigned int\nfbcon_transmit_ready(void)\n{\n\treturn 1;\n}\n\nstatic void\nfbcon_transmit_data(uint8_t c)\n{\n\tif (!fbcon_ensure_mapped()) {\n\t\treturn;\n\t}\n\tif (c == \'\\r\') {\n\t\tfbcon_col = 0;\n\t\treturn;\n\t}\n\tif (c == \'\\n\') {\n\t\tfbcon_newline();\n\t\treturn;\n\t}\n\tif (fbcon_col >= fbcon_cols) {\n\t\tfbcon_newline();\n\t}\n\n\tconst unsigned char *glyph = iso_font + c * 16;\n\tuint32_t x0 = fbcon_col * 8 * FBCON_SCALE;\n\tuint32_t y0 = fbcon_row * 16 * FBCON_SCALE;\n\n\tfor (uint32_t gy = 0; gy < 16; gy++) {\n\t\tfor (uint32_t sy = 0; sy < FBCON_SCALE; sy++) {\n\t\t\tvolatile uint32_t *line = fbcon_base +\n\t\t\t    (y0 + gy * FBCON_SCALE + sy) * (PEBBLE_FB_STRIDE / 4) + x0;\n\t\t\tfor (uint32_t gx = 0; gx < 8; gx++) {\n\t\t\t\tuint32_t px = (glyph[gy] & (1u << gx)) ? FBCON_FG : FBCON_BG;\n\t\t\t\tfor (uint32_t sx = 0; sx < FBCON_SCALE; sx++) {\n\t\t\t\t\tline[gx * FBCON_SCALE + sx] = px;\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n\tfbcon_col++;\n}\n\nstatic unsigned int\nfbcon_receive_ready(void)\n{\n\treturn 0;\n}\n\nstatic uint8_t\nfbcon_receive_data(void)\n{\n\treturn 0;\n}\n\nSECURITY_READ_ONLY_LATE(static struct pe_serial_functions)\npebble_fbcon_serial_functions = {\n\t.init = fbcon_init,\n\t.transmit_ready = fbcon_transmit_ready,\n\t.transmit_data = fbcon_transmit_data,\n\t.receive_ready = fbcon_receive_ready,\n\t.receive_data = fbcon_receive_data,\n\t.device = SERIAL_PEBBLE_FBCON\n};\n\nstatic void\nfbcon_probe(void)\n{\n\tregister_serial_functions(&pebble_fbcon_serial_functions);\n}\n'

EARLY_FB_MAP = '\n\t/*\n\t * pebble: map one 2 MB block VA=PA=0xf8c00000 so that early boot code\n\t * (before the pmap exists) can write the panel directly.  A fresh L1\n\t * page is carved from the bootstrap table free pointer and zeroed,\n\t * then create_bootstrap_mapping installs the block entry.\n\t */\n\tmov\tx1, x2\t\t\t\t\t// new L1 page\n\tadd\tx2, x2, PGBYTES\t\t\t\t// bump free pointer past it\n\tmov\tx13, x1\n\tmov\tx0, #(ARM_TTE_EMPTY)\n\tmov\tx6, #(TTE_PGENTRIES)\n2:\tstr\tx0, [x13], #8\n\tsubs\tx6, x6, #1\n\tb.ne\t2b\n\tMOV64\tx0, 0xf8c00000\n\tmov\tx4, x0\n\tmov\tx5, #1\n\tcreate_bootstrap_mapping x0, x4, x5, x1, x2, x6, x10, x11, x12, x13\n\n'

PEBBLE_EARLY_C = "void\npebble_marker(void)\n{\n\tvolatile unsigned int *fb = (volatile unsigned int *)0xf8c60000UL;\n\tunsigned int y, x;\n\n\t/* wipe the panel so anything visible afterwards is ours */\n\tfor (y = 0; y < 1200; y++) {\n\t\tfor (x = 0; x < 1120; x++) {\n\t\t\tfb[(y * (4480 / 4)) + x] = 0xff000000u;\n\t\t}\n\t}\n\n\t/* blue band = arm_init C code is running */\n\tfor (y = 120; y < 132; y++) {\n\t\tfor (x = 40; x < 400; x++) {\n\t\t\tfb[(y * (4480 / 4)) + x] = 0xff0000ffu;\n\t\t}\n\t}\n}\n\n/* pebble: early console drawn straight to the splash framebuffer.  start.s\n * maps VA=PA 0xf8000000 as one 32 MB Normal-NC block inside the V=P table,\n * so this works from the first C statement of arm_init and needs no pmap. */\nextern unsigned char iso_font[256 * 16];\n\n#define PEBBLE_EARLY_FB     0xf8c60000UL\n#define PEBBLE_EARLY_STRIDE 4480\n#define PEBBLE_EARLY_SCALE  2\n\nstatic unsigned int pebble_early_row;\nstatic unsigned int pebble_early_started;\n\nvoid\npebble_early_putc(char c)\n{\n\tvolatile unsigned int *fb = (volatile unsigned int *)0xf8c60000UL;\n\n\tif (!pebble_early_started) {\n\t\tpebble_early_started = 1;\n\t}\n\tif (c == '\\r') {\n\t\tpebble_early_row = pebble_early_row;\n\t\treturn;\n\t}\n\tif (c == '\\n') {\n\t\tpebble_early_row++;\n\t\treturn;\n\t}\n\n\t{\n\t\tconst unsigned char *glyph = iso_font + (unsigned char)c * 16;\n\t\tunsigned int y0 = 200 + pebble_early_row * 16 * PEBBLE_EARLY_SCALE;\n\n\t\tfor (unsigned int gy = 0; gy < 16; gy++) {\n\t\t\tfor (unsigned int sy = 0; sy < PEBBLE_EARLY_SCALE; sy++) {\n\t\t\t\tvolatile unsigned int *line = fb +\n\t\t\t\t    (y0 + gy * PEBBLE_EARLY_SCALE + sy) *\n\t\t\t\t    (PEBBLE_EARLY_STRIDE / 4);\n\t\t\t\tfor (unsigned int gx = 0; gx < 8; gx++) {\n\t\t\t\t\tif (glyph[gy] & (1u << gx)) {\n\t\t\t\t\t\tfor (unsigned int sx = 0; sx < PEBBLE_EARLY_SCALE; sx++) {\n\t\t\t\t\t\t\tline[40 + gx * PEBBLE_EARLY_SCALE + sx] = 0xffffffffu;\n\t\t\t\t\t\t}\n\t\t\t\t\t}\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n\nstatic void\npebble_early_puts(const char *str)\n{\n\twhile (*str) {\n\t\tpebble_early_putc(*str++);\n\t}\n}\n\n"

PEBBLE_STAGE0 = '\n#if defined(PEBBLE)\n\t/* Stage 0: white band written straight to the framebuffer while the MMU\n\t * is still off, so it needs no stack, no tables and no boot args. */\n\tMOV64\tx0, 0xf8c60000\n\tMOV64\tx4, 4480\n\tmov\tx2, #8\n\tadd\tx0, x0, x4, lsl #8\nLpebble_mark_row:\n\tmov\tx3, #96\n\tmov\tx5, x0\nLpebble_mark_pixel:\n\tmov\tw1, #-1\n\tstr\tw1, [x5], #4\n\tsubs\tx3, x3, #1\n\tb.ne\tLpebble_mark_pixel\n\tadd\tx0, x0, x4\n\tsubs\tx2, x2, #1\n\tb.ne\tLpebble_mark_row\n\tdsb\tsy\n#endif\n'

PEBBLE_FB_BLOCK = '\n#if defined(PEBBLE)\n\t/*\n\t * Map the splash framebuffer with one 32 MB Normal-NC block inside the\n\t * EXISTING V=P L2 table. Framebuffer and RAM share L1 index 0, so a\n\t * second create_bootstrap_mapping would replace the RAM L2 table.\n\t * 16K granule: the first V=P L2 page is bootstrap_pagetables + 2 pages.\n\t */\n\tMOV64\tx10, 0xf8000000\n\tMOV64\tx11, (ARM_TTE_TYPE_BLOCK | ARM_TTE_VALID | ARM_TTE_BLOCK_SH(SH_OUTER_MEMORY) | ARM_TTE_BLOCK_ATTRINDX(CACHE_ATTRINDX_WRITECOMB) | ARM_TTE_BLOCK_AF | ARM_TTE_BLOCK_PNX | ARM_TTE_BLOCK_NX)\n\torr\tx11, x11, x10\n\tand\tx12, x10, #(ARM_TT_L2_INDEX_MASK)\n\tlsr\tx12, x12, #(ARM_TT_L2_SHIFT)\n\tadd\tx13, x25, #(2 * PGBYTES)\n\tstr\tx11, [x13, x12, lsl #TTE_SHIFT]\n#endif\n'

PEBBLE_STAGE1 = '\n#if defined(PEBBLE)\n\t/* Stage 1: the MMU is on now; a red band proves the V=P framebuffer\n\t * block inserted earlier is live. */\n\tMOV64\tx0, 0xf8dc5600\n\tMOV64\tx4, 4480\n\tmov\tx2, #8\nLpebble_mmu_row:\n\tmov\tx3, #96\n\tmov\tx5, x0\n\tmov\tw1, #0x0000ff00\nLpebble_mmu_pixel:\n\tstr\tw1, [x5], #4\n\tsubs\tx3, x3, #1\n\tb.ne\tLpebble_mmu_pixel\n\tadd\tx0, x0, x4\n\tsubs\tx2, x2, #1\n\tb.ne\tLpebble_mmu_row\n\tdsb\tsy\n#endif\n'

PEBBLE_STAGE2 = '#if defined(PEBBLE)\n\t/* Stage 2: reached only after SCTLR_EL1 enables the MMU, so a green band\n\t * proves the V=P framebuffer block mapping is live. */\n\tMOV64\tx0, 0xf8dd3e00\n\tMOV64\tx4, 4480\n\tmov\tx2, #8\nLpebble_va_row:\n\tmov\tx3, #96\n\tmov\tx5, x0\n\tmov\tw1, #0x0000ff00\nLpebble_va_pixel:\n\tstr\tw1, [x5], #4\n\tsubs\tx3, x3, #1\n\tb.ne\tLpebble_va_pixel\n\tadd\tx0, x0, x4\n\tsubs\tx2, x2, #1\n\tb.ne\tLpebble_va_row\n\tdsb\tsy\n#endif\n'

PEBBLE_VEC = '#if defined(PEBBLE)\n\t/* PEBBLE: own vector table so an early exception shows up as a magenta\n\t * band instead of a silent Gunyah reset. */\n\tadrp\tx0, Lpebble_vectors@PAGE\n\tadd\tx0, x0, Lpebble_vectors@PAGEOFF\n\tmsr\tVBAR_EL1, x0\n\tisb\n#endif\n'

PEBBLE_TABLE = '#if defined(PEBBLE)\n\t.p2align 11\nLpebble_vectors:\n\t.rept 16\n\t.align 7\n\tb\tLpebble_crash\n\t.endr\nLpebble_crash:\n\tMOV64\tx0, 0xf8fcb000\n\tMOV64\tx4, 4480\n\tmov\tx2, #16\nLpebble_crash_row:\n\tmov\tx3, #200\n\tmov\tx5, x0\n\tmov\tw1, #0x00ff00ff\nLpebble_crash_px:\n\tstr\tw1, [x5], #4\n\tsubs\tx3, x3, #1\n\tb.ne\tLpebble_crash_px\n\tadd\tx0, x0, x4\n\tsubs\tx2, x2, #1\n\tb.ne\tLpebble_crash_row\n\tdsb\tsy\n1:\twfe\n\tb\t1b\n#endif\n'

PEBBLE_VEC = '#if defined(PEBBLE)\n\t.p2align 11\nLpebble_vectors:\n\t.rept 16\n\t.align 7\n\tb\tLpebble_crash\n\t.endr\n\nLpebble_crash:\n\tadrp\tx10, Lpebble_frame@PAGE\n\tadd\tx10, x10, Lpebble_frame@PAGEOFF\n\tstp\tx0, x1, [x10, #0]\n\tstp\tx2, x3, [x10, #16]\n\tstp\tx4, x5, [x10, #32]\n\tstp\tx6, x7, [x10, #48]\n\tstp\tx8, x9, [x10, #64]\n\tstp\tx11, x12, [x10, #80]\n\tstp\tx13, x14, [x10, #96]\n\tstp\tx15, x16, [x10, #112]\n\tstp\tx17, x18, [x10, #128]\n\tstp\tx19, x20, [x10, #144]\n\tstp\tx21, x22, [x10, #160]\n\tstp\tx23, x24, [x10, #176]\n\tstp\tx25, x26, [x10, #192]\n\tstp\tx27, x28, [x10, #208]\n\tstp\tx29, x30, [x10, #224]\n\tmrs\tx0, esr_el1\n\tstr\tx0, [x10, #240]\n\tmrs\tx0, elr_el1\n\tstr\tx0, [x10, #248]\n\tmrs\tx0, far_el1\n\tstr\tx0, [x10, #256]\n\tmrs\tx0, spsr_el1\n\tstr\tx0, [x10, #264]\n\tstr\tx10, [x10, #272]\n\t/* stack: top of the frame area */\n\tmov\tx0, #8176\n\tadd\tx0, x10, x0\n\tmov\tsp, x0\n\tmov\tx0, x10\n\tbl\tEXT(pebble_dump)\n1:\twfe\n\tb\t1b\n\n\t.p2align 12\nLpebble_frame:\n\t.space\t8192\n#endif\n'

PEBBLE_DUMP_C = '/* pebble: crash dump. Prints ESR/ELR/FAR/SPSR and x0..x30 as hex. */\nstatic const unsigned char pebble_hex[16][7] = {\n\t{0x0E,0x11,0x13,0x15,0x19,0x11,0x0E},\n\t{0x04,0x0C,0x04,0x04,0x04,0x04,0x0E},\n\t{0x0E,0x11,0x01,0x02,0x04,0x08,0x1F},\n\t{0x1F,0x02,0x04,0x02,0x01,0x11,0x0E},\n\t{0x02,0x06,0x0A,0x12,0x1F,0x02,0x02},\n\t{0x1F,0x10,0x1E,0x01,0x01,0x11,0x0E},\n\t{0x06,0x08,0x10,0x1E,0x11,0x11,0x0E},\n\t{0x1F,0x01,0x02,0x04,0x08,0x08,0x08},\n\t{0x0E,0x11,0x11,0x0E,0x11,0x11,0x0E},\n\t{0x0E,0x11,0x11,0x0F,0x01,0x02,0x0C},\n\t{0x0E,0x11,0x11,0x1F,0x11,0x11,0x11},\n\t{0x1E,0x11,0x11,0x1E,0x11,0x11,0x1E},\n\t{0x0E,0x11,0x10,0x10,0x10,0x11,0x0E},\n\t{0x1C,0x12,0x11,0x11,0x11,0x12,0x1C},\n\t{0x1F,0x10,0x10,0x1E,0x10,0x10,0x1F},\n\t{0x1F,0x10,0x10,0x1E,0x10,0x10,0x10}\n};\n\nstatic void\npebble_glyph(unsigned int x, unsigned int y, unsigned int d, unsigned int scale)\n{\n\tvolatile unsigned int *fb = (volatile unsigned int *)0xf8c60000UL;\n\tunsigned int gy, gx, sy, sx;\n\n\tfor (gy = 0; gy < 7; gy++) {\n\t\tfor (gx = 0; gx < 5; gx++) {\n\t\t\tif (!(pebble_hex[d][gy] & (0x10u >> gx))) {\n\t\t\t\tcontinue;\n\t\t\t}\n\t\t\tfor (sy = 0; sy < scale; sy++) {\n\t\t\t\tfor (sx = 0; sx < scale; sx++) {\n\t\t\t\t\tfb[((y + gy * scale + sy) * (4480 / 4)) + x + gx * scale + sx] =\n\t\t\t\t\t    0xffffffffu;\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n\nstatic void\npebble_hex64(unsigned int x, unsigned int y, unsigned long v, unsigned int scale)\n{\n\tfor (unsigned int i = 0; i < 16; i++) {\n\t\tunsigned int d = (unsigned int)((v >> (4 * (15 - i))) & 0xfu);\n\t\tpebble_glyph(x + i * 6 * scale, y, d, scale);\n\t}\n}\n\nvoid\npebble_dump(unsigned long *f)\n{\n\tvolatile unsigned int *fb = (volatile unsigned int *)0xf8c60000UL;\n\tunsigned int y, x;\n\tconst unsigned int scale = 2;\n\tunsigned int row = 0;\n\n\tfor (y = 0; y < 1400; y++) {\n\t\tfor (x = 0; x < 1120; x++) {\n\t\t\tfb[(y * (4480 / 4)) + x] = 0xff000000u;\n\t\t}\n\t}\n\n\tpebble_hex64(200, 40 + row++ * 20, f[30], scale);   /* ESR  */\n\tpebble_hex64(200, 40 + row++ * 20, f[31], scale);   /* ELR  */\n\tpebble_hex64(200, 40 + row++ * 20, f[32], scale);   /* FAR  */\n\tpebble_hex64(200, 40 + row++ * 20, f[33], scale);   /* SPSR */\n\n\trow++;\n\tfor (unsigned int i = 0; i < 15; i++) {\n\t\tpebble_hex64(200, 40 + (row++) * 20, f[i * 2], scale);\n\t}\n\tfor (unsigned int i = 0; i < 16; i++) {\n\t\tpebble_hex64(560, 40 + (11 + i) * 20, f[i * 2 + 1], scale);\n\t}\n}\n'

PEBBLE_VEC_SETUP = '#if defined(PEBBLE)\n\t/* PEBBLE: own vectors so an early exception dumps registers */\n\tadrp\tx0, Lpebble_vectors@PAGE\n\tadd\tx0, x0, Lpebble_vectors@PAGEOFF\n\tmsr\tVBAR_EL1, x0\n\tisb\n#endif\n'

PPL_STUBS = "\n/* ---- PEBBLE_PPL_STUBS ----------------------------------------------------\n * Apple's closed Page Protection Layer supplies these helpers and their\n * declarations.  A Gunyah guest has no PPL, and the open drop never declares\n * them, but pmap.c calls several of them unconditionally.\n */\n#ifndef PEBBLE_PPL_STUBS\n#define PEBBLE_PPL_STUBS\n\n/* The closed uat.h normally supplies pte_t. */\n#ifndef PEBBLE_PTE_T_DEFINED\n#define PEBBLE_PTE_T_DEFINED\ntypedef uint64_t pte_t;\n#endif\n\nstatic inline void *\nptep_get_iommu(pte_t pte)\n{\n\t(void)pte;\n\treturn NULL;\n}\n\nstatic inline void\npmap_ppl_lockdown_pages(vm_address_t kva, vm_size_t size, int flags,\n    bool ppl_writable)\n{\n\t(void)kva;\n\t(void)size;\n\t(void)flags;\n\t(void)ppl_writable;\n}\n\nstatic inline void\npmap_ppl_unlockdown_pages(vm_address_t kva, vm_size_t size, int flags,\n    bool ppl_writable)\n{\n\t(void)kva;\n\t(void)size;\n\t(void)flags;\n\t(void)ppl_writable;\n}\n\nstatic inline void\npmap_ppl_lockdown_page(vm_address_t va, int flags, bool ppl_writable)\n{\n\t(void)va;\n\t(void)flags;\n\t(void)ppl_writable;\n}\n\nstatic void pmap_phys_write_disable(vm_address_t va);\n\n#endif /* PEBBLE_PPL_STUBS */\n"


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
                src_path = ln.split()[0]
                # only un-gate files that actually ship in the open drop:
                # several of these (amcc_rorgn_*.c, ...) are Apple-internal
                if ("kpc" in ln or
                        not os.path.exists(os.path.join(XNU, src_path))):
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


    # 17. assembly stubs for Apple-only symbols (kpc, cache helper, monitor)
    if read("osfmk/arm64/pebble_stubs.s") is None:
        write("osfmk/arm64/pebble_stubs.s", STUBS_ASM)
        report.append("CREATED  osfmk/arm64/pebble_stubs.s")

    src = read("osfmk/conf/files.arm64")
    if src is not None and "pebble_stubs.s" not in src:
        write("osfmk/conf/files.arm64",
              src + "osfmk/arm64/pebble_stubs.s\t\tstandard\n")
        report.append("PATCHED  osfmk/conf/files.arm64 (stubs entry)")


    # 18. framebuffer console: kprintf goes to the panel instead of a UART
    src = read("pexpert/arm/pe_serial.c")
    if src is not None and "pebble_fbcon_serial_functions" not in src:
        src = src.replace("\tgPESF = fns;\n}",
                          "\tgPESF = fns;\n}\n" + FBCON_C, 1)
        src = src.replace(
            'PE_parse_boot_argn("disable-uart-irq", &disable_uart_irq, sizeof(disable_uart_irq));',
            'PE_parse_boot_argn("disable-uart-irq", &disable_uart_irq, sizeof(disable_uart_irq));\n\n\tfbcon_probe();',
            1)
        write("pexpert/arm/pe_serial.c", src)
        report.append("PATCHED  pexpert/arm/pe_serial.c (fbcon)")

    sub("pexpert/pexpert/arm/protos.h",
        "SERIAL_DCC_UART=0x10",
        "SERIAL_DCC_UART=0x10,\n\tSERIAL_PEBBLE_FBCON=0x20",
        "fbcon serial device id")


    # 19. bisect aid: hang inside arm_init so "stopped" vs "rebooted" tells us
    #     whether XNU reached this point (kprintf is not live this early).
    if STALL_SITE == "arm_init":
        sub("osfmk/arm/arm_init.c",
            "\tarm_setup_pre_sign();",
            "\tfor (;;) { __asm__ volatile(\"wfe\"); }\n"
            "\tarm_setup_pre_sign();",
            "stall at arm_init")


    # 21. early console: draw from arm_init, before kprintf exists
    src = read("osfmk/arm/arm_init.c")
    if src is not None and "pebble_early_puts" not in src:
        src = src.replace("void arm_init(boot_args * args);",
                          PEBBLE_EARLY_C + "void arm_init(boot_args * args);", 1)
        src = src.replace("\tarm_vm_init(xmaxmem, args);",
                          '\tpebble_early_puts("PEBBLE dt ok\\n");\n'
                          '\tfor (;;) { __asm__ volatile("wfe"); }\n'
                          '\tarm_vm_init(xmaxmem, args);', 1)
        src = src.replace("\tPE_init_platform(FALSE, args);",
                          '\tpebble_early_puts("PEBBLE platform ok\\n");\n\tPE_init_platform(FALSE, args);', 1)
        write("osfmk/arm/arm_init.c", src)
        report.append("PATCHED  osfmk/arm/arm_init.c (early console)")


    # 20. early framebuffer, following ios-sweet 0010: a stage-0 band written
    #     with the MMU off, then one 32 MB block in the existing V=P L2 table.
    src = read("osfmk/arm64/start.s")
    if src is not None and "Lpebble_mark_row" not in src:
        src = src.replace("\tmov\t\tx20, x0\n",
                          "\tmov\t\tx20, x0\n" + PEBBLE_STAGE0, 1)
        src = src + PEBBLE_VEC   # table + dump handler, out of the code stream
        write("osfmk/arm64/start.s", src)
        report.append("PATCHED  osfmk/arm64/start.s (early framebuffer)")


    src = read("osfmk/arm64/start.s")
    if src is not None and "Lpebble_mmu_row" not in src:
        src = src.replace("\tbl\t\tEXT(set_mmu_ttb)\n\tmov\t\tlr, x19\n",
                          "\tbl\t\tEXT(set_mmu_ttb)\n\tmov\t\tlr, x19\n" + PEBBLE_STAGE1, 1)
        write("osfmk/arm64/start.s", src)
        report.append("PATCHED  osfmk/arm64/start.s (stage 1 marker)")

    src = read("osfmk/arm64/start.s")
    if src is not None and "Lpebble_va_row" not in src:
        src = src.replace("\tMSR_SCTLR_EL1_X0\n\tisb\t\tsy\n",
                          "\tMSR_SCTLR_EL1_X0\n\tisb\t\tsy\n" + PEBBLE_STAGE2, 1)
        write("osfmk/arm64/start.s", src)
        report.append("PATCHED  osfmk/arm64/start.s (MMU-on marker)")

    src = read("osfmk/arm/arm_init.c")
    if src is not None and "pebble_marker();" not in src:
        src = src.replace("\tarm_setup_pre_sign();",
                          "\tpebble_marker();\n\tarm_setup_pre_sign();", 1)
        write("osfmk/arm/arm_init.c", src)
        report.append("PATCHED  osfmk/arm/arm_init.c (blue marker)")

    # 22. start.s: skip Apple-only steps that have no PEBBLE equivalent and
    #     would fault or jump through an empty tunables table.
    src = read("osfmk/arm64/start.s")
    if src is not None and "PEBBLE: no tunables" not in src:
        src = src.replace("\tAPPLY_TUNABLES x12, x13, x14\n",
                          "\t/* PEBBLE: no tunables */\n", 1)
        src = src.replace("#if HAS_CLUSTER\n",
                          "#if HAS_CLUSTER && !defined(PEBBLE)\n", 1)
        write("osfmk/arm64/start.s", src)
        report.append("PATCHED  osfmk/arm64/start.s (skip tunables, no CPU_OVRD)")

    src = read("osfmk/arm64/start.s")
    if src is not None and "Lpebble_crash" not in src:
        src = src.replace("\tMOV64   x0, SCTLR_EL1_DEFAULT\n",
                          PEBBLE_VEC_SETUP + "\tMOV64   x0, SCTLR_EL1_DEFAULT\n", 1)
        src = src + PEBBLE_VEC   # table + dump handler, out of the code stream
        write("osfmk/arm64/start.s", src)
        report.append("PATCHED  osfmk/arm64/start.s (crash vectors)")

    src = read("osfmk/arm/arm_init.c")
    if src is not None and "pebble_dump" not in src:
        src = src.replace("void arm_init(boot_args * args);",
                          PEBBLE_DUMP_C + "void arm_init(boot_args * args);", 1)
        write("osfmk/arm/arm_init.c", src)
        report.append("PATCHED  osfmk/arm/arm_init.c (crash dump)")

    # 23. skip EL1 system-register writes that Gunyah is likely to trap
    #     (FP/SIMD enable and debug control).
    src = read("osfmk/arm64/start.s")
    if src is not None and "PEBBLE: skip CPACR" not in src:
        src = src.replace("\tmov\t\tx0, #(CPACR_FPEN_ENABLE)\n",
                          "#if !defined(PEBBLE)  /* PEBBLE: skip CPACR */\n"
                          "\tmov\t\tx0, #(CPACR_FPEN_ENABLE)\n", 1)
        src = src.replace("\tmsr\t\tCPACR_EL1, x0\n",
                          "\tmsr\t\tCPACR_EL1, x0\n#endif\n", 1)
        src = src.replace("\tmrs\t\tx12, MDSCR_EL1\n",
                          "#if !defined(PEBBLE)\n\tmrs\t\tx12, MDSCR_EL1\n", 1)
        src = src.replace("\tmsr\t\tMDSCR_EL1, x12\n",
                          "\tmsr\t\tMDSCR_EL1, x12\n#endif\n", 1)
        write("osfmk/arm64/start.s", src)
        report.append("PATCHED  osfmk/arm64/start.s (skip CPACR and MDSCR)")

    print("\n".join(report))
    bad = [r for r in report if r.startswith(("NOANCHOR", "MISSING"))]
    print("\n%d edits, %d problems" % (len(report), len(bad)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
