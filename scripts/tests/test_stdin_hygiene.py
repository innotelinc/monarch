#!/usr/bin/env python3
"""The rule that came out of the 2026-10-06/07 stdin hunt, as a test.

A host-side script is often fed to a shell on stdin (`ssh host 'bash -s' <<EOF`,
which is how this deployment is driven, and how the drift check's own runs were
verified). Any child that reads the caller's stdin therefore eats the rest of the
script: the run does not fail, it *ends*, silently, wherever that child was. Two
consumers have been found that way - `docker exec -i` in
`scripts/clipbucket-install.py` and the `ffprobe` in `scripts/clipbucket-library.py`
- and both were fixed by saying at the spawn that the child gets no stdin.

Saying it per-site does not survive the next site, so this test is the rule:

  * a spawn of a tool that reads stdin (`ffprobe`, `ffmpeg`, `ssh`, `mysql`,
    `nsupdate`, and `docker` carrying `-i` or a `compose run`) must pass `stdin=`
    or `input=` - the choice has to be written down, even when it is DEVNULL;
  * a shell invocation of `docker` that attaches stdin (`exec -i`, `run -i`,
    `compose ... run`) must redirect stdin, because `docker compose run` forwards
    the caller's stream into the container and `-T` does not stop it (measured on
    monarch 2026-10-07: a 5000-line file on stdin was consumed to its last byte,
    23893 of 23893);
  * `ssh` must be `ssh -n` (or redirect its stdin), because `ssh` without `-n`
    forwards the caller's stdin to the far side (measured the same day: 1092 of
    1092 bytes consumed by `ssh host cat`, 0 by `ssh -n host cat`). A remote
    command line is a command, not a filter.

What this cannot see: argv built at runtime (`["docker", *args]`) and commands
assembled by `eval`. It is a ratchet, not a proof - the two consumers above are
pinned behaviourally where they lived, next to the code that spawns them.

Run: python3 -m unittest discover -s scripts/tests -t scripts/tests -v
"""

from __future__ import annotations

import ast
import re
import shlex
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

# Spawns to inspect, and the tools that read the caller's stdin when they are run.
SPAWN_CALLS = {"run", "Popen", "call", "check_call", "check_output"}
STDIN_READING_TOOLS = {"ffprobe", "ffmpeg", "ssh", "mysql", "nsupdate", "nsupdate.exe"}

# Shell files to inspect: everything deployed (scripts/, setup.sh, the git hooks).
SHELL_GLOBS = ("scripts/*.sh", "setup.sh", ".githooks/*.sh")


def command_words(source: str) -> list[str]:
    """A shell segment as words, ignoring anything that is not a word."""
    try:
        words = shlex.split(source)
    except ValueError:  # an unbalanced quote: fall back to whitespace
        words = source.split()
    return [word for word in words if word]


def python_violations(source: str, label: str = "test") -> list[str]:
    """Spawns of stdin-reading tools that do not say what happens to stdin."""
    problems: list[str] = []
    for node in ast.walk(ast.parse(source, filename=label)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in SPAWN_CALLS:
            continue
        if not isinstance(func.value, ast.Name) or func.value.id != "subprocess":
            continue
        words = _literal_argv(node)
        if words is None or not _reads_stdin(words):
            continue
        if any(keyword.arg in ("stdin", "input") for keyword in node.keywords):
            continue
        problems.append(
            f"{label}:{node.lineno}: subprocess.{func.attr}({words[0]!r} ...) "
            "reads the caller's stdin and does not pass stdin= or input="
        )
    return problems


def _literal_argv(node: ast.Call) -> list[str] | None:
    """The argv words, or None when they are not all literals.

    A `None` word means "built at runtime" - the flags after it are unknown, so
    the call is not judged rather than judged wrongly.
    """
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, (ast.List, ast.Tuple)):
        words = []
        for element in first.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                words.append(element.value)
            else:
                words.append("")
        return words
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return command_words(first.value)
    return None


def _reads_stdin(words: list[str]) -> bool:
    known = [word for word in words if word]
    if not known:
        return False
    tool = known[0].rsplit("/", 1)[-1]
    if tool in STDIN_READING_TOOLS:
        return True
    if tool == "docker":
        if "-i" in known or "--interactive" in known:
            return True
        # `docker compose run` attaches and forwards the caller's stdin.
        return "compose" in known and "run" in known
    if tool in ("env", "sudo") and len(known) > 1:
        return _reads_stdin(known[1:])
    return False


