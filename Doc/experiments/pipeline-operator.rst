.. _pipeline-operator-experiment:

Experimental pipeline operator
==============================

This document describes an experimental language feature added to this
interpreter: the **pipeline operator** ``|>`` and its **topic** ``$``.

The feature is self-contained.  It adds two exact tokens, two AST node types,
a grammar level, lexical topic validation, and compiler lowering.  It adds no
new opcode, runtime wrapper, or runtime pipeline protocol.  Topics are lowered
through ordinary CPython name, local, cell, and free-variable machinery.

Status
------

This is an experiment, not a proposed PEP.  The operator is always enabled in
this build; there is no ``from __future__ import`` and no command-line flag.
Nothing here is an upstream Python compatibility guarantee.

Syntax and precedence
---------------------

The grammar is conceptually::

    expression:
        | pipeline 'if' disjunction 'else' expression
        | pipeline
        | lambdef

    pipeline:
        | pipeline '|>' disjunction
        | disjunction

Consequently, a pipeline chain is left-recursive and left-associative.  The
base/first left operand is a disjunction, a later left operand may itself be a
pipeline, and every immediate right/body operand is a disjunction.  ``or``
binds tighter than ``|>``, while conditional expressions and lambdas bind
looser::

    x or y |> f($)          # (x or y) |> f($)
    1 + 2 |> $ * 10         # (1 + 2) |> ($ * 10), i.e. 30
    x |> (f($) if cond else g($))
    x |> (lambda: $ + 1)()
    1 if (x |> f($)) else y

Chains group to the left::

    a |> b($) |> c($)       # (a |> b($)) |> c($)

The topic
---------

Inside a pipeline body, ``$`` is an expression atom referring to that stage's
value.  It can be used in ordinary expression positions, including::

    "hello" |> len($)
    args |> f(*$)
    kwargs |> f(**$)
    obj |> $.method()
    seq |> $[0]
    7 |> ($, $)

``$`` is a dedicated exact punctuation/operator token in this fork.  It is
neither an identifier nor a keyword.  Outside a pipeline body compilation
reports the dedicated topic error.  ``keyword.iskeyword("$")`` and
``keyword.issoftkeyword("$")`` are both false.

Every body must lexically reference **its own** topic.  There is no implicit
application or argument insertion::

    x |> f        # SyntaxError: this body's own topic is unused
    x |> f()      # SyntaxError: same
    x |> f($)     # explicit call

Evaluation contract
-------------------

- The pipeline value is evaluated **exactly once** and completes before the
  body begins.
- Reading ``$`` reads that one value through the compiler-generated ordinary
  binding for the stage.
- The pipeline result is the body result.
- Chaining makes the previous stage's result the next stage's value::

      5 |> $ + 1 |> $ - 1       # 5
      "hello" |> len($) |> hex($) # '0x5'

Nested pipelines
----------------

A nested pipeline introduces a fresh topic for its own body.  Its value/LHS is
still part of the outer body, so it is evaluated before the nested topic is
introduced and can see the outer topic::

    10 |> ($ |> $ + 1)        # nested value is outer topic 10
    10 |> ($ + 1 |> $ * 2)    # nested value 11, nested body 22
    10 |> ($ + 90 |> $ + $)   # nested value 100, result 200

An outer pipeline whose only source ``$`` occurrences belong to an inner body
is therefore rejected::

    10 |> (100 |> $ + $)      # SyntaxError: outer body never uses outer topic

Scoping and closures
--------------------

The generated topic binding participates in ordinary Python symbol-table and
closure behavior.  A lambda, nested function, comprehension, or generator can
capture an enclosing topic in the same way it captures an ordinary local,
including ordinary late binding.

A single compilation uses a **compilation-wide monotonically increasing
serial** to assign deterministic source-unspellable names of the current form
``.pipe_topic_<n>``.  The serial deliberately does not reset at function or
comprehension boundaries.  A nested scope can simultaneously capture an outer
topic and define an inner pipeline topic; compilation-wide uniqueness prevents
those two distinct bindings from receiving the same internal spelling.

The symbol table owns the Pipeline-AST-node -> generated-name mapping.  Codegen
retrieves that mapping and does not independently allocate or reconstruct topic
names.  The exact generated spelling is an implementation detail, not language
API, and adding an earlier pipeline may renumber later private names.

Implementation
--------------

Tokens and grammar
~~~~~~~~~~~~~~~~~~

``Grammar/Tokens`` defines exact ``VBARGREATER`` (``|>``) and ``DOLLAR``
(``$``) tokens.  ``Grammar/python.gram`` inserts the left-recursive pipeline
rule between conditional/lambda expressions and ``or`` and admits ``$`` as the
``PipeTopic`` atom.

The generated C parser's non-WASI ``MAXSTACK`` is 6300 rather than upstream
3.14.7's 6000 because the extra common expression grammar level increases PEG
call depth.  This is distinct from the tokenizer's parenthesis nesting limit.
``Lib/test/test_grammar.py::TokenTests.test_max_level`` defines that existing
language boundary: 200 nested parentheses are accepted and 201 fail with
``too many nested parentheses``.  The fork must preserve that behavior rather
than increasing parser limits without a reproduced need.

An older stock host Python need not know ``token.VBARGREATER`` or
``token.DOLLAR`` merely to regenerate the fork's C parser: pegen reads the
repository's supplied ``Grammar/Tokens`` exact-token map for quoted grammar
literals.  Requiring an old host's own pure-Python tokenizer to parse pipeline
*source* is a different and unnecessary contract.

