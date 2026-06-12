"""
Pull a "popular build" template for a skill from poe.ninja and assemble it
into a Build object the calc engine can chew on.

Phase 1 scope (intentional): gems only. Pulls top supports paired with the
skill (from the skill-specific `supportgems-<skill>` dimension) and top
mana-reserving buffs (auras and heralds) from the `skills` dimension. Items
and passives are left empty — those need PoB's item / tree data to translate
into Modifier objects (phase 4).
"""
from __future__ import annotations
import hashlib
import pickle
import time
from pathlib import Path

import requests
import blackboxprotobuf

from build import Build, GemSocket
import pob_data

BASE_URL = "https://poe.ninja/poe1/api"

DEFAULT_LEAGUE = "mirage"
DEFAULT_BUILD_TYPE = "exp"
DEFAULT_CHAR_LEVEL = 90
DEFAULT_GEM_LEVEL = 20
DEFAULT_GEM_QUALITY = 20

# Disk cache. Bump CACHE_SCHEMA when the assembled template shape changes
# (e.g., new metadata fields) so old cached entries are ignored.
CACHE_DIR = Path(__file__).parent / "data" / "cache" / "templates"
CACHE_TTL_SECONDS = 6 * 3600  # 6h: poe.ninja snapshots tick over slowly
CACHE_SCHEMA = 5  # bumped: auto-trim auras + cost-pool detection

# Gems / display-name substrings to exclude from aggregation because their
# popularity reflects an in-game bug, not a deliberately balanced interaction.
# See MEMORY: project_meta_exclusions for current entries and rationale.
# Matching is case-insensitive against BOTH the PoB skill key and display name.
EXCLUDED_GEM_PATTERNS: tuple[str, ...] = (
    "ward",   # Cast on Ward Break Support and any future ward-trigger gems
)


# ---------------------------------------------------------------------------
# Skill-data lookup helpers
# ---------------------------------------------------------------------------

def _tags(skill_data: dict) -> set[str]:
    st = skill_data.get("skillTypes")
    if isinstance(st, dict):
        return {k for k, v in st.items() if v}
    if isinstance(st, list):
        return set(st)
    return set()


def _is_support(skill_data: dict) -> bool:
    return bool(skill_data.get("support"))


def _has_mana_reservation(skill_data: dict) -> bool:
    """True if the skill reserves any mana (% or flat) at its first level.
    Reservation values are constant across gem levels for nearly all PoE
    auras, so the level-1 sample is sufficient."""
    levels = skill_data.get("levels") or []
    if not levels:
        return False
    lvl = levels[0]
    if not isinstance(lvl, dict):
        return False
    pct = float(lvl.get("manaReservationPercent") or 0)
    flat = float(lvl.get("manaReservationFlat") or 0)
    return pct > 0 or flat > 0


def _is_reserved_buff(skill_data: dict) -> bool:
    """True for skills that buff the caster (auras / heralds / banners).

    Requires:
      - `Buff` tag (it's a buff applied to you/allies; Hatred, HoI, Banners all have this)
      - Reserves mana (% or flat)
      - NOT `AuraNotOnCaster` (excludes curses and similar enemy-targeted auras)
      - NOT `AuraAffectsEnemies` (excludes mines and debuff fields)
    """
    tags = _tags(skill_data)
    if "Buff" not in tags:
        return False
    if "AuraNotOnCaster" in tags or "AuraAffectsEnemies" in tags:
        return False
    return _has_mana_reservation(skill_data)


def build_name_index() -> dict[str, list[str]]:
    """Map display name -> list of PoB skill keys (multiple if there are
    Vaal / alternate variants).
    """
    skills = pob_data.load_skills()
    index: dict[str, list[str]] = {}
    for key, data in skills.items():
        name = data.get("name")
        if not name:
            continue
        index.setdefault(name, []).append(key)
    return index


def is_excluded(display_name: str, key: str | None) -> bool:
    """True if either the display name or PoB key matches an exclusion pattern."""
    haystack = (display_name + " " + (key or "")).lower()
    return any(pat.lower() in haystack for pat in EXCLUDED_GEM_PATTERNS)


