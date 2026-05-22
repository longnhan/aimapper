<!-- aimapper-map -->
## AI map
Read `aimapper.md` before exploring this codebase — it maps every function and
call graph so you can locate the right file/line without reading raw source.
Run `python3 aimapper.py` to regenerate after big changes.
<!-- /aimapper-map -->

# aimapper
Scans your codebase and generates a compact function map so AI assistants need fewer tokens to understand your project.

## Why it saves tokens

Without a map, every AI session starts with blind exploration — multiple `grep`, `find`, and file-read calls just to orient. On a medium project (20–50 files) that burns **3,000–8,000 tokens before any real work begins**.

aimapper replaces that with a single pre-built index: every file, every function signature, every call edge. The AI reads it once and immediately knows where to look.

| | Tokens spent orienting |
|---|---|
| No map (grep + read exploration) | ~3,000–8,000 per session |
| With `aimapper.md` | ~800–2,000 (map size) |
| **Saving** | **~50–70% of orientation cost** |

**Keep the map fresh.** Regenerate after big changes — a stale map is worse than no map.

# app demo
<img width="1844" height="1083" alt="image" src="https://github.com/user-attachments/assets/7f48eed8-dac3-4e1c-9bcb-af2e3c9f1174" />
<img width="1844" height="1083" alt="image" src="https://github.com/user-attachments/assets/f22ce0b7-a6e0-47ed-aee0-38cd736368b9" />
