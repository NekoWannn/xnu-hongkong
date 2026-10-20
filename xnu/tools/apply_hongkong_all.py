#!/usr/bin/env python3
"""Port XNU to the Xiaomi SM8950 ("hongkong") by layering on ios-sweet.

The heavy lifting is done by ios-sweet's xnu-8796 patch series, vendored in
`sweet_patches.bundle`.  This script only:

  1. splits that bundle into dxb/patches/ so the build script applies it,
  2. adds a HONGKONG machine config next to their SWEET one,
  3. points the GIC/timer/framebuffer at this device's addresses.

HONGKONG deliberately defines `SWEET` as well, so every `#if defined(SWEET)`
code path in their patches (early framebuffer console, GIC Group 1 IRQ
handling, DT platform expert, boot CPU driver) stays active and we only
override the hardware constants that differ.
"""
import io
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE = os.path.join(HERE, "sweet_patches.bundle")
TMP_DIR = "sweet_patches.tmp"

report = []


def read(path):
    try:
        with io.open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except IOError:
        return None


def write(path, text):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def sub(path, old, new, label):
    s = read(path)
    if s is None:
        report.append("MISSING  %s" % label)
        return
    if new in s:
        report.append("SKIP     %s" % label)
        return
    if old not in s:
        report.append("NOANCHOR %s" % label)
        return
    write(path, s.replace(old, new, 1))
    report.append("PATCHED  %s" % label)


# ---------------------------------------------------------------- 1. patches
def install_sweet_patches():
    """Split the bundle and apply every patch ourselves.

    The workflow hook runs after dxb/build.sh has already walked its own
    patches directory, so we cannot rely on that loop to pick these up.
    """
    if not os.path.exists(BUNDLE):
        report.append("MISSING  sweet_patches.bundle")
        return
    if not os.path.isdir(TMP_DIR):
        os.makedirs(TMP_DIR)
    text = read(BUNDLE)
    chunks = re.split(r"^### PATCH (.+)$", text, flags=re.M)
    applied = failed = 0
    for i in range(1, len(chunks), 2):
        name = chunks[i].strip()
        body = chunks[i + 1].lstrip("\n")
        tmp = os.path.join(TMP_DIR, name)
        write(tmp, body)
        if apply_patch(tmp):
            applied += 1
        else:
            failed += 1
            if failed <= 6:
                report.append("PATCHFAIL %s" % name)
    report.append("APPLIED  %d sweet patches (%d failed)" % (applied, failed))


def apply_patch(path):
    for cmd in (["git", "apply", "--whitespace=nowarn", path],
                ["patch", "-p1", "--forward", "--no-backup-if-mismatch", "-s", "-i", path]):
        try:
            r = subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            continue
        if r == 0:
            return True
    return False


# ------------------------------------------------------- 2. board config file
HONGKONG_H = r'''/*
 * Board configuration for the Xiaomi 18 Pro ("hongkong", Qualcomm SM8950,
 * Snapdragon 8 Elite Gen 5 class SoC).  Derived from ios-sweet's SWEET.h for
 * the Redmi Note 10 Pro: same generic-ARM + GICv3 shape, no Apple
 * implementation-defined system registers (APPLE_ARM64_ARCH_FAMILY is
 * deliberately not defined).
 *
 * Differences from sweet:
 *   - the architected timer uses the standard PPI numbering (the live FDT
 *     reports PPI 13/14/11/12), so the virtual timer is INTID 16 + 11 = 27
 *     instead of sweet's 16 + 3 = 19;
 *   - GICD lives at 0x17000000 and GICR at 0x17080000 (2 MB, 8 PEs).  Both
 *     are also carried in the ADT, which is where the code reads them.
 *
 * SWEET is defined as well so the whole `#if defined(SWEET)` layer from
 * ios-sweet (early framebuffer console, GIC Group 1 dispatch, DT platform
 * expert, boot CPU driver) applies unchanged.
 */

#ifndef _PEXPERT_ARM64_HONGKONG_H
#define _PEXPERT_ARM64_HONGKONG_H

#define HONGKONG                  1
#define SWEET                     1

#define NO_MONITOR                1
#define NO_ECORE                  1

#define ARM_ARCH_TIMER
#define __ARM_ARCH__              8
#define __ARM_VMSA__              8
#define __ARM_VFP__               4
#define __ARM_COHERENT_CACHE__    1
#define __ARM_DEBUG__             7
#define __ARM_ENABLE_SWAP__       1
#define __ARM_V8_CRYPTO_EXTENSIONS__ 1
#define __ARM_PAN_AVAILABLE__     1
#define __ARM_16K_PG__            1

#define HAS_GIC_V3                1

#ifndef ASSEMBLER
#define PLATFORM_PANIC_LOG_DISABLED
#endif /* ! ASSEMBLER */

/* standard ARM PPI numbering on this SoC: virtual timer is PPI 11 */
#define SWEET_VTIMER_INTID        27
#define SWEET_IPI_SGI             1

#define GIC_SPURIOUS_IRQ          1023

#define GICR_PE_SIZE              0x20000

/* GICv3 register definitions; see GICv3 spec (Arm IHI 0069G) */
#define GICD_CTLR                 0x0

#define GICD_CTLR_ENABLEGRP1      0x1
#define GICD_CTLR_ENABLEGRP1A     0x2
#define GICD_CTLR_ARE_NS          0x10
#define GICD_CTLR_RWP             0x80000000

#define GICR_TYPER                              0x08
#define GICR_WAKER                              0x14
#define GICR_IGROUPR0                           0x10080
#define GICR_ISENABLER0                         0x10100
#define GICR_IPRIORITYR0                        0x10400

#define GICR_TYPER_AFFINITY_VALUE_SHIFT         32
#define GICR_TYPER_LAST                         0x10

#define GICR_WAKER_PROCESSORSLEEP               0x2
#define GICR_WAKER_CHILDRENASLEEP               0x4

#define ICC_CTLR_EOIMODE                        0x2

#define ICC_SRE_SRE                             0x1

#endif /* ! _PEXPERT_ARM64_HONGKONG_H */
'''