def clamp_gem_level(skill_data: dict, requested: int) -> int:
    """Some gems cap below the default 20 (e.g., Cast on Ward Break max 15).
    Clamp to whatever the gem actually supports."""
    levels = skill_data.get("levels") or []
    if not levels:
        return 1
    return min(requested, len(levels))


def resolve_skill_key(display_name: str, *, prefer_support: bool = False,
                      prefer_aura: bool = False) -> str | None:
    """Display name -> PoB skill key, preferring the variant that matches the
    context (support vs aura). Skips Vaal variants by default.

    poe.ninja appends ' Support' to support gem display names but PoB stores
    them without that suffix — try the stripped form first when we know we're
    looking for a support.
    """
    skills = pob_data.load_skills()
    index = build_name_index()

    candidate_names = [display_name]
    if display_name.endswith(" Support"):
        # Try the stripped form (PoB) first.
        candidate_names.insert(0, display_name[: -len(" Support")])

    candidates: list[str] = []
    for name in candidate_names:
        for key in index.get(name, []):
            if key not in candidates:
                candidates.append(key)

    # Filter out Vaal variants
    candidates = [c for c in candidates if not c.startswith("Vaal")]
    if not candidates:
        return None

    def score(key: str) -> tuple[int, ...]:
        data = skills[key]
        s = _is_support(data)
        a = _is_reserved_buff(data)
        # Higher score wins; preference dimensions ordered for stable sort.
        return (
            int(prefer_support and s),
            int(prefer_aura and a),
            -len(key),   # prefer shorter PoB keys
        )

    candidates.sort(key=score, reverse=True)
    return candidates[0]


# ---------------------------------------------------------------------------
# poe.ninja query
# ---------------------------------------------------------------------------

def _get_snapshot_version(league: str, build_type: str) -> str | None:
    resp = requests.get(f"{BASE_URL}/data/index-state", timeout=30)
    resp.raise_for_status()
    for sv in resp.json().get("snapshotVersions", []):
        if sv["url"] == league and sv["type"] == build_type:
            return sv["version"]
    return None


def _fetch_search(version: str, league: str, build_type: str, skill: str) -> dict:
    url = f"{BASE_URL}/builds/{version}/search"
    resp = requests.get(url, params={"overview": league, "type": build_type, "skills": skill},
                        timeout=60)
    resp.raise_for_status()
    msg, _ = blackboxprotobuf.decode_message(resp.content)
    return msg["1"]


def _fetch_dictionary(dict_hash: str) -> list[str]:
    resp = requests.get(f"{BASE_URL}/builds/dictionary/{dict_hash}", timeout=30)
    resp.raise_for_status()
    msg, _ = blackboxprotobuf.decode_message(resp.content)
    entries = msg.get("2", [])
    if not isinstance(entries, list):
        entries = [entries]
    names = []
    for e in entries:
        if isinstance(e, bytes):
            names.append(e.decode("utf-8"))
        elif isinstance(e, dict):
            # The dict shape is {"1": b"<name>"} — grab the first bytes value.
            for v in e.values():
                if isinstance(v, bytes):
                    names.append(v.decode("utf-8"))
                    break
            else:
                names.append("")
        else:
            names.append(str(e))
    return names


def _decode(v):
    return v.decode("utf-8") if isinstance(v, bytes) else v


def _dim(root: dict, name: str) -> list[dict]:
    """Return the list of (id, count) entries for a named dimension."""
    dims = root["2"]
    if not isinstance(dims, list):
        dims = [dims]
    for d in dims:
        if _decode(d["1"]) == name:
            entries = d.get("3", [])
            if not isinstance(entries, list):
                entries = [entries] if entries else []
            return entries
    return []


