import os
import shutil
from pathlib import Path
from urllib.parse import urlparse

import requests as _requests

from app.tools.builtin import tool_manager
from app.tools.fetch import _check_url

WORKSPACE_DIR = Path(
    os.getenv("WORKSPACE_DIR") or "data/workspace"
).resolve()
_MAX_READ_BYTES = 100_000   # 100 KB
_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 50 MB
_DOWNLOAD_TIMEOUT = 30


def _safe_path(user_path: str) -> Path | None:
    try:
        resolved = (WORKSPACE_DIR / user_path).resolve()
        resolved.relative_to(WORKSPACE_DIR)  # raises ValueError if outside
        return resolved
    except (ValueError, OSError):
        return None


def _ensure_workspace() -> None:
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)


def _list_files(path: str = ".") -> str:
    _ensure_workspace()
    target = _safe_path(path)
    if target is None:
        return "Error: path is outside the workspace."
    if not target.exists():
        return f"Error: '{path}' does not exist."
    if not target.is_dir():
        return f"Error: '{path}' is not a directory."
    entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
    if not entries:
        return "(empty directory)"
    lines = []
    for entry in entries:
        rel = entry.relative_to(WORKSPACE_DIR)
        if entry.is_dir():
            lines.append(f"[dir]  {rel}/")
        else:
            lines.append(f"[file] {rel}  ({entry.stat().st_size} bytes)")
    return "\n".join(lines)


def _find_files(pattern: str) -> str:
    _ensure_workspace()
    matches = sorted(WORKSPACE_DIR.glob(pattern))
    if not matches:
        return f"No files match '{pattern}'."
    lines = []
    for p in matches:
        rel = p.relative_to(WORKSPACE_DIR)
        lines.append(f"{'[dir]' if p.is_dir() else '[file]'} {rel}")
    return "\n".join(lines)


def _read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    _ensure_workspace()
    target = _safe_path(path)
    if target is None:
        return "Error: path is outside the workspace."
    if not target.exists():
        return f"Error: '{path}' does not exist."
    if not target.is_file():
        return f"Error: '{path}' is not a file."

    partial = start_line is not None or end_line is not None
    size = target.stat().st_size

    if not partial and size > _MAX_READ_BYTES:
        return (
            f"Error: file too large ({size} bytes; limit is {_MAX_READ_BYTES}). "
            "Use start_line/end_line to read a specific range."
        )

    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "Error: file is not valid UTF-8 text."

    if not partial:
        return text

    lines = text.splitlines(keepends=True)
    total = len(lines)

    # Positive values are 1-based inclusive; negative count from the end (-1 = last line).
    def to_slice_index(n: int) -> int:
        return (n - 1) if n > 0 else max(0, total + n)

    s = to_slice_index(start_line) if start_line is not None else 0
    e = (to_slice_index(end_line) + 1) if end_line is not None else total

    selected = lines[s:e]
    actual_end = min(e, total)
    header = f"[Lines {s + 1}–{actual_end} of {total}]\n"
    result = header + "".join(selected)

    if len(result) > _MAX_READ_BYTES:
        result = result[:_MAX_READ_BYTES] + "\n[content truncated]"

    return result


