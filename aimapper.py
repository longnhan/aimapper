#!/usr/bin/env python3
"""aimapper — compact codebase function map for AI-assisted development."""

import ast
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

VERSION = "0.1.0"
OUTPUT_MD = "aimapper.md"
OUTPUT_JSON = "aimapper.json"

C_EXTS = {".c", ".h", ".cpp", ".hpp"}
PY_EXTS = {".py"}
JSON_EXTS = {".json"}
JSON_SIZE_LIMIT = 512 * 1024  # skip JSON files larger than 512 KB

SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache",
    "node_modules", ".venv", "venv", "env", ".env", "dist", "build",
    ".tox", ".eggs",
}
SKIP_FILES = {OUTPUT_MD, OUTPUT_JSON}

_C_KEYWORDS = frozenset(
    "if else while for do switch return sizeof typeof alignof "
    "offsetof assert static_assert".split()
)
_CALL_SKIP = _C_KEYWORDS | {
    "printf", "fprintf", "sprintf", "snprintf", "vprintf",
    "malloc", "calloc", "realloc", "free",
}

# ─── C / C++ ─────────────────────────────────────────────────────────────────

_C_INC_RE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]')
_C_DEF_RE = re.compile(r'^\s*#\s*define\s+([A-Za-z_]\w*)')
_C_CALL_RE = re.compile(r'\b([A-Za-z_]\w*)\s*\(')
_C_NAME_RE = re.compile(r'(\~?[A-Za-z_]\w*(?:\s*::\s*\~?[A-Za-z_]\w*)*)$')


def _strip_c_comments(text: str) -> str:
    """Remove C/C++ comments, preserving newlines for line-number accuracy."""
    result: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i:i+2] == '/*':
            j = text.find('*/', i + 2)
            if j == -1:
                break
            result.append('\n' * text[i:j+2].count('\n'))
            i = j + 2
        elif text[i:i+2] == '//':
            result.append(' ')
            i += 2
            while i < n and text[i] != '\n':
                i += 1
        elif text[i] in ('"', "'"):
            q = text[i]
            result.append(q)
            i += 1
            while i < n and text[i] != q:
                if text[i] == '\\':
                    result.append(text[i])
                    i += 1
                if i < n:
                    result.append(text[i])
                    i += 1
            if i < n:
                result.append(text[i])
                i += 1
        else:
            result.append(text[i])
            i += 1
    return ''.join(result)


def _extract_c_func(context: str) -> Optional[Tuple[str, str, str]]:
    """Extract (name, return_type, params) from a context string ending with '{'."""
    ctx = context.rstrip()
    if not ctx.endswith('{'):
        ctx = ctx + '{'
    ctx = ctx[:-1].rstrip()

    close = ctx.rfind(')')
    if close == -1:
        return None
    open_ = ctx.rfind('(', 0, close)
    if open_ == -1:
        return None

    params = re.sub(r'\s+', ' ', ctx[open_+1:close].strip())
    before = ctx[:open_].strip()

    m = _C_NAME_RE.search(before)
    if not m:
        return None
    name = m.group(1)
    base_name = name.split('::')[-1].lstrip('~')
    if base_name in _C_KEYWORDS:
        return None

    ret_type = re.sub(r'\s+', ' ', before[:m.start()].strip())
    if re.search(r'\b(if|else|while|for|do|switch)\b', ret_type):
        return None
    if not ret_type and not name.startswith('~'):
        return None

    return name, ret_type, params


def _parse_c(path: Path, root: Path) -> dict:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return {}

    raw_lines = text.splitlines()
    total = len(raw_lines)

    includes = [m.group(1) for ln in raw_lines for m in [_C_INC_RE.match(ln)] if m]
    defines = [m.group(1) for ln in raw_lines for m in [_C_DEF_RE.match(ln)] if m]

    stripped = _strip_c_comments(text)
    lines = stripped.splitlines()

    depths: List[int] = []
    d = 0
    for ln in lines:
        depths.append(d)
        for ch in ln:
            if ch == '{':
                d += 1
            elif ch == '}':
                d = max(0, d - 1)

    functions = []
    seen_names: set = set()

    for i, (ln, depth_before) in enumerate(zip(lines, depths)):
        if '{' not in ln or depth_before != 0:
            continue

        ctx_start = i
        lookback = 0
        j = i - 1
        while j >= 0 and lookback < 4:
            s = lines[j].strip()
            if not s or s.startswith('#') or s == '}':
                break
            ctx_start = j
            lookback += 1
            j -= 1

        context = ' '.join(lines[ctx_start:i+1])
        result = _extract_c_func(context)
        if not result:
            continue
        name, ret_type, params = result

        if name in seen_names:
            continue
        seen_names.add(name)

        brace_count = 0
        end_line = i
        for k in range(i, len(lines)):
            for ch in lines[k]:
                if ch == '{':
                    brace_count += 1
                elif ch == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        end_line = k
                        break
            if brace_count == 0:
                break

        body = '\n'.join(lines[i:end_line+1])
        calls = sorted({
            m2.group(1) for m2 in _C_CALL_RE.finditer(body)
            if m2.group(1) not in _CALL_SKIP and m2.group(1) != name
        })

        sig = f"{ret_type} {name}({params})" if ret_type else f"{name}({params})"
        functions.append({
            "signature": sig,
            "line": ctx_start + 1,
            "length": end_line - ctx_start + 1,
            "calls": calls,
        })

    return {
        "lines": total,
        "includes": includes,
        "defines": defines,
        "functions": functions,
    }


