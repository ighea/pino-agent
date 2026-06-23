import contextvars
from typing import Awaitable, Callable

from app.tools.builtin import tool_manager

_deliver_fn_var: contextvars.ContextVar[Callable[[str], Awaitable[str]] | None] = (
    contextvars.ContextVar("deliver_fn", default=None)
)


def set_deliver_fn(fn: Callable[[str], Awaitable[str]] | None) -> None:
    _deliver_fn_var.set(fn)


async def _share_file(path: str) -> str:
    from app.tools.files import WORKSPACE_DIR, _ensure_workspace, _safe_path

    _ensure_workspace()
    target = _safe_path(path)
    if target is None:
        return "Error: path is outside the workspace."
    if not target.exists():
        return f"Error: '{path}' does not exist."
    if not target.is_file():
        return f"Error: '{path}' is not a file."

    fn = _deliver_fn_var.get()
    if fn is None:
        return f"File is saved in workspace at: {WORKSPACE_DIR / path}"
    return await fn(path)


tool_manager.register(
    name="share_file",
    fn=_share_file,
    description=(
        "Deliver a workspace file to the user. "
        "HTTP clients receive a download URL, Matrix users receive the file as an attachment, "
        "CLI shows the absolute file path. Use after write_file or download_file to send the result."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to workspace root, e.g. 'report.pdf' or 'exports/data.csv'.",
            },
        },
        "required": ["path"],
    },
    status_template="Sharing file: {path}",
)
