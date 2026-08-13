# mcp-e2studio-server

A **Model Context Protocol (MCP) server** for Renesas e2studio (RA / RX MCU)
embedded development, letting AI tools such as Claude Code / opencode close the
full loop autonomously:

> Wiring → Requirement → AI writes code → **Build → Flash → Debug → Fault diagnosis**

Every tool returns **structured JSON** (`structuredContent`); on failure the
result sets `isError=true` and carries fields like `error` / `error_type` so
the AI can handle errors precisely.

中文说明见 [README.md](README.md)。

---

## Features (15 tools + 2 resources)

| Category | Tool | Description |
|----------|------|-------------|
| Environment | `discover_tools` | Scan for e2studio CLI / GCC / GDB / objcopy / make / RFP / J-Link, report FOUND/MISSING |
| Project | `get_project_info` | Parse project name, MCU part number, FSP version, source file count, build config dirs |
| Build | `build_project` | Incremental build via e2studio-cli; returns parsed error list (file/line/column/message) |
| Build | `clean_project` | True clean (removes only `.o/.d/.elf/.map/.bin/.hex` artifacts, **does not rebuild**) |
| Build | `get_build_output` | Read raw output / error list of the last build |
| Flash | `flash_firmware` | Flash via Renesas Flash Programmer CLI (auto-locates project/ELF/RFP device) |
| Flash | `flash_jlink` | Flash via J-Link Commander (requires SEGGER J-Link software) |
| Debug | `debug_flash` | Start J-Link GDB Server and flash the ELF to target |
| Debug | `debug_run` | Run firmware via GDB for N seconds; on timeout captures HardFault registers automatically |
| Debug | `debug_halt` / `debug_resume` | Halt / resume the CPU |
| Debug | `debug_memory_read` | Read memory (hex dump + ASCII) |
| Debug | `debug_registers_read` | Read core registers + auto-parse CFSR/HFSR/BFAR fault registers with meanings |
| Debug | `debug_status` / `debug_stop` | Inspect / stop the background J-Link GDB Server process |

Resources:

- `e2studio://tools` — tool discovery result (JSON)
- `e2studio://workspace` — workspace directory and project list

---

## Install / Register

Requires Python 3.10+, `mcp>=1.0.0` (tested with 1.28.1)

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
      "enabled": true,
      "environment": { "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

> It is recommended to use the **absolute path** of your Python interpreter in
> `command` to avoid mixing in other versions from PATH.

---

## Configuration

Copy `config.example.json` to `config.json` and adjust the paths (config.json
is gitignored and never committed):

| Key | Description | Default |
|-----|-------------|---------|
| `e2studio_install` | e2studio install dir (contains `e2studio-cli.exe`) | `C:\Renesas\RA\e2studio_v2025-12_fsp_v6.4.0\eclipse` |
| `e2studio_toolchains` | GCC toolchain root bundled with e2studio | `toolchains` under the install dir |
| `rfp_base` | Renesas Flash Programmer root | `C:\Program Files (x86)\Renesas Electronics\Programming Tools` |
| `workspace` | Workspace directory (shown by `e2studio://workspace`) | `D:\e2_ws_mcp` |
| `jlink_port` | J-Link GDB Server port | `2331` |
| `default_mcu` | Fallback MCU when none can be detected | `""` |
| `gcc_extra_roots` | Extra roots scanned for arm-none-eabi-gcc | see config.example.json |
| `segger_roots` | SEGGER J-Link install roots | `C:\Program Files\SEGGER` etc. |
| `gcc_version_prefs` | GCC version priority (prefix match) | `["13.2.rel1", ...]` |

### Environment variables (take priority over config.json)

`E2STUDIO_CLI_PATH`, `ARM_GCC_PATH`, `ARM_GDB_PATH`, `ARM_OBJCOPY_PATH`,
`GNU_MAKE_PATH`, `JLINK_GDB_PATH`, `JLINK_PATH`, `RFP_CLI_PATH`,
`E2STUDIO_WORKSPACE`, `RFP_DEVICE`, `RFP_TOOL_TYPE`.

---

## Prerequisites

- **e2studio + FSP**: required for build/flash.
- **Renesas Flash Programmer**: required for `flash_firmware`.
- **SEGGER J-Link software**: required for `debug_*` and `flash_jlink`
  (`discover_tools` reports `jlink_gdb`/`jlink` as MISSING otherwise).

---

## Tests

```bash
# Unit tests (pure functions, no hardware/toolchain required)
python -m unittest test_server -v
```

---

## Notes

- Logs go to **stderr** (stdout is the MCP transport channel and must not be polluted).
- Build/flash/debug operations are serialized by a global mutex to avoid conflicts.
- The J-Link GDB Server runs as a background process managed by the main event
  loop; call `debug_stop` to release it after a debug session.
- Garbled Chinese in the Windows console is only a GBK display issue; the
  protocol transport is UTF-8.
