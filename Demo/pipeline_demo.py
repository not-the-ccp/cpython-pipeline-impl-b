#!/usr/bin/env python3
"""Demo program for the experimental pipeline operator ``|>``.

Run with the interpreter built from this tree::

    ./python Demo/pipeline_demo.py

The pipeline operator binds the value of its left-hand side to the
hidden topic ``$`` for the duration of its body::

    value |> body_using_$

The value is evaluated exactly once and completes before the body
starts; ``$`` may appear anywhere in the body (including inside
nested comprehensions and lambdas); and a nested pipeline introduces
a fresh topic for its own body while its value expression still sees
the outer topic.

This demo shows the operator on realistic standard-library work:
filtering and reshaping records, a text-analysis chain, and a small
aggregation pipeline.  Every expression below is valid 3.14 code in
this build.
"""


def pipeline_over_records():
    """Filter, sort, and project a list of records in one chain."""
    records = [
        {"name": "quinoa", "calories": 132, "protein": 4.4},
        {"name": "oats", "calories": 158, "protein": 6.0},
        {"name": "rice", "calories": 165, "protein": 4.3},
        {"name": "lentils", "calories": 178, "protein": 9.0},
        {"name": "amaranth", "calories": 219, "protein": 13.6},
    ]

    # The classic filter/sort/project pass over the records.
    lines = (
        records
        |> [row for row in $ if row["protein"] >= 6.0]
        |> sorted($, key=lambda row: row["protein"])
        |> [f'{row["name"]}: {row["protein"]}g' for row in $]
    )

    print("protein-dense grains:")
    for line in lines:
        print("  ", line)


def text_analysis_chain():
    """A word-frequency pipeline over a document."""
    document = (
        "The pipeline operator binds its value to a hidden topic. "
        "The topic can be read many times; the value is evaluated "
        "exactly once. Nested pipelines get their own topics, so a "
        "pipeline inside a pipeline body does not affect the outer "
        "topic's value expression."
    )

    # Clean the document, count the words, and keep the top five.
    top = (
        document.lower()
        |> $ .replace(",", " ") .replace(".", " ") .replace(";", " ")
        |> $ .replace("'", "")
        |> $ .split()
        |> {word: $.count(word) for word in set($)}
        |> sorted($ .items(), key=lambda item: -item[1])[:5]
    )

    print("top-5 words:")
    for word, count in top:
        print(f"  {word} x{count}")


def aggregation_pipeline():
    """Aggregate a sequence, with a nested pipeline in the body."""
    measurements = [2.5, 3.1, 2.9, 4.4, 3.8, 3.0, 2.2, 4.1]

    # Outer topic: the whole list.  The nested pipeline computes the
    # variance from the outer topic and gets its own topic for the
    # nested body (the square root).
    stats = (
        measurements
        |> {
            "count": len($),
            "mean": sum($) / len($),
            "stdev": (
                sum((x - sum($) / len($)) ** 2 for x in $) / len($)
                |> $ ** 0.5
            ),
        }
    )

    print("measurement stats:")
    for key, value in stats.items():
        print(f"  {key}: {value:.3f}")


def once_only():
    """Show the value is evaluated exactly once."""
    calls = 0

    def expensive():
        nonlocal calls
        calls += 1
        return [calls * i for i in range(1, 4)]

    doubled = expensive() |> [2 * x for x in $]
    print(f"expensive() called {calls} time(s), doubled = {doubled}")


if __name__ == "__main__":
    pipeline_over_records()
    print()
    text_analysis_chain()
    print()
    aggregation_pipeline()
    print()
    once_only()