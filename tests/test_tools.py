import tempfile
import unittest
from pathlib import Path

from backend.prompts import build_system_message
from backend.runner import run_code
from backend.tools import Tool, ToolRegistry, register_file_tools, register_mcp_tool, register_search_tool


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.registry = ToolRegistry()
        register_file_tools(self.registry, self.root)

    def test_file_workflow(self):
        result = run_code(
            'write_file("sample.py", "print(1)")\n'
            'text = read_file("sample.py")\n'
            'if "print" in text:\n    print(list_directory())\n    print(text)',
            self.registry, approved=True,
        )
        self.assertEqual(result.status, "success", result.error)
        self.assertIn("sample.py", result.output)
        self.assertIn("print(1)", result.output)
        self.assertEqual((self.root / "sample.py").read_text(), "print(1)")

    def test_path_and_overwrite_guards(self):
        functions = self.registry.namespace()
        functions["write_file"]("a.py", "original")
        with self.assertRaises(FileExistsError):
            functions["write_file"]("a.py", "changed")
        self.assertEqual(functions["read_file"]("a.py"), "original")
        with self.assertRaises(ValueError):
            functions["read_file"]("../outside.txt")
        with tempfile.TemporaryDirectory() as outside:
            (self.root / "link").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                functions["write_file"]("link/escape.txt", "text")

    def test_approval_error_timeout_and_output_limit(self):
        with self.assertRaises(PermissionError):
            run_code('print("hello")', self.registry)
        result = run_code('print("before"); raise ValueError("bad")', self.registry, approved=True)
        self.assertEqual(result.status, "error")
        self.assertIn("before", result.output)
        self.assertIn("ValueError: bad", result.error)
        result = run_code("while True: pass", self.registry, approved=True, timeout=0.1)
        self.assertEqual(result.status, "timeout")
        result = run_code('print("x" * 100_000)', self.registry, approved=True, max_output_chars=100)
        self.assertEqual(result.status, "success")
        self.assertIn("输出已截断", result.output)
        self.assertLess(len(result.output), 150)

    def test_context_and_provider_adapters(self):
        self.assertNotIn("web_search(", build_system_message(self.registry).content)
        # 测试替身：用于验证接入链路，不代表真实网络或 MCP 请求。
        register_search_tool(self.registry, lambda query: f"search:{query}")

        def lookup(symbol: str) -> str:
            return f"symbol:{symbol}"

        register_mcp_tool(self.registry, "mcp_lookup", "查找远端符号", lookup)
        context = build_system_message(self.registry)
        self.assertIn("web_search(query: str) -> str", context.content)
        self.assertIn("mcp_lookup(symbol: str) -> str", context.content)
        result = run_code(
            'print(web_search("Python")); print(mcp_lookup("main"))',
            self.registry, approved=True,
        )
        self.assertEqual(result.status, "success", result.error)
        self.assertIn("search:Python", result.output)
        self.assertIn("symbol:main", result.output)
        with self.assertRaises(ValueError):
            self.registry.register(Tool("mcp_lookup", "重复工具", lookup))


if __name__ == "__main__":
    unittest.main()
