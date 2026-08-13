"""Unit tests for mcp-e2studio-server pure helper functions.

Run:  python -m unittest test_server -v   (from this directory)
No hardware / external tools are required.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402


class TestMcuMapping(unittest.TestCase):
    def test_jlink_device_strips_package(self):
        self.assertEqual(server._mcu_to_jlink_device("R7FA4M1AB3CFP"), "R7FA4M1AB")

    def test_jlink_device_keeps_plain(self):
        self.assertEqual(server._mcu_to_jlink_device("R7FA4M1AB"), "R7FA4M1AB")

    def test_jlink_device_empty(self):
        self.assertIsNone(server._mcu_to_jlink_device(""))

    def test_rfp_device_uppercased(self):
        self.assertEqual(server._mcu_to_rfp_device("r7fa4m1ab"), "R7FA4M1AB")


class TestParseBuildErrors(unittest.TestCase):
    def test_gcc_error_with_position(self):
        out = r"C:\proj\src\main.c:42:10: error: 'FOO' undeclared (first use in this function)"
        records = server._parse_build_errors(out)
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r["type"], "error")
        self.assertEqual(r["file"], r"C:\proj\src\main.c")
        self.assertEqual(r["line"], 42)
        self.assertEqual(r["column"], 10)

    def test_fatal_error(self):
        out = "src/main.c:1:6: fatal error: gcc_main.h: No such file or directory"
        records = server._parse_build_errors(out)
        self.assertEqual(records[0]["type"], "fatal")
        self.assertEqual(records[0]["line"], 1)

    def test_warning_kept(self):
        out = "src/util.c:10:5: warning: unused variable 'x'"
        records = server._parse_build_errors(out)
        self.assertEqual(records[0]["type"], "warning")

    def test_undefined_reference(self):
        out = "/usr/lib/gcc/arm-none-eabi/13.2/../../../../arm-none-eabi/bin/ld: main.o: in function `main':\nmain.c:(.text+0x8): undefined reference to `foo'"
        records = server._parse_build_errors(out)
        self.assertTrue(any(r["type"] == "error" and "undefined reference" in r["message"]
                            for r in records))

    def test_overflow(self):
        out = "region `FLASH' overflowed by 128 bytes"
        records = server._parse_build_errors(out)
        self.assertTrue(any("overflow" in r["message"] for r in records))

    def test_dedupe(self):
        out = "a.c:1:2: error: x\na.c:1:2: error: x\n"
        records = server._parse_build_errors(out)
        self.assertEqual(len(records), 1)


class TestParseFaultRegisters(unittest.TestCase):
    SAMPLE = """\