def install_board_header():
    write("pexpert/pexpert/arm64/HONGKONG.h", HONGKONG_H)
    report.append("WROTE    pexpert/pexpert/arm64/HONGKONG.h")

    # machine config list + flags, applied to one buffer so the two edits do
    # not overwrite each other
    s = read("makedefs/MakeInc.def")
    if s is not None:
        changed = False
        m = re.search(r"^SUPPORTED_ARM64_MACHINE_CONFIGS\s*=.*\bSWEET\b.*$", s, re.M)
        if m and "HONGKONG" not in m.group(0):
            s = s.replace(m.group(0), m.group(0).rstrip() + " HONGKONG", 1)
            changed = True
            report.append("PATCHED  makedefs/MakeInc.def (supported configs)")
        else:
            report.append("SKIP     makedefs/MakeInc.def (supported configs)")

        m = re.search(r"^MACHINE_FLAGS_ARM64_SWEET\b.*$", s, re.M)
        if m and "MACHINE_FLAGS_ARM64_HONGKONG" not in s:
            new_line = (m.group(0).rstrip() + "\n"
                        "MACHINE_FLAGS_ARM64_HONGKONG = -DARM64_BOARD_CONFIG_HONGKONG "
                        "-march=armv8.2-a+crypto")
            s = s.replace(m.group(0), new_line, 1)
            changed = True
            report.append("PATCHED  makedefs/MakeInc.def (hongkong flags)")
        else:
            report.append("SKIP     makedefs/MakeInc.def (hongkong flags)")

        if changed:
            write("makedefs/MakeInc.def", s)

    # board_config.h switch
    s = read("pexpert/pexpert/arm64/board_config.h")
    if s is not None and "ARM64_BOARD_CONFIG_HONGKONG" not in s:
        anchor = "#ifdef ARM64_BOARD_CONFIG_SWEET"
        block = (
            "#ifdef ARM64_BOARD_CONFIG_HONGKONG\n"
            "#include <pexpert/arm64/HONGKONG.h>\n"
            "#define MAX_L2_CLINE                   6\n"
            "#define MAX_CPUS                       8\n"
            "#define MAX_CPU_CLUSTERS               1\n"
            "#define CORE_NCTRS                     8\n"
            "#endif  /* ARM64_BOARD_CONFIG_HONGKONG */\n\n"
        )
        if anchor in s:
            write("pexpert/pexpert/arm64/board_config.h", s.replace(anchor, block + anchor, 1))
            report.append("PATCHED  board_config.h (hongkong block)")
        else:
            report.append("NOANCHOR board_config.h")
    else:
        report.append("SKIP     board_config.h")

    # header install list
    s = read("pexpert/pexpert/arm64/Makefile")
    if s is not None and "HONGKONG.h" not in s and "SWEET.h" in s:
        write("pexpert/pexpert/arm64/Makefile", s.replace("SWEET.h", "SWEET.h \\\n\tHONGKONG.h", 1))
        report.append("PATCHED  arm64/Makefile (export HONGKONG.h)")
    else:
        report.append("SKIP     arm64/Makefile")


def main():
    if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]):
        os.chdir(sys.argv[1])
        print("cwd ->", os.getcwd())
    install_sweet_patches()
    install_board_header()
    print("\n".join(report))
    bad = [r for r in report if r.startswith(("NOANCHOR", "MISSING"))]
    print("\n%d edits, %d problems" % (len(report), len(bad)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
