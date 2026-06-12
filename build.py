"""
Build model and pre-calc validation.

A Build is a player-spec: class, ascendancy, level, main skill + supports,
auras, and gear/passive modifiers. validate(build) runs the two gates
flagged in MEMORY before any DPS math:

  1. Tag-compatibility: each support must have requireSkillTypes overlap
     with the main skill's tags, and no excludeSkillTypes overlap.
  2. Mana-reservation budget: sum of aura reservation (less reduction)
     must leave at least `min_free_mana_pct` of the pool free.

Anything that fails the gates is reported in ValidationResult — the caller
decides whether to drop it or warn. Calc engine consumes only the
post-validation, filtered set.
"""
from dataclasses import dataclass, field

import pob_data


@dataclass
class GemSocket:
    """A single gem placement: which gem, what level, what quality."""

    gem_name: str
    level: int = 20
    quality: int = 0


# Modifier is defined in stat_map.py and re-exported here so callers can
# construct gear/passive mods without depending on the stat-map module name.
from stat_map import Modifier  # noqa: E402, F401


# --- Playstyle constants ---
# QoL weighting and (later) optimizer scoring are archetype-dependent. The
# build declares its intended playstyle; downstream code reads this to decide
# how much each QoL metric matters.
PLAYSTYLE_MAPPING = "mapping"      # fast clear, lots of movement, random monster skills
PLAYSTYLE_BOSSING = "bossing"      # stand-still encounters, specific boss-skill mitigation
PLAYSTYLE_BALANCED = "balanced"    # default; do both well enough
PLAYSTYLES = {PLAYSTYLE_MAPPING, PLAYSTYLE_BOSSING, PLAYSTYLE_BALANCED}


@dataclass
class Build:
    char_class: str
    ascendancy: str | None
    level: int

    main_skill: GemSocket
    supports: list[GemSocket] = field(default_factory=list)
    auras: list[GemSocket] = field(default_factory=list)

    gear_mods: list[Modifier] = field(default_factory=list)
    # Per-slot gear (helmet/body/weapon/...). When populated, calc should
    # prefer aggregating from `gear` over reading the flat `gear_mods` list;
    # the latter is kept for backwards compat with callers that haven't
    # migrated yet.
    gear: dict = field(default_factory=dict)  # dict[str, GearItem] from gear.py
    passive_mods: list[Modifier] = field(default_factory=list)
    # Allocated passive tree nodes by PoB node id. Translated to Modifiers
    # at calc time via mod_parser. Use names with care — same names can
    # exist across leagues, so the canonical id is preferred.
    allocated_nodes: list[str] = field(default_factory=list)

    # --- attributes (totals, including gear/passive contributions the user has rolled up) ---
    strength: int = 0
    dexterity: int = 0
    intelligence: int = 0

    # --- defense config ---
    resist_penalty: float = -60.0   # endgame default; -30 yellow maps, -60 red maps
    max_resist_pct: float = 75.0    # cap; can be raised by specific mods
    enemy_accuracy: float = 1000.0  # rough endgame monster value; used for evasion check
    reference_hit_damage: float = 1000.0  # size of incoming hit used to size up EHP

    # --- validation config ---
    min_free_mana_pct: float = 35.0
    reservation_reduction_pct: float = 0.0  # global % reduced reservation (Charisma, Enlighten, etc.)

    # --- playstyle (drives QoL weighting; see project_optimization_loop memory) ---
    playstyle: str = PLAYSTYLE_BALANCED


@dataclass
class ValidationResult:
    valid: bool
    warnings: list[str] = field(default_factory=list)

    # Tag-compat results
    valid_supports: list[GemSocket] = field(default_factory=list)
    rejected_supports: list[tuple[GemSocket, str]] = field(default_factory=list)

    # Reservation results
    active_auras: list[GemSocket] = field(default_factory=list)
    total_reservation_pct: float = 0.0  # after reduction
    free_mana_pct: float = 100.0

    def report(self) -> str:
        lines = [f"Valid: {self.valid}"]
        if self.warnings:
            lines.append("Warnings:")
            for w in self.warnings:
                lines.append(f"  - {w}")
        lines.append(f"Active supports ({len(self.valid_supports)}):")
        for s in self.valid_supports:
            lines.append(f"  + {s.gem_name} (lvl {s.level})")
        if self.rejected_supports:
            lines.append(f"Rejected supports ({len(self.rejected_supports)}):")
            for s, reason in self.rejected_supports:
                lines.append(f"  - {s.gem_name}: {reason}")
        lines.append(f"Active auras ({len(self.active_auras)}):")
        for a in self.active_auras:
            lines.append(f"  + {a.gem_name} (lvl {a.level})")
        lines.append(f"Reservation: {self.total_reservation_pct:.1f}% reserved, "
                     f"{self.free_mana_pct:.1f}% free")
        return "\n".join(lines)


def _check_tag_compat(main_tags: set[str], support_skill: dict) -> tuple[bool, str]:
    """OR-semantics on require, NONE-OF on exclude."""
    req = pob_data.support_requires(support_skill)
    exc = pob_data.support_excludes(support_skill)

    if req and not (req & main_tags):
        missing = ", ".join(sorted(req))
        return False, f"requires any of {{{missing}}}; main skill has none"
    if exc and (exc & main_tags):
        clash = exc & main_tags
        return False, f"excludes {{{', '.join(sorted(clash))}}} present on main skill"
    return True, ""


# --- Cost pool detection -----------------------------------------------------
# A build can pay for skill mana costs out of life or energy shield instead
# of mana, freeing the entire mana pool for aura reservation.

