"""Sandboxed Python code execution and package installation.

Code runs in a subprocess with the workspace as its working directory.
File I/O is restricted to the workspace via an injected preamble that overrides
builtins.open. Subprocess and shell-execution APIs are disabled in the same preamble.

install_python_package runs pip install in the main process (outside the sandbox),
so newly installed packages are immediately available to subsequent run_python calls.
"""

import asyncio
import os
import re
import site
import subprocess
import sys
import tempfile

from app.tools.builtin import tool_manager
from app.tools.files import WORKSPACE_DIR

_DEFAULT_TIMEOUT = int(os.getenv("CODE_EXEC_TIMEOUT", "30"))
_MAX_OUTPUT_CHARS = int(os.getenv("CODE_MAX_OUTPUT_CHARS", "3000"))

# Injected before the user's code. Uses .format(workspace=...) — double-brace {{ }}
# for literal braces that appear in the generated Python source.
_PREAMBLE_TEMPLATE = """\
def _sandbox_setup():
    import builtins as _b, os as _os, sys as _sys

    _ws = {workspace!r}
    _ws_prefix = _ws if _ws.endswith(_os.sep) else _ws + _os.sep

    _real_open = _b.open
    def _safe_open(file, *args, **kwargs):
        if isinstance(file, (str, bytes, _os.PathLike)):
            try:
                p = _os.path.realpath(_os.path.join(_os.getcwd(), _os.fsdecode(file)))
            except Exception:
                p = str(file)
            if p != _ws and not p.startswith(_ws_prefix):
                raise PermissionError(
                    f"Sandbox: {{file!r}} is outside the workspace. "
                    "Use relative paths or paths within the workspace directory."
                )
        return _real_open(file, *args, **kwargs)
    _b.open = _safe_open

    def _blocked(*a, **kw):
        raise PermissionError("Sandbox: subprocess and shell execution are disabled.")

    for _name in ("system", "popen", "execv", "execve", "execvp", "execvpe",
                  "execl", "execle", "execlp", "spawnv", "spawnve", "spawnl", "spawnle"):
        if hasattr(_os, _name):
            setattr(_os, _name, _blocked)

    try:
        import subprocess as _sp
        for _name in ("run", "Popen", "call", "check_call", "check_output",
                      "getoutput", "getstatusoutput"):
            if hasattr(_sp, _name):
                setattr(_sp, _name, _blocked)
    except ImportError:
        pass

_sandbox_setup()
del _sandbox_setup
# ---- user code below ----
"""


def _build_script(code: str) -> str:
    preamble = _PREAMBLE_TEMPLATE.format(workspace=str(WORKSPACE_DIR))
    return preamble + "\n" + code


def _run_python(code: str, timeout: int = _DEFAULT_TIMEOUT) -> str:
    timeout = min(max(1, int(timeout)), 120)

    script = _build_script(code)

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".py")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(script)

        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONIOENCODING": "utf-8",
            # HOME points inside workspace so relative-home paths stay sandboxed.
            # PYTHONUSERBASE is set explicitly so Python can still find packages
            # installed to the real ~/.local even when HOME is overridden.
            "HOME": str(WORKSPACE_DIR),
            "PYTHONUSERBASE": site.getuserbase(),
        }
        if "PYTHONPATH" in os.environ:
            env["PYTHONPATH"] = os.environ["PYTHONPATH"]

        preexec_fn = None
        try:
            import resource as _resource

            def _apply_limits():
                # CPU time hard limit — kills the process if it exceeds it
                try:
                    _resource.setrlimit(
                        _resource.RLIMIT_CPU, (timeout, timeout + 5)
                    )
                except Exception:
                    pass

            preexec_fn = _apply_limits
        except ImportError:
            pass

        result = subprocess.run(
            [sys.executable, "-u", tmp_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(WORKSPACE_DIR),
            env=env,
            preexec_fn=preexec_fn,
        )

        stdout = result.stdout or ""
        stderr = result.stderr or ""

        # Filter out sandbox setup noise — there should be none, but just in case
        combined = stdout
        if stderr:
            combined += ("\n" if combined else "") + stderr

        if not combined.strip():
            return f"(no output, exit code {result.returncode})" if result.returncode else "(no output)"

        if len(combined) > _MAX_OUTPUT_CHARS:
            combined = combined[:_MAX_OUTPUT_CHARS] + f"\n... [truncated at {_MAX_OUTPUT_CHARS} chars]"

        return combined.strip()

    except subprocess.TimeoutExpired:
        return f"Error: execution timed out after {timeout}s."
    except Exception as e:
        return f"Error: {e}"
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


tool_manager.register(
    name="run_python",
    fn=_run_python,
    description=(
        "Execute Python code in a sandboxed subprocess and return stdout + stderr. "
        "The code runs inside the workspace directory — file I/O is restricted to the workspace "
        "and subprocess/shell execution is disabled. Use relative paths to read and write files. "
        "All packages installed in the Python environment are available. "
        "If a needed package is missing, call install_python_package first, then run your code. "
        "Use this for data analysis, calculations, CSV/JSON processing, generating plots "
        "(save images to the workspace with matplotlib), string manipulation, and similar tasks. "
        "Use print() to produce output. Execution is capped at `timeout` seconds (default 30, max 120)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Valid Python source code to execute. Use print() to surface results.",
            },
            "timeout": {
                "type": "integer",
                "description": "Maximum run time in seconds (default 30, max 120).",
            },
        },
        "required": ["code"],
    },
    status_template="Running Python code...",
)

# Only allow safe PyPI package specifications: names, versions, extras.
_SAFE_PKG_RE = re.compile(r'^[A-Za-z0-9_.+\-\[\]>=<!~,\s;]+$')


def _install_python_package(package: str) -> str:
    package = package.strip()
    if not package:
        return "Error: package specification is empty."
    if not _SAFE_PKG_RE.match(package):
        return f"Error: invalid package specification: {package!r}"
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", package],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return "Error: pip install timed out after 120s."
    except Exception as e:
        return f"Error: {e}"
    if result.returncode == 0:
        return f"Successfully installed: {package}"
    # Show the tail of stderr which contains the actual error message
    error = (result.stderr or result.stdout or "unknown error").strip()
    if len(error) > 600:
        error = "[...]\n" + error[-600:]
    return f"pip install failed for '{package}':\n{error}"


tool_manager.register(
    name="install_python_package",
    fn=_install_python_package,
    description=(
        "Install a Python package via pip so it becomes available to run_python. "
        "Accepts any valid pip package specification, e.g. 'requests', 'pandas>=2.0', "
        "'scipy[extra]'. The installation is persistent for the lifetime of the process. "
        "Call this before run_python when the required package is not yet installed."
    ),
    parameters={
        "type": "object",
        "properties": {
            "package": {
                "type": "string",
                "description": "Package name or pip specifier, e.g. 'numpy', 'httpx>=0.27', 'pillow[jpeg]'.",
            },
        },
        "required": ["package"],
    },
    status_template="Installing Python package: {package}",
)
