#!/usr/bin/env python3
"""Generate the Python API reference from mkdocstrings-style ::: directives.

Reads scripts/python-api-directives.md, the checked-in source of truth for
which classes and functions appear in the reference and in what order
(mirrors the codebase structure; see AGENTS.md). Resolves each
`::: module.Class` directive with griffe against the installed/source
package, and writes plain markdown to docs/rp-sdk/reference/python-api.md at
the monorepo root.

docs/rp-sdk/reference/python-api.md is committed, generated output. Do not
hand-edit it. Edit the source docstrings in src/researcher_profiles/, or add
a directive to python-api-directives.md, then re-run this script.

Requires `griffe` (the `docs` extra). Griffe reads source statically via
`search_paths`, so no runtime extras are required to regenerate this file. It
never imports researcher_profiles or scholarcore; it only parses their source
trees. A failure to resolve a directive (missing griffe, an unloadable
module, a missing member) is a hard error: `main()` exits non-zero rather than
writing a placeholder into the committed output. Pass `--check` to render to
memory and compare against the committed file without writing, for use in CI.
"""

import argparse
import re
import sys
from functools import lru_cache
from pathlib import Path

RP_SDK_ROOT = Path(__file__).resolve().parent.parent
MONOREPO_ROOT = RP_SDK_ROOT.parent
SRC_DIR = RP_SDK_ROOT / "src"
SCHOLARCORE_SRC_DIR = MONOREPO_ROOT / "scholarcore" / "src"

SOURCE_FILE = RP_SDK_ROOT / "scripts" / "python-api-directives.md"
OUT_FILE = MONOREPO_ROOT / "docs" / "rp-sdk" / "reference" / "python-api.md"

DIRECTIVE_RE = re.compile(r"^::: (.+)$")
OPTION_RE = re.compile(r"^    (\w+):(.*)$")


def parse_directive(lines, start):
    """Parse a ::: directive block starting at line index `start`."""
    m = DIRECTIVE_RE.match(lines[start])
    module_path = m.group(1).strip()
    options = {}
    i = start + 1

    if i < len(lines) and lines[i].strip() == "options:":
        i += 1
        while i < len(lines):
            om = OPTION_RE.match(lines[i])
            if om:
                options[om.group(1).strip()] = om.group(2).strip()
                i += 1
            elif lines[i].startswith("      "):
                i += 1
            else:
                break

    return module_path, options, i


def _resolve(obj):
    """Resolve a griffe Alias to its final target."""
    import griffe

    if isinstance(obj, griffe.Alias):
        try:
            return obj.final_target
        except Exception:
            return obj
    return obj


def _format_signature(obj):
    """Format a function/method signature string."""
    obj = _resolve(obj)
    if not hasattr(obj, "parameters") or not obj.parameters:
        return obj.name

    params = []
    for p in obj.parameters:
        if p.name in ("self", "cls"):
            continue
        s = p.name
        if p.annotation:
            ann = str(p.annotation)
            ann = ann.replace("typing.", "")
            s += f": {ann}"
        if p.default and str(p.default) != "None":
            s += f" = {p.default}"
        elif p.default:
            s += " = None"
        params.append(s)
    return f"{obj.name}({', '.join(params)})"


_INLINE_ROLE_RE = re.compile(r":(?:meth|attr|class|mod|func|obj|data|exc):`([^`]*)`")
_RST_DOUBLE_BACKTICK_RE = re.compile(r"``([^`]+?)``")


def _convert_inline_roles(text):
    """Convert Sphinx/rST inline markup that plain markdown does not share:
    roles (``:meth:`X``, ``:class:`~a.b.C`, ...) and double-backtick
    ``literal`` spans, both to single-backtick markdown code spans. Role
    targets follow Sphinx's own display rules: a leading ``~`` shows only the
    last dotted component, and ``Title <target>`` shows just the title."""

    def repl(m):
        target = m.group(1)
        if "<" in target and target.endswith(">"):
            title, _, _ = target.rpartition("<")
            return f"`{title.strip()}`"
        if target.startswith("~"):
            target = target[1:].rsplit(".", 1)[-1]
        return f"`{target}`"

    text = _INLINE_ROLE_RE.sub(repl, text)
    text = _RST_DOUBLE_BACKTICK_RE.sub(r"`\1`", text)
    return text


