# mcp-e2studio-server

[![M8ven Score](https://m8ven.ai/badge/mcp/helistana-mcp-e2studio-server-1apw5m)](https://m8ven.ai/mcp/helistana-mcp-e2studio-server-1apw5m)

Renesas e2studio（RA / RX MCU）嵌入式开发的 **MCP (Model Context Protocol) Server**，
让 Claude Code / opencode 等 AI 工具能够自主完成：

> 接线 → 提需求 → AI 写代码 → **编译 → 烧录 → 调试 → 故障诊断** 的完整闭环。

所有工具返回**结构化 JSON**（`structuredContent`），失败时 `isError=true` 并携带
`error`/`error_type` 等字段，便于 AI 精确处理错误。

English docs: [README_EN.md](README_EN.md)

---

## 功能一览（15 个工具 + 2 个资源）

| 类别 | 工具 | 说明 |
|------|------|------|
| 环境 | `discover_tools` | 扫描 e2studio CLI / GCC / GDB / objcopy / make / RFP / J-Link，报告 FOUND/MISSING |
| 工程 | `get_project_info` | 解析工程名、MCU 型号、FSP 版本、源文件数、构建配置目录 |
| 构建 | `build_project` | 用 e2studio-cli 增量构建；返回解析后的错误列表（file/line/column/message） |
| 构建 | `clean_project` | 真清理（仅删 `.o/.d/.elf/.map/.bin/.hex` 等产物，**不重建**） |
| 构建 | `get_build_output` | 读取最近一次构建的原始输出/错误清单 |
| 烧录 | `flash_firmware` | 用 Renesas Flash Programmer CLI 烧录（自动定位工程名/ELF/RFP 设备） |
| 烧录 | `flash_jlink` | 用 J-Link Commander 烧录（需安装 SEGGER J-Link 软件） |
| 调试 | `debug_flash` | 启动 J-Link GDB Server 并烧录 ELF 到目标 |
| 调试 | `debug_run` | 通过 GDB 运行固件指定秒数，超时自动触发 HardFault 寄存器抓取 |
| 调试 | `debug_halt` / `debug_resume` | 挂起 / 恢复 CPU |
| 调试 | `debug_memory_read` | 读取内存（hex dump + ASCII） |
| 调试 | `debug_registers_read` | 读取核心寄存器 + 自动解析 CFSR/HFSR/BFAR 故障寄存器与含义 |
| 调试 | `debug_status` / `debug_stop` | 查看 / 停止 J-Link GDB Server 后台进程 |

资源：

- `e2studio://tools` — 工具发现结果（JSON）
- `e2studio://workspace` — 工作区目录与工程列表

---

## 安装 / 注册

依赖：Python 3.10+，`mcp>=1.0.0`（实测 1.28.1）

```bash
pip install "mcp>=1.0.0"
```

### Claude Code — `.mcp.json`

```json
{
  "mcpServers": {
    "e2studio": {
      "command": "C:\\Users\\<you>\\AppData\\Local\\Programs\\Python\\Python312\\python.exe",
      "args": ["D:\\path\\to\\mcp-e2studio-server\\server.py"]
    }
  }
}
```

### opencode — `opencode.json`

```json
{
  "mcp": {
    "e2studio": {
      "type": "local",
      "command": ["python", "D:\\path\\to\\mcp-e2studio-server\\server.py"],
      "enabled": true
    }
  }
}
```

> 建议 `command` 使用 Python **绝对路径**（避免 PATH 里混入其他版本）。


---

## 配置 `config.json`

将 `config.example.json` 复制为 `config.json` 并修改路径（`config.json` 已被
`.gitignore` 忽略，不会提交到仓库）：

| 键 | 说明 | 默认 |
|----|------|------|
| `e2studio_install` | e2studio 安装目录（含 `e2studio-cli.exe`） | `C:\Renesas\RA\e2studio_v2025-12_fsp_v6.4.0\eclipse` |
| `e2studio_toolchains` | e2studio 自带 GCC 工具链根目录 | 同上目录下的 `toolchains` |
| `rfp_base` | Renesas Flash Programmer 根目录 | `C:\Program Files (x86)\Renesas Electronics\Programming Tools` |
| `workspace` | 工作区目录（资源 `e2studio://workspace` 展示） | `D:\e2_ws_mcp` |
| `jlink_port` | J-Link GDB Server 端口 | `2331` |
| `default_mcu` | 默认 MCU（找不到时兜底） | `""` |
| `gcc_extra_roots` | 额外扫描 arm-none-eabi-gcc 的根目录列表 | 见 config.json |
| `segger_roots` | SEGGER J-Link 安装根目录列表 | `C:\Program Files\SEGGER` 等 |
| `gcc_version_prefs` | GCC 版本优先级（前缀匹配） | `["13.2.rel1", ...]` |

### 环境变量（优先于 config.json）

`E2STUDIO_CLI_PATH`、`ARM_GCC_PATH`、`ARM_GDB_PATH`、`ARM_OBJCOPY_PATH`、
`GNU_MAKE_PATH`、`JLINK_GDB_PATH`、`JLINK_PATH`、`RFP_CLI_PATH`、
`E2STUDIO_WORKSPACE`、`RFP_DEVICE`、`RFP_TOOL_TYPE`。

---

## 前提条件

- **e2studio + FSP**：构建/烧录必需。
- **Renesas Flash Programmer**：`flash_firmware` 必需。
- **SEGGER J-Link 软件**：`debug_*` 与 `flash_jlink` 必需（`discover_tools` 会
  报告 `jlink_gdb`/`jlink` MISSING）。

---

## 测试

```bash
# 单元测试（纯函数，无需硬件/工具链）
python -m unittest test_server -v

# 协议自测（启动 server 做 initialize / tools/list / tools/call / resources/read）
python "C:\Users\32725\AppData\Local\Temp\opencode\mcp_e2studio_handshake_test.py"
```

---

## 备注

- 日志写入 **stderr**（stdout 是 MCP 传输通道，不得污染）。
- 构建/烧录/调试通过全局互斥锁串行化，避免并发冲突。
- J-Link GDB Server 作为后台进程常驻主事件循环，调试结束后用 `debug_stop` 释放。
- Windows 控制台中文乱码仅为 GBK 显示问题，协议传输为 UTF-8。