COST_POOL_MANA = "mana"
COST_POOL_LIFE = "life"
COST_POOL_ES = "es"

# Known node IDs for the relevant keystones in the 3.28 tree. Names are stable
# across leagues but IDs change occasionally; resolve at call time so a tree
# re-extract corrects them automatically.
_BLOOD_MAGIC_NODE_NAMES = ("Blood Magic", "Lifetap")
_ELDRITCH_BATTERY_NODE_NAMES = ("Eldritch Battery",)
# Strings to scan in gear stat lines (any item that makes skills cost life)
_LIFE_COST_GEAR_KEYWORDS = (
    "Skills Cost Life instead of Mana",
    "Skills Reserve Life instead of Mana",
    "Spend Life instead of Mana",
)


def _node_ids_for(names: tuple[str, ...]) -> set[str]:
    name_to_id = pob_data.tree_name_to_id()
    return {name_to_id[n] for n in names if name_to_id.get(n)}


def cost_pool(build: Build) -> str:
    """Which resource pays for the main skill's casting cost.

    Order of precedence: Blood Magic-style mechanics (life) > Eldritch
    Battery (energy shield) > default mana. When the pool is life or ES,
    mana can be reserved up to 100% without breaking the build.
    """
    allocated = set(build.allocated_nodes)

    if _node_ids_for(_BLOOD_MAGIC_NODE_NAMES) & allocated:
        return COST_POOL_LIFE

    if any(s.gem_name == "SupportLifetap" for s in build.supports):
        return COST_POOL_LIFE

    if build.gear:
        for item in build.gear.values():
            for line in (getattr(item, "stat_lines", None) or []):
                if any(kw in line for kw in _LIFE_COST_GEAR_KEYWORDS):
                    return COST_POOL_LIFE

    if _node_ids_for(_ELDRITCH_BATTERY_NODE_NAMES) & allocated:
        return COST_POOL_ES

    return COST_POOL_MANA


def _aura_reservation_pct(auras: list[GemSocket], reduction_pct: float) -> float:
    total = 0.0
    for a in auras:
        data = pob_data.get_skill(a.gem_name)
        if data is None:
            continue
        pct, _flat = pob_data.reservation(data, a.level)
        total += pct
    reduction = max(0.0, min(95.0, reduction_pct))
    return total * (1.0 - reduction / 100.0)


def trim_auras_to_fit(build: Build) -> tuple[list[GemSocket], list[GemSocket]]:
    """Drop auras from the end of build.auras (least popular first) until
    reservation fits the build's budget. Returns (kept, dropped).

    Budget depends on the cost pool:
      - mana: needs to leave `build.min_free_mana_pct`% free
      - life/es: anything up to 100% reservation is fine
    The caller assigns `build.auras = kept`.
    """
    pool = cost_pool(build)
    if pool == COST_POOL_MANA:
        budget = 100.0 - build.min_free_mana_pct
    else:
        budget = 100.0

    auras = list(build.auras)
    dropped: list[GemSocket] = []
    while auras and _aura_reservation_pct(auras, build.reservation_reduction_pct) > budget:
        dropped.append(auras.pop())
    return auras, dropped


def validate(build: Build) -> ValidationResult:
    result = ValidationResult(valid=True)

    main = pob_data.get_skill(build.main_skill.gem_name)
    if main is None:
        result.valid = False
        result.warnings.append(f"Main skill '{build.main_skill.gem_name}' not found in data")
        return result

    main_tags = pob_data.skill_tags(main)

    # Gate 1: tag-compat for supports
    for sup_socket in build.supports:
        sup = pob_data.get_skill(sup_socket.gem_name)
        if sup is None:
            result.rejected_supports.append((sup_socket, "not found in data"))
            continue
        ok, reason = _check_tag_compat(main_tags, sup)
        if ok:
            result.valid_supports.append(sup_socket)
        else:
            result.rejected_supports.append((sup_socket, reason))

    # Gate 2: aura reservation budget
    total_pct = 0.0
    flat_warned = False
    for aura_socket in build.auras:
        aura = pob_data.get_skill(aura_socket.gem_name)
        if aura is None:
            result.warnings.append(f"Aura '{aura_socket.gem_name}' not found in data")
            continue
        pct, flat = pob_data.reservation(aura, aura_socket.level)
        if flat and not flat_warned:
            result.warnings.append(
                "Flat-reservation auras (e.g. Clarity) not yet supported in budget calc -- counting as 0"
            )
            flat_warned = True
        total_pct += pct
        result.active_auras.append(aura_socket)

    # Apply reservation reduction (multiplicative, "X% reduced reservation")
    reduction = max(0.0, min(95.0, build.reservation_reduction_pct))
    effective_pct = total_pct * (1.0 - reduction / 100.0)
    result.total_reservation_pct = effective_pct
    result.free_mana_pct = max(0.0, 100.0 - effective_pct)

    pool = cost_pool(build)
    if pool == COST_POOL_MANA:
        if result.free_mana_pct < build.min_free_mana_pct:
            result.warnings.append(
                f"Aura set leaves only {result.free_mana_pct:.1f}% free mana "
                f"(need >= {build.min_free_mana_pct:.1f}%). "
                f"Drop an aura or add more reservation reduction."
            )
            result.valid = False
    else:
        # Mana cost is paid from life/ES, so all mana can be reserved.
        # Anything over 100% still can't run — auras silently fail in PoE.
        if effective_pct > 100.0:
            result.warnings.append(
                f"Reservation {effective_pct:.1f}% exceeds 100%; "
                f"some auras won't activate even with cost pool '{pool}'."
            )
            result.valid = False

    return result