def _download_file(url: str, path: str) -> str:
    _ensure_workspace()
    err = _check_url(url)
    if err:
        return f"Error: {err}"

    target = _safe_path(path)
    if target is None:
        return "Error: path is outside the workspace."

    try:
        resp = _requests.get(
            url,
            timeout=_DOWNLOAD_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Pino-Agent/1.0)"},
            stream=True,
            allow_redirects=True,
        )
    except _requests.exceptions.Timeout:
        return "Error: Request timed out."
    except _requests.exceptions.RequestException as e:
        return f"Error: {e}"

    # Re-validate redirect target
    if resp.url != url:
        err = _check_url(resp.url)
        if err:
            return f"Error: Redirect target blocked — {err}"

    if not resp.ok:
        return f"Error: Server returned HTTP {resp.status_code}."

    target.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with target.open("wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > _MAX_DOWNLOAD_BYTES:
                target.unlink(missing_ok=True)
                return f"Error: file exceeds {_MAX_DOWNLOAD_BYTES // (1024*1024)} MB limit; download aborted."
            f.write(chunk)

    return f"Downloaded {total:,} bytes to '{path}'."


_MAX_SEARCH_RESULTS = 20
_MAX_CONTEXT_CHARS = 200


def _search_files(query: str, path: str = ".", case_sensitive: bool = False) -> str:
    _ensure_workspace()
    target = _safe_path(path)
    if target is None:
        return "Error: path is outside the workspace."
    if not target.exists():
        return f"Error: '{path}' does not exist."
    if not target.is_dir():
        return f"Error: '{path}' is not a directory."

    needle = query if case_sensitive else query.lower()
    hits: list[str] = []

    for file in sorted(target.rglob("*")):
        if not file.is_file():
            continue
        if file.stat().st_size > _MAX_READ_BYTES:
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        lines = text.splitlines()
        file_hits: list[str] = []
        for lineno, line in enumerate(lines, 1):
            haystack = line if case_sensitive else line.lower()
            if needle in haystack:
                snippet = line.strip()
                if len(snippet) > _MAX_CONTEXT_CHARS:
                    idx = haystack.find(needle)
                    start = max(0, idx - 60)
                    snippet = ("…" if start else "") + line[start : idx + len(needle) + 80].strip() + "…"
                file_hits.append(f"  line {lineno}: {snippet}")
                if len(file_hits) >= 5:
                    break

        if file_hits:
            rel = file.relative_to(WORKSPACE_DIR)
            hits.append(f"[{rel}]\n" + "\n".join(file_hits))
            if len(hits) >= _MAX_SEARCH_RESULTS:
                break

    if not hits:
        return f"No matches for '{query}' in '{path}'."
    return "\n\n".join(hits)


def _write_file(path: str, content: str) -> str:
    _ensure_workspace()
    target = _safe_path(path)
    if target is None:
        return "Error: path is outside the workspace."
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Written {len(content)} characters to '{path}'."


def _append_file(path: str, content: str) -> str:
    _ensure_workspace()
    target = _safe_path(path)
    if target is None:
        return "Error: path is outside the workspace."
    if target.exists() and not target.is_file():
        return f"Error: '{path}' is not a file."
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as f:
        f.write(content)
    return f"Appended {len(content)} characters to '{path}'."


def _patch_file(path: str, start_line: int, end_line: int, content: str) -> str:
    _ensure_workspace()
    target = _safe_path(path)
    if target is None:
        return "Error: path is outside the workspace."
    if not target.exists():
        return f"Error: '{path}' does not exist."
    if not target.is_file():
        return f"Error: '{path}' is not a file."

    try:
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return "Error: file is not valid UTF-8 text."

    total = len(lines)

    def to_slice_index(n: int) -> int:
        return (n - 1) if n > 0 else max(0, total + n)

    s = to_slice_index(start_line)
    e = to_slice_index(end_line) + 1  # inclusive → exclusive

    if not (0 <= s < total):
        return f"Error: start_line {start_line} is out of range (file has {total} lines)."
    if e < s:
        return f"Error: end_line {end_line} resolves before start_line {start_line}."

    # Ensure the replacement block ends with a newline when it sits mid-file.
    replacement = content
    if replacement and not replacement.endswith("\n") and e < total:
        replacement += "\n"

    new_lines = lines[:s] + ([replacement] if replacement else []) + lines[e:]
    target.write_text("".join(new_lines), encoding="utf-8")

    replaced = e - s
    return (
        f"Replaced {replaced} line{'s' if replaced != 1 else ''} "
        f"({s + 1}–{min(e, total)}) in '{path}'."
    )


def _delete_file(path: str, recursive: bool = False) -> str:
    _ensure_workspace()
    target = _safe_path(path)
    if target is None:
        return "Error: path is outside the workspace."
    if not target.exists():
        return f"Error: '{path}' does not exist."
    if target.is_file() or target.is_symlink():
        target.unlink()
        return f"Deleted file '{path}'."
    if target.is_dir():
        if recursive:
            shutil.rmtree(target)
            return f"Deleted directory '{path}' and all its contents."
        try:
            target.rmdir()
            return f"Deleted empty directory '{path}'."
        except OSError:
            return (
                f"Error: '{path}' is a non-empty directory. "
                "Set recursive=true to delete it and all its contents."
            )
    return f"Error: '{path}' is not a file or directory."


tool_manager.register(
    name="list_files",
    fn=_list_files,
    description=(
        "List files and directories inside the agent workspace. "
        "Use path '.' for the workspace root or any subdirectory path."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path relative to workspace root. Defaults to '.' (root).",
            },
        },
        "required": [],
    },
    status_template="Listing files in: {path}",
)

tool_manager.register(
    name="find_files",
    fn=_find_files,
    description=(
        "Search for files in the workspace using a glob pattern. "
        "Examples: '*.txt', '**/*.py', 'docs/*.md'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern relative to workspace root, e.g. '*.txt' or '**/*.md'.",
            },
        },
        "required": ["pattern"],
    },
    status_template="Finding files matching: {pattern}",
)