def _convert_docstring(text):
    """Convert Sphinx/rST docstring markup to markdown."""
    if not text:
        return ""

    text = _convert_inline_roles(text)

    lines = text.split("\n")
    result = []
    i = 0
    in_params = False

    while i < len(lines):
        line = lines[i]

        m = re.match(r"^:param\s+(.+?)\s+(\w+):\s*(.*)", line)
        if m:
            ptype_candidate, pname, desc = m.group(1), m.group(2), m.group(3)
            if not re.search(r"\.\s", ptype_candidate):
                if not in_params:
                    result.append("")
                    result.append("**Parameters:**")
                    result.append("")
                    in_params = True
                while i + 1 < len(lines) and lines[i + 1].startswith("    "):
                    i += 1
                    desc += " " + lines[i].strip()
                result.append(f"- `{pname}` (*{ptype_candidate}*): {desc}")
                i += 1
                continue

        m = re.match(r"^:param\s+(\w+):\s*(.*)", line)
        if m:
            if not in_params:
                result.append("")
                result.append("**Parameters:**")
                result.append("")
                in_params = True
            pname, desc = m.group(1), m.group(2)
            while i + 1 < len(lines) and lines[i + 1].startswith("    "):
                i += 1
                desc += " " + lines[i].strip()
            result.append(f"- `{pname}`: {desc}")
            i += 1
            continue

        m = re.match(r"^:type\s+\w+:\s*(.*)", line)
        if m:
            i += 1
            continue

        m = re.match(r"^:returns?\s+(.+?):\s*(.*)", line)
        if m:
            in_params = False
            rtype, desc = m.group(1), m.group(2)
            while i + 1 < len(lines) and lines[i + 1].startswith("    "):
                i += 1
                desc += " " + lines[i].strip()
            result.append("")
            result.append(f"**Returns** (*{rtype}*): {desc}")
            i += 1
            continue

        m = re.match(r"^:returns?:\s*(.*)", line)
        if m:
            in_params = False
            desc = m.group(1)
            while i + 1 < len(lines) and lines[i + 1].startswith("    "):
                i += 1
                desc += " " + lines[i].strip()
            result.append("")
            result.append(f"**Returns:** {desc}")
            i += 1
            continue

        m = re.match(r"^:rtype:\s*(.*)", line)
        if m:
            result.append(f"**Return type:** *{m.group(1)}*")
            i += 1
            continue

        m = re.match(r"^:raises?\s+(\w+):\s*(.*)", line)
        if m:
            in_params = False
            desc = m.group(2)
            while i + 1 < len(lines) and lines[i + 1].startswith("    "):
                i += 1
                desc += " " + lines[i].strip()
            result.append("")
            result.append(f"**Raises** *{m.group(1)}*: {desc}")
            i += 1
            continue

        if line.strip() == ":Example:":
            in_params = False
            result.append("")
            result.append("**Example:**")
            i += 1
            continue

        m = re.match(r"^\.\. code-block::\s*(\w+)", line)
        if m:
            in_params = False
            lang = m.group(1)
            result.append(f"```{lang}")
            i += 1
            if i < len(lines) and lines[i].strip() == "":
                i += 1
            while i < len(lines) and (lines[i].startswith("    ") or lines[i].strip() == ""):
                if (
                    lines[i].strip() == ""
                    and i + 1 < len(lines)
                    and not lines[i + 1].startswith("    ")
                ):
                    break
                result.append(lines[i][4:] if lines[i].startswith("    ") else "")
                i += 1
            result.append("```")
            continue

        in_params = False
        result.append(line)
        i += 1

    return "\n".join(result)


def render_object_doc(module_path, options):
    """Use griffe to extract and format documentation for a Python object.

    Raises rather than returning a placeholder on any failure path: a griffe
    import failure, an unloadable module, or a missing member. A placeholder
    string silently written into the committed output is exactly how this
    file went stale before; this must fail the generation run instead.
    """
    try:
        import griffe
    except ImportError as e:
        raise RuntimeError(
            f"cannot render `{module_path}`: griffe is not installed (requires the 'docs' extra)"
        ) from e

    parts = module_path.rsplit(".", 1)
    if len(parts) == 2:
        module_name, obj_name = parts
    else:
        module_name = parts[0]
        obj_name = None

    try:
        mod = griffe.load(module_name, search_paths=[str(SRC_DIR), str(SCHOLARCORE_SRC_DIR)])
    except Exception as e:
        raise RuntimeError(f"cannot render `{module_path}`: module not loadable: {e}") from e

    if obj_name:
        try:
            obj = mod.members[obj_name]
        except KeyError as e:
            raise RuntimeError(f"cannot render `{module_path}`: object not found") from e
    else:
        obj = mod

    return _format_object(obj, options)


_PYDANTIC_INTERNAL_NAMES = {
    "model_config",
    "model_fields",
    "model_fields_set",
    "model_extra",
    "model_computed_fields",
}


