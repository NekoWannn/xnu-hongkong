# 路线决策：不修 SMP，转向 UEFI（2026-10-21）

## 决策

在 XNU 直启（ABL → 自制 shim）路线上**不再投入** SMP 支持（`cpus=1` 保持现状），
优先转向 **UEFI + BootShim + UniXNU/DarwinBoot** 路线。

## 理由

### 1. 投入会白费

SMP 的修目标是"让 XNU 在 ABL 直启路径下用上 8 核"。迁到 UEFI 后，
这套自制 shim 与内核初始化路径大多退役，修好的部分会被丢掉。

### 2. 根源不在 XNU

`PSCI CPU_ON` 返回 `-4`（ALREADY_ON）是 **Gunyah（EL2）** 的行为，不是 XNU 的缺陷。
要修需弄清 Gunyah 的 vCPU 模型（是否把 8 个 vCPU 全部置 on、如何投递启动请求），
属于逆向课题，不确定性高。

### 3. UEFI 路线同样要面对，但方式更可控

Mu-Silicium 有成熟的从核启动实现（trampoline + QCOM SCM），
且**菜单阶段本身不需要多核**，可以在固件跑起来之后再单独攻 SMP。

### 4. 单核不是死路

单核下已完成"内核 → 根文件系统 → PID 1 → 用户态输出"全链路（见
`milestone-userspace.md`）。iOS 用户态在单核下可启动，只是慢。

## 优先级

```
现在：UEFI 路线（提取 DXE 驱动 → 拼固件卷 → BootShim → 菜单）
之后：UEFI 下若能多核，SMP 问题自然解决
兜底：XNU 单核（已验证可用，tag userspace-v1）
```

## 唯一需要留意

若 UEFI 起来后发现**同样**卡在多核启动，说明 Gunyah 的限制是全局的。
届时把 `cpus=1` 作为临时手段带过去即可，成本很低。