Breakpoint 1, HardFault_Handler () at C:/proj/src/hal_entry.c:33
pc             0x00000890       0x890
lr             0x20001ffc       536874492
sp             0x20001fe8       536874472
xpsr           0x81000003       2164260867
0xe000ed28 <CFSR>:\t0x00000200
0xe000ed2c <HFSR>:\t0x40000000
0xe000ed38 <BFAR>:\t0x20004000
#0  HardFault_Handler () at C:/proj/src/hal_entry.c:33
#1  0x00000890 in main () at C:/proj/src/main.c:21
"""

    def test_parse_core_registers(self):
        r = server._parse_fault_registers(self.SAMPLE)
        self.assertEqual(r["pc"], "0x00000890")
        self.assertEqual(r["lr"], "0x20001ffc")
        self.assertEqual(r["sp"], "0x20001fe8")

    def test_cfsr_usage_fault(self):
        r = server._parse_fault_registers(self.SAMPLE)
        self.assertEqual(r["cfsr"]["value"], "0x00000200")
        self.assertIn("usage_fault", r["cfsr"]["flags"])
        # 0x100 = DIVBYZERO bit
        self.assertTrue(any("DIVBYZERO" in f for f in r["cfsr"]["flags"]["usage_fault"]))

    def test_hfsr_forced(self):
        r = server._parse_fault_registers(self.SAMPLE)
        self.assertEqual(r["hfsr"]["value"], "0x40000000")
        self.assertTrue(any("FORCED" in f for f in r["hfsr"]["flags"]))

    def test_bfar(self):
        r = server._parse_fault_registers(self.SAMPLE)
        self.assertEqual(r["bfar"], "0x20004000")

    def test_backtrace(self):
        r = server._parse_fault_registers(self.SAMPLE)
        self.assertEqual(len(r["backtrace"]), 2)

    def test_unparseable(self):
        r = server._parse_fault_registers("(gdb) No registers.\n")
        self.assertFalse(r["parsed"])


class TestFindElf(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="e2_test_")
        self.proj = os.path.join(self.root, "proj")
        os.makedirs(os.path.join(self.proj, "Debug", "ra", "fsp"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_recursive_find(self):
        elf = os.path.join(self.proj, "Debug", "ra", "fsp", "proj.elf")
        Path(elf).write_bytes(b"\x7fELF")
        found = server._find_elf(self.proj, "Debug")
        self.assertEqual(found, elf)

    def test_prefers_shallow(self):
        top = os.path.join(self.proj, "Debug", "a.elf")
        deep = os.path.join(self.proj, "Debug", "x", "b.elf")
        Path(top).write_bytes(b"1")
        os.makedirs(os.path.dirname(deep))
        Path(deep).write_bytes(b"2")
        self.assertEqual(server._find_elf(self.proj, "Debug"), top)

    def test_not_found(self):
        self.assertIsNone(server._find_elf(self.proj, "Debug"))

    def test_missing_config_dir(self):
        self.assertIsNone(server._find_elf(self.proj, "Release"))


class TestCleanArtifacts(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="e2_clean_")
        self.proj = os.path.join(self.root, "proj")
        self.cfg = os.path.join(self.proj, "Debug")
        os.makedirs(os.path.join(self.cfg, "sub"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_removes_artifacts_only(self):
        for name in ["a.o", "b.d", "proj.elf", "proj.map", "proj.bin", "proj.hex"]:
            Path(os.path.join(self.cfg, name)).write_bytes(b"x")
        Path(os.path.join(self.cfg, "sub", "c.o")).write_bytes(b"x")
        src = os.path.join(self.proj, "src")
        os.makedirs(src)
        Path(os.path.join(src, "main.c")).write_text("int main(){}")
        removed = server._clean_artifacts(self.proj, "Debug")
        self.assertEqual(removed, 7)
        self.assertTrue(os.path.isfile(os.path.join(src, "main.c")))
        self.assertEqual(len(os.listdir(self.cfg)), 1)  # only sub/ dir left


class TestProjectParsing(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="e2_proj_")
        self.proj = os.path.join(self.root, "MyProj")
        os.makedirs(self.proj)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_find_project_name(self):
        Path(os.path.join(self.proj, ".project")).write_text(
            '<?xml version="1.0"?><projectDescription><name>MyProj</name></projectDescription>',
            encoding="utf-8")
        self.assertEqual(server._find_project_name(self.proj), "MyProj")

    def test_project_name_fallback(self):
        self.assertEqual(server._find_project_name(self.proj), "MyProj")

    def test_detect_mcu_from_scfg(self):
        Path(os.path.join(self.proj, "sample.scfg")).write_text(
            '<config version="1.0.0"><fsp version="6.4.0"><bsp>'
            '<device>R7FA4M1AB3CFP</device></bsp></fsp></config>',
            encoding="utf-8")
        self.assertEqual(server._detect_mcu(self.proj), "R7FA4M1AB3CFP")

    def test_detect_mcu_from_scfg_attr(self):
        Path(os.path.join(self.proj, "sample.scfg")).write_text(
            '<config version="1.0.0"><fsp version="6.4.0">'
            '<bsp><device id="R7FA4M1AB3CFP"/></bsp></fsp></config>',
            encoding="utf-8")
        self.assertEqual(server._detect_mcu(self.proj), "R7FA4M1AB3CFP")

    def test_fsp_version(self):
        Path(os.path.join(self.proj, "sample.scfg")).write_text(
            '<config version="1.0.0"><fsp version="6.4.0"><bsp>'
            '<device>R7FA4M1AB3CFP</device></bsp></fsp></config>',
            encoding="utf-8")
        self.assertEqual(server._detect_fsp_version(self.proj), "6.4.0")

    def test_fsp_version_not_confused_with_config_version(self):
        # root config version="1.0.0" is the file format, must NOT be returned
        Path(os.path.join(self.proj, "sample.scfg")).write_text(
            '<config version="1.0.0"><fsp><bsp>'
            '<device>R7FA4M1AB3CFP</device></bsp></fsp></config>',
            encoding="utf-8")
        self.assertIsNone(server._detect_fsp_version(self.proj))

    def test_detect_mcu_from_cproject(self):
        Path(os.path.join(self.proj, ".cproject")).write_text(
            '<?xml version="1.0"?><cproject><storageModule moduleId="com.renesas.cproject.mcu">'
            '<property id="com.renesas.cproject.mcu.type">R7FA4M1AB</property></storageModule></cproject>',
            encoding="utf-8")
        self.assertEqual(server._detect_mcu(self.proj), "R7FA4M1AB")

    def test_get_project_info(self):
        Path(os.path.join(self.proj, ".project")).write_text(
            '<?xml version="1.0"?><projectDescription><name>MyProj</name></projectDescription>',
            encoding="utf-8")
        os.makedirs(os.path.join(self.proj, "src"))
        Path(os.path.join(self.proj, "src", "main.c")).write_text("int main(){}", encoding="utf-8")
        Path(os.path.join(self.proj, "sample.scfg")).write_text(
            '<config version="1.0.0"><fsp version="6.4.0"><bsp>'
            '<device>R7FA4M1AB3CFP</device></bsp></fsp></config>',
            encoding="utf-8")
        info = server.tool_get_project_info.__wrapped__(self.proj) if False else None
        # tool_get_project_info is async; call via run
        import asyncio
        info = asyncio.run(server.tool_get_project_info(self.proj))
        self.assertEqual(info["name"], "MyProj")
        self.assertEqual(info["mcu"], "R7FA4M1AB3CFP")
        self.assertEqual(info["fsp_version"], "6.4.0")
        self.assertEqual(info["src_file_count"], 1)


class TestParseMapFile(unittest.TestCase):
    SAMPLE = """\