def _pydantic_field_info(attr):
    """If a griffe Attribute's assigned value is a ``pydantic.Field(...)``
    call, return its ``(default, description)`` as plain strings (either may
    be ``None``). Griffe extracts field annotations but not ``Field()``
    keyword arguments. Without this, a model's per-field default and
    description, the bulk of what a pydantic settings-style class
    communicates, would silently drop out of the generated reference."""
    import griffe

    if not isinstance(attr, griffe.Attribute):
        return None, None
    value = attr.value
    if not isinstance(value, griffe.ExprCall):
        return None, None
    func = getattr(value, "function", None)
    if getattr(func, "name", None) != "Field":
        return None, None
    default = None
    description = None
    for kw in value.arguments:
        if not isinstance(kw, griffe.ExprKeyword):
            continue
        if kw.name == "default":
            default = str(kw.value)
        elif kw.name == "description":
            description = str(kw.value).strip("'\"")
    return default, description


@lru_cache(maxsize=None)
def _read_lines(filepath):
    return Path(filepath).read_text().split("\n")


_COLON_COMMENT_RE = re.compile(r"^(\s*)#:\s?(.*)$")


def _source_comment_doc(attr):
    """Fall back to a Sphinx-style ``#: comment`` immediately above an
    attribute's assignment, when griffe found no real docstring for it.

    This codebase documents most dataclass/SQLModel/pydantic fields with
    ``#:`` comments (651 occurrences repo-wide) rather than post-assignment
    string-literal docstrings. Griffe is AST-based and comments never reach
    the AST, so it has no way to see these. Without this fallback, every
    such field would render with a bare type and no description at all,
    which is most of what these classes have to say. Reads the field's own
    source file directly (via its recorded line number) rather than
    requiring any change to the source's documentation style."""
    filepath = getattr(attr, "filepath", None)
    lineno = getattr(attr, "lineno", None)
    if not filepath or not lineno:
        return None
    try:
        lines = _read_lines(filepath)
    except OSError:
        return None

    collected = []
    i = lineno - 2  # 0-indexed line just above the (1-indexed) assignment
    while i >= 0:
        m = _COLON_COMMENT_RE.match(lines[i])
        if not m:
            break
        collected.append(m.group(2))
        i -= 1
    if not collected:
        return None
    collected.reverse()
    return _convert_inline_roles(" ".join(collected))


def _format_object(obj, options, heading_level=3):
    """Format a griffe object as markdown."""
    import griffe

    resolved = _resolve(obj)
    hl = int(options.get("heading_level", heading_level))
    lines = []
    prefix = "#" * hl

    is_class = isinstance(resolved, griffe.Class)
    is_func = isinstance(resolved, griffe.Function)
    is_attr = isinstance(resolved, griffe.Attribute)

    if is_class:
        sig = _format_signature(resolved)
        lines.append(f"{prefix} *class* `{sig}`")
        lines.append("")

        if resolved.docstring:
            lines.append(_convert_docstring(resolved.docstring.value))
            lines.append("")

        if options.get("merge_init_into_class", "false").lower() == "true":
            if "__init__" in resolved.members:
                init = _resolve(resolved.members["__init__"])
                if init.docstring:
                    lines.append(_convert_docstring(init.docstring.value))
                    lines.append("")

        methods = []
        properties = []
        classmethods = []

        for member_name, member in resolved.members.items():
            if member_name.startswith("_") or member_name in _PYDANTIC_INTERNAL_NAMES:
                continue
            m = _resolve(member)
            if isinstance(m, griffe.Function):
                if any(
                    d.value == "property" or "property" in str(d.value)
                    for d in (m.decorators or [])
                ):
                    properties.append((member_name, m))
                elif any(
                    d.value == "classmethod" or "classmethod" in str(d.value)
                    for d in (m.decorators or [])
                ):
                    classmethods.append((member_name, m))
                else:
                    methods.append((member_name, m))
            elif isinstance(m, griffe.Attribute):
                properties.append((member_name, m))

        if properties:
            lines.append(f"{'#' * (hl + 1)} Properties")
            lines.append("")
            for pname, prop in sorted(properties, key=lambda x: x[0]):
                prop_resolved = _resolve(prop)
                ret = ""
                if isinstance(prop_resolved, griffe.Function) and prop_resolved.returns:
                    ret = f" -> *{prop_resolved.returns}*"
                elif isinstance(prop_resolved, griffe.Attribute) and prop_resolved.annotation:
                    ret = f": *{prop_resolved.annotation}*"
                default, field_doc = _pydantic_field_info(prop_resolved)
                if default is not None:
                    ret += f" = `{default}`"
                lines.append(f"**`{pname}`**{ret}")
                if prop_resolved.docstring:
                    doc = _convert_inline_roles(prop_resolved.docstring.value.split("\n")[0])
                    lines.append(f": {doc}")
                elif field_doc:
                    lines.append(f": {_convert_inline_roles(field_doc)}")
                elif isinstance(prop_resolved, griffe.Attribute):
                    source_doc = _source_comment_doc(prop_resolved)
                    if source_doc:
                        lines.append(f": {source_doc}")
                lines.append("")

        if classmethods:
            lines.append(f"{'#' * (hl + 1)} Class Methods")
            lines.append("")
            for mname, method in sorted(classmethods, key=lambda x: x[0]):
                _render_method(lines, method, hl + 2)

        if methods:
            lines.append(f"{'#' * (hl + 1)} Methods")
            lines.append("")
            for mname, method in sorted(methods, key=lambda x: x[0]):
                _render_method(lines, method, hl + 2)

    elif is_func:
        sig = _format_signature(resolved)
        lines.append(f"{prefix} `{sig}`")
        lines.append("")
        if resolved.docstring:
            lines.append(_convert_docstring(resolved.docstring.value))
            lines.append("")

    elif is_attr:
        ann = f": *{resolved.annotation}*" if resolved.annotation else ""
        lines.append(f"{prefix} `{resolved.name}`{ann}")
        lines.append("")
        if resolved.docstring:
            lines.append(_convert_docstring(resolved.docstring.value))
            lines.append("")

    elif isinstance(resolved, griffe.Module):
        if resolved.docstring:
            lines.append(_convert_docstring(resolved.docstring.value))
            lines.append("")
        for member_name, member in resolved.members.items():
            if member_name.startswith("_"):
                continue
            m = _resolve(member)
            if isinstance(m, (griffe.Class, griffe.Function)):
                lines.append(_format_object(member, {}, hl))

    return "\n".join(lines)