tool_manager.register(
    name="read_file",
    fn=_read_file,
    description=(
        "Read the text content of a file in the workspace. "
        "Use start_line and end_line to read a specific range instead of the whole file — "
        "useful for large files or when search_files has already located the relevant lines. "
        "Positive line numbers are 1-based; negative numbers count from the end (-1 = last line, -20 = last 20 lines)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to workspace root, e.g. 'notes.txt' or 'reports/summary.md'.",
            },
            "start_line": {
                "type": "integer",
                "description": (
                    "First line to read (1-based). Negative counts from end: -20 reads from the 20th-to-last line. "
                    "Omit to start from the beginning."
                ),
            },
            "end_line": {
                "type": "integer",
                "description": (
                    "Last line to read, inclusive (1-based). Negative counts from end: -1 is the last line. "
                    "Omit to read to the end of the file."
                ),
            },
        },
        "required": ["path"],
    },
    status_template="Reading file: {path}",
)

tool_manager.register(
    name="download_file",
    fn=_download_file,
    description=(
        "Download a file from a URL and save it to the workspace. "
        "Supports any file type (PDF, CSV, images, archives, etc.). "
        "Private or internal addresses are blocked. Maximum file size is 50 MB."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The full URL to download from, e.g. 'https://example.com/data.csv'.",
            },
            "path": {
                "type": "string",
                "description": "Destination path relative to workspace root, e.g. 'downloads/data.csv'.",
            },
        },
        "required": ["url", "path"],
    },
    status_template="Downloading {url} → {path}",
)

tool_manager.register(
    name="search_files",
    fn=_search_files,
    description=(
        "Search for a text string across all files in the workspace. "
        "Returns matching filenames and the lines containing the match. "
        "Use this to find relevant information in notes, reports, or other files you have previously written."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The text string to search for.",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in, relative to workspace root. Defaults to '.' (entire workspace).",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Whether the search is case-sensitive. Defaults to false.",
            },
        },
        "required": ["query"],
    },
    status_template='Searching files for: "{query}"',
)

tool_manager.register(
    name="append_file",
    fn=_append_file,
    description=(
        "Append text to the end of a file in the workspace, creating it if it doesn't exist. "
        "Use instead of write_file when you want to add to existing content rather than overwrite it."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to workspace root, e.g. 'log.txt'.",
            },
            "content": {
                "type": "string",
                "description": "Text to append. Include a leading newline if the file already has content and you want separation.",
            },
        },
        "required": ["path", "content"],
    },
    status_template="Appending to file: {path}",
)

tool_manager.register(
    name="patch_file",
    fn=_patch_file,
    description=(
        "Replace a range of lines in a workspace file with new content. "
        "Use after search_files or read_file to make targeted edits without rewriting the whole file. "
        "Positive line numbers are 1-based; negative count from the end (-1 = last line). "
        "Set content to an empty string to delete the lines."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to workspace root.",
            },
            "start_line": {
                "type": "integer",
                "description": "First line of the range to replace (1-based, or negative from end).",
            },
            "end_line": {
                "type": "integer",
                "description": "Last line of the range to replace, inclusive (1-based, or negative from end).",
            },
            "content": {
                "type": "string",
                "description": "Replacement text. Use an empty string to delete the lines.",
            },
        },
        "required": ["path", "start_line", "end_line", "content"],
    },
    status_template="Patching file: {path} (lines {start_line}–{end_line})",
)

tool_manager.register(
    name="write_file",
    fn=_write_file,
    description=(
        "Write text content to a file in the workspace, creating it if it doesn't exist "
        "and overwriting if it does. Parent directories are created automatically."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to workspace root, e.g. 'notes.txt' or 'reports/summary.md'.",
            },
            "content": {
                "type": "string",
                "description": "Text content to write to the file.",
            },
        },
        "required": ["path", "content"],
    },
    status_template="Writing file: {path}",
)

tool_manager.register(
    name="delete_file",
    fn=_delete_file,
    description=(
        "Delete a file or directory from the workspace. "
        "For directories, set recursive=true to delete them along with all contents; "
        "otherwise only empty directories are deleted."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file or directory to delete, relative to workspace root.",
            },
            "recursive": {
                "type": "boolean",
                "description": "If true, delete a directory and all its contents. Defaults to false.",
            },
        },
        "required": ["path"],
    },
    status_template="Deleting: {path}",
)
