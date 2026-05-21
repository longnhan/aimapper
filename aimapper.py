#!/usr/bin/env python3
"""aimapper — compact codebase function map for AI-assisted development."""

import ast
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

VERSION = "0.2.0"
OUTPUT_DIR = ".aimapper"
OUTPUT_MD = "aimapper.md"
OUTPUT_JSON = "aimapper.json"
OUTPUT_GRAPH = "graph.html"

C_EXTS = {".c", ".h", ".cpp", ".hpp"}
PY_EXTS = {".py"}
JSON_EXTS = {".json"}
JSON_SIZE_LIMIT = 512 * 1024  # skip JSON files larger than 512 KB

SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache",
    "node_modules", ".venv", "venv", "env", ".env", "dist", "build",
    ".tox", ".eggs", ".aimapper",
}
SKIP_FILES = {OUTPUT_MD, OUTPUT_JSON, OUTPUT_GRAPH}

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


def collect_files(root: Path, scan_dirs: List[Path]) -> List[Path]:
    """Walk each directory in scan_dirs and collect supported files."""
    seen: set = set()
    files = []
    for base in scan_dirs:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in SKIP_DIRS and not d.endswith(".egg-info")
            )
            for fname in sorted(filenames):
                if fname in SKIP_FILES:
                    continue
                fpath = Path(dirpath) / fname
                if fpath in seen:
                    continue
                seen.add(fpath)
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


def _aimapper_block(script_path: Path) -> str:
    return (
        "<!-- aimapper-map -->\n"
        "## AI map\n"
        "Read `.aimapper/aimapper.md` before exploring this codebase — it maps every function and\n"
        "call graph so you can locate the right file/line without reading raw source.\n"
        f"Regenerate after big changes: `python3 {script_path}`\n"
        "<!-- /aimapper-map -->\n"
    )


def _inject_claude_md(project_root: Path, script_path: Path) -> Path:
    """
    Ensure CLAUDE.md in project_root contains the aimapper pointer block.
    Creates CLAUDE.md if it doesn't exist. Returns the path that was written.
    """
    target = project_root / "CLAUDE.md"
    block = _aimapper_block(script_path)

    if target.exists():
        text = target.read_text(encoding="utf-8")
        if _AIMAPPER_MARKER in text:
            text = re.sub(
                r'<!-- aimapper-map -->.*?<!-- /aimapper-map -->',
                block.rstrip(),
                text,
                flags=re.DOTALL,
            )
        else:
            m = re.search(r'^#+\s', text, re.MULTILINE)
            if m and m.start() > 0:
                text = text[:m.start()] + block + "\n" + text[m.start():]
            else:
                text = block + "\n" + text
        target.write_text(text, encoding="utf-8")
    else:
        target.write_text(block, encoding="utf-8")

    return target


# ─── network graph ───────────────────────────────────────────────────────────

_LANG_MAP = {
    "py": "py",
    "c": "c", "h": "c", "cpp": "c", "hpp": "c",
    "json": "json",
}

_BARE_RE = re.compile(r'(?:async\s+def\s+|def\s+)?(\w+)\s*\(')


def _bare_name(sig: str) -> str:
    m = _BARE_RE.search(sig)
    return m.group(1) if m else sig.split("(")[0].split()[-1]