# ─── Python ──────────────────────────────────────────────────────────────────

def _ast_calls(node: ast.AST) -> List[str]:
    calls = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            if isinstance(child.func, ast.Name):
                calls.append(child.func.id)
            elif isinstance(child.func, ast.Attribute):
                calls.append(child.func.attr)
    return sorted(set(calls))


def _func_sig(node) -> str:
    args = node.args
    parts: List[str] = []
    for a in getattr(args, 'posonlyargs', []):
        parts.append(a.arg)
    if getattr(args, 'posonlyargs', []):
        parts.append('/')
    for a in args.args:
        parts.append(a.arg)
    if args.vararg:
        parts.append(f'*{args.vararg.arg}')
    elif args.kwonlyargs:
        parts.append('*')
    for a in args.kwonlyargs:
        parts.append(a.arg)
    if args.kwarg:
        parts.append(f'**{args.kwarg.arg}')
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({', '.join(parts)})"


def _func_entry(node) -> dict:
    end = getattr(node, 'end_lineno', node.lineno)
    return {
        "signature": _func_sig(node),
        "line": node.lineno,
        "length": end - node.lineno + 1,
        "calls": _ast_calls(node),
    }


def _parse_py(path: Path, root: Path) -> dict:
    try:
        source = path.read_text(errors="replace")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError):
        return {}

    total = source.count('\n') + 1
    imports: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                imports.append(f"{mod}.{alias.name}" if mod else alias.name)

    functions = []
    classes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(_func_entry(node))
        elif isinstance(node, ast.ClassDef):
            methods = [
                _func_entry(item)
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            end = getattr(node, 'end_lineno', node.lineno)
            classes.append({
                "name": node.name,
                "line": node.lineno,
                "length": end - node.lineno + 1,
                "methods": methods,
            })

    return {
        "lines": total,
        "imports": imports,
        "functions": functions,
        "classes": classes,
    }


# ─── JSON ────────────────────────────────────────────────────────────────────

def _schema(obj, depth: int = 0, max_depth: int = 2):
    if depth >= max_depth:
        return "..."
    if isinstance(obj, dict):
        return {k: _schema(v, depth+1, max_depth) for k, v in list(obj.items())[:16]}
    if isinstance(obj, list):
        return [_schema(obj[0], depth+1, max_depth)] if obj else []
    if isinstance(obj, bool):
        return "bool"
    if isinstance(obj, int):
        return "int"
    if isinstance(obj, float):
        return "float"
    if isinstance(obj, str):
        return "str"
    if obj is None:
        return "null"
    return type(obj).__name__


def _parse_json(path: Path, root: Path) -> dict:
    try:
        if path.stat().st_size > JSON_SIZE_LIMIT:
            return {}
        text = path.read_text(errors="replace")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}

    total = text.count('\n') + 1
    top_keys = list(data.keys()) if isinstance(data, dict) else None
    return {
        "lines": total,
        "top_keys": top_keys,
        "schema": _schema(data),
    }


# ─── dispatch ────────────────────────────────────────────────────────────────

def parse_file(path: Path, root: Path) -> Optional[dict]:
    ext = path.suffix.lower()
    if ext in C_EXTS:
        return _parse_c(path, root)
    if ext in PY_EXTS:
        return _parse_py(path, root)
    if ext in JSON_EXTS:
        return _parse_json(path, root)
    return None


# ─── file collection ─────────────────────────────────────────────────────────

_ALL_EXTS = C_EXTS | PY_EXTS | JSON_EXTS


def collect_files(root: Path) -> List[Path]:
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in SKIP_DIRS and not d.endswith(".egg-info")
        )
        for fname in sorted(filenames):
            if fname in SKIP_FILES:
                continue
            fpath = Path(dirpath) / fname
            if fpath.suffix.lower() in _ALL_EXTS:
                files.append(fpath)
    return files


# ─── render ──────────────────────────────────────────────────────────────────

def _calls_str(calls: List[str], limit: int = 8) -> str:
    if not calls:
        return ""
    shown = calls[:limit]
    suffix = ", ..." if len(calls) > limit else ""
    return f"  → [{', '.join(shown)}{suffix}]"


