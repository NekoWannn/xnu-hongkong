# 启动链（UniXNU 参考，用于本项目迁移）

来源：<https://unixnu.com/zh/concepts/boot-chain>

启动链有四次交接，每次都有固定输入、校验步骤和失败返回路径。

## 1. bootloader → BootShim

Android bootloader 读取启动镜像：**kernel 字段是 gzip 流，前半是 BootShim，后半是 UEFI 固件卷**，gzip 流之后是设备树。

BootShim 按 Mu-Silicium 设备配置中的固件基址和大小汇编：

```bash
clang --target=aarch64-none-elf -c \
    -DFD_BASE=0xA7100000 -DFD_SIZE=0x300000 \
    -DREQUIRES_KERNEL_HEADER=1 \
    BootShim.S -o shim.o
llvm-objcopy -O binary shim.o shim.bin
```

设备配置要求时，shim 含 ARM64 image header，描述 shim 加固件卷，bootloader 可把两者加载到指定地址。BootShim 重定位固件卷并进入 UEFI。

> **关键**：UEFI 是随 `boot.img` 一起加载的，**不需要刷写或占用任何分区**。
> 这与本项目目前的 `fastboot boot` + 自定义 shim 完全同构。

## 2. UEFI → UniXNU

固件卷包含 UniXNU 启动管理器和它的驱动。`UniXNUDxe` 把 `UniXNU.efi` 注册为 `Boot####` 选项，load option 数据为 `unixnu:firmware`。

启动管理器先扫描启动所在卷，再扫描其他卷，寻找 `\EFI\UniXNU\unixnu.toml`。

菜单出现前，UniXNU 会：

- 发布每个 `[[block_slice]]`
- 加载并启动每个 `[[driver]]`
- 安装 ACPI 表
- 合并 `[match]` 条件满足的 kit
- 构造菜单

## 3. UniXNU → DarwinBoot

类型为 `xnu` 的菜单项交给加载器协议：

```c
typedef struct {
    EFI_HANDLE    Volume;
    CONST CHAR16 *KernelPath;
    CONST CHAR16 *BootArgs;
    VOID         *DeviceTree;
    UINTN         DeviceTreeSize;
    VOID         *Ramdisk;
    UINTN         RamdiskSize;
    CONST UNIXNU_LOADER_KEXT *Kexts;
    UINTN         KextCount;
} UNIXNU_LOADER_BOOT_REQUEST;
```

设备树的准备顺序：取固件 DTB / 文件 DTB / 无 DTB → 复制并预留 64 KiB → 应用配置和 kit 的全局补丁 → 应用菜单项补丁 → 最后应用 `[entry.chosen]` 属性 → 安装为 `EFI_DTB_TABLE`。

## 4. DarwinBoot → XNU

DarwinBoot 校验并加载内核：

- 从菜单项目录读取 `kernel.macho`
- 要求 `MH_MAGIC_64`、`CPU_TYPE_ARM64`、`MH_EXECUTE`、一个 `LC_MAIN`、至少一个 `LC_SEGMENT_64`
- 检查入口位于可执行段
- 检查每个段页对齐、不重叠、位于 256 MiB 窗口内
- 读取 `kernel.macho.adt`、`kernel.macho.trustcache` 并校验
- 从 **GOP** 读取 framebuffer 信息
- 分配连续物理窗口，复制段/ADT/trust cache/ramdisk
- 构造 `DARWIN_BOOT_ARGS`，退出 boot services，**关闭 MMU 和缓存**，在 EL1 跳转

`DARWIN_BOOT_ARGS`（匹配 Darwin 23）：

| 偏移 | 字段 |
|---|---|
| 0x00 | revision、version、padding |
| 0x08 | 虚拟基址 |
| 0x10 | 物理基址 |
| 0x18 | 内存大小 |
| 0x20 | 内核数据末端 |
| 0x28 | video，六个 64 位值 |
| 0x60 | 设备树地址 |
| 0x68 | 设备树长度 |
| 0x6c | 命令行，1024 字节 |
| 0x478 | 实际内存大小 |

## 5. XNU → 用户态

XNU 从 `start.s` 开始，安装 bootstrap 页表，进入 C 启动路径，初始化平台专家并发布 IOKit registry。

平台专家匹配 ADT 根节点。通用 `ArmDTPlatform` 要求 `/cpus`、`/arm-io`、`/defaults`、`/options` 和 `/chosen` 中的 CHRP NVRAM proxy。

存储驱动发布 userdata 分区，分区方案发布 `OS` 和 `SystemOS`。XNU 按 UUID 匹配根卷，挂载并启动 `launchd`。

## 失败行为

| 阶段 | 失败 | 结果 |
|---|---|---|
| bootloader | header 不匹配 | 拒绝镜像，设备留在 bootloader |
| 固件 | 无 GOP 或无输入 | UniXNU 以 headless 或文本模式运行 |
| UniXNU | 无配置 | 使用内置配置和可移动加载器 |
| DarwinBoot | 内核/ADT/trust cache 无效 | `EFI_COMPROMISED_DATA`，固件继续运行 |
| XNU | 缺少平台或存储驱动 | panic 日志给出缺失的 provider |