Memory Configuration

Name             Origin             Length
FLASH            0x0000000000000000 0x00040000
RAM              0x0000000020000000 0x00010000

.text           0x0000000000008000     0x1a80     ./src/main.o
.data           0x0000000020000000      0x100     ./src/main.o
.bss            0x0000000020000100      0x200     ./src/main.o
.rodata         0x0000000000009a80       0x40     ./src/util.o
"""

    def test_parse(self):
        fd, path = tempfile.mkstemp(suffix=".map")
        try:
            os.write(fd, self.SAMPLE.encode())
            os.close(fd)
            r = server._parse_map_file(path)
            self.assertEqual(r["flash_used"], 0x1a80 + 0x100 + 0x40)
            self.assertEqual(r["ram_used"], 0x100 + 0x200)
            self.assertEqual(r["regions"]["FLASH"], 0x40000)
            self.assertNotIn("overflow", r)
        finally:
            os.unlink(path)

    def test_overflow_detection(self):
        fd, path = tempfile.mkstemp(suffix=".map")
        try:
            os.write(fd, (self.SAMPLE + "\nregion `FLASH' overflowed by 128 bytes\n").encode())
            os.close(fd)
            r = server._parse_map_file(path)
            self.assertEqual(r["overflow"]["region"], "FLASH")
            self.assertEqual(r["overflow"]["bytes"], 128)
        finally:
            os.unlink(path)


class TestToolRegistry(unittest.TestCase):
    def test_handlers_and_defs_match(self):
        names = {d["name"] for d in server._TOOL_DEFS}
        self.assertEqual(names, set(server._HANDLERS.keys()))
        self.assertEqual(len(server._TOOL_DEFS), len(server._HANDLERS))

    def test_discover_tools_returns_all_keys(self):
        import asyncio
        r = asyncio.run(server.tool_discover_tools(refresh=False))
        self.assertIn("tools", r)
        for key in ["e2studio_cli", "gcc", "gdb", "objcopy", "make", "rfp_cli",
                    "jlink_gdb", "jlink"]:
            self.assertIn(key, r["tools"], f"missing {key}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