def _build_graph_data(file_map: Dict[str, dict]) -> dict:
    name_to_ids: Dict[str, List[str]] = {}
    modules = []
    functions = []

    for rel, info in sorted(file_map.items()):
        ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
        lang = _LANG_MAP.get(ext, "other")
        mid = f"m:{rel}"
        fn_ids: List[str] = []

        all_fns: List[dict] = list(info.get("functions", []))
        for cls in info.get("classes", []):
            all_fns.extend(cls.get("methods", []))

        for fn in all_fns:
            name = _bare_name(fn["signature"])
            fid = f"f:{rel}::{name}"
            fn_ids.append(fid)
            functions.append({
                "id": fid, "module": mid, "name": name,
                "sig": fn["signature"], "line": fn["line"],
                "calls": fn.get("calls", []),
            })
            name_to_ids.setdefault(name, []).append(fid)

        modules.append({
            "id": mid,
            "label": Path(rel).name,
            "path": rel,
            "lang": lang,
            "lines": info.get("lines", 0),
            "fn_count": len(fn_ids),
            "functions": fn_ids,
        })

    seen_fe: set = set()
    fn_edges = []
    for fn in functions:
        for called in fn["calls"]:
            for tgt in name_to_ids.get(called, []):
                key = (fn["id"], tgt)
                if tgt != fn["id"] and key not in seen_fe:
                    seen_fe.add(key)
                    fn_edges.append({"from": fn["id"], "to": tgt})

    fn_mod: Dict[str, str] = {f["id"]: f["module"] for f in functions}
    mod_counts: Dict[tuple, int] = {}
    for e in fn_edges:
        sm, tm = fn_mod.get(e["from"], ""), fn_mod.get(e["to"], "")
        if sm and tm and sm != tm:
            mod_counts[(sm, tm)] = mod_counts.get((sm, tm), 0) + 1

    mod_edges = [{"from": k[0], "to": k[1], "count": v} for k, v in mod_counts.items()]

    for fn in functions:
        del fn["calls"]

    return {
        "modules": modules,
        "functions": functions,
        "fn_edges": fn_edges,
        "mod_edges": mod_edges,
    }