AST and validation
~~~~~~~~~~~~~~~~~~

``Parser/Python.asdl`` defines public ``Pipeline(value, body)`` and leaf
``PipeTopic`` expression nodes.  ``PipeTopic`` has no ``expr_context`` field;
it is read-only topic syntax.

AST preprocessing performs the context-sensitive lexical validation in source
order: visit ``Pipeline.value`` under the current outer context, push a fresh
unused body-topic context, visit the body, require that current context to have
been used, then pop it.  A ``PipeTopic`` marks only the current/top context as
used.  Generic AST structure validation and generated AST fields continue to
handle ordinary structure/traversal.

Both the Python and limited C unparsers have a PIPE precedence level.  Grammar
slots that specifically accept a ``disjunction`` (for example conditional
body/test and comprehension iterable/filter positions) must request OR
precedence so a manually constructed Pipeline AST is parenthesized there.

Compiler topic stacks
~~~~~~~~~~~~~~~~~~~~~

The compiler and symbol-table **active topic stacks are compile-time
bookkeeping only**.  The final implementation should use raw ``_Py_c_array_t``
stacks of borrowed ``PyObject *`` generated-name pointers rather than Python
``list`` objects.  The caller already owns a strong name reference throughout
recursive body traversal, so the stack need not own another reference.

That gives the desired error model: capacity growth can fail before push,
successful push is a raw pointer store, current-topic lookup is a borrowed
pointer read, and pop is an infallible decrement/raw clear with no allocation
or decref.  There is no runtime topic stack.

Symbol table and code generation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The symbol table visits a pipeline value first, allocates/looks up the
deterministic generated name, defines it as an ordinary local/name in the
current Python scope, pushes that name only for body traversal, and always pops
on recursive exit.

``codegen_pipeline`` evaluates the LHS, stores it with ordinary name-op code,
pushes the same generated name while compiling the body, then leaves the
body's value on the VM stack.  ``codegen_pipetopic`` is an ordinary Load of the
current generated binding.  Disassembly therefore uses existing FAST/NAME/CELL
and FREE opcode families only; there is no ``PIPE`` opcode.

Hidden-local metadata
~~~~~~~~~~~~~~~~~~~~~

For function-like compiler units, generated pipeline locals should be marked
through CPython's existing ``u_fasthidden`` -> ``CO_FAST_HIDDEN`` machinery.
That means tooling which honors the hidden-local bit does not treat the
compiler temporary exactly like a normal user-writable fast local.

This bit does **not** make the spelling disappear from all introspection.  A
locals-plus entry can simultaneously be ``CO_FAST_LOCAL | CO_FAST_HIDDEN``
(and also ``CO_FAST_CELL`` when captured), so ``co_varnames`` and
``co_cellvars`` can still expose the generated name.  A nested code object can
show it in ``co_freevars``.  Module/class lowering can leave a source-unspellable
key in the namespace.  These are accepted low-level implementation artifacts;
this experiment does not add a new code-object slot kind or global frame-local
filter to conceal them.

Source-cache and ABI identity
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The fork remains CPython 3.14.7:

- ``sys.implementation.name == 'cpython'``;
- ``platform.python_implementation() == 'CPython'``;
- numeric Python version, pyc magic, opcode set, SOABI, extension suffixes,
  stable ABI, and native extension ABI remain the underlying CPython 3.14.7
  values.

Normal source caches use ``sys.implementation.cache_tag ==
'cpython-314-pipeline'`` and expose private marker
``sys.implementation._pipeline_fork is True``.  The distinct cache filename is
important because fork bytecode intentionally remains ordinary 3.14 bytecode:
a stock interpreter must not accidentally select a fork-generated normal cache
for source syntax that stock CPython cannot parse.  Directly supplied compatible
3.14 pyc files may still execute under a like-version stock interpreter because
no code-object or opcode format was added.

Errors
------

``$`` outside a pipeline body::

    $                    # SyntaxError: topic is only valid in a pipeline body

A body that never references its own topic::

    10 |> f()            # SyntaxError: pipeline body must reference '$'

A topic used in a top-level pipeline value::

    ($) |> $ + 1         # SyntaxError: topic is not inside any body yet

Target-form diagnostics should identify the expression as a pipeline expression
and must never expose the generated internal name.

Local verification
------------------

After building this checkout, run::

    ./python Tools/scripts/check_pipeline_fork.py

for the focused compiler/parser/AST/token regression set.  From a clean tracked
worktree, run::

    ./python Tools/scripts/check_pipeline_fork.py --regen-check

for two-pass token/AST/parser regeneration determinism, or::

    ./python Tools/scripts/check_pipeline_fork.py --full

for two-pass ``make regen-all`` determinism.  The verifier never resets, cleans,
or restores developer work and no hosted CI is required.

Testing and demo
----------------

``Lib/test/test_pipeline.py`` is the primary feature suite.  Adjacent CPython
tests for AST, annotation stringification, symbol tables, compilation,
disassembly, code/frame metadata, tokens/tokenization, syntax/grammar,
f-strings, t-strings, and pegen are part of the focused verifier because the
feature crosses all of those frontend surfaces.

``Demo/pipeline_demo.py`` shows the operator applied to realistic code.  The
final integration gate additionally requires deterministic regeneration, a
normal and debug build, broad CPython regression testing, documentation build,
old-host C-parser regeneration, and cache/ABI compatibility experiments.
