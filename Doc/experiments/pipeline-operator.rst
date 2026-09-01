.. _pipeline-operator-experiment:

Experimental pipeline operator
==============================

This document describes an experimental language feature added to this
interpreter: the **pipeline operator** ``|>`` and its **topic** ``$``.

The feature is self-contained.  It adds two tokens, two AST node
types, a small lexical rule, and a compiler lowering.  It adds no new
opcode, no new runtime wrapper, and no new scoping rules: the body of
a pipeline is compiled over a deterministic hidden local name that
the rest of the compiler already knows how to handle.

Status
------

This is an experiment, not a proposed PEP.  The operator is always
enabled in this build (there is no ``from __future__ import`` and no
command-line flag); it is documented here so that its behavior and its
limits can be evaluated on their own merits.  No compatibility
guarantees are made, and nothing in the standard library has been
rewritten to use it.

Syntax and precedence
---------------------

::

    value |> body

Both ``value`` and ``body`` are *disjunctions*: everything from
``or`` down to atoms, including nested pipelines.  The pipe is a
binary operator that binds looser than every other binary operator,
so the value side absorbs the whole surrounding disjunction::

    x or y |> f($)          # (x or y) |> f($)
    1 + 2 |> $ * 10         # (1 + 2) |> ($ * 10), i.e. 30

As with every binary operator, a conditional expression or a lambda
used directly as an operand must be parenthesized::

    x |> (f($) if cond else g($))
    x |> (lambda: $ + 1)()
    1 if (x |> f($)) else y     # the conditional test is a disjunction

Chains are left-associative::

    a |> b |> c                 # (a |> b) |> c

The topic
---------

Inside the body, ``$`` is an expression atom that refers to the value
of the pipeline.  It has the same syntactic position as any other
atom (``$[0]``, ``$.attr``, ``f($)``), and it may appear any number of
times in the body, including inside nested comprehensions, generator
expressions, and lambdas defined in the body::

    "hello" |> len($)
    3 |> [$ * i for i in range(3)]          # [0, 3, 6]
    10 |> (lambda: $ + 1)()                 # 11
    10 |> [$ + i for i in range(3)]         # [10, 11, 12]

``$`` is **not a keyword**.  It is a single-character operator token.
Outside a pipeline body it is an error (``$`` has never been a valid
Python identifier character, so this changes no existing program); the
error is reported as a pipeline diagnostic rather than a generic
"invalid syntax".  ``$`` does not interact with the keyword machinery
(``keyword.iskeyword("$")`` and ``keyword.issoftkeyword("$")`` are
both ``False``).

Evaluation contract
-------------------

- ``value`` is evaluated **exactly once**, and the evaluation
  **completes before the body begins**.  The pipeline is not lazily
  threaded: ``f() |> g($)`` calls ``f()`` once, binds its result, then
  evaluates ``g($)``.
- ``$`` is a read-only reference to that single value.  Reading it
  repeatedly is cheap (it is an ordinary local read); it cannot be
  assigned, and the pipeline's result is the value of the body, not
  the topic.
- Chaining rebinds the topic at each step: the value of the second
  pipeline is the *result* of the first::

      5 |> ($ + 1) |> ($ - 1)     # 5
      "hello" |> len($) |> hex($) # '0x5'

Nested pipelines
----------------

A nested pipeline introduces a **fresh topic** for its own body.  Its
*value* expression, however, is still inside the outer body, so it
sees the **outer** topic::

    10 |> ($ |> $ + 1)        # inner value $ is 10; inner body is 11
    10 |> ($ + 1 |> $ * 2)    # inner value ($ + 1) is 11; body is 22
    10 |> ($ + 90 |> $ + $)   # inner value is 100; body is 200

Because of that, a pipeline whose body never references *its own*
topic is rejected, even when the only ``$`` spellings in the source
resolve to an inner topic::

    10 |> (100 |> $ + $)    # SyntaxError: pipeline body must reference '$'

The topic of a comprehension or generator expression is *not*
shadowed by a comprehension variable, because the topic is never
spelled in source at all.

Scoping and closures
--------------------

The body sees the topic as an ordinary local.  A lambda or
comprehension defined in the body captures it through the normal cell
and free-variable machinery, with the usual late-binding semantics::

    def make():
        funcs = [x |> (lambda: $) for x in (1, 2, 3)]
        return funcs
    [f() for f in make()]    # [3, 3, 3]

Class scopes follow ordinary Python class-scope rules: a lambda in a
class body cannot capture a class local, and the hidden topic obeys
the same rule.

Implementation
--------------

The feature touches the frontend and the compiler only.

Tokens and grammar
~~~~~~~~~~~~~~~~~~

``|>`` and ``$`` are new operator tokens (``VBARGREATER``, ``DOLLAR``)
in ``Grammar/Tokens``.  ``Grammar/python.gram`` inserts a
left-recursive ``pipeline`` rule between the conditional/lambda level
and ``or``, and admits ``$`` as an atom.  Because the new rule adds
one parser stack frame per nesting level, ``pegen``'s ``MAXSTACK``
guard grows from 6000 to 6300 so the documented 200-level
nested-parentheses behavior is unchanged.

AST
~~~

``Pipeline(value, body)`` and ``PipeTopic`` are the new public node
types.  ``ast.parse`` validates structure (both children of
``Pipeline`` are expressions; ``PipeTopic`` only occurs in Load
context) and enforces the lexical rules: a ``PipeTopic`` outside a
pipeline body is an error, and every pipeline body must lexically
reference its own topic.  Both unparsers (C and
``Lib/_ast_unparse.py``) print pipelines with the correct
parenthesization.

Symbol table
~~~~~~~~~~~~

Each pipeline body is bound to a deterministic hidden local name,
``.pipe_topic_<n>``, where ``<n>`` counts pipelines within the
enclosing function or module scope.  The name depends only on the
structure of the code, so recompiles and equivalent ASTs produce
identical names and bytecode.  The topic is marked as a local
definition, so the existing closure, cell, and free-variable logic
treats it exactly like a compiler-generated local such as the
inlined-comprehension temporaries.

Code generation
~~~~~~~~~~~~~~~

``codegen_pipeline`` evaluates the value, stores it in the hidden
topic local, compiles the body, and returns the body's value.  No new
opcode is emitted; ``dis`` of a pipeline shows only pre-existing names
(``STORE_FAST``/``LOAD_FAST``, ``STORE_NAME``/``LOAD_NAME``, and the
usual cell and free-variable opcodes).

Errors
------

``$`` outside a pipeline body::

    $                    # SyntaxError: pipeline topic '$' is only valid in a pipeline body

A pipeline body that never references its own topic::

    10 |> f()            # SyntaxError: pipeline body must reference '$'

A ``$`` used where a pipeline's *value* appears (a value is not inside
any body)::

    ($) |> $ + 1         # SyntaxError: pipeline topic '$' is only valid in a pipeline body

Target-form errors name the pipeline expression, e.g. ``x |> f($) +=
1`` reports that a ``pipeline expression`` is an illegal augmented
assignment target.

Testing and demo
----------------

The behavior is covered by ``Lib/test/test_pipeline.py`` (evaluation
contract, every required error, AST structure and round trips,
tokenization, scoping, suspend/resume, and a check that no new opcode
is introduced).  ``Demo/pipeline_demo.py`` shows the operator applied
to realistic standard-library work.