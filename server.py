"""
MCP Server for Renesas e2studio automation.
Controls build, flashing, and debugging via e2studio CLI + RFP CLI + J-Link + GCC toolchain.

Design notes:
- All tool handlers are async and run on the main event loop (no asyncio.run nesting).
- A global asyncio.Lock serializes build/flash/debug operations to protect shared
  resources (J-Link port, workspace, temp hex files).
- Tools return structured dicts; the MCP SDK exposes them as both structuredContent
  and readable JSON text. Failures return CallToolResult with isError=True.
- Logging goes to stderr (stdout is the MCP transport and must stay clean).
"""

import asyncio
import glob
import json
import logging
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, Resource, TextContent, Tool

logger = logging.getLogger("e2studio-server")

server = Server("e2studio-server")

# ── configuration ──────────────────────────────────────────────────────────

CONFIG_FILE = Path(__file__).resolve().parent / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "e2studio_install": r"C:\Renesas\RA\e2studio_v2025-12_fsp_v6.4.0\eclipse",
    "e2studio_toolchains": r"C:\Renesas\RA\e2studio_v2025-12_fsp_v6.4.0\toolchains",
    "rfp_base": r"C:\Program Files (x86)\Renesas Electronics\Programming Tools",
    "workspace": r"D:\e2_ws_mcp",
    "jlink_port": 2331,
    "default_mcu": "",
    # Extra roots scanned for the Arm GNU toolchain when not found in e2studio toolchains
    "gcc_extra_roots": [r"C:\Program Files (x86)\Arm GNU Toolchain arm-none-eabi"],
    "segger_roots": [r"C:\Program Files\SEGGER", r"C:\Program Files (x86)\SEGGER"],
    "gcc_version_prefs": ["13.2.rel1", "12.3.rel1", "10.3.rel1"],
}


def _load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.is_file():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg.update(data)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("config.json 解析失败，使用默认配置: %s", e)
    return cfg


config = _load_config()

# ── tool discovery ─────────────────────────────────────────────────────────

_tools_cache: dict[str, str | None] = {}


def _discover_tools(force_refresh: bool = False) -> dict[str, str | None]:
    """Scan known install locations for e2studio CLI, GCC, GDB, J-Link, make, RFP."""
    global _tools_cache
    if _tools_cache and not force_refresh:
        return _tools_cache

    cache: dict[str, str | None] = {}
    e2_install = config.get("e2studio_install", "")
    e2_toolchains = config.get("e2studio_toolchains", "")
    rfp_base = config.get("rfp_base", "")
    segger_roots = config.get("segger_roots", [])
    gcc_extra_roots = config.get("gcc_extra_roots", [])

    def env_or(key: str, *candidates: str) -> str | None:
        env_val = os.environ.get(key)
        if env_val and os.path.isfile(env_val):
            return env_val
        for c in candidates:
            if c and os.path.isfile(c):
                return c
        return None

    # ── e2studio CLI ──
    cache["e2studio_cli"] = env_or("E2STUDIO_CLI_PATH",
                                   os.path.join(e2_install, "e2studio-cli.exe"))

    # ── GCC / GDB / objcopy (arm-none-eabi) ──
    gcc_bins: list[str] = []
    for base in gcc_extra_roots:
        gcc_bins += glob.glob(os.path.join(base, "*", "bin", "arm-none-eabi-gcc.exe"))
    gcc_bins += glob.glob(os.path.join(e2_toolchains, "gcc_arm", "*", "bin", "arm-none-eabi-gcc.exe"))
    # Prefer configured versions, then whatever exists
    gcc_bins.sort(key=lambda p: next((i for i, v in enumerate(config.get("gcc_version_prefs", []))
                                      if v in p), 99))
    gcc = next((b for b in gcc_bins if os.path.isfile(b)), None)
    cache["gcc"] = env_or("ARM_GCC_PATH", gcc)

    gdb_candidates = [b.replace("arm-none-eabi-gcc.exe", "arm-none-eabi-gdb.exe")
                      for b in gcc_bins if os.path.isfile(b)]
    cache["gdb"] = env_or("ARM_GDB_PATH", next((g for g in gdb_candidates), None))

    objcopy_candidates = [b.replace("arm-none-eabi-gcc.exe", "arm-none-eabi-objcopy.exe")
                          for b in gcc_bins if os.path.isfile(b)]
    cache["objcopy"] = env_or("ARM_OBJCOPY_PATH", next((o for o in objcopy_candidates), None))

    # ── GNU Make (bundled with e2studio) ──
    make_matches = glob.glob(os.path.join(
        e2_install, "plugins",
        "com.renesas.ide.exttools.gnumake.win32.x86_64_*", "mk", "make.exe"))
    cache["make"] = env_or("GNU_MAKE_PATH", make_matches[0] if make_matches else None)

    # ── J-Link GDB Server CLI / Commander ──
    jlink_gdb = jlink = None
    for root in segger_roots:
        if not os.path.isdir(root):
            continue
        if jlink_gdb is None:
            found = glob.glob(os.path.join(root, "JLink*", "JLinkGDBServerCL.exe"))
            jlink_gdb = found[0] if found else None
        if jlink is None:
            found = glob.glob(os.path.join(root, "JLink*", "JLink.exe"))
            jlink = found[0] if found else None
    cache["jlink_gdb"] = env_or("JLINK_GDB_PATH", jlink_gdb)
    cache["jlink"] = env_or("JLINK_PATH", jlink)

    # ── RFP CLI ──
    rfp_candidates = [os.path.join(rfp_base, "Renesas Flash Programmer V3.19", "rfp-cli.exe")]
    for version_dir in glob.glob(os.path.join(rfp_base, "Renesas Flash Programmer V3.*")):
        rfp_candidates.append(os.path.join(version_dir, "rfp-cli.exe"))
    cache["rfp_cli"] = env_or("RFP_CLI_PATH", *rfp_candidates)

    _tools_cache = cache
    return cache


def _get_tool(tool: str) -> str:
    """Get path to a specific tool, checking env var first, then discovery."""
    env_map = {
        "e2studio_cli": "E2STUDIO_CLI_PATH",
        "gcc": "ARM_GCC_PATH",
        "gdb": "ARM_GDB_PATH",
        "objcopy": "ARM_OBJCOPY_PATH",
        "make": "GNU_MAKE_PATH",
        "jlink_gdb": "JLINK_GDB_PATH",
        "jlink": "JLINK_PATH",
        "rfp_cli": "RFP_CLI_PATH",
    }
    env_key = env_map.get(tool)
    if env_key:
        env_val = os.environ.get(env_key)
        if env_val and os.path.exists(env_val):
            return env_val

    discovered = _discover_tools().get(tool)
    if discovered:
        return discovered

    raise ToolError(
        f"未找到 {tool}。请设置环境变量 {env_key} 指向可执行文件路径，"
        f"或修改 config.json 中的路径配置。",
        tool=tool,
    )