def _render_method(lines, method, hl):
    """Render a single method."""
    resolved = _resolve(method)
    sig = _format_signature(resolved)
    prefix = "#" * hl
    lines.append(f"{prefix} `{sig}`")
    lines.append("")
    if resolved.docstring:
        lines.append(_convert_docstring(resolved.docstring.value))
    lines.append("")


GENERATED_BANNER = (
    "<!-- Generated by rp-sdk/scripts/render_python_api.py. Do not edit by "
    "hand; edit the docstrings or python-api-directives.md. -->"
)


def strip_source_template_note(lines):
    """Drop the leading ``<!-- SOURCE TEMPLATE ... -->`` maintainer comment
    and replace it with a one-line generated-output banner.

    The SOURCE TEMPLATE note documents this file's own role as the unrendered
    source; it says nothing about the rendered output and must not leak into
    docs/rp-sdk/reference/python-api.md.
    """
    for i, line in enumerate(lines):
        if line.strip().startswith("<!-- SOURCE TEMPLATE"):
            for j in range(i, len(lines)):
                if lines[j].rstrip().endswith("-->"):
                    end = j + 1
                    if end < len(lines) and lines[end].strip() == "":
                        end += 1  # also drop one blank separator line
                    return lines[:i] + [GENERATED_BANNER, ""] + lines[end:]
            break
    return [GENERATED_BANNER, ""] + lines


def render_api_text(src):
    """Render the directives source file to markdown text (in memory)."""
    text = src.read_text()
    lines = strip_source_template_note(text.split("\n"))
    result = []
    i = 0

    while i < len(lines):
        if DIRECTIVE_RE.match(lines[i]):
            module_path, options, end_i = parse_directive(lines, i)
            rendered = render_object_doc(module_path, options)
            result.append(rendered)
            i = end_i
        else:
            result.append(lines[i])
            i += 1

    return "\n".join(result)


def process_api_file(src, dst):
    """Process the directives source file, writing rendered output to dst."""
    if not src.exists():
        print(f"  SKIP (not found): {src}")
        return False

    output = render_api_text(src)

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(output)
    print(f"  OK: {dst.relative_to(MONOREPO_ROOT)}")
    return True


def check_api_file(src, dst):
    """Render to memory and compare against the committed file. Returns True
    when they match (nothing to regenerate), False otherwise."""
    if not src.exists():
        print(f"  SKIP (not found): {src}")
        return False

    output = render_api_text(src)
    committed = dst.read_text() if dst.exists() else None
    if committed == output:
        print(f"  OK (up to date): {dst.relative_to(MONOREPO_ROOT)}")
        return True

    print(f"  STALE: {dst.relative_to(MONOREPO_ROOT)} does not match the source docstrings.")
    print(f"  Run: python {Path(__file__).relative_to(MONOREPO_ROOT)}")
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Render to memory and exit 1 if it differs from the committed file; write nothing.",
    )
    args = parser.parse_args()

    if args.check:
        print("=== Checking Python API reference is up to date ===")
        if check_api_file(SOURCE_FILE, OUT_FILE):
            print("Done.")
        else:
            sys.exit(1)
        return

    print("=== Rendering Python API reference ===")
    if process_api_file(SOURCE_FILE, OUT_FILE):
        print("Done.")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
