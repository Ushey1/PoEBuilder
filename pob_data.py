"""
Thin loader/lookup layer over the extracted PoB data (data/skills.json,
data/gems.json). Keeps file I/O and shape quirks contained so the rest of
the code works with plain dicts.
"""
import json
from pathlib import Path
from functools import lru_cache

DATA_DIR = Path(__file__).parent / "data"


@lru_cache(maxsize=1)
def load_skills() -> dict:
    return json.loads((DATA_DIR / "skills.json").read_text())


@lru_cache(maxsize=1)
def load_gems() -> dict:
    return json.loads((DATA_DIR / "gems.json").read_text())


@lru_cache(maxsize=1)
def load_misc() -> dict:
    return json.loads((DATA_DIR / "misc.json").read_text())


@lru_cache(maxsize=1)
def game_constants() -> dict:
    return load_misc().get("gameConstants", {})


@lru_cache(maxsize=1)
def load_tree() -> dict:
    return json.loads((DATA_DIR / "tree.json").read_text())


@lru_cache(maxsize=1)
def tree_name_to_id() -> dict[str, str]:
    """Index notable/keystone names -> node ID for lookup by display name
    (which is what poe.ninja's keypassives dimension gives us)."""
    nodes = load_tree().get("nodes", {})
    out: dict[str, str] = {}
    for nid, n in nodes.items():
        nm = n.get("name")
        if nm:
            # Prefer notable/keystone if there's a name clash with a regular node
            existing = out.get(nm)
            if existing is None:
                out[nm] = nid
            else:
                ex_node = nodes.get(existing, {})
                if not (ex_node.get("isNotable") or ex_node.get("isKeystone")):
                    if n.get("isNotable") or n.get("isKeystone"):
                        out[nm] = nid
    return out


def get_tree_node(node_id: str) -> dict | None:
    return load_tree().get("nodes", {}).get(str(node_id))


def class_start_node(class_name: str) -> str | None:
    """Display class name ('Witch', 'Marauder', ...) -> root passive tree node id."""
    return load_tree().get("class_name_to_start_node", {}).get(class_name)


def class_names() -> list[str]:
    """All base class display names, ordered to match the tree's class list."""
    classes = load_tree().get("classes") or []
    return [c["name"] for c in classes if isinstance(c, dict) and c.get("name")]


@lru_cache(maxsize=1)
def ascendancy_to_base_class() -> dict[str, str]:
    """Map ascendancy display name -> base class display name.

    poe.ninja's `class` dimension mixes ascendancies and base classes; this
    lets callers normalize 'Elementalist' -> 'Witch' so we can look up the
    correct starting node.
    """
    out: dict[str, str] = {}
    for c in load_tree().get("classes") or []:
        if not isinstance(c, dict):
            continue
        base = c.get("name")
        if not base:
            continue
        # Base class maps to itself so the lookup is uniform.
        out[base] = base
        for asc in c.get("ascendancies") or []:
            if isinstance(asc, dict) and asc.get("name"):
                out[asc["name"]] = base
    return out


@lru_cache(maxsize=1)
def load_uniques() -> dict:
    return json.loads((DATA_DIR / "uniques.json").read_text())


def get_unique(name: str) -> dict | None:
    """Look up a unique item by display name."""
    return load_uniques().get(name)


def get_skill(name: str) -> dict | None:
    """Look up a skill by its PoB key (e.g. 'FrostBolt', 'SupportSpellEcho')."""
    return load_skills().get(name)


def skill_tags(skill: dict) -> set[str]:
    """Return the set of skillType tags. Handles dict-shape {tag: true} vs list-shape."""
    st = skill.get("skillTypes")
    if isinstance(st, dict):
        return {k for k, v in st.items() if v}
    if isinstance(st, list):
        return set(st)
    return set()


def support_requires(support: dict) -> set[str]:
    return set(support.get("requireSkillTypes") or [])


def support_excludes(support: dict) -> set[str]:
    return set(support.get("excludeSkillTypes") or [])


def gem_level_data(skill: dict, level: int) -> dict:
    """levels is a 0-indexed list in the JSON; gem level N -> levels[N-1]."""
    levels = skill["levels"]
    idx = level - 1
    if idx < 0 or idx >= len(levels):
        raise ValueError(
            f"Gem level {level} out of range for {skill.get('name')} (max {len(levels)})"
        )
    return levels[idx]


def reservation(skill: dict, level: int) -> tuple[float, float]:
    """Returns (percent_reserved, flat_reserved). Either may be 0."""
    lvl = gem_level_data(skill, level)
    return (
        float(lvl.get("manaReservationPercent") or 0),
        float(lvl.get("manaReservationFlat") or 0),
    )