def _entries_to_pairs(entries: list[dict], names: list[str]) -> list[tuple[str, int]]:
    """Convert protobuf entries to [(name, count), ...] sorted by count desc."""
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        vals = list(e.values())
        if len(vals) != 2:
            continue
        idx, count = vals
        if not isinstance(idx, int) or idx >= len(names):
            continue
        out.append((names[idx], count))
    out.sort(key=lambda p: p[1], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Template assembly
# ---------------------------------------------------------------------------

def _cache_key(params: dict) -> str:
    payload = "|".join(f"{k}={params[k]}" for k in sorted(params))
    payload = f"v{CACHE_SCHEMA}|{payload}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.pkl"


def _read_cache(key: str) -> tuple[Build, dict] | None:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        with path.open("rb") as f:
            entry = pickle.load(f)
    except Exception:
        return None  # corrupt / version mismatch
    if entry.get("schema") != CACHE_SCHEMA:
        return None
    if time.time() - entry["timestamp"] > CACHE_TTL_SECONDS:
        return None
    build = entry["build"]
    metadata = dict(entry["metadata"])
    metadata["cache_status"] = "hit"
    metadata["cached_at"] = entry["timestamp"]
    return build, metadata


def _write_cache(key: str, build: Build, metadata: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with _cache_path(key).open("wb") as f:
            pickle.dump(
                {"schema": CACHE_SCHEMA, "timestamp": time.time(),
                 "build": build, "metadata": metadata},
                f,
            )
    except Exception:
        pass  # cache write failure is non-fatal


def clear_template_cache() -> int:
    """Delete all cached templates. Returns the number of files removed."""
    if not CACHE_DIR.exists():
        return 0
    n = 0
    for p in CACHE_DIR.glob("*.pkl"):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


def template_for_skill(
    skill_display_name: str,
    *,
    league: str = DEFAULT_LEAGUE,
    build_type: str = DEFAULT_BUILD_TYPE,
    n_supports: int = 5,
    n_auras: int = 3,
    n_keypassives: int = 12,
    n_unique_items: int = 8,
    char_level: int = DEFAULT_CHAR_LEVEL,
    gem_level: int = DEFAULT_GEM_LEVEL,
    gem_quality: int = DEFAULT_GEM_QUALITY,
    use_cache: bool = True,
) -> tuple[Build, dict]:
    """Return (Build, metadata).

    With `use_cache=True` (default), assembled templates are pickled to
    `data/cache/templates/` for CACHE_TTL_SECONDS and reused on subsequent
    calls. `metadata["cache_status"]` is "hit" / "miss" / "bypass".
    """
    params = dict(
        skill=skill_display_name, league=league, build_type=build_type,
        n_supports=n_supports, n_auras=n_auras, n_keypassives=n_keypassives,
        n_unique_items=n_unique_items, char_level=char_level,
        gem_level=gem_level, gem_quality=gem_quality,
    )
    key = _cache_key(params)

    if use_cache:
        cached = _read_cache(key)
        if cached is not None:
            return cached

    build, metadata = _fetch_template_uncached(
        skill_display_name, league=league, build_type=build_type,
        n_supports=n_supports, n_auras=n_auras, n_keypassives=n_keypassives,
        n_unique_items=n_unique_items, char_level=char_level,
        gem_level=gem_level, gem_quality=gem_quality,
    )
    metadata["cache_status"] = "miss" if use_cache else "bypass"

    if use_cache:
        _write_cache(key, build, metadata)
    return build, metadata


def _fetch_template_uncached(
    skill_display_name: str,
    *,
    league: str = DEFAULT_LEAGUE,
    build_type: str = DEFAULT_BUILD_TYPE,
    n_supports: int = 5,
    n_auras: int = 3,
    n_keypassives: int = 12,
    n_unique_items: int = 8,
    char_level: int = DEFAULT_CHAR_LEVEL,
    gem_level: int = DEFAULT_GEM_LEVEL,
    gem_quality: int = DEFAULT_GEM_QUALITY,
) -> tuple[Build, dict]:
    """Network + assembly path. Always hits poe.ninja; no caching."""
    version = _get_snapshot_version(league, build_type)
    if not version:
        raise RuntimeError(f"Could not find snapshot for {league}/{build_type}")

    root = _fetch_search(version, league, build_type, skill_display_name)
    total_chars = root["1"]

    # Find the gem dictionary
    refs = root.get("6", [])
    if not isinstance(refs, list):
        refs = [refs]
    gem_hash = None
    for ref in refs:
        if _decode(ref["1"]) == "gem":
            gem_hash = _decode(ref["2"])
            break
    if not gem_hash:
        raise RuntimeError("Gem dictionary not advertised on response")
    gem_names = _fetch_dictionary(gem_hash)

    # Determine the main skill PoB key (we need it for the supportgems-<X> dimension lookup)
    main_key = resolve_skill_key(skill_display_name)
    if not main_key:
        raise RuntimeError(f"Could not resolve PoB key for '{skill_display_name}'")
    main_skill_data = pob_data.get_skill(main_key)
    if main_skill_data is None:
        raise RuntimeError(f"Skill data not loaded for {main_key}")

    # Supports: skill-specific dimension `supportgems-<DisplayName>`
    sup_dim_name = f"supportgems-{skill_display_name}"
    sup_entries = _entries_to_pairs(_dim(root, sup_dim_name), gem_names)

    supports: list[GemSocket] = []
    support_provenance: list[tuple[str, int, str | None]] = []
    excluded_supports: list[tuple[str, int, str | None]] = []
    for name, count in sup_entries[:n_supports * 4]:  # over-pull, filter unresolved + excluded
        key = resolve_skill_key(name, prefer_support=True)
        sup_data = pob_data.get_skill(key) if key else None
        if key is None or sup_data is None or not _is_support(sup_data):
            support_provenance.append((name, count, None))
            continue
        if is_excluded(name, key):
            excluded_supports.append((name, count, key))
            continue
        lvl = clamp_gem_level(sup_data, gem_level)
        supports.append(GemSocket(gem_name=key, level=lvl, quality=gem_quality))
        support_provenance.append((name, count, key))
        if len(supports) >= n_supports:
            break

    # Auras: walk `allgems` (the catchall) and filter to `Aura` tag.
    # `skills` dimension only contains active damage skills — auras live in allgems.
    allgems_entries = _entries_to_pairs(_dim(root, "allgems"), gem_names)
    auras: list[GemSocket] = []
    aura_provenance: list[tuple[str, int, str | None]] = []
    excluded_auras: list[tuple[str, int, str | None]] = []
    for name, count in allgems_entries:
        if name == skill_display_name:
            continue
        key = resolve_skill_key(name, prefer_aura=True)
        if key is None:
            continue
        data = pob_data.get_skill(key) or {}
        if not _is_reserved_buff(data):
            continue
        if is_excluded(name, key):
            excluded_auras.append((name, count, key))
            continue
        lvl = clamp_gem_level(data, gem_level)
        auras.append(GemSocket(gem_name=key, level=lvl, quality=0))
        aura_provenance.append((name, count, key))
        if len(auras) >= n_auras:
            break

    # --- Unique items: lookup top-popular items, apply their mods ---
    item_hash = None
    for ref in refs:
        if _decode(ref["1"]) == "item":
            item_hash = _decode(ref["2"])
            break
    item_provenance: list[tuple[str, int, str | None]] = []
    picked_uniques: list[dict] = []
    unparsed_unique_lines: list[tuple[str, str]] = []
    if item_hash:
        import gear as _gear_module_preview
        item_names = _fetch_dictionary(item_hash)
        item_entries = _entries_to_pairs(_dim(root, "items"), item_names)
        # Generic categories (Rare Wand / Magic Flask / etc.) are filled in
        # by gear.populate_gear_from_uniques as synthetic rare baselines.
        skip_prefixes = ("Rare ", "Magic ", "Normal ")
        added = 0
        for name, count in item_entries:
            if added >= n_unique_items:
                break
            if any(name.startswith(p) for p in skip_prefixes):
                continue
            unique = pob_data.get_unique(name)
            if unique is None:
                item_provenance.append((name, count, None))
                continue
            picked_uniques.append(unique)
            item_provenance.append((name, count, unique["slot"]))
            # Only count picks that map to an equippable slot — jewels/flasks
            # aren't modeled yet, so they shouldn't consume the budget.
            if _gear_module_preview.map_unique_slot(unique["slot"]) is not None:
                added += 1

    import gear as gear_module
    full_gear = gear_module.populate_gear_from_uniques(
        picked_uniques, fill_empty_with_rares=True,
    )
    gear_mods_from_uniques = gear_module.aggregate_mods(full_gear)
    # Track which slots got uniques vs synthetic rares so the UI can display it.
    for slot, item in full_gear.items():
        for line in item.unparsed_lines:
            unparsed_unique_lines.append((item.display_name, line))

    # --- Keypassives: pull top notable nodes for this skill ---
    keypassive_hash = None
    for ref in refs:
        if _decode(ref["1"]) == "keypassive":
            keypassive_hash = _decode(ref["2"])
            break
    kp_provenance: list[tuple[str, int, str | None]] = []
    required_notables: list[str] = []
    if keypassive_hash:
        kp_names = _fetch_dictionary(keypassive_hash)
        kp_entries = _entries_to_pairs(_dim(root, "keypassives"), kp_names)
        name_to_id = pob_data.tree_name_to_id()
        # Over-pull aggressively because a large fraction of popular notables
        # in PoE 1 are jewel-granted (no tree path) — we need enough main-tree
        # anchors to drive a meaningful Steiner path.
        for name, count in kp_entries[:n_keypassives * 5]:
            nid = name_to_id.get(name)
            kp_provenance.append((name, count, nid))
            if nid:
                required_notables.append(nid)
            if len(required_notables) >= n_keypassives:
                break

    # --- Class: pull most-popular class (or ascendancy) and resolve base class ---
    class_hash = None
    for ref in refs:
        if _decode(ref["1"]) == "class":
            class_hash = _decode(ref["2"])
            break
    chosen_class: str | None = None
    chosen_ascendancy: str | None = None
    base_class: str | None = None
    if class_hash:
        class_names = _fetch_dictionary(class_hash)
        class_pairs = _entries_to_pairs(_dim(root, "class"), class_names)
        if class_pairs:
            top_class = class_pairs[0][0]
            asc_to_base = pob_data.ascendancy_to_base_class()
            base_class = asc_to_base.get(top_class)
            if base_class == top_class:
                # Player picked just a base class with no ascendancy yet.
                chosen_class = top_class
            elif base_class is not None:
                chosen_class = base_class
                chosen_ascendancy = top_class

    # --- Pathfinding: connect notables through the tree from class start ---
    # Split required notables: main-tree (has edges) vs jewel-granted (no
    # group/edges — comes from cluster or timeless jewels). Only the former
    # can be reached by pathfinding; the latter are added directly so their
    # stats get aggregated as if from a socketed jewel.
    path_required: list[str] = []
    jewel_granted: list[str] = []
    for nid in required_notables:
        node = pob_data.get_tree_node(nid)
        if node and (node.get("in") or node.get("out")):
            path_required.append(nid)
        else:
            jewel_granted.append(nid)

    allocated_nodes: list[str] = list(required_notables)  # fallback if no class
    path_summary: dict | None = None
    if base_class:
        start_node = pob_data.class_start_node(base_class)
        if start_node:
            import tree_path
            budget = tree_path.default_budget(char_level)
            result = tree_path.steiner_approximation(
                start_node, path_required, budget=budget,
            )
            # Path result + jewel-granted notables = full allocation
            allocated_nodes = list(result.allocated) + jewel_granted
            path_summary = {
                "start_node": start_node,
                "budget_total": budget,
                "budget_used": result.budget_used,
                "n_path_allocated": len(result.allocated),
                "n_jewel_granted": len(jewel_granted),
                "n_path_required": len(path_required),
                "n_path_required_reached": sum(1 for r in path_required
                                               if r in result.allocated),
                "unmet_required": result.unmet_required,
            }

    main_gem_level = clamp_gem_level(main_skill_data, gem_level)
    build = Build(
        char_class=base_class or "Unspecified",
        ascendancy=chosen_ascendancy,
        level=char_level,
        main_skill=GemSocket(gem_name=main_key, level=main_gem_level, quality=gem_quality),
        supports=supports,
        auras=auras,
        allocated_nodes=allocated_nodes,
        gear_mods=gear_mods_from_uniques,
        gear=full_gear,
    )

    # Auras come out of poe.ninja in popularity order; if the combined
    # reservation exceeds the build's mana budget AND no alternate cost pool
    # is active (Blood Magic / Eldritch Battery / Lifetap / etc.), drop the
    # least-popular auras until the build can actually run.
    from build import trim_auras_to_fit, cost_pool
    detected_pool = cost_pool(build)
    kept_auras, dropped_auras = trim_auras_to_fit(build)
    build.auras = kept_auras

    metadata = {
        "total_characters_in_sample": total_chars,
        "league": league,
        "build_type": build_type,
        "snapshot_version": version,
        "main_skill": (skill_display_name, main_key),
        "chosen_class": chosen_class,
        "chosen_ascendancy": chosen_ascendancy,
        "support_provenance": support_provenance,
        "aura_provenance": aura_provenance,
        "excluded_supports": excluded_supports,
        "excluded_auras": excluded_auras,
        "keypassive_provenance": kp_provenance,
        "item_provenance": item_provenance,
        "unparsed_unique_lines": unparsed_unique_lines,
        "path_summary": path_summary,
        "cost_pool": detected_pool,
        "dropped_auras": [a.gem_name for a in dropped_auras],
    }
    return build, metadata


# ---------------------------------------------------------------------------
# Pretty-print
# ---------------------------------------------------------------------------

def print_template_summary(build: Build, metadata: dict):
    print(f"poe.ninja template for {metadata['main_skill'][0]} -> "
          f"PoB key '{metadata['main_skill'][1]}'")
    print(f"League: {metadata['league']}/{metadata['build_type']}  "
          f"sample size: {metadata['total_characters_in_sample']} characters")

    chosen_class = metadata.get("chosen_class")
    chosen_asc = metadata.get("chosen_ascendancy")
    if chosen_class:
        asc_str = f" / {chosen_asc}" if chosen_asc else ""
        print(f"Class (top from poe.ninja): {chosen_class}{asc_str}")
    ps = metadata.get("path_summary")
    if ps:
        print(f"Passive tree: {ps['n_path_allocated']} tree nodes "
              f"({ps['budget_used']}/{ps['budget_total']} points), "
              f"+{ps['n_jewel_granted']} jewel-granted notables; "
              f"{ps['n_path_required_reached']}/{ps['n_path_required']} "
              f"key passives reached via tree"
              + (f", {len(ps['unmet_required'])} unreachable" if ps['unmet_required'] else ""))
    print()
    print(f"Top supports (by occurrence with main skill):")
    for name, count, key in metadata["support_provenance"]:
        marker = "  +" if (key and any(s.gem_name == key for s in build.supports)) else "  ?"
        resolved = key or "<no PoB match>"
        print(f"{marker} {name:<35} {count:>5}   {resolved}")
    print()
    pool = metadata.get("cost_pool", "mana")
    if pool != "mana":
        print(f"Cost pool detected: {pool} (mana freed for full reservation)")
    dropped = metadata.get("dropped_auras") or []
    if dropped:
        print(f"Dropped to fit reservation budget: {', '.join(dropped)}")

    print(f"Top auras (filtered from `allgems` dimension by Aura tag):")
    for name, count, key in metadata["aura_provenance"]:
        in_build = any(a.gem_name == key for a in build.auras)
        was_dropped = key in dropped
        marker = "  +" if in_build else ("  -" if was_dropped else "  ?")
        print(f"{marker} {name:<35} {count:>5}   {key}")

    excl_sup = metadata.get("excluded_supports") or []
    excl_aura = metadata.get("excluded_auras") or []
    if excl_sup or excl_aura:
        print()
        print(f"Excluded by meta-filter (see EXCLUDED_GEM_PATTERNS):")
        for name, count, key in excl_sup:
            print(f"  x {name:<35} {count:>5}   {key}   [support]")
        for name, count, key in excl_aura:
            print(f"  x {name:<35} {count:>5}   {key}   [aura]")

    kp_prov = metadata.get("keypassive_provenance") or []
    if kp_prov:
        print()
        print(f"Top key passives (allocated):")
        for name, count, nid in kp_prov:
            marker = "  +" if (nid and nid in build.allocated_nodes) else "  ?"
            resolved = nid or "<no tree match>"
            print(f"{marker} {name:<35} {count:>5}   {resolved}")

    item_prov = metadata.get("item_provenance") or []
    if item_prov:
        print()
        print(f"Top items (uniques only — generics like 'Rare Wand' skipped):")
        for name, count, slot in item_prov:
            marker = "  +" if slot else "  ?"
            resolved = slot or "<not a known unique>"
            print(f"{marker} {name:<35} {count:>5}   {resolved}")

    if build.gear:
        print()
        print(f"Equipped gear (uniques where popular, synthetic rares for empty slots):")
        for slot, item in build.gear.items():
            tag = "[U]" if item.item_type == "unique" else (
                "[R]" if item.item_type == "rare" else "[-]")
            print(f"  {tag} {slot:8s}  {item.display_name}")
