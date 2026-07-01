"""Semantic (vector) index over workspace files.

Files are chunked and embedded on demand when search_files_semantic is called.
The index is persisted to WORKSPACE_INDEX_FILE so embeddings survive restarts.
UTF-8 text files under _MAX_FILE_BYTES are indexed; PDFs under _MAX_PDF_SOURCE_BYTES
have their text extracted via pypdf (see app.tools.files._extract_text).
"""

import json
import os
from pathlib import Path

from app.embed import cosine, embed
from app.tools.builtin import tool_manager

_INDEX_FILE = Path(os.getenv("WORKSPACE_INDEX_FILE", "data/workspace_index.json"))
_CHUNK_SIZE = 400       # chars per chunk
_CHUNK_OVERLAP = 80     # overlap between adjacent chunks
_MAX_FILE_BYTES = 50_000
_MAX_FILES = 50
_MAX_CHUNKS_PER_FILE = 20
_TOP_K = 5
_MIN_SCORE = 0.35


def _chunk(text: str) -> list[tuple[int, str]]:
    """Return (char_offset, chunk_text) pairs with sliding window."""
    chunks: list[tuple[int, str]] = []
    step = _CHUNK_SIZE - _CHUNK_OVERLAP
    i = 0
    while i < len(text):
        chunks.append((i, text[i: i + _CHUNK_SIZE]))
        i += step
    return chunks


def _load_index() -> dict:
    if _INDEX_FILE.exists():
        try:
            return json.loads(_INDEX_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_index(index: dict) -> None:
    _INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    _INDEX_FILE.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")


def _index_file(path: Path, rel: str, index: dict) -> bool:
    """Embed and store chunks for one file. Returns True if the index was updated."""
    from app.tools.files import _extract_text, _MAX_PDF_SOURCE_BYTES

    size_limit = _MAX_PDF_SOURCE_BYTES if path.suffix.lower() == ".pdf" else _MAX_FILE_BYTES
    if not path.is_file() or path.stat().st_size > size_limit:
        return False
    text = _extract_text(path)
    if text is None:
        return False

    mtime = path.stat().st_mtime
    existing = index.get(rel, {})
    if existing.get("mtime") == mtime and existing.get("chunks"):
        return False  # already up to date

    chunks = _chunk(text)[: _MAX_CHUNKS_PER_FILE]
    embedded: list[dict] = []
    for offset, chunk_text in chunks:
        emb = embed(chunk_text)
        if emb is not None:
            embedded.append({"offset": offset, "text": chunk_text, "embedding": emb})

    if not embedded:
        return False

    index[rel] = {"mtime": mtime, "chunks": embedded}
    return True


def _search_files_semantic(query: str, path: str = ".") -> str:
    from app.tools.files import WORKSPACE_DIR, _safe_path

    target = _safe_path(path)
    if target is None:
        return "Error: path is outside the workspace."
    if not target.exists() or not target.is_dir():
        return f"Error: '{path}' does not exist or is not a directory."

    index = _load_index()
    updated = False
    file_count = 0

    for file in sorted(target.rglob("*")):
        if not file.is_file():
            continue
        rel = str(file.relative_to(WORKSPACE_DIR))
        if _index_file(file, rel, index):
            updated = True
        file_count += 1
        if file_count >= _MAX_FILES:
            break

    if updated:
        _save_index(index)

    q_emb = embed(query)
    if q_emb is None:
        return "Error: embedding model unavailable. Set EMBEDDING_MODEL to enable semantic search."

    results: list[dict] = []
    for rel_path, file_data in index.items():
        for chunk in file_data.get("chunks", []):
            emb = chunk.get("embedding")
            if not emb:
                continue
            score = cosine(q_emb, emb)
            if score >= _MIN_SCORE:
                results.append({"path": rel_path, "text": chunk["text"], "score": score})

    results.sort(key=lambda x: x["score"], reverse=True)
    top = results[:_TOP_K]

    if not top:
        return f"No semantically similar content found for '{query}'."

    lines = []
    for r in top:
        snippet = r["text"][:300].strip()
        lines.append(f"[{r['path']}] (relevance: {r['score']:.2f})\n{snippet}")
    return "\n\n".join(lines)


tool_manager.register(
    name="search_files_semantic",
    fn=_search_files_semantic,
    description=(
        "Search workspace files (including PDFs, which are text-extracted automatically) "
        "using natural-language or concept-based queries via semantic (vector) similarity. "
        "Use this when you want to find files related to a topic or idea rather than a "
        "specific keyword — for example, 'budget projections', 'meeting notes about the "
        "product launch', or 'Python scripts'. "
        "For exact keyword/string search use search_files instead. "
        "Returns the most relevant text chunks with their file paths."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language description of what you are looking for.",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in, relative to workspace root. Defaults to '.' (entire workspace).",
            },
        },
        "required": ["query"],
    },
    status_template='Searching files semantically for: "{query}"',
)