# ── errors ─────────────────────────────────────────────────────────────────

class ToolError(Exception):
    """Controlled tool failure carrying a structured payload."""

    def __init__(self, message: str, **structured: Any):
        super().__init__(message)
        self.message = message
        self.structured = structured


def _error_result(message: str, **structured: Any) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(
            {"success": False, "error": message, **structured}, ensure_ascii=False, indent=2))],
        structuredContent={"success": False, "error": message, **structured},
        isError=True,
    )


# ── helpers ────────────────────────────────────────────────────────────────

def _locale_decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("gbk")
        except UnicodeDecodeError:
            return data.decode("utf-8", errors="replace")


async def _run(cmd: list[str], timeout: int = 120, cwd: str | None = None) -> tuple[int, str, str]:
    """Run a command asynchronously, return (returncode, stdout, stderr)."""
    logger.info("run: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ToolError(f"命令超时（{timeout}s）: {' '.join(cmd)}")
    return proc.returncode, _locale_decode(stdout), _locale_decode(stderr)


# ── project / MCU parsing ─────────────────────────────────────────────────

def _find_elf(project_path: str, config_name: str = "Debug") -> str | None:
    """Find built .elf file recursively under project output directory."""
    base = os.path.join(project_path, config_name)
    if not os.path.isdir(base):
        return None
    matches = sorted(glob.glob(os.path.join(base, "**", "*.elf"), recursive=True),
                     key=lambda p: p.count(os.sep))
    return matches[0] if matches else None


def _find_project_name(project_path: str) -> str:
    """Extract project name from .project XML."""
    project_file = os.path.join(project_path, ".project")
    if os.path.isfile(project_file):
        try:
            tree = ET.parse(project_file)
            name_el = tree.getroot().find("name")
            if name_el is not None and name_el.text:
                return name_el.text.strip()
        except ET.ParseError:
            pass
    return os.path.basename(project_path)


def _iter_scfg(project_path: str):
    files = glob.glob(os.path.join(project_path, "*.scfg"))
    files.extend(glob.glob(os.path.join(project_path, "**", "*.scfg"), recursive=True))
    for scfg in files:
        try:
            tree = ET.parse(scfg)
        except (ET.ParseError, OSError):
            continue
        yield scfg, tree.getroot()


def _detect_mcu(project_path: str) -> str | None:
    """Detect Renesas MCU from project files (.scfg or .cproject)."""
    for _f, root in _iter_scfg(project_path):
        for el in root.iter():
            tag = el.tag.lower()
            if "device" in tag:
                if el.text and el.text.strip().upper().startswith("R7F"):
                    return el.text.strip()
            for attr_val in el.attrib.values():
                if isinstance(attr_val, str) and re.match(r"R7F[AS]\d", attr_val.strip().upper()):
                    return attr_val.strip()

    cproject = os.path.join(project_path, ".cproject")
    if os.path.isfile(cproject):
        try:
            tree = ET.parse(cproject)
            text = ET.tostring(tree.getroot(), encoding="unicode")
            m = re.search(r'R7F[AS]\d[A-Z]\d[A-Z]{1,3}', text)
            if m:
                return m.group(0)
        except ET.ParseError:
            pass
    return None


def _detect_fsp_version(project_path: str) -> str | None:
    """Extract FSP version (e.g. 4.6.0 / 6.4.0) from .scfg or .cproject."""
    version_re = re.compile(r"\d+\.\d+\.\d+")
    for _f, root in _iter_scfg(project_path):
        # FSP version lives on the <fsp version="..."> element
        for el in root.iter():
            if "fsp" in el.tag.lower():
                v = el.attrib.get("version")
                if v and version_re.match(v):
                    return version_re.match(v).group(0)
                for k, av in el.attrib.items():
                    if "version" in k.lower() and re.match(r"\d+\.\d+\.\d+", av):
                        return re.match(r"\d+\.\d+\.\d+", av).group(0)
    cproject = os.path.join(project_path, ".cproject")
    if os.path.isfile(cproject):
        try:
            text = Path(cproject).read_text(encoding="utf-8", errors="replace")
            m = re.search(r'fsp[_-]?version["\s:=]+(\d+\.\d+\.\d+)', text, re.I)
            if m:
                return m.group(1)
        except OSError:
            pass
    return None


def _mcu_to_jlink_device(mcu: str) -> str | None:
    """Convert Renesas RA part number to J-Link device name (strip package suffix)."""
    if not mcu:
        return None
    m = re.match(r"(R7F[AS]\d[A-Z]\d[A-Z]{1,2})", mcu.strip().upper())
    return m.group(1) if m else mcu


def _mcu_to_rfp_device(mcu: str) -> str | None:
    """RFP CLI accepts the full RA part number."""
    if not mcu:
        return None
    return mcu.strip().upper()


def _parse_build_errors(stderr: str, stdout: str = "") -> list[dict[str, Any]]:
    """Extract GCC/Renesas error/warning lines as structured records."""
    records: list[dict[str, Any]] = []
    lines = list(stderr.splitlines()) + list(stdout.splitlines())
    seen: set[str] = set()
    line_re = re.compile(r"^(.*?):(\d+)(?::(\d+))?:\s*(fatal\s+error|error|warning):\s*(.*)$")

    for line in lines:
        line = line.rstrip("\r")
        stripped = line.strip()
        if not stripped:
            continue
        m = line_re.match(stripped)
        if m:
            fname, lineno, colno, level, message = m.groups()
            key = f"{fname}:{lineno}:{colno}:{level}"
            if key in seen:
                continue
            seen.add(key)
            records.append({
                "type": "fatal" if "fatal" in level else (level),
                "file": os.path.normpath(fname),
                "line": int(lineno),
                "column": int(colno) if colno else None,
                "message": message.strip(),
                "raw": stripped,
            })
            continue
        lower = stripped.lower()
        if "undefined reference" in lower:
            if stripped in seen:
                continue
            seen.add(stripped)
            records.append({"type": "error", "file": None, "line": None,
                            "column": None, "message": stripped, "raw": stripped})
        elif "cannot find" in stripped and ("-l" in stripped or ".a" in stripped):
            records.append({"type": "error", "file": None, "line": None,
                            "column": None, "message": stripped, "raw": stripped})
        elif "overflow" in lower and ("section" in lower or "region" in lower):
            records.append({"type": "error", "file": None, "line": None,
                            "column": None, "message": stripped, "raw": stripped})

    return records


def _clean_artifacts(project_path: str, config_name: str) -> int:
    """Delete build artifacts for a config. Returns number of files removed."""
    out_dir = os.path.join(project_path, config_name)
    removed = 0
    if not os.path.isdir(out_dir):
        return 0
    patterns = ["*.o", "*.d", "*.elf", "*.bin", "*.hex", "*.map", "*.lst", "*.su"]
    for pat in patterns:
        for f in glob.glob(os.path.join(out_dir, "**", pat), recursive=True):
            try:
                os.remove(f)
                removed += 1
            except OSError as e:
                logger.warning("clean: 无法删除 %s: %s", f, e)
    return removed


def _parse_fault_registers(gdb_output: str) -> dict[str, Any]:
    """Parse Cortex-M fault registers from GDB output into a structured diagnosis."""
    result: dict[str, Any] = {"faulted": True}

    def reg(name: str) -> str | None:
        m = re.search(rf"^{name}\s+(0x[0-9a-fA-F]+)", gdb_output, re.M)
        return m.group(1) if m else None

    pc, lr, sp = reg("pc"), reg("lr"), reg("sp")
    if pc:
        result["pc"] = pc
    if lr:
        result["lr"] = lr
    if sp:
        result["sp"] = sp

    def mmio(addr: str) -> str | None:
        m = re.search(re.escape(addr) + r"\b.*?:\s*(0x[0-9a-fA-F]+)",
                      gdb_output, re.IGNORECASE)
        return m.group(1) if m else None

    cfsr = mmio("0xE000ED28")
    if cfsr:
        val = int(cfsr, 16)
        flags: dict[str, list[str]] = {}

        ufsr = val & 0xFFFF
        u_flags = []
        if ufsr & (1 << 0): u_flags.append("UNDEFINSTR: 执行了未定义指令")
        if ufsr & (1 << 1): u_flags.append("INVSTATE: 非法指令状态(EPSR)，可能跳转到了数据段")
        if ufsr & (1 << 2): u_flags.append("INVPC: 非法PC加载")
        if ufsr & (1 << 3): u_flags.append("NOCP: 使用了禁用的协处理器")
        if ufsr & (1 << 8): u_flags.append("UNALIGNED: 非对齐内存访问")
        if ufsr & (1 << 9): u_flags.append("DIVBYZERO: 除零错误")
        if u_flags:
            flags["usage_fault"] = u_flags

        bfsr = (val >> 8) & 0xFF
        b_flags = []
        if bfsr & (1 << 0): b_flags.append("IBUSERR: 取指令时总线错误（PC指向无效地址）")
        if bfsr & (1 << 1): b_flags.append("PRECISERR: 精确数据总线错误（地址存在但不可访问）")
        if bfsr & (1 << 2): b_flags.append("IMPRECISERR: 不精确数据总线错误（异步发生，需查BFAR）")
        if bfsr & (1 << 3): b_flags.append("UNSTKERR: 异常返回时出栈错误")
        if bfsr & (1 << 4): b_flags.append("STKERR: 异常入栈错误")
        if bfsr & (1 << 5): b_flags.append("LSPERR: 浮点惰性状态保存错误")
        if b_flags:
            flags["bus_fault"] = b_flags

        mmsr = (val >> 24) & 0xFF
        m_flags = []
        if mmsr & (1 << 0): m_flags.append("IACCVIOL: 尝试从不可执行地址取指令")
        if mmsr & (1 << 1): m_flags.append("DACCVIOL: 非法数据访问（如访问未映射/受保护内存）")
        if mmsr & (1 << 3): m_flags.append("MUNSTKERR: 出栈时MPU访问违规")
        if mmsr & (1 << 4): m_flags.append("MSTKERR: 入栈时MPU访问违规")
        if mmsr & (1 << 5): m_flags.append("MLSPERR: 浮点惰性状态MPU违规")
        if m_flags:
            flags["mem_manage"] = m_flags

        result["cfsr"] = {"value": cfsr, "flags": flags}

    hfsr = mmio("0xE000ED2C")
    if hfsr:
        val = int(hfsr, 16)
        h_flags = []
        if val & (1 << 30):
            h_flags.append("FORCED: 硬故障由其他故障升级而来（优先排查上面的 UsageFault/BusFault/MemManage）")
        if val & (1 << 1):
            h_flags.append("VECTBL: 向量表读取失败")
        result["hfsr"] = {"value": hfsr, "flags": h_flags}

    bfar = mmio("0xE000ED38")
    if bfar and bfar not in ("0x00000000", "0xE000ED38"):
        result["bfar"] = bfar

    bt = re.findall(r"^#\d+\s+.*$", gdb_output, re.M)
    if bt:
        result["backtrace"] = bt[:10]

    if len(result) <= 1:
        result["parsed"] = False
        result["raw"] = gdb_output[-2000:]
    else:
        result["parsed"] = True
    return result


def _parse_map_file(map_path: str) -> dict[str, Any]:
    """Parse a GNU ld map file for flash/RAM usage (heuristic)."""
    try:
        text = Path(map_path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise ToolError(f"无法读取 map 文件: {e}", map_path=map_path)

    result: dict[str, Any] = {"map_file": map_path, "approximate": True}
    flash_used = ram_used = 0

    # "Memory Configuration" region sizes
    mem_cfg = re.search(
        r"Memory Configuration\s*\n.*?\n((?:\w+\s+0x[0-9a-fA-F]+\s+0x[0-9a-fA-F]+\s*\n)+)",
        text, re.S)
    if mem_cfg:
        regions = re.findall(r"^(\w+)\s+0x[0-9a-fA-F]+\s+(0x[0-9a-fA-F]+)\s*$",
                             mem_cfg.group(1), re.M)
        for name, size in regions:
            result.setdefault("regions", {})[name] = int(size, 16)

    # Section sizes:  .name  0xaddr  0xsize  ...
    sec_re = re.compile(r"^\.(\w+)\s+0x[0-9a-fA-F]+\s+(0x[0-9a-fA-F]+)\s", re.M)
    for m in sec_re.finditer(text):
        name, size = m.group(1), int(m.group(2), 16)
        if name in ("text", "rodata", "data"):
            flash_used += size
        if name in ("data", "bss"):
            ram_used += size

    result["flash_used"] = flash_used
    result["ram_used"] = ram_used

    overflow = re.search(r"region\s*[`']?(\w+)[`']?\s+overflowed by\s+([0-9]+)\s+bytes", text)
    if overflow:
        result["overflow"] = {"region": overflow.group(1), "bytes": int(overflow.group(2))}

    return result


# ── concurrency guard ──────────────────────────────────────────────────────

_task_lock = asyncio.Lock()


# ── J-Link debug infrastructure (main event loop) ─────────────────────────

_jlink_proc: asyncio.subprocess.Process | None = None
_jlink_gdb_port: int = 2331
_jlink_device: str = ""


async def _start_jlink_gdb(device: str) -> int:
    """Start J-Link GDB Server as a background process on the main loop."""
    global _jlink_proc, _jlink_gdb_port, _jlink_device

    if _jlink_proc is not None and _jlink_proc.returncode is None:
        if _jlink_device == device:
            return _jlink_gdb_port
        await _stop_jlink_gdb()

    jlink_gdb = _get_tool("jlink_gdb")
    cmd = [
        jlink_gdb,
        "-device", device,
        "-if", "SWD",
        "-speed", "4000",
        "-port", str(_jlink_gdb_port),
        "-silent",
        "-nogui",
    ]
    logger.info("start J-Link GDB Server: %s", " ".join(cmd))
    _jlink_proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _jlink_device = device
    await asyncio.sleep(1.5)
    return _jlink_gdb_port


async def _stop_jlink_gdb():
    """Kill the J-Link GDB Server background process."""
    global _jlink_proc, _jlink_device
    if _jlink_proc is not None:
        try:
            _jlink_proc.kill()
            await asyncio.wait_for(_jlink_proc.wait(), timeout=5)
        except Exception as e:
            logger.warning("stop J-Link GDB Server: %s", e)
        _jlink_proc = None
        _jlink_device = ""


def _jlink_running() -> bool:
    return _jlink_proc is not None and _jlink_proc.returncode is None


def _require_jlink() -> int:
    if not _jlink_running():
        raise ToolError("J-Link GDB Server 未在运行。请先调用 debug_run 启动调试会话。")
    return _jlink_gdb_port


async def _gdb_run(commands: list[str], elf_path: str = "", timeout: int = 30) -> str:
    """Run GDB in batch mode. Returns combined stdout+stderr."""
    gdb = _get_tool("gdb")
    fd, script_path = tempfile.mkstemp(suffix=".gdb", prefix="gdb_cmd_")
    try:
        script = "\n".join(commands)
        os.write(fd, script.encode("ascii", errors="replace"))
        os.close(fd)

        cmd = [gdb, "-batch", "-x", script_path]
        if elf_path:
            cmd.append(elf_path)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise ToolError(f"GDB 命令超时（{timeout}s）")

        return _locale_decode(stdout) + "\n" + _locale_decode(stderr)
    finally:
        if os.path.exists(script_path):
            try:
                os.remove(script_path)
            except OSError:
                pass


# ── RFP helpers ───────────────────────────────────────────────────────────

async def _detect_rfp_tool(rfp_cli: str, device: str) -> str | None:
    """Auto-detect connected programming tool via RFP CLI -list-tools."""
    if not device:
        return None
    cmd = [rfp_cli, "-d", device, "-lt", "-nologo", "-noquery"]
    try:
        returncode, stdout, stderr = await _run(cmd, timeout=15)
        if returncode == 0:
            output = stdout + "\n" + stderr
            for tool_type in ["e2l", "e2", "e1", "e20", "jlink", "usb"]:
                if tool_type.upper() in output.upper():
                    return tool_type
    except ToolError:
        pass
    return None


def _parse_rfp_error(output: str) -> str:
    """Extract relevant error messages from RFP CLI output."""
    errors = [ln.strip() for ln in output.splitlines() if ln.strip() and
              ("error" in ln.lower() or "fail" in ln.lower())]
    return "\n".join(errors[:15]) if errors else output[-1000:]


def _resolve_device(device: str, project_path: str, extra_path: str = "") -> str:
    """Resolve MCU from param -> env -> project -> directory of firmware."""
    mcu = device or config.get("default_mcu", "") or os.environ.get("RFP_DEVICE", "") \
        or os.environ.get("RENESAS_MCU", "")
    if not mcu and project_path:
        mcu = _detect_mcu(project_path) or ""
    if not mcu and extra_path:
        mcu = _detect_mcu(extra_path) or ""
    return mcu


def _resolve_tool(tool: str) -> str:
    return tool or os.environ.get("RFP_TOOL_TYPE", "")


# ── tool implementations ───────────────────────────────────────────────────

async def tool_discover_tools(refresh: bool = False) -> dict[str, Any]:
    """Scan and report all tool paths."""
    discovery = _discover_tools(force_refresh=bool(refresh))
    items = [
        ("e2studio_cli", "e2studio CLI (e2studio-cli.exe)"),
        ("gcc", "GCC (arm-none-eabi-gcc)"),
        ("gdb", "GDB (arm-none-eabi-gdb)"),
        ("objcopy", "objcopy (arm-none-eabi-objcopy)"),
        ("make", "GNU Make (make.exe)"),
        ("rfp_cli", "RFP CLI (rfp-cli.exe)"),
        ("jlink_gdb", "J-Link GDB Server (JLinkGDBServerCL)"),
        ("jlink", "J-Link Commander (JLink.exe)"),
    ]
    tools: dict[str, Any] = {}
    for key, label in items:
        path = discovery.get(key)
        tools[key] = {"label": label, "path": path, "found": bool(path)}
    return {
        "tools": tools,
        "found": [k for k, v in tools.items() if v["found"]],
        "missing": [k for k, v in tools.items() if not v["found"]],
        "config_file": str(CONFIG_FILE),
    }


async def tool_get_project_info(project_path: str) -> dict[str, Any]:
    """Read project metadata from .cproject and .scfg."""
    if not os.path.isdir(project_path):
        raise ToolError(f"工程路径不存在: {project_path}", project_path=project_path)

    info: dict[str, Any] = {
        "name": os.path.basename(project_path),
        "project_path": project_path,
    }

    cproject = os.path.join(project_path, ".cproject")
    configs: list[str] = []
    if os.path.isfile(cproject):
        try:
            tree = ET.parse(cproject)
            root = tree.getroot()
            for cc in root.iter("configuration"):
                name = cc.get("name")
                if name:
                    configs.append(name)
            for tool in root.iter("tool"):
                sc = tool.get("superClass", "")
                if "clang" in sc.lower() or "llvm" in sc.lower():
                    info["compiler"] = "LLVM/Clang"
                    break
                if "gnu.c.compiler" in sc or "gcc.compiler" in sc:
                    info["compiler"] = "arm-none-eabi-gcc"
                    break
        except ET.ParseError:
            info["cproject_parse_error"] = True
    if configs:
        info["configs"] = configs

    scfg_files = glob.glob(os.path.join(project_path, "*.scfg"))
    scfg_files.extend(glob.glob(os.path.join(project_path, "**", "*.scfg"), recursive=True))
    if scfg_files:
        info["config_file"] = os.path.basename(scfg_files[0])

    mcu = _detect_mcu(project_path)
    if mcu:
        info["mcu"] = mcu

    fsp = _detect_fsp_version(project_path)
    if fsp:
        info["fsp_version"] = fsp

    src_files = []
    for ext in ["*.c", "*.cpp", "*.h", "*.s", "*.S"]:
        src_files.extend(glob.glob(os.path.join(project_path, "**", ext), recursive=True))
    info["src_file_count"] = len(src_files)

    return info


async def tool_build_project(project_path: str, config_name: str = "Debug",
                             clean: bool = True) -> dict[str, Any]:
    """Build a Renesas RA project using e2studio CLI."""
    async with _task_lock:
        if not os.path.isdir(project_path):
            raise ToolError(f"工程路径不存在: {project_path}", project_path=project_path)

        e2studio_cli = _get_tool("e2studio_cli")
        workspace = config.get("workspace") or os.environ.get("E2STUDIO_WORKSPACE") or ""
        if not workspace:
            workspace = tempfile.mkdtemp(prefix="e2studio_ws_")
        os.makedirs(workspace, exist_ok=True)

        project_name = _find_project_name(project_path)

        # Step 1: Import project into workspace
        import_cmd = [e2studio_cli, "-data", workspace, "project", "import", project_path]
        import_rc, _import_out, import_err = await _run(import_cmd, timeout=60)
        if import_rc != 0:
            raise ToolError(f"工程导入失败:\n{import_err[:1000]}", project_path=project_path)

        # Step 2: optional clean
        removed = 0
        if clean:
            removed = _clean_artifacts(project_path, config_name)

        # Step 3: Build
        build_cmd = [e2studio_cli, "-data", workspace,
                     "project", "build", f"{project_name}/{config_name}"]
        returncode, stdout, stderr = await _run(build_cmd, timeout=300)

        if returncode == 0:
            elf = _find_elf(project_path, config_name)
            return {
                "success": True,
                "project": project_name,
                "config": config_name,
                "elf_path": elf,
                "cleaned_files": removed,
            }

        errors = _parse_build_errors(stderr, stdout)
        error_records = [e for e in errors if e["type"] != "warning"]
        raise ToolError(
            f"构建失败，{len(error_records) or '未知'} 个错误",
            success=False,
            project=project_name,
            config=config_name,
            error_count=len(error_records),
            errors=error_records,
            raw_stderr=stderr[-3000:],
            raw_stdout=stdout[-3000:],
        )


async def tool_clean_project(project_path: str, config_name: str = "Debug") -> dict[str, Any]:
    """Delete build artifacts for a config (does NOT rebuild)."""
    if not os.path.isdir(project_path):
        raise ToolError(f"工程路径不存在: {project_path}", project_path=project_path)
    removed = _clean_artifacts(project_path, config_name)
    return {"success": True, "config": config_name, "removed_files": removed,
            "project_path": project_path}


async def _flash_with_rfp(firmware_path: str, project_path: str, device: str,
                          tool: str, address: str, convert_elf: bool) -> dict[str, Any]:
    """Shared RFP flashing logic. Returns structured result or raises ToolError."""
    if not os.path.isfile(firmware_path):
        raise ToolError(f"固件文件不存在: {firmware_path}", firmware_path=firmware_path)

    rfp_cli = _get_tool("rfp_cli")
    rfp_device = _resolve_device(device, project_path, os.path.dirname(firmware_path))
    if not rfp_device:
        raise ToolError(
            "无法确定 MCU 型号。请通过 device 参数指定（如 R7FA4M1AB），或设置 RFP_DEVICE 环境变量。")

    rfp_tool = _resolve_tool(tool)
    if not rfp_tool:
        rfp_tool = await _detect_rfp_tool(rfp_cli, rfp_device) or ""

    ext = os.path.splitext(firmware_path)[1].lower()
    cmd = [rfp_cli, "-d", rfp_device, "-nologo", "-noquery"]
    if rfp_tool:
        cmd.extend(["-t", rfp_tool])

    tmp_hex = ""
    try:
        if ext == ".elf" and convert_elf:
            objcopy = _get_tool("objcopy")
            tmp_hex = os.path.join(tempfile.gettempdir(),
                                   f"mcp_e2_hex_{os.getpid()}_{os.path.basename(firmware_path).replace('.elf', '.hex')}")
            conv_cmd = [objcopy, "-O", "ihex", firmware_path, tmp_hex]
            rc, _out, err = await _run(conv_cmd, timeout=30)
            if rc != 0:
                raise ToolError(f"objcopy 转换失败:\n{err}", firmware_path=firmware_path)
            target = tmp_hex
        elif ext == ".bin":
            cmd.extend(["-bin", address, firmware_path, "-a"])
            target = firmware_path
        else:
            cmd.extend(["-a", firmware_path])
            target = firmware_path

        if ext == ".elf" and not convert_elf:
            cmd.extend(["-a", firmware_path])
            target = firmware_path

        returncode, stdout, stderr = await _run(cmd, timeout=120)
        output = stdout + "\n" + stderr

        if returncode == 0:
            return {
                "success": True,
                "device": rfp_device,
                "tool": rfp_tool or "auto",
                "firmware": firmware_path,
                "programmer": "rfp_cli",
                "output_tail": stdout[-500:],
            }

        err_text = _parse_rfp_error(output)
        raise ToolError(f"烧录失败（返回码 {returncode}）: {err_text}",
                        success=False, device=rfp_device, firmware=firmware_path)
    finally:
        if tmp_hex and os.path.exists(tmp_hex):
            try:
                os.remove(tmp_hex)
            except OSError:
                pass


async def tool_flash_firmware(firmware_path: str, project_path: str = "",
                              device: str = "", tool: str = "",
                              address: str = "0x00000000") -> dict[str, Any]:
    """Flash firmware to Renesas RA MCU via RFP CLI."""
    async with _task_lock:
        return await _flash_with_rfp(firmware_path, project_path, device, tool, address,
                                     convert_elf=True)


async def tool_debug_flash(elf_path: str, project_path: str = "",
                           device: str = "", tool: str = "") -> dict[str, Any]:
    """Flash .elf firmware via RFP CLI (for debug sessions)."""
    async with _task_lock:
        return await _flash_with_rfp(elf_path, project_path, device, tool, "0x00000000",
                                     convert_elf=True)


async def tool_debug_run(project_path: str, config_name: str = "Debug",
                         timeout: int = 15, device: str = "") -> dict[str, Any]:
    """Flash ELF via RFP CLI, run under J-Link GDB, capture fault registers."""
    async with _task_lock:
        if not os.path.isdir(project_path):
            raise ToolError(f"工程路径不存在: {project_path}", project_path=project_path)
        elf = _find_elf(project_path, config_name)
        if not elf:
            raise ToolError(
                f"未找到 .elf 文件。请先编译工程: {os.path.join(project_path, config_name)}",
                project_path=project_path, config=config_name)

        mcu = _resolve_device(device, project_path)
        if not mcu:
            raise ToolError("无法自动识别 Renesas MCU 型号，请检查 .scfg/.cproject，或通过 device 参数指定。")
        jlink_device = _mcu_to_jlink_device(mcu)
        rfp_device = _mcu_to_rfp_device(mcu)

        # Step 1: flash
        try:
            await _flash_with_rfp(elf, project_path, device, "", "0x00000000", convert_elf=True)
        except ToolError as e:
            raise ToolError(f"烧录失败: {e.message}", **e.structured)

        # Step 2: run under J-Link GDB
        if not jlink_device:
            raise ToolError("烧录成功，但无法启动调试：缺少 J-Link device 名称", device=mcu)

        try:
            gdb_port = await _start_jlink_gdb(jlink_device)
        except ToolError as e:
            raise ToolError(f"烧录成功，但 J-Link GDB Server 无法启动: {e.message}")

        if not _jlink_running():
            raise ToolError("烧录成功，但 J-Link GDB Server 启动失败")

        run_cmds = [
            f"target extended-remote localhost:{gdb_port}",
            "monitor halt",
            "load",
            "break HardFault_Handler",
            "break MemManage_Handler",
            "break BusFault_Handler",
            "break UsageFault_Handler",
            "continue",
            "monitor halt",
        ]
        try:
            run_output = await _gdb_run(run_cmds, elf_path=elf, timeout=timeout + 20)
        except ToolError as e:
            # Timeout during run == firmware ran the full window without hitting a fault
            run_output = ""

        faulted = any(marker in run_output for marker in
                      ["HardFault_Handler", "MemManage_Handler",
                       "BusFault_Handler", "UsageFault_Handler"])

        base = {"success": True, "project": os.path.basename(project_path),
                "config": config_name, "mcu": mcu, "elf_path": elf}
        if faulted:
            diag_cmds = [
                f"target extended-remote localhost:{gdb_port}",
                "monitor halt",
                "info registers pc lr sp xpsr",
                "x/1xw 0xE000ED28",
                "x/1xw 0xE000ED2C",
                "x/1xw 0xE000ED38",
                "bt",
            ]
            try:
                diag_output = await _gdb_run(diag_cmds, timeout=15)
            except ToolError:
                diag_output = ""
            diagnosis = _parse_fault_registers(diag_output)
            return {**base, "faulted": True, "diagnosis": diagnosis}
        return {**base, "faulted": False,
                "ran_seconds": timeout,
                "message": f"固件正常运行（{timeout}s 内无故障触发）"}


async def tool_debug_status() -> dict[str, Any]:
    """Report J-Link GDB Server status."""
    running = _jlink_running()
    return {"running": running, "port": _jlink_gdb_port if running else None,
            "device": _jlink_device if running else None}


async def tool_debug_stop() -> dict[str, Any]:
    """Stop the J-Link GDB Server background process."""
    was_running = _jlink_running()
    await _stop_jlink_gdb()
    return {"stopped": was_running}


async def tool_debug_halt() -> dict[str, Any]:
    """Halt target MCU via J-Link GDB Server."""
    port = _require_jlink()
    gdb_cmds = [f"target extended-remote localhost:{port}", "monitor halt"]
    try:
        output = await _gdb_run(gdb_cmds, timeout=10)
    except ToolError as e:
        raise ToolError(f"暂停超时: {e.message}")
    return {"halted": True, "output_tail": output[:500]}


async def tool_debug_resume() -> dict[str, Any]:
    """Resume target MCU via J-Link GDB Server."""
    port = _require_jlink()
    gdb_cmds = [f"target extended-remote localhost:{port}", "continue"]
    try:
        output = await _gdb_run(gdb_cmds, timeout=10)
    except ToolError as e:
        raise ToolError(f"恢复运行超时: {e.message}")
    return {"resumed": True, "output_tail": output[:500]}


async def tool_debug_memory_read(address: str, count: int = 16, size: int = 4,
                                 elf_path: str = "") -> dict[str, Any]:
    """Read memory from the target via GDB x command."""
    port = _require_jlink()
    fmt = {1: "b", 2: "h", 4: "w"}.get(size, "w")
    gdb_cmds = [
        f"target extended-remote localhost:{port}",
        "monitor halt",
        f"x/{count}{fmt}x {address}",
    ]
    output = await _gdb_run(gdb_cmds, elf_path=elf_path, timeout=10)

    words: list[str] = []
    for m in re.finditer(r"0x[0-9a-fA-F]+\s*<\S+>:\s*((?:0x[0-9a-fA-F]+\s*)+)", output):
        words += m.group(1).split()
    if not words:
        m = re.search(r"0x[0-9a-fA-F]+\s*:\s*((?:0x[0-9a-fA-F]+\s*)+)", output)
        if m:
            words = m.group(1).split()

    if not words:
        raise ToolError("无法解析 GDB 内存读取结果（目标可能未连接）", raw=output[-1000:])

    return {"address": address, "count": len(words), "size_bytes": size,
            "values": words, "output_tail": output[:2000]}


async def tool_debug_registers_read(elf_path: str = "") -> dict[str, Any]:
    """Read core + Cortex-M fault registers from the target."""
    port = _require_jlink()
    gdb_cmds = [
        f"target extended-remote localhost:{port}",
        "monitor halt",
        "info registers pc lr sp xpsr msp psp",
        "x/1xw 0xE000ED28",
        "x/1xw 0xE000ED2C",
        "x/1xw 0xE000ED38",
        "bt",
    ]
    output = await _gdb_run(gdb_cmds, elf_path=elf_path, timeout=10)

    registers: dict[str, str] = {}
    for name in ["pc", "lr", "sp", "xpsr", "msp", "psp"]:
        m = re.search(rf"^{name}\s+(0x[0-9a-fA-F]+)", output, re.M)
        if m:
            registers[name] = m.group(1)

    diagnosis = _parse_fault_registers(output)
    return {"registers": registers, "fault": diagnosis, "output_tail": output[:2000]}


async def tool_flash_jlink(firmware_path: str, device: str = "", project_path: str = "",
                           address: str = "0x00000000") -> dict[str, Any]:
    """Flash firmware via J-Link Commander (alternative to RFP CLI)."""
    async with _task_lock:
        if not os.path.isfile(firmware_path):
            raise ToolError(f"固件文件不存在: {firmware_path}", firmware_path=firmware_path)
        mcu = _resolve_device(device, project_path, os.path.dirname(firmware_path))
        if not mcu:
            raise ToolError("无法确定 MCU 型号。请通过 device 参数指定，或设置 RFP_DEVICE 环境变量。")
        jlink_dev = _mcu_to_jlink_device(mcu)

        jlink = _get_tool("jlink")
        fd, script_path = tempfile.mkstemp(suffix=".jlink", prefix="jlink_cmd_")
        try:
            ext = os.path.splitext(firmware_path)[1].lower()
            load_cmd = f"LoadFile \"{firmware_path}\"" + (f", {address}" if ext == ".bin" else "")
            script = "\n".join([
                f"Device = {jlink_dev}",
                "SelectInterface = SWD",
                "Speed = 4000",
                "Connect",
                load_cmd,
                "Reset",
                "Go",
                "Exit",
            ])
            os.write(fd, script.encode("ascii", errors="replace"))
            os.close(fd)

            cmd = [jlink, "-NoGui", "1", "-ExitOnError", "1",
                   "-CommanderScript", script_path]
            returncode, stdout, stderr = await _run(cmd, timeout=120)
            output = stdout + "\n" + stderr

            if returncode == 0 and ("O.K." in output or "Downloading file" in output):
                return {"success": True, "device": mcu, "firmware": firmware_path,
                        "programmer": "jlink", "output_tail": stdout[-800:]}

            err_tail = _parse_rfp_error(output) or output[-1500:]
            raise ToolError(f"J-Link 烧录失败（返回码 {returncode}）: {err_tail}",
                            success=False, device=mcu, firmware=firmware_path)
        finally:
            if os.path.exists(script_path):
                try:
                    os.remove(script_path)
                except OSError:
                    pass


async def tool_get_build_output(project_path: str, config_name: str = "Debug") -> dict[str, Any]:
    """Report build artifacts and flash/RAM usage from the linker map."""
    if not os.path.isdir(project_path):
        raise ToolError(f"工程路径不存在: {project_path}", project_path=project_path)
    elf = _find_elf(project_path, config_name)
    if not elf:
        raise ToolError(f"未找到 .elf 文件。请先编译工程: {os.path.join(project_path, config_name)}",
                        project_path=project_path, config=config_name)

    result: dict[str, Any] = {"elf_path": elf, "config": config_name,
                              "project": os.path.basename(project_path)}
    map_path = elf.replace(".elf", ".map")
    if os.path.isfile(map_path):
        result["map"] = _parse_map_file(map_path)
    return result


# ── MCP protocol handlers ──────────────────────────────────────────────────

_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "discover_tools",
        "description": "扫描系统中的 e2studio CLI / GCC / GDB / objcopy / GNU Make / RFP CLI / J-Link 安装路径，"
                       "返回每个工具是否存在及完整路径。适用于首次配置排查与安装后验证。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "refresh": {
                    "type": "boolean",
                    "description": "是否强制重新扫描磁盘（默认 false，使用缓存）",
                    "default": False,
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_project_info",
        "description": "读取 Renesas RA 工程元数据：MCU 型号、FSP 版本、构建配置、编译器、源文件统计。"
                       "返回结构化 JSON，供后续 build/debug 调用决定参数。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {
                    "type": "string",
                    "description": "工程目录路径（包含 .project / .cproject / .scfg 的目录）",
                },
            },
            "required": ["project_path"],
        },
    },
    {
        "name": "build_project",
        "description": "编译 Renesas RA 工程（e2studio CLI headless 构建）。失败时 isError=true 并返回"
                       "结构化错误列表 [{file, line, column, message, type}] 便于直接定位修复。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "description": "工程目录路径"},
                "config": {"type": "string", "description": "构建配置（Debug / Release）", "default": "Debug"},
                "clean": {"type": "boolean", "description": "先清理再编译（Clean Build）", "default": True},
            },
            "required": ["project_path"],
        },
    },
    {
        "name": "clean_project",
        "description": "仅删除指定配置的编译产物（.o/.d/.elf/.hex/.bin/.map），不触发重新编译。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "description": "工程目录路径"},
                "config": {"type": "string", "description": "构建配置（Debug / Release）", "default": "Debug"},
            },
            "required": ["project_path"],
        },
    },
    {
        "name": "flash_firmware",
        "description": "通过 Renesas Flash Programmer CLI（RFPV3）烧录固件到 RA 芯片。支持 .elf/.bin/.hex/.mot。"
                       "MCU 可自动从工程识别。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "firmware_path": {"type": "string", "description": "固件文件路径（.elf/.bin/.hex/.mot）"},
                "project_path": {"type": "string", "description": "工程目录路径（用于自动识别 MCU）", "default": ""},
                "device": {"type": "string", "description": "MCU 型号（如 R7FA4M1AB），留空自动检测", "default": ""},
                "tool": {"type": "string", "description": "烧录器类型（e2/e2l/jlink/usb），留空自动检测", "default": ""},
                "address": {"type": "string", "description": "烧录起始地址（仅 .bin 需要）", "default": "0x00000000"},
            },
            "required": ["firmware_path"],
        },
    },
    {
        "name": "debug_flash",
        "description": "将 .elf 固件烧录到芯片（RFP CLI），用于调试前的烧录步骤。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "elf_path": {"type": "string", "description": ".elf 固件完整路径"},
                "project_path": {"type": "string", "description": "工程目录路径（用于自动识别 MCU）", "default": ""},
                "device": {"type": "string", "description": "MCU 型号，留空自动检测", "default": ""},
                "tool": {"type": "string", "description": "烧录器类型，留空自动检测", "default": ""},
            },
            "required": ["elf_path"],
        },
    },
    {
        "name": "debug_run",
        "description": "烧录并运行固件，自动捕获 HardFault/MemManage/BusFault/UsageFault，读取 Cortex-M "
                       "故障寄存器并返回结构化中文诊断（含 PC/LR/CFSR/HFSR/BFAR/backtrace）。"
                       "典型用途：代码改动后一键验证是否触发硬件故障。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "description": "工程目录路径（需已编译，存在 .elf）"},
                "config": {"type": "string", "description": "构建配置", "default": "Debug"},
                "timeout": {"type": "integer", "description": "运行超时秒数（超时未触发故障视为正常）", "default": 15},
                "device": {"type": "string", "description": "MCU 型号（留空自动检测）", "default": ""},
            },
            "required": ["project_path"],
        },
    },
    {
        "name": "debug_status",
        "description": "查看 J-Link GDB Server 运行状态、GDB 端口与目标 MCU。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "debug_stop",
        "description": "停止 J-Link GDB Server 后台进程。调试会话结束后调用以释放端口与连接。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "debug_halt",
        "description": "暂停目标 MCU（需要 J-Link GDB Server 正在运行）。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "debug_resume",
        "description": "恢复目标 MCU 运行（需要 J-Link GDB Server 正在运行且目标已暂停）。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "debug_memory_read",
        "description": "通过 GDB 读取目标内存（x 命令）。返回按字节/半字/字解析的十六进制值列表。"
                       "用于检查变量、外设寄存器、DMA buffer 等。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "address": {"type": "string", "description": "起始地址，如 0x20000000"},
                "count": {"type": "integer", "description": "读取单元数量", "default": 16},
                "size": {"type": "integer", "description": "单元字节数：1=byte, 2=halfword, 4=word", "default": 4},
                "elf_path": {"type": "string", "description": ".elf 路径（可选，用于符号解析）", "default": ""},
            },
            "required": ["address"],
        },
    },
    {
        "name": "debug_registers_read",
        "description": "读取核心寄存器（pc/lr/sp/xpsr/msp/psp）与 Cortex-M 故障寄存器（CFSR/HFSR/BFAR）+ 调用栈。"
                       "用于故障现场分析。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "elf_path": {"type": "string", "description": ".elf 路径（可选，用于符号解析）", "default": ""},
            },
            "required": [],
        },
    },
    {
        "name": "flash_jlink",
        "description": "通过 J-Link Commander 直接烧录固件（RFP 之外的备选烧录路径，依赖 SEGGER 驱动与 J-Link 设备）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "firmware_path": {"type": "string", "description": "固件路径（.hex/.elf/.bin）"},
                "device": {"type": "string", "description": "MCU 型号，留空自动检测", "default": ""},
                "project_path": {"type": "string", "description": "工程目录路径（用于自动识别 MCU）", "default": ""},
                "address": {"type": "string", "description": "烧录地址（仅 .bin 需要）", "default": "0x00000000"},
            },
            "required": ["firmware_path"],
        },
    },
    {
        "name": "get_build_output",
        "description": "报告构建产物：.elf 路径与链接 map 解析出的 Flash/RAM 占用（近似值）。"
                       "用于判断固件体积与资源余量。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_path": {"type": "string", "description": "工程目录路径"},
                "config": {"type": "string", "description": "构建配置", "default": "Debug"},
            },
            "required": ["project_path"],
        },
    },
]

