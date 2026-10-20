# hongkong 固件事实（从设备提取）

全部数据来自设备自身的 `uefi_a` / `uefi_dtb_a` 分区，不是推断。

## SoC

| 项 | 值 |
|---|---|
| 市场型号 | SM8950（`ro.soc.model`）|
| 代号 | **maili**，基于 **hawi** |
| 平台 DTB 根 compatible | `qcom,hawi-uefi-1.0 qcom,hawi` |
| `ro.board.platform` | `art` |
| 设备 | `hongkong` / `M610BB` |
| CPU implementer | `0x51`（Qualcomm）|

## UEFI 固件卷（`uefi_a`，`/dev/block/sde1`，5 MiB）

分区开头是 **ELF64**（`ET_EXEC`, `EM_AARCH64`）：

```
e_entry = 0x00000000C6800000
PT_LOAD off=0x1000  vaddr=paddr=0xC6800000  filesz=memsz=0x369000
```

因此：

| 参数 | 值 |
|---|---|
| **FD_BASE** | **`0xC6800000`** |
| **FD_SIZE** | **`0x369000`**（3.57 MB）|

FV 头 `_FVH` 位于文件偏移 `0x1000`，FV 长度 `0x368000`，属性 `0xCFEFF`。
用 UEFIExtract 解出 **1305 个文件**（含 `DxeCore`、`ArmCpuDxe`、`PlatformInfoDxeDriver`、`DDRInfoDxe`、`BDS_Menu.cfg`、`PanelSimConfig.csv` 等）。

## 内存映射

### 基础 `memorymap`（29 条，来自平台 DTB）

| 基址 | 大小 | 说明 |
|---|---|---|
| `0x80000000` | `0x1960000` | 25.4 MiB，XBL/UEFI 早期区 |
| `0x82700000` | `0x400000` | 4 MiB |
| `0xC6400000` | `0x400000` | 4 MiB |
| `0xC6800000` | `0xD00000` | **13 MiB，FD 窗口** |
| `0xC7800000` | `0x10000000` | 256 MiB |
| `0xD8800000` | `0x75A0000` | 117.6 MiB |
| **`0xF8C60000`** | **`0x2B00000`** | **帧缓冲（43 MiB）** |

### 设备覆盖 `fragment@9`（6 条，带 `mem-label`）

| 基址 | 大小 | label |
|---|---|---|
| `0x89D80000` | `0x80000` | HLOS1 |
| `0x89E00000` | `0x21B00000` | PIL_Reserved（539 MiB）|
| `0xAB900000` | `0x2C80000` | Display_Demura |
| `0xAE580000` | `0xEE80000` | DXE_Heap（238.5 MiB）|
| `0xBE400000` | `0x5EE0000` | DBI_Dump |
| `0xC4800000` | `0x1C00000` | DXE_Heap2（28 MiB）|

## MMIO

| 项 | 基址 | 大小 | 来源 |
|---|---|---|---|
| GIC Distributor | `0x17000000` | `0x8000000` | DTB `registermap` |
| GIC Redistributors | `0x17080000` | — | 与 `KaanapaliPkg` 一致 |
| UFS | `0x1D80000` | `0x35000` | DTB `/soc/ufs0` |
| UART | `0x894000` | `0x100000` | DTB `/soc/uart` |
| SPMI | `0xC425000` | `0x500000` | DTB `/soc/spmi-bus` |
| QSPI | `0x88DC000` | `0x1000` | DTB `/soc/quadspi` |

## DTB

`uefi_dtb_a`（`/dev/block/sde3`，400 KB）内有 **3 个 DTB**：

| 偏移 | 大小 | 内容 |
|---|---|---|
| `0xE8` | 177 728 | 平台主 DTB（`qcom,hawi-uefi`）|
| `0x2C320` | 60 288 | **设备覆盖层**（`fragment@0..15`，含 `fragment@9` 内存映射）|
| `0x3B610` | 75 096 | 另一个覆盖层 |

主 DTB 已提取为 `port/fw/hongkong.dtb`。

## 与 sheng 的对照

| 项 | sheng（SM8550）| hongkong（SM8950）|
|---|---|---|
| FD_BASE | `0xA7100000` | **`0xC6800000`** |
| FD_SIZE | `0x300000` | **`0x369000`** |
| 面板 | 3048×2032 | **1120×2436** |
| 帧缓冲 | Display Reserved | **`0xF8C60000` + `0x2B00000`** |
| SoC 包 | `KailuaPkg` | 需新建 `MailiPkg`（以 `KaanapaliPkg` 为蓝本）|
| 厂商驱动 | `Binaries/sheng` | 待从本机 `uefi_a` 提取 |
