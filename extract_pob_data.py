"""
Extract Path of Building Community data files into JSON.

PoB stores skill/gem data as Lua files that reference globals (SkillType,
KeywordFlag) and call helper functions (mod, flag, skill). We use lupa
(embedded Lua 5.3) to evaluate them with stubbed helpers, then snapshot
the resulting tables as JSON.

Run: python extract_pob_data.py
"""
import json
from pathlib import Path
import lupa
from lupa import LuaRuntime

POB_SRC = Path("c:/VSCodeProjects/PathOfBuilding/src")
OUT_DIR = Path("c:/VSCodeProjects/PoEBuilder/data")

SKILL_FILES = [
    "Data/Skills/act_int.lua",   # Frostbolt, Discipline, most spells
    "Data/Skills/act_dex.lua",   # Hatred, Frostbite, dex auras/curses
    "Data/Skills/act_str.lua",   # Anger, Determination, str auras
    "Data/Skills/sup_int.lua",   # Spell supports
    "Data/Skills/sup_dex.lua",   # Projectile supports
    "Data/Skills/sup_str.lua",   # Generic supports
]


def lua_to_py(obj, _seen=None):
    """Recursively convert Lua values to plain Python types."""
    if _seen is None:
        _seen = set()
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if lupa.lua_type(obj) != "table":
        return repr(obj)

    obj_id = id(obj)
    if obj_id in _seen:
        return "<cycle>"
    _seen.add(obj_id)
    try:
        keys = list(obj.keys())
        if not keys:
            return {}
        # Pure 1-indexed array
        if all(isinstance(k, int) for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
            return [lua_to_py(obj[k], _seen) for k in sorted(keys)]
        # Mixed / dict
        result = {}
        for k in keys:
            py_k = str(k) if not isinstance(k, str) else k
            result[py_k] = lua_to_py(obj[k], _seen)
        return result
    finally:
        _seen.discard(obj_id)


def make_runtime():
    """Build a Lua runtime preloaded with the stubs PoB skill files expect."""
    L = LuaRuntime(unpack_returned_tuples=True)

    # Auto-keyed tag tables: SkillType.Spell -> "Spell", any access returns the key name
    setup = """
        local function autokey()
            return setmetatable({}, {__index = function(t, k) rawset(t, k, k); return k end})
        end
        SkillType = autokey()
        KeywordFlag = autokey()
        ModFlag = autokey()
        GlobalCache = {}
        bit = {
            bor = function(...) local t = {}; for i, v in ipairs({...}) do t[i] = v end; return t end,
            band = function(...) local t = {}; for i, v in ipairs({...}) do t[i] = v end; return t end,
            bnot = function(x) return x end,
            lshift = function(a, b) return a end,
            rshift = function(a, b) return a end,
        }
    """
    L.execute(setup)

    # mod/flag/skill stubs: capture call as a Lua table {_call=..., args=...}
    capture_src = """
        function _make_capture(name)
            return function(...)
                local args = {...}
                return { _call = name, args = args }
            end
        end
    """
    L.execute(capture_src)
    make_capture = L.globals()._make_capture
    L.globals().mod = make_capture("mod")
    L.globals().flag = make_capture("flag")
    L.globals().skill = make_capture("skill")

    return L


def extract_skill_file(L, rel_path):
    """Load a Skills/*.lua file and return its `skills` table as Python."""
    abs_path = (POB_SRC / rel_path).as_posix()

    # The file starts with `local skills, mod, flag, skill = ...`
    # We load it as a chunk and pass these as varargs.
    loader = f"""
        local chunk, err = loadfile([[{abs_path}]])
        if not chunk then error(err) end
        local skills_out = {{}}
        chunk(skills_out, mod, flag, skill)
        return skills_out
    """
    skills_table = L.execute(loader)
    return lua_to_py(skills_table)


def extract_gems(L):
    """Gems.lua just `return { ... }` — pure data."""
    abs_path = (POB_SRC / "Data/Gems.lua").as_posix()
    loader = f"""
        local chunk, err = loadfile([[{abs_path}]])
        if not chunk then error(err) end
        return chunk()
    """
    return lua_to_py(L.execute(loader))


def extract_misc(L):
    """Misc.lua starts with `local data = ...`, then assigns to `data.X`.
    We pass an empty table as the vararg and read it back."""
    abs_path = (POB_SRC / "Data/Misc.lua").as_posix()
    loader = f"""
        local chunk, err = loadfile([[{abs_path}]])
        if not chunk then error(err) end
        local data = {{}}
        chunk(data)
        return data
    """
    return lua_to_py(L.execute(loader))


import re as _re

_UNIQUE_TAG_RE = _re.compile(r"\{[^}]*\}")
_UNIQUE_VARIANT_RE = _re.compile(r"\{variant:([^}]+)\}")
_UNIQUE_RANGE_RE = _re.compile(r"\((-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)\)")


def _resolve_unique_ranges(line: str) -> str:
    """Substitute (min-max) value ranges with their midpoint."""
    def repl(m):
        lo, hi = float(m.group(1)), float(m.group(2))
        mid = (lo + hi) / 2
        return f"{int(mid)}" if mid == int(mid) else f"{mid:g}"
    return _UNIQUE_RANGE_RE.sub(repl, line)


def _strip_inline_tags(line: str) -> str:
    """Drop `{tags:...}` and `{variant:...}` prefixes — they're internal hints,
    not part of the display text."""
    return _UNIQUE_TAG_RE.sub("", line).strip()


def _line_applies_to_variant(line: str, current_idx: int | None) -> bool:
    """If the line has a {variant:N,M,...} prefix, it applies only when
    current_idx is in that list. Lines without a variant prefix apply always."""
    m = _UNIQUE_VARIANT_RE.search(line)
    if not m:
        return True
    if current_idx is None:
        return True
    ids = [int(x.strip()) for x in m.group(1).split(",") if x.strip().isdigit()]
    return current_idx in ids


def _parse_unique_block(text: str) -> dict | None:
    """Parse one [[ ... ]] block (one unique item) into a structured record."""
    raw_lines = [ln for ln in text.split("\n") if ln.strip()]
    if len(raw_lines) < 2:
        return None

    name = raw_lines[0].strip()
    base_type = raw_lines[1].strip()
    level_req = None
    variants: list[str] = []
    stat_lines: list[str] = []

    for line in raw_lines[2:]:
        l = line.strip()
        if l.startswith("Variant:"):
            variants.append(l[len("Variant:"):].strip())
        elif l.startswith("Requires Level"):
            try:
                level_req = int(l.split()[-1])
            except ValueError:
                pass
        elif l.startswith("LevelReq:"):
            try:
                level_req = int(l.split(":")[-1].strip())
            except ValueError:
                pass
        elif l.startswith(("Implicits:", "Source:", "Selected Variant:", "Has Alt Variant",
                           "Item Class:", "Has Alt Variant:",
                           "League:", "Limited to:", "Radius:", "Quality:",
                           "Sockets:", "Item Level:", "Upgrade:")):
            continue  # metadata we don't use
        elif l in ("Corrupted", "Mirrored", "Shaper Item", "Elder Item",
                   "No Physical Damage"):
            continue
        elif _re.match(r"^Has\s+\d+\s+Sockets?$", l):
            continue
        elif _re.match(r"^\+?\d+\s+to\s+Level\s+of\s+Socketed\s+.+\s+Gems?$", l):
            continue  # "+1 to Level of Socketed Cold Gems" — gem-level boost, defer
        else:
            stat_lines.append(l)

    # If there are variant declarations, figure out which one is "Current"
    current_variant_idx: int | None = None
    if variants:
        for i, v in enumerate(variants, start=1):
            if v.lower() == "current":
                current_variant_idx = i
                break
        # No "Current" marker -> use the last variant (latest)
        if current_variant_idx is None:
            current_variant_idx = len(variants)

    # Filter and clean stat lines
    cleaned: list[str] = []
    _post_strip_metadata = {"Corrupted", "Mirrored", "Shaper Item", "Elder Item",
                            "No Physical Damage", "Rampage", "Extra gore"}
    for line in stat_lines:
        if not _line_applies_to_variant(line, current_variant_idx):
            continue
        stripped = _strip_inline_tags(line)
        if not stripped:
            continue
        # Some metadata lines have inline tag prefixes (e.g. `{variant:2}Corrupted`)
        # so they slip past the earlier prefix check — re-check after stripping.
        if stripped in _post_strip_metadata:
            continue
        if stripped.startswith("Upgrade:") or stripped.startswith("League:"):
            continue
        resolved = _resolve_unique_ranges(stripped)
        cleaned.append(resolved)

    return {
        "name": name,
        "base_type": base_type,
        "level_req": level_req,
        "stats": cleaned,
    }


def extract_uniques(L):
    """Extract every Uniques/*.lua file. Returns flat dict {name -> record}.

    Skips Special/ subdir (Atlas-specific & non-standard uniques) for MVP.
    """
    uniques_dir = POB_SRC / "Data/Uniques"
    files = [p for p in uniques_dir.glob("*.lua") if p.is_file()]
    out: dict[str, dict] = {}
    for f in files:
        loader = f"""
            local chunk, err = loadfile([[{f.as_posix()}]])
            if not chunk then error(err) end
            return chunk()
        """
        try:
            raw = lua_to_py(L.execute(loader))
        except Exception as e:
            print(f"  skipping {f.name}: {e}")
            continue
        if not isinstance(raw, list):
            continue
        slot = f.stem  # e.g., "amulet", "body"
        for entry in raw:
            if not isinstance(entry, str):
                continue
            rec = _parse_unique_block(entry)
            if rec is None or not rec["name"]:
                continue
            rec["slot"] = slot
            # If name collision, keep first (oldest variant) — both are likely the same item
            out.setdefault(rec["name"], rec)
    return out


def extract_tree(L, league_dir: str = "3_28"):
    """Tree.lua is pure-data `return {...}` — classes, groups, and the node
    map. We keep classes, nodes (with stats + edges + class-start markers),
    and a precomputed class_start_nodes index. Visual/group data is dropped
    to keep the JSON manageable, but edges are kept for path-finding."""
    abs_path = (POB_SRC / f"TreeData/{league_dir}/tree.lua").as_posix()
    loader = f"""
        local chunk, err = loadfile([[{abs_path}]])
        if not chunk then error(err) end
        return chunk()
    """
    raw = lua_to_py(L.execute(loader))

    nodes_raw = raw.get("nodes", {})
    slim_nodes = {}
    # `in` / `out` are edge lists (neighboring node IDs). classStartIndex
    # marks the class root. group/orbit are kept so a future visual layer
    # could render the tree without re-extraction.
    keep_node_fields = {"skill", "name", "stats", "isKeystone", "isNotable",
                        "isMastery", "isJewelSocket", "ascendancyName",
                        "isAscendancyStart", "reminderText",
                        "in", "out", "classStartIndex", "group",
                        "orbit", "orbitIndex"}
    class_start_nodes: dict[int, str] = {}
    for nid, node in (nodes_raw.items() if isinstance(nodes_raw, dict) else []):
        if not isinstance(node, dict):
            continue
        slim = {k: v for k, v in node.items() if k in keep_node_fields}
        # Normalize edges to lists of string IDs
        for edge_key in ("in", "out"):
            edges = slim.get(edge_key)
            if isinstance(edges, list):
                slim[edge_key] = [str(e) for e in edges]
            elif edges is None:
                slim[edge_key] = []
        slim_nodes[str(nid)] = slim
        if "classStartIndex" in slim:
            class_start_nodes[int(slim["classStartIndex"])] = str(nid)

    return {
        "tree_version": league_dir,
        "classes": raw.get("classes"),
        "nodes": slim_nodes,
        # Maps class index (0=Scion, 1=Marauder, 2=Ranger, 3=Witch, 4=Duelist,
        # 5=Templar, 6=Shadow) -> starting node ID. Class names also indexed
        # for convenience.
        "class_start_nodes": class_start_nodes,
        "class_name_to_start_node": _build_class_name_index(raw.get("classes"), class_start_nodes),
    }


def _build_class_name_index(classes_raw, class_start_nodes: dict[int, str]) -> dict[str, str]:
    """classes_raw is the lua array (1-indexed in lua -> 0-indexed in Python list).
    classStartIndex on nodes is 0-indexed (Scion=0)."""
    if not isinstance(classes_raw, list):
        return {}
    out: dict[str, str] = {}
    for idx, c in enumerate(classes_raw):
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        node_id = class_start_nodes.get(idx)
        if name and node_id:
            out[name] = node_id
    return out


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    L = make_runtime()

    print("Extracting Gems.lua...")
    gems = extract_gems(L)
    (OUT_DIR / "gems.json").write_text(json.dumps(gems, indent=2))
    print(f"  -> {len(gems)} gem entries")

    print("Extracting Misc.lua (gameConstants etc.)...")
    misc = extract_misc(L)
    (OUT_DIR / "misc.json").write_text(json.dumps(misc, indent=2))
    consts = misc.get("gameConstants", {})
    print(f"  -> gameConstants: {len(consts)} entries (sample: "
          f"SkillDamageBaseEffectiveness={consts.get('SkillDamageBaseEffectiveness')}, "
          f"SkillDamageIncrementalEffectiveness={consts.get('SkillDamageIncrementalEffectiveness')})")

    print("Extracting passive tree (3_28)...")
    tree = extract_tree(L, "3_28")
    (OUT_DIR / "tree.json").write_text(json.dumps(tree, indent=2))
    print(f"  -> {len(tree['nodes'])} nodes, {len(tree['classes'])} classes")

    print("Extracting unique items...")
    uniques = extract_uniques(L)
    (OUT_DIR / "uniques.json").write_text(json.dumps(uniques, indent=2))
    by_slot: dict = {}
    for rec in uniques.values():
        by_slot[rec["slot"]] = by_slot.get(rec["slot"], 0) + 1
    print(f"  -> {len(uniques)} unique items across {len(by_slot)} slots")

    all_skills = {}
    for rel in SKILL_FILES:
        print(f"Extracting {rel}...")
        skills = extract_skill_file(L, rel)
        print(f"  -> {len(skills)} skills")
        # Tag the file source so we can find skills later
        for key, val in skills.items():
            if isinstance(val, dict):
                val["_source"] = rel
        all_skills.update(skills)

    (OUT_DIR / "skills.json").write_text(json.dumps(all_skills, indent=2))
    print(f"\nTotal skills: {len(all_skills)}")

    # Sanity check: print Frostbolt's headline numbers
    fb = all_skills.get("FrostBolt") or all_skills.get("Frostbolt")
    if fb:
        print("\nFrostbolt extracted:")
        print(f"  name = {fb.get('name')}")
        print(f"  castTime = {fb.get('castTime')}")
        print(f"  baseEffectiveness = {fb.get('baseEffectiveness')}")
        print(f"  incrementalEffectiveness = {fb.get('incrementalEffectiveness')}")
        print(f"  skillTypes = {list(fb.get('skillTypes', {}).keys())[:8]}...")
        levels = fb.get("levels", {})
        if "20" in levels:
            print(f"  levels[20] = {levels['20']}")
    else:
        print("\nWARNING: Frostbolt not found in extracted skills!")


if __name__ == "__main__":
    main()