def render_md(file_map: Dict[str, dict]) -> str:
    out: List[str] = ["# aimapper function map", ""]

    for rel, info in sorted(file_map.items()):
        total = info.get("lines", 0)
        out.append(f"## {rel}  ({total} lines)")

        if "includes" in info:  # C/C++
            incs = info.get("includes", [])
            defs = info.get("defines", [])
            if incs:
                out.append("  includes: " + ", ".join(incs[:8]) + (" ..." if len(incs) > 8 else ""))
            if defs:
                out.append("  defines: " + ", ".join(defs[:8]) + (" ..." if len(defs) > 8 else ""))
            for fn in info.get("functions", []):
                out.append(
                    f"  {fn['signature']}  [L{fn['line']}, {fn['length']}ln]"
                    + _calls_str(fn.get("calls", []))
                )

        elif "imports" in info:  # Python
            imps = info.get("imports", [])
            if imps:
                out.append("  imports: " + ", ".join(imps[:8]) + (" ..." if len(imps) > 8 else ""))
            for fn in info.get("functions", []):
                out.append(
                    f"  {fn['signature']}  [L{fn['line']}, {fn['length']}ln]"
                    + _calls_str(fn.get("calls", []))
                )
            for cls in info.get("classes", []):
                out.append(f"  class {cls['name']}  [L{cls['line']}, {cls['length']}ln]")
                for mth in cls.get("methods", []):
                    out.append(
                        f"    {mth['signature']}  [L{mth['line']}, {mth['length']}ln]"
                        + _calls_str(mth.get("calls", []))
                    )

        elif "top_keys" in info:  # JSON
            keys = info.get("top_keys") or []
            if keys:
                shown = [str(k) for k in keys[:12]]
                out.append("  keys: " + ", ".join(shown) + (" ..." if len(keys) > 12 else ""))

        out.append("")

    return "\n".join(out)


# ─── CLAUDE.md injection ─────────────────────────────────────────────────────

_AIMAPPER_MARKER = "<!-- aimapper-map -->"
_AIMAPPER_BLOCK = """\
<!-- aimapper-map -->
## AI map
Read `aimapper.md` before exploring this codebase — it maps every function and
call graph so you can locate the right file/line without reading raw source.
Run `python3 aimapper.py` to regenerate after big changes.
<!-- /aimapper-map -->
"""


def _inject_claude_md(project_root: Path) -> Path:
    """
    Ensure CLAUDE.md in project_root contains the aimapper pointer block.
    Prefers existing CLAUDE.md; falls back to README.md; creates CLAUDE.md if
    neither exists. Returns the path that was written.
    """
    claude = project_root / "CLAUDE.md"
    readme = project_root / "README.md"

    if claude.exists():
        target = claude
    elif readme.exists():
        target = readme
    else:
        target = claude  # will be created

    if target.exists():
        text = target.read_text(encoding="utf-8")
        if _AIMAPPER_MARKER in text:
            # Already injected — update the block in place
            text = re.sub(
                r'<!-- aimapper-map -->.*?<!-- /aimapper-map -->',
                _AIMAPPER_BLOCK.rstrip(),
                text,
                flags=re.DOTALL,
            )
        else:
            # Prepend before the first heading (or at very top)
            m = re.search(r'^#+\s', text, re.MULTILINE)
            if m and m.start() > 0:
                text = text[:m.start()] + _AIMAPPER_BLOCK + "\n" + text[m.start():]
            else:
                text = _AIMAPPER_BLOCK + "\n" + text
        target.write_text(text, encoding="utf-8")
    else:
        target.write_text(_AIMAPPER_BLOCK, encoding="utf-8")

    return target


# ─── interactive prompt ───────────────────────────────────────────────────────

def _ask_root() -> Path:
    cwd = Path.cwd()
    try:
        raw = input(f"Project root directory [{cwd}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    p = Path(raw).resolve() if raw else cwd
    if not p.is_dir():
        print(f"error: '{p}' is not a directory", file=sys.stderr)
        sys.exit(1)
    return p


# ─── main ────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(
        prog="aimapper",
        description="Generate a compact function map of your codebase for AI-assisted development.",
    )
    ap.add_argument("--no-json", action="store_true", help="skip aimapper.json output")
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = ap.parse_args()

    root = _ask_root()

    files = collect_files(root)
    if not files:
        print("no supported files found", file=sys.stderr)
        sys.exit(0)

    file_map: Dict[str, dict] = {}
    for fpath in files:
        rel = str(fpath.relative_to(root))
        info = parse_file(fpath, root)
        if info:
            file_map[rel] = info

    md_content = render_md(file_map)

    md_path = root / OUTPUT_MD
    md_path.write_text(md_content, encoding="utf-8")

    if not args.no_json:
        json_path = root / OUTPUT_JSON
        json_path.write_text(json.dumps(file_map, indent=2), encoding="utf-8")

    claude_path = _inject_claude_md(root)

    total_files = len(file_map)
    total_lines = sum(v.get("lines", 0) for v in file_map.values())
    map_lines = md_content.count('\n') + 1
    pct = 100 * (1 - map_lines / total_lines) if total_lines else 0

    print(f"wrote {md_path}")
    if not args.no_json:
        print(f"wrote {json_path}")
    print(f"updated {claude_path}")
    print(f"mapped {total_files} files / {total_lines:,} lines → {map_lines:,} map lines  ({pct:.0f}% reduction)")


if __name__ == "__main__":
    main()
