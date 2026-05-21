# aimapper function map

## aimapper.py  (580 lines)
  imports: ast, json, os, re, sys, pathlib.Path, typing.Dict, typing.List ...
  def _strip_c_comments(text)  [L45, 35ln]  → [append, count, find, join, len]
  def _extract_c_func(context)  [L82, 32ln]  → [endswith, group, lstrip, rfind, rstrip, search, split, start, ...]
  def _parse_c(path, root)  [L116, 87ln]  → [_extract_c_func, _strip_c_comments, add, append, enumerate, finditer, group, join, ...]
  def _ast_calls(node)  [L207, 9ln]  → [append, isinstance, set, sorted, walk]
  def _func_sig(node)  [L218, 19ln]  → [append, getattr, isinstance, join]
  def _func_entry(node)  [L239, 8ln]  → [_ast_calls, _func_sig, getattr]
  def _parse_py(path, root)  [L249, 43ln]  → [_func_entry, append, count, getattr, isinstance, parse, read_text, str, ...]
  def _schema(obj, depth, max_depth)  [L296, 18ln]  → [_schema, isinstance, items, list, type]
  def _parse_json(path, root)  [L316, 16ln]  → [_schema, count, isinstance, keys, list, loads, read_text, stat]
  def parse_file(path, root)  [L336, 9ln]  → [_parse_c, _parse_json, _parse_py, lower]
  def collect_files(root, scan_dirs)  [L352, 20ln]  → [Path, add, append, endswith, lower, set, sorted, walk]
  def _calls_str(calls, limit)  [L376, 6ln]  → [join, len]
  def render_md(file_map)  [L384, 46ln]  → [_calls_str, append, get, items, join, len, sorted, str]
  def _inject_claude_md(project_root)  [L445, 30ln]  → [exists, read_text, rstrip, search, start, sub, write_text]
  def _ask_root()  [L479, 12ln]  → [Path, cwd, exit, input, is_dir, print, resolve, strip]
  def _ask_sources(root)  [L493, 32ln]  → [append, exists, exit, input, is_dir, print, resolve, split, ...]
  def main()  [L529, 47ln]  → [ArgumentParser, _ask_root, _ask_sources, _inject_claude_md, add_argument, collect_files, count, dumps, ...]
