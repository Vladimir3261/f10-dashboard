"""
A small, dependency-free parser for the YAML subset used by mapping files.

The runtime deliberately has no third-party dependencies: this code runs on
a laptop in a car, and `pip install` is not always an option there. Rather
than make PyYAML a hard requirement - and rather than let a mapping file
load on one machine and fail on another because the two have different YAML
libraries - mapping files are restricted to a subset that is fully specified
here and parsed identically everywhere.

Supported
---------
    block mappings          key: value
    block sequences         - item
    nested blocks by indentation (spaces only)
    inline flow collections [1, 2, 3] and {a: 1, b: 2}
    block scalars           | and > with optional - chomping
    comments                # to end of line, outside quotes
    document markers        --- and ...
    scalars                 null ~ true false
                            123  -4  0x0C  0o17  0b1010
                            1.5  -2e3
                            'single quoted'  "double quoted \n escapes"
                            plain strings

Not supported (and rejected loudly): anchors, aliases, tags, merge keys,
multi-line flow collections, complex keys, tab indentation.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from .errors import MappingSyntaxError

__all__ = ["load", "loads"]


_INT_RE = re.compile(r"^[+-]?[0-9][0-9_]*$")
_FLOAT_RE = re.compile(
    r"^[+-]?(?:[0-9][0-9_]*\.[0-9_]*|\.[0-9][0-9_]*|[0-9][0-9_]*)"
    r"(?:[eE][+-]?[0-9]+)?$"
)
_KEY_RE = re.compile(r"^(?:\"(?:[^\"\\]|\\.)*\"|'(?:[^']|'')*'|[^:#\[\]{},]+?)\s*:(?=\s|$)")

_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "0": "\0",
    "\\": "\\", '"': '"', "'": "'", "/": "/", " ": " ",
}


class _Line:
    __slots__ = ("no", "indent", "text", "raw")

    def __init__(self, no: int, indent: int, text: str, raw: str):
        self.no = no
        self.indent = indent
        self.text = text
        self.raw = raw


def _strip_comment(line: str) -> str:
    """Remove a trailing # comment, ignoring # inside quotes."""
    out: List[str] = []
    quote: Optional[str] = None
    i = 0

    while i < len(line):
        ch = line[i]

        if quote:
            out.append(ch)

            if ch == "\\" and quote == '"' and i + 1 < len(line):
                out.append(line[i + 1])
                i += 2
                continue

            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#" and (not out or out[-1] in " \t"):
            break
        else:
            out.append(ch)

        i += 1

    return "".join(out).rstrip()


