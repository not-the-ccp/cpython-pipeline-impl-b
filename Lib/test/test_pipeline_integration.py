"""Cross-cutting regression tests for the experimental pipeline fork."""

import ast
import dis
import importlib.util
import opcode
import platform
import sys
import token
import unittest


class PipelineIntegrationTests(unittest.TestCase):
    def test_fork_identity(self):
        self.assertEqual(sys.implementation.name, "cpython")
        self.assertEqual(sys.implementation.cache_tag, "cpython-314-pipeline")
        self.assertIs(sys.implementation._pipeline_fork, True)
        self.assertEqual(sys.version_info[:3], (3, 14, 7))
        self.assertEqual(platform.python_implementation(), "CPython")

    def test_exact_tokens(self):
        self.assertEqual(token.EXACT_TOKEN_TYPES["|>"], token.VBARGREATER)
        self.assertEqual(token.EXACT_TOKEN_TYPES["$"], token.DOLLAR)

    def test_compilation_wide_topic_serial_avoids_closure_collision(self):
        def outer(x):
            return x |> (
                lambda y: (
                    $,
                    y |> $ + 1,
                    $,
                )
            )

        f = outer(10)
        self.assertEqual(f(20), (10, 21, 10))

    def test_compilation_wide_topic_serial_with_generator(self):
        def outer(x):
            return x |> (
                lambda values: (
                    $,
                    tuple(v |> $ + 1 for v in values),
                    $,
                )
            )

        f = outer(10)
        self.assertEqual(f((1, 2)), (10, (2, 3), 10))

    def test_hidden_names_are_deterministic_but_implementation_private(self):
        source = (
            "def f(x):\n"
            "    a = x |> $ + 1\n"
            "    return a |> (lambda: $)\n"
        )
        first = compile(source, "<pipeline>", "exec")
        second = compile(source, "<pipeline>", "exec")
        self.assertEqual(first.co_code, second.co_code)

        f1 = next(c for c in first.co_consts if isinstance(c, type(first)))
        f2 = next(c for c in second.co_consts if isinstance(c, type(second)))
        self.assertEqual(f1.co_varnames, f2.co_varnames)
        self.assertEqual(f1.co_cellvars, f2.co_cellvars)

        # CO_FAST_HIDDEN is compiler metadata, not a promise that low-level
        # code-object APIs erase compiler-generated spellings.
        self.assertTrue(any(name.startswith(".pipe_topic_") for name in f1.co_varnames))

    def test_pipeline_uses_only_existing_name_and_expression_opcodes(self):
        ns = {}
        exec("def f(x):\n    return x |> $ + 1\n", ns)
        instructions = list(dis.get_instructions(ns["f"]))
        opnames = {instruction.opname for instruction in instructions}

        self.assertIn("LOAD_FAST", opnames)
        self.assertIn("STORE_FAST", opnames)
        self.assertIn("BINARY_OP", opnames)
        self.assertFalse(any("PIPE" in opname for opname in opcode.opmap))
        self.assertFalse(any("PIPE" in opname for opname in opnames))

    def test_manual_ast_unparse_pipeline_precedence(self):
        topic = ast.PipeTopic()
        pipe = ast.Pipeline(value=ast.Name(id="x", ctx=ast.Load()), body=topic)

        body = ast.IfExp(
            test=ast.Name(id="cond", ctx=ast.Load()),
            body=pipe,
            orelse=ast.Name(id="z", ctx=ast.Load()),
        )
        self.assertEqual(ast.unparse(ast.fix_missing_locations(body)), "(x |> $) if cond else z")

        test = ast.IfExp(
            test=pipe,
            body=ast.Name(id="y", ctx=ast.Load()),
            orelse=ast.Name(id="z", ctx=ast.Load()),
        )
        self.assertEqual(ast.unparse(ast.fix_missing_locations(test)), "y if (x |> $) else z")

        comp_iter = ast.ListComp(
            elt=ast.Name(id="v", ctx=ast.Load()),
            generators=[ast.comprehension(
                target=ast.Name(id="v", ctx=ast.Store()),
                iter=pipe,
                ifs=[],
                is_async=0,
            )],
        )
        self.assertEqual(ast.unparse(ast.fix_missing_locations(comp_iter)), "[v for v in (x |> $)]")

        comp_filter = ast.ListComp(
            elt=ast.Name(id="v", ctx=ast.Load()),
            generators=[ast.comprehension(
                target=ast.Name(id="v", ctx=ast.Store()),
                iter=ast.Name(id="items", ctx=ast.Load()),
                ifs=[pipe],
                is_async=0,
            )],
        )
        self.assertEqual(ast.unparse(ast.fix_missing_locations(comp_filter)), "[v for v in items if (x |> $)]")

    def test_cache_tag_isolated_and_round_trips(self):
        path = "/tmp/example.py"
        cache = importlib.util.cache_from_source(path)
        self.assertTrue(cache.endswith("example.cpython-314-pipeline.pyc"), cache)
        self.assertEqual(importlib.util.source_from_cache(cache), path)

        optimized = importlib.util.cache_from_source(path, optimization="2")
        self.assertTrue(
            optimized.endswith("example.cpython-314-pipeline.opt-2.pyc"),
            optimized,
        )
        self.assertEqual(importlib.util.source_from_cache(optimized), path)


if __name__ == "__main__":
    unittest.main()