_GRAPH_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>aimapper graph</title>
<script>__VIS_JS__</script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#c9d1d9;font:13px/1.4 monospace;overflow:hidden}
#app{display:flex;flex-direction:column;height:100vh}
#bar{display:flex;align-items:center;gap:10px;padding:7px 16px;background:#161b22;border-bottom:1px solid #30363d;flex-shrink:0}
#bar h1{font-size:14px;color:#58a6ff;font-weight:bold;margin-right:4px}
button{background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:3px 12px;border-radius:6px;cursor:pointer;font:inherit}
button:hover{background:#30363d}
#hint{margin-left:auto;color:#8b949e;font-size:12px}
#graph{flex:1}
#leg{position:fixed;bottom:14px;right:14px;background:#161b22dd;border:1px solid #30363d;border-radius:8px;padding:10px 14px;font-size:12px;line-height:2}
.lr{display:flex;align-items:center;gap:8px}
.lc{width:10px;height:10px;border-radius:50%;flex-shrink:0}
</style>
</head>
<body>
<div id="app">
  <div id="bar">
    <h1>aimapper</h1>
    <button onclick="resetView()">&#8635; Reset</button>
    <button onclick="expandAll()">&#10753; Expand all</button>
    <button onclick="collapseAll()">&#8863; Collapse all</button>
    <span id="hint">Click module to expand &bull; Click function to show its calls</span>
  </div>
  <div id="graph"></div>
</div>
<div id="leg">
  <div class="lr"><span class="lc" style="background:#58a6ff"></span>Python</div>
  <div class="lr"><span class="lc" style="background:#3fb950"></span>C / C++</div>
  <div class="lr"><span class="lc" style="background:#e3b341"></span>JSON</div>
  <div class="lr"><span class="lc" style="background:#8b949e"></span>Other</div>
</div>
<script>
const D=__GRAPH_DATA__;
const mods={},fns={};
D.modules.forEach(m=>mods[m.id]=m);
D.functions.forEach(f=>fns[f.id]=f);

const expanded=new Set(),activeCalls=new Set();
const nodes=new vis.DataSet(),edges=new vis.DataSet();

const CL={
  py:  {b:'#58a6ff',bg:'#1c2d4a'},
  c:   {b:'#3fb950',bg:'#1a2d1f'},
  json:{b:'#e3b341',bg:'#2d2a1a'},
  other:{b:'#8b949e',bg:'#1e2030'}
};
const gc=l=>CL[l]||CL.other;

function init(){
  D.modules.forEach(m=>{
    const c=gc(m.lang);
    nodes.add({id:m.id,label:m.label,
      title:m.path+'\n'+m.lines+' lines  '+m.fn_count+' fns\nClick to expand',
      shape:'box',margin:8,
      color:{background:c.bg,border:c.b,highlight:{background:'#2d333b',border:c.b}},
      font:{color:c.b,size:14,bold:true,face:'monospace'},
      borderWidth:2,_t:'m'});
  });
  D.mod_edges.forEach((e,i)=>{
    edges.add({id:'me'+i,from:e.from,to:e.to,
      arrows:{to:{enabled:true,scaleFactor:0.6}},
      color:{color:'#30363d',highlight:'#58a6ff'},
      width:Math.max(1,Math.min(Math.sqrt(e.count),4)),
      title:e.count+' cross-call'+(e.count>1?'s':''),
      _t:'me'});
  });
}

function expand(mid){
  if(expanded.has(mid))return;
  expanded.add(mid);
  const m=mods[mid],c=gc(m.lang),n=m.functions.length;
  const p=net.getPosition(mid);
  const r=70+Math.min(n,30)*5;
  m.functions.forEach((fid,i)=>{
    const f=fns[fid];if(!f||nodes.get(fid))return;
    const a=(2*Math.PI*i/n)-Math.PI/2;
    nodes.add({id:f.id,label:f.name,
      title:f.sig+'\nL'+f.line+'\nClick to show calls',
      shape:'dot',size:10,
      x:p.x+r*Math.cos(a),y:p.y+r*Math.sin(a),
      color:{background:c.b+'44',border:c.b,highlight:{background:c.b,border:'#fff'}},
      font:{color:'#c9d1d9',size:11,face:'monospace'},
      _t:'f',_m:mid});
    edges.add({id:'mf'+fid,from:mid,to:fid,
      arrows:'',dashes:[4,4],width:1,
      color:{color:c.b+'30'},_t:'xe'});
  });
  nodes.update({id:mid,label:m.label+' ▾',borderWidth:3});
}

function collapse(mid){
  if(!expanded.has(mid))return;
  expanded.delete(mid);
  const m=mods[mid],rn=[],re=[];
  m.functions.forEach(fid=>{
    if(!nodes.get(fid))return;
    rn.push(fid);re.push('mf'+fid);
    if(activeCalls.has(fid)){
      activeCalls.delete(fid);
      D.fn_edges.forEach((_,i)=>{if(D.fn_edges[i].from===fid)re.push('fe'+i);});
    }
  });
  nodes.remove(rn);edges.remove([...new Set(re)]);
  nodes.update({id:mid,label:m.label,borderWidth:2});
}

function toggleCalls(fid){
  if(activeCalls.has(fid)){
    activeCalls.delete(fid);
    const rm=[];
    D.fn_edges.forEach((_,i)=>{if(D.fn_edges[i].from===fid)rm.push('fe'+i);});
    edges.remove(rm);
    nodes.update({id:fid,size:10});
  } else {
    activeCalls.add(fid);
    D.fn_edges.forEach((e,i)=>{
      if(e.from===fid&&nodes.get(e.to)&&!edges.get('fe'+i))
        edges.add({id:'fe'+i,from:e.from,to:e.to,
          arrows:{to:{enabled:true,scaleFactor:0.5}},
          color:{color:'#f78166',highlight:'#ffa07a'},
          width:1.5,_t:'fe'});
    });
    nodes.update({id:fid,size:14});
  }
}

const net=new vis.Network(document.getElementById('graph'),{nodes,edges},{
  physics:{solver:'forceAtlas2Based',
    forceAtlas2Based:{gravitationalConstant:-80,centralGravity:0.01,springLength:200,springConstant:0.05,damping:0.4},
    stabilization:{iterations:150,updateInterval:25}},
  layout:{randomSeed:42},
  interaction:{hover:true,tooltipDelay:150,navigationButtons:true,keyboard:true},
  edges:{smooth:{type:'dynamic'},selectionWidth:2},
});

net.on('click',p=>{
  if(!p.nodes.length)return;
  const id=p.nodes[0],nd=nodes.get(id);
  if(!nd)return;
  if(nd._t==='m'){expanded.has(id)?collapse(id):expand(id);}
  else if(nd._t==='f'){toggleCalls(id);}
});

net.on('stabilizationIterationsDone',()=>net.setOptions({physics:{enabled:false}}));

function resetView(){
  [...expanded].forEach(id=>collapse(id));
  net.setOptions({physics:{enabled:true,stabilization:{iterations:100}}});
  setTimeout(()=>{net.setOptions({physics:{enabled:false}});net.fit();},1500);
}
function expandAll(){D.modules.forEach(m=>expand(m.id));net.setOptions({physics:{enabled:true}});}
function collapseAll(){[...expanded].forEach(id=>collapse(id));}

init();
</script>
</body>
</html>
"""


_VIS_URL = "https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"
_VIS_CACHE = Path(__file__).parent / ".vis-network.min.js"


def _get_vis_js() -> str:
    """Return vis-network JS, caching it beside aimapper.py after first download."""
    if _VIS_CACHE.exists():
        return _VIS_CACHE.read_text(encoding="utf-8")
    try:
        print("  downloading vis-network.js (one-time, ~689 KB)…", end=" ", flush=True)
        with urllib.request.urlopen(_VIS_URL, timeout=15) as r:
            js = r.read().decode("utf-8")
        _VIS_CACHE.write_text(js, encoding="utf-8")
        print("cached.")
        return js
    except Exception as e:
        print(f"failed ({e}).")
        return None


def generate_graph(file_map: Dict[str, dict], out_path: Path) -> bool:
    """Generate a self-contained collapsible tree network HTML graph."""
    data = _build_graph_data(file_map)
    vis_js = _get_vis_js()
    if vis_js is None:
        # CDN fallback with visible error overlay if network unavailable
        vis_tag = (
            'document.addEventListener("DOMContentLoaded",function(){'
            'if(typeof vis==="undefined"){'
            'document.getElementById("graph").innerHTML='
            '"<div style=\'color:#f78166;padding:20px;font-size:14px\'>'
            'vis-network failed to load from CDN. Run aimapper with internet access to cache it locally.</div>";}});'
            '</script><script src="' + _VIS_URL + '">'
        )
        html = _GRAPH_TEMPLATE.replace("__VIS_JS__", vis_tag)
    else:
        html = _GRAPH_TEMPLATE.replace("__VIS_JS__", vis_js)
    html = html.replace("__GRAPH_DATA__", json.dumps(data, separators=(",", ":")))
    out_path.write_text(html, encoding="utf-8")
    return True


# ─── interactive prompts ─────────────────────────────────────────────────────

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


def _ask_sources(root: Path) -> List[Path]:
    """
    Ask which subdirectories to scan.
    Input is a space/comma-separated list of paths relative to root.
    Empty input means scan the entire root.
    Invalid entries are reported and skipped.
    """
    try:
        raw = input("Source directories to scan (space or comma separated) [.]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)

    if not raw:
        return [root]

    # Split on commas and/or whitespace
    tokens = [t.strip() for t in re.split(r'[,\s]+', raw) if t.strip()]
    dirs: List[Path] = []
    for token in tokens:
        p = (root / token).resolve()
        if not p.exists():
            print(f"  warning: '{token}' not found, skipping", file=sys.stderr)
        elif not p.is_dir():
            print(f"  warning: '{token}' is not a directory, skipping", file=sys.stderr)
        else:
            dirs.append(p)

    if not dirs:
        print("error: no valid source directories given", file=sys.stderr)
        sys.exit(1)
    return dirs


# ─── main ────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(
        prog="aimapper",
        description="Generate a compact function map of your codebase for AI-assisted development.",
    )
    ap.add_argument("--no-json", action="store_true", help="skip aimapper.json output")
    ap.add_argument("--no-graph", action="store_true", help="skip graph.html output")
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = ap.parse_args()

    root = _ask_root()
    scan_dirs = _ask_sources(root)

    files = collect_files(root, scan_dirs)
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

    out_dir = root / OUTPUT_DIR
    out_dir.mkdir(exist_ok=True)

    md_path = out_dir / OUTPUT_MD
    md_path.write_text(md_content, encoding="utf-8")

    json_path = None
    if not args.no_json:
        json_path = out_dir / OUTPUT_JSON
        json_path.write_text(json.dumps(file_map, indent=2), encoding="utf-8")

    graph_path = out_dir / OUTPUT_GRAPH
    graph_ok = False
    if not args.no_graph:
        graph_ok = generate_graph(file_map, graph_path)

    claude_path = _inject_claude_md(root, Path(__file__).resolve())

    total_files = len(file_map)
    total_lines = sum(v.get("lines", 0) for v in file_map.values())
    map_lines = md_content.count('\n') + 1
    pct = 100 * (1 - map_lines / total_lines) if total_lines else 0

    print(f"wrote {md_path}")
    if json_path:
        print(f"wrote {json_path}")
    if graph_ok:
        print(f"wrote {graph_path}")
    print(f"updated {claude_path}")
    print(f"mapped {total_files} files / {total_lines:,} lines → {map_lines:,} map lines  ({pct:.0f}% reduction)")


if __name__ == "__main__":
    main()
