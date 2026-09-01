# -*- coding: utf-8 -*-
"""Tests for the experimental pipeline operator ``|>`` and its topic ``$``.

The pipeline operator binds the value of its left-hand side to the
hidden topic ``$`` for the duration of its body::

    value |> body_using_$

- The value is evaluated exactly once, and completes before the body
  starts.
- ``$`` is an expression atom that refers to the current pipeline's
  topic.  It is not a keyword: outside a pipeline body, ``$`` keeps
  meaning exactly what it has always meant in Python source (an
  identifier, subject to the ordinary identifier rules).
- Every pipeline body must lexically reference ``$`` at least once,
  directly or inside a nested function, comprehension, or generator
  expression defined in the body.
- A nested pipeline introduces a fresh topic for its own body; the
  value expression of the nested pipeline still sees the outer topic.
"""

import ast
import asyncio
import contextlib
import io
import tokenize
import unittest

import opcode
import symtable as symtable_module
import token

from test import support
from test.support import check_syntax_error


class PipelineSemanticsTestCase(unittest.TestCase):

    def test_basic_binding(self):
        self.assertEqual("hello" |> len($), 5)
        self.assertEqual(10 |> $ + 1, 11)
        self.assertEqual(2 |> $ ** 10, 1024)
        self.assertEqual("ab" |> $[::-1], "ba")
        self.assertEqual([1, 2, 3] |> sum($), 6)
        self.assertEqual((1, 2) |> len($) + max($), 4)
        self.assertEqual((3, 4) |> max($) - min($), 1)

    def test_value_evaluated_exactly_once(self):
        calls = []

        def produce():
            calls.append(1)
            return 41

        result = produce() |> $ + 1
        self.assertEqual(result, 42)
        self.assertEqual(calls, [1])

    def test_value_completes_before_body(self):
        events = []
        # The value's effect must precede the body's first effect.
        result = (events.append("value"), 7)[1] |> (
            events.append("body"), $
        )[1]
        self.assertEqual(result, 7)
        self.assertEqual(events, ["value", "body"])

    def test_multiple_topic_references(self):
        self.assertEqual(10 |> $ + $, 20)
        self.assertEqual(3 |> $ * $ * $, 27)
        self.assertEqual("ok" |> ($, $), ("ok", "ok"))

    def test_chained_pipelines(self):
        self.assertEqual("hello" |> len($) |> hex($), "0x5")
        self.assertEqual(1 |> ($ * 2) |> ($ * 3), 6)
        # The topic of each chain step is the result of the previous
        # step.
        self.assertEqual(5 |> ($ + 1) |> ($ - 1), 5)

    def test_nested_pipelines_fresh_topic(self):
        # The inner pipeline's topic shadows the outer one in the inner
        # body; the inner *value* still sees the outer topic.
        self.assertEqual(10 |> ($ |> $ + 1), 11)
        self.assertEqual(10 |> ($ + 1 |> $ * 2), 22)
        self.assertEqual(10 |> ($ + 90 |> $ + $), 200)
        # Deeply nested: each level's value may use the topic of its
        # enclosing pipeline.
        self.assertEqual(2 |> ($ |> ($ |> $ * $)), 4)
        self.assertEqual(10 |> (($ |> $ + 1), $), (11, 10))

    def test_nested_pipeline_unused_topic_is_error(self):
        # A pipeline body that never references its *own* topic is an
        # error, even when the only $ spellings in the source resolve
        # to an inner topic.
        check_syntax_error(
            self, "10 |> (100 |> $ + $)", "pipeline body must reference '\\$'"
        )
        check_syntax_error(
            self, "2 |> (3 |> (4 |> $ * $))", "pipeline body must reference '\\$'"
        )

    def test_topic_not_rebound_outside_body(self):
        # The hidden topic is scoped to the pipeline and does not leak
        # into surrounding names under a source-visible spelling.
        ns = {}
        exec(compile("x = 1 |> $ + 1", "<t>", "exec"), ns)
        self.assertEqual(ns["x"], 2)
        self.assertNotIn("$", ns)

    def test_precedence(self):
        # A bare disjunction is a legal pipeline value: the pipe binds
        # looser than 'or'.
        self.assertEqual((1 or 2) |> $ * 10, 10)
        # ... and the body is a full expression:
        self.assertEqual(1 |> $ or "fallback", 1)
        # Comparison binds tighter than the pipe on the body side.
        self.assertEqual(1 |> $ + 1 == 2, True)
        # The value side is a full disjunction: the whole sum is the
        # pipeline value, so (1 + 2) |> ($ * 10) == 30.
        self.assertEqual(1 + 2 |> $ * 10, 30)
        # Lambda and conditional bodies.
        self.assertEqual(1 |> (lambda: $ + 1)(), 2)
        self.assertTrue(1 |> ($ if True else 0))

    def test_value_positions(self):
        class A:
            pass
        a = A()
        a.v = 5
        self.assertEqual(a |> getattr($, "v"), 5)
        self.assertEqual((1, 2, 3) |> $[1], 2)
        self.assertEqual((lambda x: x * 2) |> $(10), 20)
        self.assertEqual("abc" |> $ + "def", "abcdef")
        self.assertEqual([1, 2] |> len($[::-1]), 2)

    def test_call_unpacking(self):
        self.assertEqual((10, 3) |> divmod(*$), (3, 1))
        self.assertEqual([10, 3] |> divmod(*$), (3, 1))

        def s(*a):
            return sum(a)

        self.assertEqual((1, 2) |> s(*$), 3)

        def f(**kw):
            return sum(kw.values())

        self.assertEqual({"a": 1, "b": 2} |> f(**$), 3)

    def test_attribute_subscript_call_topic(self):
        self.assertEqual("abc" |> $.upper(), "ABC")
        self.assertEqual({"k": 9} |> $["k"], 9)
        self.assertEqual([10, 20] |> $[0] + $[1], 30)
        self.assertEqual(21 |> (lambda a: a * 2)($), 42)

    def test_fstring_topic(self):
        self.assertEqual("world" |> f"hello {$}", "hello world")
        self.assertEqual(3 |> f"n={$}", "n=3")
        # Conversions and format specs apply to the topic value.
        self.assertEqual(123 |> f"{$!r}", "123")
        self.assertEqual(3.5 |> f"{$:.1f}", "3.5")

    def test_walrus_in_grouped_body(self):
        self.assertEqual(10 |> (y := $ + 1), 11)

        def f():
            x = 5 |> (y := $ * 2)
            return y

        self.assertEqual(f(), 10)

    def test_comprehensions_in_body(self):
        self.assertEqual(3 |> [$ * i for i in range(3)], [0, 3, 6])
        self.assertEqual(
            3 |> [$ * i for i in range(5) if $ > 2 and i > 0],
            [3, 6, 9, 12],
        )
        self.assertEqual(3 |> {i + $ for i in range(2)}, {3, 4})
        self.assertEqual(
            3 |> {str(i): i + $ for i in range(2)}, {"0": 3, "1": 4}
        )
        # The comprehension scope reads the outer topic through a
        # closure.
        self.assertEqual(2 |> [i + $ for i in range(2)], [2, 3])
        self.assertEqual(2 |> [j + $ for i in [1] for j in [0]], [2])
        # A comprehension's own iteration variable does not shadow the
        # pipeline's topic (the topic is not spelled in source).
        self.assertEqual(2 |> [i + $ for i in (5, 6)], [7, 8])

    def test_generator_expressions_in_body(self):
        self.assertEqual(2 |> sum(i + $ for i in range(3)), 2 + 3 + 4)
        self.assertEqual(10 |> [i for i in ($ // 2, $ // 5)], [5, 2])

    def test_short_circuiting(self):
        def boom():
            raise AssertionError("must not run")

        # 'and' short-circuits: a false topic skips the rest.
        self.assertFalse(0 |> ($ and boom()))
        # 'or' short-circuits: a truthy topic skips the rest.
        self.assertEqual(7 |> ($ or boom()), 7)

    def test_yield_suspension(self):
        def gen():
            v = 10 |> ($ + 1)   # completes to 11
            y = (yield v)
            return v + (y |> $ * 10)

        g = gen()
        self.assertEqual(next(g), 11)
        with self.assertRaises(StopIteration) as cm:
            g.send(0)
        self.assertEqual(cm.exception.value, 11)

        g = gen()
        next(g)
        with self.assertRaises(StopIteration) as cm:
            g.send(5)
        self.assertEqual(cm.exception.value, 11 + 50)

    def test_yield_expression_body(self):
        # A yield expression as the pipeline body suspends the whole
        # pipeline: the topic is read before the suspension, and the
        # body resumes with the sent value.
        def gen():
            y = 10 |> (yield $)
            return y

        g = gen()
        self.assertEqual(next(g), 10)
        with self.assertRaises(StopIteration) as cm:
            g.send(99)
        self.assertEqual(cm.exception.value, 99)

    def test_async(self):
        async def coro(v):
            await asyncio.sleep(0)
            return v * 2

        async def main():
            assert 21 |> await coro($) == 42
            assert 5 |> await coro($) == 10
            assert 2 |> await coro($) |> await coro($) == 8
            a, b = 21 |> (await twice_local($), $)
            assert (a, b) == (42, 21)

        async def twice_local(v):
            await asyncio.sleep(0)
            return v * 2

        asyncio.run(main())

    def test_pipeline_in_control_flow(self):
        try:
            1 / 0
        except ZeroDivisionError:
            r = 6 |> $ + 1
        self.assertEqual(r, 7)

        class CM:
            def __enter__(self):
                return self.v

            def __exit__(self, *exc):
                return False

        cm = CM()
        cm.v = 5
        with (cm |> (lambda: $)()) as x:
            self.assertEqual(x, 5)

        m = (1, 2)
        match m:
            case [a, b] if (m |> len($)) == 2:
                self.assertEqual((a, b), (1, 2))

        assert 1, (2 |> f"{$}!")
        self.assertTrue(True)

    def test_default_arguments(self):
        def f(x=(21 |> $ // 7)):
            return x

        self.assertEqual(f(), 3)

    def test_del_of_pipeline_result(self):
        d = 5 |> (lambda: $)
        del d
        with self.assertRaises(NameError):
            d  # noqa: B018


class PipelineScopingTestCase(unittest.TestCase):

    def test_closure_late_binding(self):
        def make():
            funcs = []
            for x in (1, 2, 3):
                funcs.append(x |> (lambda: $))
            return funcs

        self.assertEqual([f() for f in make()], [3, 3, 3])

    def test_closure_cell_uses_hidden_name(self):
        code = compile(
            "def f(x):\n    return x |> (lambda: $)\n", "<t>", "exec"
        )
        outer = code.co_consts[0]
        self.assertEqual(outer.co_cellvars, (".pipe_topic_0",))
        inner = outer.co_consts[0]
        self.assertEqual(inner.co_freevars, (".pipe_topic_0",))

    def test_function_scope_hidden_local(self):
        code = compile(
            "def f(x):\n    return x |> $ + 1\n", "<t>", "exec"
        )
        fn = code.co_consts[0]
        # The topic is an ordinary hidden local of the function.
        self.assertEqual(fn.co_varnames, ("x", ".pipe_topic_0"))

    def test_multiple_pipelines_get_sequential_names(self):
        code = compile(
            "def f(x):\n"
            "    a = x |> $ + 1\n"
            "    b = x |> $ + 2\n"
            "    return a, b\n",
            "<t>",
            "exec",
        )
        fn = code.co_consts[0]
        self.assertIn(".pipe_topic_0", fn.co_varnames)
        self.assertIn(".pipe_topic_1", fn.co_varnames)

    def test_global_statement(self):
        ns = {"counter": 0}
        exec(
            compile(
                "def f():\n"
                "    global counter\n"
                "    counter = counter |> ($ + 1)\n"
                "    return counter\n",
                "<t>",
                "exec",
            ),
            ns,
        )
        self.assertEqual(ns["f"](), 1)
        self.assertEqual(ns["counter"], 1)
        self.assertEqual(ns["f"](), 2)
        self.assertEqual(ns["counter"], 2)

    def test_class_scope_matches_ordinary_python(self):
        # A lambda in a class body cannot capture class locals (ordinary
        # Python semantics); the hidden topic obeys the same rule.
        class A:
            x = 1 |> $ + 1
            y = 5 |> $ * 2

        self.assertEqual((A.x, A.y), (2, 10))

        class B:
            x = 1
            y = lambda: x

        with self.assertRaises(NameError):
            B.y()

        class C:
            x = 1
            y = x |> (lambda: $)

        with self.assertRaises(NameError):
            C.y()

    def test_annotations(self):
        def f(x: (10 |> $ + 1)) -> (5 |> $ * 2):
            return x

        self.assertEqual(f.__annotations__, {"x": 11, "return": 10})

    def test_annotations_are_lazy_at_module_scope(self):
        import types

        src = (
            "counts = []\n"
            "def c(v):\n"
            "    counts.append(v)\n"
            "    return v * 10\n"
            "x: (1 |> c($))\n"
        )
        mod = types.ModuleType("m")
        exec(compile(src, "<m>", "exec"), mod.__dict__)
        # PEP 649/749: the annotation is not evaluated during execution.
        self.assertEqual(mod.counts, [])
        self.assertEqual(mod.__annotations__["x"], 10)
        self.assertEqual(mod.counts, [1])

    def test_module_level_pipeline(self):
        ns = {}
        exec(compile("total = sum([1, 2, 3]) |> $ * 2", "<t>", "exec"), ns)
        self.assertEqual(ns["total"], 12)

    def test_interactive_mode(self):
        code = compile("10 |> $ + 1", "<i>", "single")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(code, {})
        self.assertIn("11", buf.getvalue())

    def test_symtable_sees_hidden_names(self):
        st = symtable_module.symtable(
            "def f(x):\n    return x |> $ + 1\n", "<t>", "exec"
        )
        fn = next(c for c in st.get_children() if c.get_name() == "f")
        symbols = {s.get_name(): s for s in fn.get_symbols()}
        self.assertIn(".pipe_topic_0", symbols)
        self.assertTrue(symbols[".pipe_topic_0"].is_local())
        self.assertNotIn("$", symbols)


class PipelineSyntaxErrorTestCase(unittest.TestCase):

    def test_standalone_topic(self):
        check_syntax_error(
            self, "$", "pipeline topic '\\$' is only valid in a pipeline body"
        )
        check_syntax_error(
            self,
            "def f():\n    return $\n",
            "pipeline topic '\\$' is only valid in a pipeline body",
        )

    def test_topic_in_value_position(self):
        check_syntax_error(
            self,
            "$ |> $ + 1",
            "pipeline topic '\\$' is only valid in a pipeline body",
        )
        check_syntax_error(
            self,
            "($) |> $ + 1",
            "pipeline topic '\\$' is only valid in a pipeline body",
        )
        # A function defined in the value sees no topic at all.
        check_syntax_error(
            self,
            "(lambda: $)() |> $ + 1",
            "pipeline topic '\\$' is only valid in a pipeline body",
        )
        # A nested pipeline as a value is fine: the inner value sees
        # the outer topic, and each body uses its own topic.
        self.assertEqual(10 |> ($ |> $ + 1), 11)
        # A chained pipeline is fine for the same reason.
        self.assertEqual(10 |> $ + 1 |> $ + 2, 13)
        self.assertEqual((5 |> $) + 1 |> $ + 1, 7)

    def test_body_without_topic(self):
        check_syntax_error(
            self, "10 |> 20", "pipeline body must reference '\\$'"
        )
        check_syntax_error(
            self, "10 |> f()", "pipeline body must reference '\\$'"
        )
        check_syntax_error(
            self, "10 |> [1, 2, 3]", "pipeline body must reference '\\$'"
        )
        check_syntax_error(
            self, "10 |> 'literal'", "pipeline body must reference '\\$'"
        )
        # A lambda that never reads $ does not count either.
        check_syntax_error(
            self, "10 |> (lambda: 1)", "pipeline body must reference '\\$'"
        )
        # ... but one that does counts.
        self.assertEqual(10 |> (lambda: $)(), 10)

    def test_nested_unused_body(self):
        # The error comes from the inner pipeline; the outer body does
        # reference its own topic.
        check_syntax_error(
            self, "10 |> ($, 5 |> 6)", "pipeline body must reference '\\$'"
        )
        with self.assertRaises(SyntaxError) as cm:
            compile("10 |> (5 |> 6)", "<t>", "exec")
        self.assertIn("pipeline body must reference '$'", str(cm.exception))

    def test_dollar_is_not_a_keyword(self):
        # '$' is a single-character operator token, not a keyword: it
        # does not participate in the keyword machinery (import, soft
        # keywords, match subjects, ...).  Outside a pipeline body it
        # is rejected with the topic diagnostic rather than as an
        # identifier.
        check_syntax_error(
            self, "x = " + chr(36) + "\n",
            "pipeline topic '\\$' is only valid in a pipeline body",
        )
        # ... and it is never in the keyword set:
        import keyword
        self.assertFalse(keyword.iskeyword("$"))
        self.assertFalse(keyword.issoftkeyword("$"))

    def test_target_positions(self):
        check_syntax_error(
            self,
            "x |> f($) += 1",
            "illegal expression for augmented assignment",
        )
        check_syntax_error(
            self,
            "await $\n",
            "pipeline topic '\\$' is only valid in a pipeline body",
        )

    def test_error_reports_pipeline_description(self):
        # Target-form errors name the pipeline expression.
        with self.assertRaises(SyntaxError) as cm:
            compile("x |> f($) = 3", "<t>", "exec")
        self.assertIn("pipeline expression", str(cm.exception))

    def test_topic_in_comprehension_iterator_of_body(self):
        # A comprehension iterator is still inside the body, so $ is
        # allowed there (it refers to the pipeline topic).
        self.assertEqual(10 |> [$ // i for i in (1, 2)], [10, 5])


class PipelineAstTestCase(unittest.TestCase):

    def parse(self, source, mode="eval"):
        return ast.parse(source, mode=mode)

    def test_ast_structure(self):
        tree = self.parse("x |> f($)")
        expr = tree.body
        self.assertIsInstance(expr, ast.Pipeline)
        self.assertEqual(tuple(expr._fields), ("value", "body"))
        self.assertIsInstance(expr.value, ast.Name)
        self.assertEqual(expr.value.id, "x")
        self.assertIsInstance(expr.body, ast.Call)
        self.assertIsInstance(expr.body.args[0], ast.PipeTopic)
        self.assertEqual(ast.PipeTopic()._fields, ())

    def test_nested_ast(self):
        tree = self.parse("10 |> ($ |> $ + $)")
        outer = tree.body
        self.assertIsInstance(outer, ast.Pipeline)
        inner = outer.body
        self.assertIsInstance(inner, ast.Pipeline)
        self.assertIsInstance(outer.value, ast.Constant)
        self.assertEqual(outer.value.value, 10)
        # The inner value is the outer topic...
        self.assertIsInstance(inner.value, ast.PipeTopic)
        # ...and both operands of the inner body are the inner topic.
        binop = inner.body
        self.assertIsInstance(binop, ast.BinOp)
        self.assertIsInstance(binop.left, ast.PipeTopic)
        self.assertIsInstance(binop.right, ast.PipeTopic)

    def test_only_load_context(self):
        bad = ast.Expression(ast.Tuple([ast.PipeTopic()], ctx=ast.Store()))
        ast.fix_missing_locations(bad)
        with self.assertRaises(ValueError):
            compile(bad, "<manual>", "eval")

    def test_manual_construction(self):
        mod = ast.Expression(
            ast.Pipeline(
                ast.Name("x", ctx=ast.Load()),
                ast.Call(
                    ast.Name("f", ctx=ast.Load()), [ast.PipeTopic()], []
                ),
            )
        )
        ast.fix_missing_locations(mod)
        code = compile(mod, "<manual>", "eval")
        self.assertEqual(eval(code, {"x": 7, "f": lambda a: a * 2}), 14)

    def test_manual_invalid_body(self):
        bad = ast.Expression(
            ast.Pipeline(
                ast.Name("x", ctx=ast.Load()),
                ast.Name("y", ctx=ast.Load()),
            )
        )
        ast.fix_missing_locations(bad)
        with self.assertRaises(SyntaxError):
            compile(bad, "<manual>", "eval")

    def test_manual_standalone_topic(self):
        bad = ast.Expression(ast.PipeTopic())
        ast.fix_missing_locations(bad)
        with self.assertRaises(SyntaxError):
            compile(bad, "<manual>", "eval")

    def test_unparse_round_trip(self):
        cases = [
            "x |> f($)",
            "x |> f($) |> g($)",
            "a + b |> f($)",
            "x or y |> f($)",
            "x |> f($) + 1",
            "x |> f($) if cond else y",
            "x |> (f($) if cond else g($))",
            "(x if cond else y) |> f($)",
            "lambda x: x |> f($)",
            "x |> (lambda: $)",
            "10 |> ($ |> $ + 1)",
            "10 |> ($ + 1 |> $ * 2)",
            "[x |> $ * 2 for x in range(4)]",
            "10 |> [$ + i for i in range(3)]",
            "name |> f'hello, {$}'",
            "args |> f(*$)",
            "kwargs |> f(**$)",
            "x |> f(a=$, b=$)",
            "x |> $.attr",
            "x |> $[0]",
            "x |> $()",
            "x |> -$",
            "x |> not $",
            "x |> [$, $]",
            "x |> {$: f($)}",
            "a if (x |> f($)) else c",
            "[i for i in (xs |> iter($))]",
            "[i for i in xs if (x |> pred($))]",
            "1 if (2 |> f($)) else 3",
            "(x := (y |> f($)))",
            "await (x |> coro($))",
            "(x |> f($))[0]",
            "(x |> f($)).attr",
            "10 |> ($ + 1 |> $ * 2) |> $",
            "f(1) |> $ + 1",
        ]
        for src in cases:
            with self.subTest(src=src):
                t1 = self.parse(src)
                out = ast.unparse(t1)
                t2 = self.parse(out)
                self.assertEqual(ast.dump(t1), ast.dump(t2))

    def test_unparse_precedence(self):
        # A disjunction value needs no parens: the pipe binds looser
        # than 'or'.
        self.assertEqual(
            ast.unparse(self.parse("x or y |> f($)")), "x or y |> f($)"
        )
        # Right-nested pipelines keep their grouping.
        self.assertEqual(
            ast.unparse(self.parse("10 |> ($ + 1 |> $ * 2)")),
            "10 |> ($ + 1 |> $ * 2)",
        )
        # A conditional-expression test is a disjunction slot, so a
        # pipeline there must be parenthesized.
        self.assertEqual(
            ast.unparse(self.parse("1 if (2 |> f($)) else 3")),
            "1 if (2 |> f($)) else 3",
        )
        # A comprehension iterator is a disjunction slot as well.
        self.assertEqual(
            ast.unparse(self.parse("[i for i in (xs |> iter($))]")),
            "[i for i in (xs |> iter($))]",
        )


class PipelineTokenizeTestCase(unittest.TestCase):

    def test_tokens(self):
        tokens = list(
            tokenize.generate_tokens(io.StringIO("x |> f($)\n").readline)
        )
        kinds = [
            (t.type, t.string)
            for t in tokens
            if t.type not in (
                tokenize.NEWLINE, tokenize.ENDMARKER,
                tokenize.NL, tokenize.COMMENT,
            )
        ]
        self.assertEqual(
            kinds,
            [
                (token.NAME, "x"),
                (token.OP, "|>"),
                (token.NAME, "f"),
                (token.OP, "("),
                (token.OP, "$"),
                (token.OP, ")"),
            ],
        )

    def test_untokenize_round_trip(self):
        src = "x |> f($) |> g($)\n"
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
        out = tokenize.untokenize(tokens)
        self.assertEqual(ast.dump(ast.parse(src)), ast.dump(ast.parse(out)))


class PipelineBytecodeTestCase(unittest.TestCase):

    def compile_func(self, source):
        code = compile(source, "<t>", "exec")
        return code.co_consts[0]

    def walk_opcodes(self, code, acc):
        import dis as _dis

        for instr in _dis.get_instructions(code):
            acc.add(instr.opname)
        for const in code.co_consts:
            if hasattr(const, "co_code"):
                self.walk_opcodes(const, acc)

    def test_no_new_opcodes(self):
        # The pipeline must compile to pre-existing opcodes only.
        fn = self.compile_func("def f(x):\n    return x |> $ + 1\n")
        used = set()
        self.walk_opcodes(fn, used)
        # Everything used must be a pre-existing opcode name.
        valid = {name for name in opcode.opname if name}
        self.assertTrue(used.issubset(valid))

    def test_deterministic_hidden_names(self):
        src = "def f(x):\n    return x |> $ + 1\n"
        c1 = self.compile_func(src)
        c2 = self.compile_func(src)
        self.assertEqual(c1.co_varnames, c2.co_varnames)
        self.assertEqual(c1.co_code, c2.co_code)
        self.assertIn(".pipe_topic_0", c1.co_varnames)


if __name__ == "__main__":
    unittest.main()