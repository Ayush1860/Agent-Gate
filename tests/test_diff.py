"""Unified-diff parsing. Only added/modified lines are reviewable."""

from __future__ import annotations

from agentgate.diff import (
    changed_files,
    parse_diff,
    render_hunks,
    total_added_lines,
)

SIMPLE = """diff --git a/app/calc.py b/app/calc.py
index 1111111..2222222 100644
--- a/app/calc.py
+++ b/app/calc.py
@@ -3,6 +3,8 @@ import math
 def area(r):
     return math.pi * r * r

+def perimeter(r):
+    return 2 * math.pi * r

 def volume(r):
     return (4 / 3) * math.pi * r ** 3
"""


def test_added_lines_are_captured_with_new_file_line_numbers():
    hunks = parse_diff(SIMPLE)
    assert len(hunks) == 1
    assert hunks[0].file == "app/calc.py"
    assert hunks[0].added_lines == [
        (6, "def perimeter(r):"),
        (7, "    return 2 * math.pi * r"),
    ]


def test_removed_lines_are_ignored_and_do_not_advance_the_counter():
    patch = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -10,4 +10,4 @@
 keep
-old_one
-old_two
+new_one
 tail
"""
    hunks = parse_diff(patch)
    assert hunks[0].added_lines == [(11, "new_one")]


def test_multiple_files_produce_separate_hunks():
    patch = SIMPLE + """diff --git a/app/db.py b/app/db.py
--- a/app/db.py
+++ b/app/db.py
@@ -1,2 +1,3 @@
 import sqlite3
+CONN = None

"""
    hunks = parse_diff(patch)
    assert changed_files(hunks) == ["app/calc.py", "app/db.py"]
    assert total_added_lines(hunks) == 3


def test_multiple_hunks_in_one_file():
    patch = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1,3 @@
 alpha
+beta
@@ -20,2 +21,3 @@
 gamma
+delta
"""
    hunks = parse_diff(patch)
    assert len(hunks) == 2
    assert hunks[0].added_lines == [(2, "beta")]
    assert hunks[1].added_lines == [(22, "delta")]


def test_a_new_file_is_parsed_from_dev_null():
    patch = """diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+import os
+X = 1
"""
    hunks = parse_diff(patch)
    assert hunks[0].file == "new.py"
    assert hunks[0].added_lines == [(1, "import os"), (2, "X = 1")]


def test_a_deleted_file_yields_no_reviewable_hunks():
    patch = """diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-import os
-X = 1
"""
    assert parse_diff(patch) == []


def test_binary_and_lockfile_changes_are_skipped():
    patch = """diff --git a/logo.png b/logo.png
--- a/logo.png
+++ b/logo.png
@@ -1 +1,2 @@
+binarygarbage
diff --git a/poetry.lock b/poetry.lock
--- a/poetry.lock
+++ b/poetry.lock
@@ -1 +1,2 @@
+name = "x"
"""
    assert parse_diff(patch) == []


def test_no_newline_marker_is_not_treated_as_content():
    patch = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1,2 @@
 x = 1
+y = 2
\\ No newline at end of file
"""
    assert parse_diff(patch)[0].added_lines == [(2, "y = 2")]


def test_an_empty_diff_parses_to_nothing():
    assert parse_diff("") == []
    assert parse_diff("not a diff at all\njust prose\n") == []


def test_a_truncated_patch_yields_what_could_be_recovered():
    truncated = SIMPLE[: SIMPLE.index("+    return 2")]
    hunks = parse_diff(truncated)
    assert hunks and hunks[0].added_lines == [(6, "def perimeter(r):")]


def test_a_single_line_hunk_header_without_a_count():
    patch = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -5 +5 @@
+only = 1
"""
    assert parse_diff(patch)[0].added_lines == [(5, "only = 1")]


# --------------------------------------------------------------------------- #
# Rendering -- this format is a contract with agentgate/llm/mock.py
# --------------------------------------------------------------------------- #
def test_render_uses_the_file_and_pipe_line_format():
    rendered = render_hunks(parse_diff(SIMPLE))
    assert rendered.splitlines() == [
        "FILE: app/calc.py",
        "6| def perimeter(r):",
        "7|     return 2 * math.pi * r",
    ]


def test_render_emits_the_file_header_once_per_file():
    patch = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,1 +1,2 @@
+one
@@ -9,1 +10,2 @@
+two
"""
    assert render_hunks(parse_diff(patch)).count("FILE: a.py") == 1


def test_render_truncates_very_large_diffs_and_says_so():
    big = ["diff --git a/big.py b/big.py", "--- a/big.py", "+++ b/big.py", "@@ -1,1 +1,900 @@"]
    big += [f"+line_{i} = {i}" for i in range(900)]
    rendered = render_hunks(parse_diff("\n".join(big)), max_lines=50)
    assert "[diff truncated at 50 added lines]" in rendered
    assert rendered.count("| line_") == 50


def test_the_render_format_round_trips_through_the_mock_parser():
    from agentgate.llm.mock import _parse_payload

    rendered = render_hunks(parse_diff(SIMPLE))
    recovered = _parse_payload([{"role": "user", "content": rendered}])
    assert recovered == {
        "app/calc.py": [
            (6, "def perimeter(r):"),
            (7, "    return 2 * math.pi * r"),
        ]
    }
