# v2.3.7 静态适配审阅

官方发行 commit 为 `e2b0e547a18841b7d944850b03c9e605df76720b`，包大小 269039968 字节，SHA-256 为 `237675011b81d60f8473a4f53ad08407e659e4a9fcf0dc571c5c15de579e39e6`。本版仍使用新的官方二进制，未混用旧编码库。

## H.264 注册

`___nvmtl_nvenc_register_block_invoke` 在本版位于 `0x9cf10`。`0x9cf79` 的 `8b0501ef0700` 读取 `g_video_blackwell`；替换为 `e9b100000090`，跳到同函数 `0x9d02f` 的注册路径。原有 Blackwell 成功分支也跳到这一地址。

该变化仍保留 `NVMTL_NO_NVENC`、WindowServer 排除和 VideoToolbox 查找，未触碰 HEVC。新增的 `nvmtl_nvencvt_register` 是另一个函数，由调用者在现有注册后独立调用；本次分支不会跳过它的权限校验。测试不启用 `NVMTL_NVENCVT`，避免外部编码器影响结果。

注册绕过分支本身不是逐卡检查，仍由覆盖工具强制限制系统内恰好一张 NVIDIA 显示卡，PCI `10de:2504`。

## 输入布局

v2.3.7 编码库的大小和完整 SHA 改变，不能使用旧 profile。逐段比较确认所有有文件数据的 Mach-O section（包括完整 `__text`）的偏移、长度、SHA 与 2.3.6 一致。两段完整原始循环仍分别唯一出现在 `0x63aa` 和 `0x64d9`，外层继续地址仍为 `0x6330` 与 `0x6450`，因此复用经审阅的 classic GOB bh2 汇编载荷。

| 文件 | 大小 | 官方输入 SHA-256 | 未签名输出 SHA-256 |
| --- | ---: | --- | --- |
| `NVMTLDriver` | 1533776 | `4744ffd12959a3271cc8a62117200f4e1e88f57f729aadec781db7fc87099047` | `fae146a02e771a15f4e37bbde7fda8751a2cc2a531859dccd2923c86991e7325` |
| `libnvenc_h264.dylib` | 80736 | `9723522487ed86721b776a9fd5cef36d71ebd046f320f17cb234466f5b056e65` | `3741057e1a0664d6ad784138b65b4054f6a1399849a3711d83f41d9f2393195a` |

完整 profile 保存在 [v2.3.7.json](../profiles/v2.3.7.json)，输入大小、完整 SHA、原始指令、Mach-O 可执行段和输出 SHA 全部校验，任一变化即停止。签名后须重新记录二进制和候选包 SHA，不能用上表未签名摘要标识实机文件。

本地针对 2.3.7 实物执行 14 项适配测试全部通过，包含双平面机器码差分、1920×1088、边界 canary 和 clang/objcopy 独立汇编复现。CI 同时测试两版 profile，并在 macOS 构建后对当前固定发行包运行实物适配测试。静态通过不代表实机通过；正式结果需另存探针及光梭报告。
