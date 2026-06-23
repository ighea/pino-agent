import os
from pathlib import Path
from urllib.parse import urlparse

import requests as _requests

from app.tools.builtin import tool_manager
from app.tools.fetch import _check_url

WORKSPACE_DIR = Path(
    os.getenv("WORKSPACE_DIR") or Path(__file__).parent.parent / "workspace"
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


def _read_file(path: str) -> str:
    _ensure_workspace()
    target = _safe_path(path)
    if target is None:
        return "Error: path is outside the workspace."
    if not target.exists():
        return f"Error: '{path}' does not exist."
    if not target.is_file():
        return f"Error: '{path}' is not a file."
    size = target.stat().st_size
    if size > _MAX_READ_BYTES:
        return f"Error: file too large ({size} bytes; limit is {_MAX_READ_BYTES})."
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "Error: file is not valid UTF-8 text."


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


def _write_file(path: str, content: str) -> str:
    _ensure_workspace()
    target = _safe_path(path)
    if target is None:
        return "Error: path is outside the workspace."
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Written {len(content)} characters to '{path}'."


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
    description="Read the text content of a file in the workspace.",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to workspace root, e.g. 'notes.txt' or 'reports/summary.md'.",
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