class _Parser:
    def __init__(self, text: str, source: str):
        self.source = source
        self.raw_lines = text.split("\n")
        self.lines: List[_Line] = []
        self.i = 0

        for no, raw in enumerate(self.raw_lines, start=1):
            if "\t" in raw[: len(raw) - len(raw.lstrip(" \t"))]:
                self._fail("tab used for indentation", no)

            stripped = raw.strip()

            if not stripped or stripped.startswith("#"):
                continue

            if stripped in ("---", "..."):
                continue

            text_ = _strip_comment(raw).strip()

            if not text_:
                continue

            self.lines.append(_Line(no, len(raw) - len(raw.lstrip(" ")), text_, raw))

    # -- helpers ----------------------------------------------------

    def _fail(self, message: str, lineno: Optional[int] = None) -> None:
        where = f"line {lineno}" if lineno else "end of file"
        raise MappingSyntaxError(f"{message} ({where})", source=self.source)

    def _peek(self) -> Optional[_Line]:
        return self.lines[self.i] if self.i < len(self.lines) else None

    # -- entry point ------------------------------------------------

    def parse(self) -> Any:
        if not self.lines:
            return None

        first = self.lines[0]
        value = self._parse_block(first.indent)

        if self.i < len(self.lines):
            self._fail("unexpected content after document", self._peek().no)

        return value

    def _parse_block(self, indent: int) -> Any:
        line = self._peek()

        if line is None or line.indent < indent:
            return None

        if line.text == "-" or line.text.startswith("- "):
            return self._parse_list(indent)

        return self._parse_map(indent)

    def _parse_map(self, indent: int) -> Dict[str, Any]:
        out: Dict[Any, Any] = {}

        while True:
            line = self._peek()

            if line is None or line.indent < indent:
                break

            if line.indent > indent:
                self._fail("unexpected indentation", line.no)

            if line.text == "-" or line.text.startswith("- "):
                self._fail("sequence item where a mapping key was expected", line.no)

            match = _KEY_RE.match(line.text)

            if not match:
                self._fail(f"expected 'key: value', got {line.text!r}", line.no)

            raw_key = match.group(0)[:-1].strip()
            rest = line.text[match.end():].strip()
            key = _scalar(raw_key, self, line.no)

            if key in out:
                self._fail(f"duplicate key {key!r}", line.no)

            self.i += 1
            out[key] = self._parse_value(rest, line)

        return out

    def _parse_list(self, indent: int) -> List[Any]:
        out: List[Any] = []

        while True:
            line = self._peek()

            if line is None or line.indent < indent:
                break

            if line.indent > indent:
                self._fail("unexpected indentation", line.no)

            if line.text != "-" and not line.text.startswith("- "):
                break

            rest = "" if line.text == "-" else line.text[2:].strip()

            if rest and _KEY_RE.match(rest):
                #
                # `- key: value` starts a mapping whose first key sits on
                # the dash line. Re-index that key at the column the rest
                # of the mapping will use, then parse it normally.
                #
                virtual = line.indent + 2
                self.lines[self.i] = _Line(line.no, virtual, rest, line.raw)
                out.append(self._parse_map(virtual))
                continue

            self.i += 1
            out.append(self._parse_value(rest, line))

        return out

    def _parse_value(self, rest: str, line: _Line) -> Any:
        if rest[:1] in ("|", ">"):
            return self._block_scalar(rest, line)

        if rest.startswith("[") or rest.startswith("{"):
            return _flow(rest, self, line.no)

        if rest:
            if rest[0] in "&*!":
                self._fail("anchors, aliases and tags are not supported", line.no)

            return _scalar(rest, self, line.no)

        nxt = self._peek()

        if nxt is None or nxt.indent <= line.indent:
            return None

        return self._parse_block(nxt.indent)

    def _block_scalar(self, header: str, line: _Line) -> str:
        style = header[0]
        chomp = header[1:].strip()

        if chomp not in ("", "-", "+"):
            self._fail(f"unsupported block scalar header {header!r}", line.no)

        body: List[str] = []
        indent: Optional[int] = None
        pos = line.no  # raw_lines is 1-based, line.no is this key's line

        while pos < len(self.raw_lines):
            raw = self.raw_lines[pos]
            stripped = raw.strip()
            cur = len(raw) - len(raw.lstrip(" "))

            if stripped and cur <= line.indent:
                break

            if indent is None and stripped:
                indent = cur

            body.append(raw[indent:] if indent is not None else "")
            pos += 1

        #
        # Skip the consumed lines in the logical (comment-stripped) stream.
        #
        while self.i < len(self.lines) and self.lines[self.i].no <= pos:
            self.i += 1

        while body and not body[-1].strip():
            body.pop()

        if style == "|":
            text = "\n".join(body)
        else:
            #
            # Folded: blank lines become hard breaks, everything else is
            # joined with a single space.
            #
            chunks: List[str] = []

            for part in body:
                if not part.strip():
                    chunks.append("")
                elif chunks and chunks[-1]:
                    chunks[-1] = chunks[-1] + " " + part.strip()
                else:
                    chunks.append(part.strip())

            text = "\n".join(chunks)

        if chomp == "-":
            return text

        return text + "\n" if text else ""


# ---------------------------------------------------------------- scalars


def _scalar(text: str, parser: "_Parser", lineno: int) -> Any:
    text = text.strip()

    if text.startswith('"'):
        if not text.endswith('"') or len(text) < 2:
            parser._fail("unterminated double-quoted string", lineno)

        return _unescape(text[1:-1])

    if text.startswith("'"):
        if not text.endswith("'") or len(text) < 2:
            parser._fail("unterminated single-quoted string", lineno)

        return text[1:-1].replace("''", "'")

    if text in ("", "null", "Null", "NULL", "~"):
        return None

    if text in ("true", "True", "TRUE"):
        return True

    if text in ("false", "False", "FALSE"):
        return False

    lowered = text.lower()

    try:
        if lowered.startswith("0x") or lowered.startswith("-0x"):
            return int(text, 16)

        if lowered.startswith("0o") or lowered.startswith("-0o"):
            return int(text, 8)

        if lowered.startswith("0b") or lowered.startswith("-0b"):
            return int(text, 2)
    except ValueError:
        parser._fail(f"malformed number {text!r}", lineno)

    if _INT_RE.match(text):
        return int(text.replace("_", ""))

    if _FLOAT_RE.match(text) and any(c in text for c in ".eE"):
        return float(text.replace("_", ""))

    if text in (".inf", ".Inf", ".INF"):
        return float("inf")

    if text in ("-.inf", "-.Inf", "-.INF"):
        return float("-inf")

    if text in (".nan", ".NaN", ".NAN"):
        return float("nan")

    return text