_HANDLERS: dict[str, Any] = {
    "discover_tools": tool_discover_tools,
    "get_project_info": tool_get_project_info,
    "build_project": tool_build_project,
    "clean_project": tool_clean_project,
    "flash_firmware": tool_flash_firmware,
    "debug_flash": tool_debug_flash,
    "debug_run": tool_debug_run,
    "debug_status": tool_debug_status,
    "debug_stop": tool_debug_stop,
    "debug_halt": tool_debug_halt,
    "debug_resume": tool_debug_resume,
    "debug_memory_read": tool_debug_memory_read,
    "debug_registers_read": tool_debug_registers_read,
    "flash_jlink": tool_flash_jlink,
    "get_build_output": tool_get_build_output,
}

_RESOURCE_URI = "e2studio://tools"
_RESOURCE_TEMPLATE_URI = "e2studio://project/{path}"


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [Tool(name=d["name"], description=d["description"], inputSchema=d["inputSchema"])
            for d in _TOOL_DEFS]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> Any:
    handler = _HANDLERS.get(name)
    if handler is None:
        return _error_result(f"未知工具: {name}")
    try:
        return await handler(**arguments)
    except ToolError as e:
        return _error_result(e.message, **e.structured)
    except FileNotFoundError as e:
        logger.exception("tool %s failed: %s", name, e)
        return _error_result(str(e))
    except Exception as e:
        logger.exception("tool %s unexpected error", name)
        return _error_result(f"执行出错: {type(e).__name__}: {e}")


@server.list_resources()
async def list_resources() -> list[Resource]:
    resources = [
        Resource(
            uri=_RESOURCE_URI,
            name="e2studio 工具发现结果",
            description="当前扫描到的 e2studio/GCC/GDB/J-Link/RFP 工具路径",
            mimeType="application/json",
        )
    ]
    ws = config.get("workspace")
    if ws and os.path.isdir(ws):
        resources.append(Resource(
            uri="e2studio://workspace",
            name="e2studio 工作区",
            description=f"e2studio 工作区目录: {ws}",
            mimeType="application/json",
        ))
    return resources


@server.read_resource()
async def read_resource(uri) -> str:
    uri = str(uri)
    if uri == _RESOURCE_URI:
        data = await tool_discover_tools(refresh=False)
        return json.dumps(data, ensure_ascii=False, indent=2)
    if uri == "e2studio://workspace":
        ws = config.get("workspace", "")
        entries = []
        if os.path.isdir(ws):
            entries = sorted(os.listdir(ws))[:500]
        return json.dumps({"workspace": ws, "projects": entries}, ensure_ascii=False, indent=2)
    raise ValueError(f"未知资源: {uri}")


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