def _blank_comments(text: str) -> str:
    """Every comment replaced by spaces of the same length, quotes respected.

    Same length on purpose: the offsets, and so the line numbers in a failure,
    still point at the original file.
    """
    blanked = []
    for line in text.splitlines(keepends=True):
        index = 0
        quote = ""
        while index < len(line):
            char = line[index]
            if quote:
                if char == quote:
                    quote = ""
            elif char in "'\"":
                quote = char
            elif char == "#" and (index == 0 or line[index - 1].isspace()):
                break
            index += 1
        blanked.append(line[:index] + " " * (len(line) - index))
    return "".join(blanked)


def _segments(text: str):
    """(offset, segment) for each simple command, continuations joined.

    Two characters become two spaces: line numbers stay right. Quoted separators
    split a command in two, which can only make this miss something, never
    invent it.
    """
    joined = re.sub(r"\\\n", "  ", _blank_comments(text))
    # NOT a lone `&`: `2>&1` is a redirect, and splitting there would put the
    # `< /dev/null` that follows it in a different command (measured: the first
    # version of this checker reported all three fixed sites).
    for match in re.finditer(r"[^;\n|]+", joined):
        yield match.start(), match.group(0)


def shell_violations(text: str, label: str = "test") -> list[str]:
    problems: list[str] = []
    line_starts = [0]
    for line in text.splitlines(keepends=True):
        line_starts.append(line_starts[-1] + len(line))

    def line_of(offset: int) -> int:
        number = 1
        for start in line_starts:
            if start <= offset:
                number = line_starts.index(start) + 1
        return number

    for offset, segment in _segments(text):
        words = command_words(segment)
        if not words:
            continue
        where = f"{label}:{line_of(offset)}"
        attaches_stdin = "docker" in words and (
            "-i" in words
            or "--interactive" in words
            or ("compose" in words and "run" in words)
        )
        if attaches_stdin and "<" not in segment:
            problems.append(
                f"{where}: {' '.join(words[:6])} ... attaches the caller's stdin "
                "and nothing redirects it (< /dev/null)"
            )
        if words[0] == "ssh" and "-n" not in words and "<" not in segment:
            problems.append(
                f"{where}: ssh without -n hands the caller's stdin to the far side"
            )
    return problems


class TheRuleHolds(unittest.TestCase):
    def test_the_checker_notices_a_planted_python_violation(self):
        # Without this, a green suite could just mean the checker matches nothing.
        planted = (
            "import subprocess\n"
            "subprocess.run(['ffprobe', '-v', 'error', 'x.mp4'], capture_output=True)\n"
            "subprocess.run(['docker', 'exec', '-i', 'clipbucket', 'cat', 'f'], capture_output=True)\n"
        )
        self.assertEqual(len(python_violations(planted, "planted.py")), 2)
        fixed = (
            "import subprocess\n"
            "subprocess.run(['ffprobe', '-v', 'error', 'x.mp4'], stdin=subprocess.DEVNULL)\n"
            "subprocess.run(['docker', 'exec', container, 'cat', 'f'], capture_output=True)\n"
            "subprocess.run(['git', 'ls-files'], capture_output=True)\n"
        )
        self.assertEqual(python_violations(fixed, "fixed.py"), [])

    def test_the_checker_notices_a_planted_shell_violation(self):
        planted = (
            "docker compose -f docker-compose.yml run --rm monarch-init >/dev/null 2>&1 || true\n"
            "# docker compose run --rm x    (a comment is not a command)\n"
            "docker exec -i clipbucket cat /srv/config.php\n"
            'ssh "$host" "id -u"\n'
        )
        self.assertEqual(len(shell_violations(planted, "planted.sh")), 3)
        fixed = (
            "docker compose -f docker-compose.yml run --rm monarch-init >/dev/null 2>&1 < /dev/null || true\n"
            "docker exec clipbucket cat /srv/config.php\n"
            "ssh -n \"$host\" \"id -u\"\n"
            "have ssh || die 'ssh is required for deploy'\n"
            "# docker compose run --rm x   (this is a comment, not a command)\n"
        )
        self.assertEqual(shell_violations(fixed, "fixed.sh"), [])

    def test_every_stdin_reading_spawn_says_what_it_does_with_stdin(self):
        problems: list[str] = []
        for path in sorted(REPO.glob("scripts/*.py")) + sorted(REPO.glob("init/*.py")):
            problems += python_violations(path.read_text(encoding="utf-8"), str(path.relative_to(REPO)))
        self.assertEqual(problems, [], "\n".join(problems))

    def test_no_shell_invocation_inherits_the_callers_stdin(self):
        problems: list[str] = []
        for pattern in SHELL_GLOBS:
            for path in sorted(REPO.glob(pattern)):
                if not path.is_file():
                    continue
                problems += shell_violations(path.read_text(encoding="utf-8"), str(path.relative_to(REPO)))
        self.assertEqual(problems, [], "\n".join(problems))


if __name__ == "__main__":
    sys.exit(unittest.main())