def _unescape(text: str) -> str:
    out: List[str] = []
    i = 0

    while i < len(text):
        ch = text[i]

        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]

            if nxt == "u" and i + 5 < len(text) + 1:
                out.append(chr(int(text[i + 2:i + 6], 16)))
                i += 6
                continue

            out.append(_ESCAPES.get(nxt, nxt))
            i += 2
            continue

        out.append(ch)
        i += 1

    return "".join(out)


# ------------------------------------------------------------------- flow


def _flow(text: str, parser: "_Parser", lineno: int) -> Any:
    value, pos = _flow_node(text, 0, parser, lineno)
    rest = text[pos:].strip()

    if rest:
        parser._fail(f"trailing content after flow collection: {rest!r}", lineno)

    return value


def _flow_node(text: str, i: int, parser: "_Parser", lineno: int) -> Tuple[Any, int]:
    i = _skip_ws(text, i)

    if i >= len(text):
        parser._fail("unexpected end of flow collection", lineno)

    if text[i] == "[":
        return _flow_seq(text, i, parser, lineno)

    if text[i] == "{":
        return _flow_map(text, i, parser, lineno)

    raw, i = _flow_token(text, i, parser, lineno)

    return _scalar(raw, parser, lineno), i


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t":
        i += 1

    return i


def _flow_token(text: str, i: int, parser: "_Parser", lineno: int) -> Tuple[str, int]:
    """Read one scalar token up to , ] } or : (respecting quotes)."""
    start = i

    if text[i] in "\"'":
        quote = text[i]
        i += 1

        while i < len(text):
            if text[i] == "\\" and quote == '"':
                i += 2
                continue

            if text[i] == quote:
                i += 1
                break

            i += 1
        else:
            parser._fail("unterminated quoted string in flow collection", lineno)

        return text[start:i], i

    while i < len(text) and text[i] not in ",]}:":
        i += 1

    return text[start:i].strip(), i


def _flow_seq(text: str, i: int, parser: "_Parser", lineno: int) -> Tuple[List[Any], int]:
    out: List[Any] = []
    i += 1
    i = _skip_ws(text, i)

    if i < len(text) and text[i] == "]":
        return out, i + 1

    while True:
        value, i = _flow_node(text, i, parser, lineno)
        out.append(value)
        i = _skip_ws(text, i)

        if i >= len(text):
            parser._fail("unterminated flow sequence", lineno)

        if text[i] == ",":
            i += 1
            i = _skip_ws(text, i)

            if i < len(text) and text[i] == "]":
                return out, i + 1

            continue

        if text[i] == "]":
            return out, i + 1

        parser._fail(f"unexpected {text[i]!r} in flow sequence", lineno)


def _flow_map(text: str, i: int, parser: "_Parser", lineno: int) -> Tuple[Dict[Any, Any], int]:
    out: Dict[Any, Any] = {}
    i += 1
    i = _skip_ws(text, i)

    if i < len(text) and text[i] == "}":
        return out, i + 1

    while True:
        i = _skip_ws(text, i)
        raw, i = _flow_token(text, i, parser, lineno)
        i = _skip_ws(text, i)

        if i >= len(text) or text[i] != ":":
            parser._fail("expected ':' in flow mapping", lineno)

        key = _scalar(raw, parser, lineno)
        value, i = _flow_node(text, i + 1, parser, lineno)

        if key in out:
            parser._fail(f"duplicate key {key!r} in flow mapping", lineno)

        out[key] = value
        i = _skip_ws(text, i)

        if i >= len(text):
            parser._fail("unterminated flow mapping", lineno)

        if text[i] == ",":
            i += 1
            i = _skip_ws(text, i)

            if i < len(text) and text[i] == "}":
                return out, i + 1

            continue

        if text[i] == "}":
            return out, i + 1

        parser._fail(f"unexpected {text[i]!r} in flow mapping", lineno)


# ------------------------------------------------------------------- API


def loads(text: str, source: str = "<string>") -> Any:
    """Parse a YAML-subset document from a string."""
    return _Parser(text, source).parse()


def load(path: str) -> Any:
    """Parse a YAML-subset document from a file."""
    with open(path, "r", encoding="utf-8") as handle:
        return loads(handle.read(), source=path)
