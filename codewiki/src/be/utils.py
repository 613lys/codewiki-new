import asyncio
import logging
import re
import sys
import threading
from pathlib import Path
from typing import List, Tuple


logger = logging.getLogger(__name__)

_main_loop: "asyncio.AbstractEventLoop | None" = None
_main_loop_thread_ident: int | None = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop, _main_loop_thread_ident
    _main_loop = loop
    _main_loop_thread_ident = threading.get_ident()


def is_complex_module(components: dict[str, object], core_component_ids: list[str]) -> bool:
    """Return True when core components span more than one source file."""
    files = set()
    for component_id in core_component_ids:
        if component_id in components:
            files.add(components[component_id].file_path)
    return len(files) > 1


def count_tokens(text: str) -> int:
    """Estimate token count locally without tokenizer packages or network data."""
    if not text:
        return 0

    cjk_chars = len(re.findall(r"[\u3400-\u9fff\uf900-\ufaff]", text))
    ascii_chars = len(text) - cjk_chars
    char_estimate = (ascii_chars + 3) // 4 + cjk_chars
    lexical_units = len(re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE))
    return max(char_estimate, lexical_units)


async def validate_mermaid_diagrams(md_file_path: str, relative_path: str) -> str:
    """
    Validate all Mermaid diagrams in a markdown file.

    Optional Mermaid validator packages are used when installed. If they are not
    installed, validation is treated as unavailable instead of blocking the
    task-only workflow.
    """
    try:
        file_path = Path(md_file_path)
        if not file_path.exists():
            return f"Error: File '{md_file_path}' does not exist"

        content = file_path.read_text(encoding="utf-8")
        mermaid_blocks = extract_mermaid_blocks(content)
        if not mermaid_blocks:
            return "No mermaid diagrams found in the file"

        errors = []
        for i, (line_start, diagram_content) in enumerate(mermaid_blocks, 1):
            error_msg = await validate_single_diagram(diagram_content, i, line_start)
            if error_msg:
                errors.append("\n")
                errors.append(error_msg)

        if errors:
            return "Mermaid syntax errors found in file: " + relative_path + "\n" + "\n".join(errors)
        return "All mermaid diagrams in file: " + relative_path + " are syntax correct"

    except Exception as error:
        return f"Error processing file: {str(error)}"


def extract_mermaid_blocks(content: str) -> List[Tuple[int, str]]:
    """Extract all Mermaid code blocks from markdown content."""
    mermaid_blocks = []
    lines = content.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i].strip()
        if line == "```mermaid" or line.startswith("```mermaid"):
            start_line = i + 1
            diagram_lines = []
            i += 1
            while i < len(lines):
                if lines[i].strip() == "```":
                    break
                diagram_lines.append(lines[i])
                i += 1
            if diagram_lines:
                mermaid_blocks.append((start_line, "\n".join(diagram_lines)))
        i += 1

    return mermaid_blocks


_PYTHONMONKEY_BROKEN = sys.version_info >= (3, 12)


async def _try_pythonmonkey_parse(diagram_content: str) -> str | None:
    """Attempt validation through mermaid-parser-py when available."""
    global _PYTHONMONKEY_BROKEN
    if _PYTHONMONKEY_BROKEN:
        return None

    import os

    try:
        from mermaid_parser.parser import parse_mermaid_py
    except Exception:
        _PYTHONMONKEY_BROKEN = True
        return None

    old_stderr = sys.stderr
    sys.stderr = open(os.devnull, "w")
    try:
        if (
            _main_loop is not None
            and _main_loop.is_running()
            and threading.get_ident() != _main_loop_thread_ident
        ):
            future = asyncio.run_coroutine_threadsafe(
                parse_mermaid_py(diagram_content),
                _main_loop,
            )
            await asyncio.wrap_future(future)
        else:
            await parse_mermaid_py(diagram_content)
        return ""
    except Exception as error:
        error_str = str(error)
        if "cannot find a running Python event-loop" in error_str:
            _PYTHONMONKEY_BROKEN = True
            return None
        match = re.search(r"Error:(.*?)(?=Stack Trace:|$)", error_str, re.DOTALL)
        if match:
            return match.group(0).strip()
        return None
    finally:
        sys.stderr.close()
        sys.stderr = old_stderr


def _parse_via_mermaid_py(diagram_content: str) -> str:
    """Validate via mermaid-py when installed."""
    try:
        import mermaid as md
    except ModuleNotFoundError:
        return ""

    try:
        md.Mermaid(diagram_content)
        return ""
    except Exception as error:
        return str(error)


def _parse_via_local_checks(diagram_content: str) -> str:
    """Catch common Mermaid syntax mistakes when parser packages are absent."""
    diagram_lines = diagram_content.splitlines()
    meaningful_lines = [
        (index, line.strip())
        for index, line in enumerate(diagram_lines, 1)
        if line.strip() and not line.strip().startswith("%%")
    ]
    if not meaningful_lines:
        return "Error: empty Mermaid diagram"

    first_line = meaningful_lines[0][1]
    known_prefixes = (
        "flowchart",
        "graph",
        "sequenceDiagram",
        "classDiagram",
        "stateDiagram",
        "stateDiagram-v2",
        "erDiagram",
        "journey",
        "gantt",
        "pie",
        "mindmap",
        "timeline",
        "gitGraph",
    )
    if not first_line.startswith(known_prefixes):
        return f"Error: Mermaid diagram starts with unsupported declaration: {first_line}"

    dangling_arrow = re.compile(r"(-->|---|-.->|==>|--x|--o|<--|<-->|<->|->|<-)\s*$")
    leading_arrow = re.compile(r"^(-->|---|-.->|==>|--x|--o|<--|<-->|<->|->|<-)")
    for line_number, line in meaningful_lines[1:]:
        if dangling_arrow.search(line):
            return f"Error: dangling Mermaid edge on line {line_number}: {line}"
        if leading_arrow.search(line):
            return f"Error: Mermaid edge is missing a source on line {line_number}: {line}"

    return ""


async def validate_single_diagram(diagram_content: str, diagram_num: int, line_start: int) -> str:
    """Validate one Mermaid diagram and return an error message if invalid."""
    core_error = await _try_pythonmonkey_parse(diagram_content)
    if core_error is None:
        try:
            core_error = _parse_via_mermaid_py(diagram_content)
        except Exception as error:
            return f"  Diagram {diagram_num}: Exception during validation - {str(error)}"
    if core_error is None or not core_error:
        core_error = _parse_via_local_checks(diagram_content)

    if not core_error:
        return ""

    if core_error.startswith("Error:"):
        return f"Diagram {diagram_num}: {core_error}"

    line_match = re.search(r"line (\d+)", core_error)
    if line_match:
        error_line_in_diagram = int(line_match.group(1))
        actual_line_in_file = line_start + error_line_in_diagram
        newline = "\n"
        return (
            f"Diagram {diagram_num}: Parse error on line {actual_line_in_file}:"
            f"{newline}{newline.join(core_error.split(newline)[1:])}"
        )
    return f"Diagram {diagram_num}: {core_error}"
