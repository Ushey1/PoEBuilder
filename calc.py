"""
Skill calc engine.

Layer 1: build_skill_instance_stats — port of PoB's
calcLib.buildSkillInstanceStats (src/Modules/CalcTools.lua line 183).
Takes a gem, level, quality, and character level; returns a dict of raw
stat-name -> numeric value. Handles all three statInterpolation methods.

Layer 2: stat_map.translate_stat — raw stat name -> list[Modifier].

Layer 3: collect_modifiers + compute_dps — walks the validated build's
skills, runs each through layers 1 and 2, then composes per the standard
PoE damage formula: `(base + added*dmgEff) × (1 + INC) × ∏(1 + MORE) × crit
× rate`.
"""
from __future__ import annotations
from dataclasses import dataclass, field

import pob_data
import stat_map
from stat_map import Modifier


def build_skill_instance_stats(
    skill: dict,
    gem_level: int,
    quality: int = 0,
    actor_level: int | None = None,
) -> dict[str, float]:
    """Return {raw_stat_name: value} for the skill at the given gem level.

    Direct port of PoB's calcLib.buildSkillInstanceStats. Quality stats,
    leveled stats (with statInterpolation methods 1/2/3), and constantStats
    are all summed into the same dict.

    actor_level defaults to the gem's level-required value at this gem level.
    """
    stats: dict[str, float] = {}

    # 1. Quality contributions: each unit of quality adds (multiplier) to its stat.
    #    PoB uses math.modf which keeps the integer part.
    quality_stats = skill.get("qualityStats")
    if quality > 0 and quality_stats:
        for entry in quality_stats:
            name, mult = entry[0], entry[1]
            stats[name] = stats.get(name, 0) + int(mult * quality)

    levels = skill["levels"]
    if gem_level < 1 or gem_level > len(levels):
        raise ValueError(
            f"Gem level {gem_level} out of range for "
            f"{skill.get('name')} (1..{len(levels)})"
        )
    level = levels[gem_level - 1]

    actor_level = actor_level or level.get("levelRequirement") or 1

    skill_stats = skill.get("stats") or []
    interp_list = level.get("statInterpolation") or []

    consts = pob_data.game_constants()
    base_eff_const = consts["SkillDamageBaseEffectiveness"]
    inc_eff_const = consts["SkillDamageIncrementalEffectiveness"]
    skill_base_eff = skill.get("baseEffectiveness") or 1.0
    skill_inc_eff = skill.get("incrementalEffectiveness") or 0.0

    available_effectiveness: float | None = None

    for i, stat_name in enumerate(skill_stats, start=1):
        # Positional access into level data uses string keys "1", "2", ...
        raw = level.get(str(i), 1)
        method = interp_list[i - 1] if i - 1 < len(interp_list) else 1

        if method == 3:
            # "Effectiveness" interpolation — gem damage scaling with actor level
            if available_effectiveness is None:
                available_effectiveness = (
                    (base_eff_const + inc_eff_const * (actor_level - 1))
                    * skill_base_eff
                    * (1.0 + skill_inc_eff) ** (actor_level - 1)
                )
            stat_value = round(available_effectiveness * raw)
        elif method == 2:
            # Linear interpolation between adjacent gem-level entries,
            # parameterized by levelRequirement.
            stat_value = _linear_interp(levels, gem_level, actor_level, str(i), raw)
        else:
            # method == 1 (or unspecified): static value at this level
            stat_value = raw

        stats[stat_name] = stats.get(stat_name, 0) + stat_value

    # 4. constantStats: fixed per-gem values that don't depend on level
    constant_stats = skill.get("constantStats")
    if constant_stats:
        for entry in constant_stats:
            name, value = entry[0], entry[1]
            stats[name] = stats.get(name, 0) + (value or 0)

    return stats


# ---------------------------------------------------------------------------
# Layer 2/3: gem -> modifiers, modifiers -> DPS
# ---------------------------------------------------------------------------

# PoE constants we don't yet pull from data
DEFAULT_CRIT_MULTI = 1.5  # base critical strike multiplier (PoE default 150%)


@dataclass
class ModifierPool:
    """Bundle of modifiers gathered from a build. Skill-specific filtering
    happens at composition time using each Modifier's `tags`.
    """
    base_damage: dict[str, float] = field(default_factory=dict)   # type -> min/max merged later
    added_damage: dict[str, float] = field(default_factory=dict)  # scales by dmgEff
    inc: list[Modifier] = field(default_factory=list)
    more: list[Modifier] = field(default_factory=list)
    # (source_name, raw_stat_name, value) — surfaced in reports so missing stat
    # translations are visible instead of silently dropped. See the safeguard
    # rationale in MEMORY: project_calc_architecture.
    unknown_stats: list[tuple[str, str, float]] = field(default_factory=list)
    extra_repeats: int = 0  # +N hits per cast (Spell Echo, Greater Spell Echo, ...)
    # Total post-reduction mana reservation (%). Populated from validate() so
    # composition rules that depend on reservation (e.g. Frostmage's
    # "% of max reserved mana as cold damage") can resolve at calc time.
    total_reservation_pct: float = 0.0
    # Notes accumulated during special-stat resolution (Frostmage etc.). The
    # DPS report surfaces these so the user sees what calc-time math happened.
    notes: list[str] = field(default_factory=list)


def _collect_into_pool(
    stats: dict[str, float], pool: ModifierPool, *, is_main: bool, source: str
):
    """Translate raw stats and merge into the pool.

    `is_main` controls whether BASE damage stats (like `spell_minimum_base_cold_damage`)
    are counted as the *skill's own* damage. Only the main skill contributes those.
    Aura/herald BASE damage describes their OWN secondary skill output, which is
    not added to the supported skill.

    `source` is the gem display name; recorded against any untranslated stat
    so reports can tell you which gem to fix translations for.
    """
    for raw_name, value in stats.items():
        mods = stat_map.translate_stat(raw_name, value)
        if not mods:
            if raw_name not in stat_map._IGNORED and value != 0:
                pool.unknown_stats.append((source, raw_name, value))
            continue
        for m in mods:
            if m.type == "BASE" and m.target.startswith("Added"):
                pool.added_damage[m.target] = pool.added_damage.get(m.target, 0) + m.value
            elif m.type == "BASE" and m.target.endswith(("Min", "Max")):
                if not is_main:
                    # Aura/support's own base damage — don't fold into main skill's base
                    continue
                pool.base_damage[m.target] = pool.base_damage.get(m.target, 0) + m.value
            elif m.type == "INC":
                pool.inc.append(m)
            elif m.type == "MORE":
                pool.more.append(m)
            else:
                pool.inc.append(m)
    # Spell Echo (and similar) emit `base_spell_repeat_count`. This isn't a
    # damage modifier but a hit-rate multiplier we need to track on the pool.
    repeats = stats.get("base_spell_repeat_count")
    if repeats:
        pool.extra_repeats += int(repeats)


def collect_modifiers(build) -> ModifierPool:
    """Walk main skill + valid supports + active auras; translate stats into
    a ModifierPool ready for composition.
    """
    from build import validate
    vr = validate(build)

    pool = ModifierPool()

    # Main skill
    main = pob_data.get_skill(build.main_skill.gem_name)
    if main is None:
        raise ValueError(f"Main skill not found: {build.main_skill.gem_name}")
    main_stats = build_skill_instance_stats(
        main, build.main_skill.level, build.main_skill.quality, actor_level=build.level
    )
    _collect_into_pool(main_stats, pool, is_main=True, source=main.get("name", build.main_skill.gem_name))

    # Supports (only those that passed tag-compat)
    for sup_socket in vr.valid_supports:
        sup = pob_data.get_skill(sup_socket.gem_name)
        if sup is None:
            continue
        sup_stats = build_skill_instance_stats(
            sup, sup_socket.level, sup_socket.quality, actor_level=build.level
        )
        _collect_into_pool(sup_stats, pool, is_main=False, source=sup.get("name", sup_socket.gem_name))

    # Auras (within reservation budget — but we still include their effects on the supported skill)
    for aura_socket in vr.active_auras:
        aura = pob_data.get_skill(aura_socket.gem_name)
        if aura is None:
            continue
        aura_stats = build_skill_instance_stats(
            aura, aura_socket.level, aura_socket.quality, actor_level=build.level
        )
        _collect_into_pool(aura_stats, pool, is_main=False, source=aura.get("name", aura_socket.gem_name))

    # Gear and passive modifiers are pre-built Modifier objects — drop them
    # straight into the right pool buckets without going through stat translation.
    # Prefer the per-slot `build.gear` dict (kept current when slots are edited)
    # over the flat `build.gear_mods` list (legacy path).
    if build.gear:
        import gear as _gear_module
        gear_mods = _gear_module.aggregate_mods(build.gear)
    else:
        gear_mods = list(build.gear_mods)
    for m in gear_mods + list(build.passive_mods):
        _push_modifier(pool, m)

    # Allocated passive tree nodes — translate each node's stat strings via
    # mod_parser. Unparsed lines are surfaced via pool.unknown_stats so we know
    # what to extend the parser for next.
    if build.allocated_nodes:
        import mod_parser  # local import to avoid pulling parser at calc import
        for node_id in build.allocated_nodes:
            node = pob_data.get_tree_node(node_id)
            if node is None:
                pool.unknown_stats.append(("Passives", f"<unknown node id {node_id}>", 0))
                continue
            source = f"Passive: {node.get('name', node_id)}"
            mods, unparsed = mod_parser.parse_node_stats(node.get("stats") or [])
            for m in mods:
                _push_modifier(pool, m)
            for line in unparsed:
                pool.unknown_stats.append((source, line, 0))

    # Cache validation's reservation total on the pool so calc-time rules can use it.
    pool.total_reservation_pct = vr.total_reservation_pct

    # Resolve stats that need character-state (mana, reservation). Runs here so
    # both compute_dps and compute_ehp see the same fully-resolved pool.
    _resolve_special_stats(build, pool)

    return pool


def _push_modifier(pool: ModifierPool, m: Modifier):
    """Insert an already-built Modifier directly into the pool (no stat translation)."""
    if m.type == "BASE" and m.target.startswith("Added"):
        pool.added_damage[m.target] = pool.added_damage.get(m.target, 0) + m.value
    elif m.type == "BASE" and m.target.endswith(("Min", "Max")):
        pool.base_damage[m.target] = pool.base_damage.get(m.target, 0) + m.value
    elif m.type == "INC":
        pool.inc.append(m)
    elif m.type == "MORE":
        pool.more.append(m)
    else:
        pool.inc.append(m)


# Raw stat names that need character-state (mana, reservation, life) to resolve.
# Each entry maps a stat name to a handler `(build, pool, value, notes) -> None`
# which mutates the pool in place. Keys are removed from `pool.unknown_stats`
# after resolution so they stop showing up as "untranslated".
def _resolve_special_stats(build, pool: ModifierPool):
    """Handle stat names whose effect depends on calc-time character state.

    Resolved stats are removed from `pool.unknown_stats` (so they stop being
    reported as untranslated) and a note is appended to `pool.notes`.
    """
    if not pool.unknown_stats:
        return

    # Pull max-mana once (only if anything below needs it).
    max_mana = None

    remaining: list[tuple[str, str, float]] = []
    for source, name, value in pool.unknown_stats:
        if name == "frostmage_gain_cold_damage_%_of_max_reserved_mana":
            # "X% of MAX reserved mana as added cold damage" — flat min=max addition.
            # Uses TOTAL reservation cached on the pool from validate().
            if max_mana is None:
                import character  # local import; avoids any import-order surprise
                max_mana = character.compute_pools(build, pool)["mana"]
            reserved_mana = max_mana * (pool.total_reservation_pct / 100.0)
            added = reserved_mana * (value / 100.0)
            pool.added_damage["AddedColdMin"] = pool.added_damage.get("AddedColdMin", 0) + added
            pool.added_damage["AddedColdMax"] = pool.added_damage.get("AddedColdMax", 0) + added
            pool.notes.append(
                f"{source}: max_mana={max_mana:.0f}, reservation={pool.total_reservation_pct:.0f}%"
                f" -> +{added:.0f} flat cold damage to spells"
            )
            continue
        remaining.append((source, name, value))
    pool.unknown_stats = remaining


def _mod_applies(mod: Modifier, skill_tags: set[str]) -> bool:
    """A scoped mod applies only if all of its tags are present on the skill.
    Empty tag-set means global.
    """
    if not mod.tags:
        return True
    return mod.tags.issubset(skill_tags)


def format_unknown_stats(unknowns: list[tuple[str, str, float]]) -> list[str]:
    """Group untranslated stats by source gem for clear, actionable reports.
    Empty list when there are no unknowns.
    """
    if not unknowns:
        return []
    by_source: dict[str, list[tuple[str, float]]] = {}
    for src, name, val in unknowns:
        by_source.setdefault(src, []).append((name, val))
    lines = [f"Untranslated stats ({len(unknowns)} across {len(by_source)} source(s)) - "
             f"add to stat_map.py to surface their effects:"]
    for src in sorted(by_source):
        lines.append(f"  {src}:")
        for name, val in by_source[src]:
            lines.append(f"    ? {name} = {val}")
    return lines


@dataclass
class DpsResult:
    base_min_total: float
    base_max_total: float
    inc_pct: float                 # summed INC, e.g., 30 means +30%
    more_factor: float             # product of (1 + MORE/100)
    crit_chance_pct: float
    effective_crit_multi: float    # incorporates crit chance into average hit multiplier
    cast_rate: float               # casts per second
    average_hit: float
    dps: float
    skipped_unknown_stats: list[tuple[str, str, float]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def report(self) -> str:
        L = [
            "DPS breakdown",
            f"  Base damage (skill):     {self.base_min_total:.0f} - {self.base_max_total:.0f}",
            f"  Increased multiplier:    +{self.inc_pct:.0f}%  -> x{1 + self.inc_pct / 100:.3f}",
            f"  More multiplier:         x{self.more_factor:.3f}",
            f"  Crit chance:             {self.crit_chance_pct:.2f}%   "
            f"(avg crit factor x{self.effective_crit_multi:.3f})",
            f"  Cast rate:               {self.cast_rate:.2f}/s",
            f"  " + ("-" * 35),
            f"  Average hit:             {self.average_hit:.0f}",
            f"  DPS:                     {self.dps:.0f}",
        ]
        if self.notes:
            L.append("  Notes:")
            for n in self.notes:
                L.append(f"    - {n}")
        unknown_lines = format_unknown_stats(self.skipped_unknown_stats)
        if unknown_lines:
            L.append("  " + unknown_lines[0])
            for ln in unknown_lines[1:]:
                L.append("  " + ln)
        return "\n".join(L)


def compute_dps(build) -> DpsResult:
    """End-to-end Frostbolt-style DPS using the gathered modifier pool.

    Assumptions baked in for MVP (see stat_map.py for the conditional mods
    these correspond to):
      - Frostbolt always pierces -> "if pierced enemy" MORE always applies
      - Enemy is chilled -> Hypothermia 29% MORE applies
      - Enemy has 0% baseline cold resistance (penetration ignored)
      - Inspiration is at max 5 charges -> elemental MORE ×5, crit INC ×5
      - Default crit multiplier 150%
    """
    main = pob_data.get_skill(build.main_skill.gem_name)
    if main is None:
        raise ValueError(f"Main skill not found: {build.main_skill.gem_name}")
    main_tags = pob_data.skill_tags(main)
    level_data = pob_data.gem_level_data(main, build.main_skill.level)
    dmg_eff = level_data.get("damageEffectiveness", 1.0) or 1.0
    cast_time = main.get("castTime", 1.0) or 1.0

    pool = collect_modifiers(build)
    # Pool already has notes from special-stat resolution (Frostmage etc.).
    notes = list(pool.notes)

    # 1. Base damage (combine skill base + added*dmgEff) per damage type
    def merge_min_max(prefix: str) -> tuple[float, float]:
        mn = pool.base_damage.get(f"{prefix}Min", 0)
        mx = pool.base_damage.get(f"{prefix}Max", 0)
        added_mn = pool.added_damage.get(f"Added{prefix}Min", 0)
        added_mx = pool.added_damage.get(f"Added{prefix}Max", 0)
        return mn + added_mn * dmg_eff, mx + added_mx * dmg_eff

    total_min = 0.0
    total_max = 0.0
    for dtype in ("Cold", "Fire", "Lightning", "Physical", "Chaos"):
        mn, mx = merge_min_max(dtype)
        if mn or mx:
            total_min += mn
            total_max += mx

    # 2. INC / MORE composition
    # Bake in "max righteous charges" assumption for Inspiration's stacking mods
    inspiration_charges = 5

    inc_sum = 0.0
    more_factor = 1.0
    crit_more_factor = 1.0
    crit_inc_sum = 0.0
    cast_more_factor = 1.0
    cast_inc_sum = 0.0

    for m in pool.inc + pool.more:
        # Special-case Inspiration's per-charge mods (multiply by charge count)
        eff_value = m.value
        if m.target.endswith("PerInspirationCharge"):
            eff_value = m.value * inspiration_charges
            if m.target == "ElementalDamagePerInspirationCharge":
                # Frostbolt is Cold (elemental). Treat as MORE damage.
                if "Cold" in main_tags or "Fire" in main_tags or "Lightning" in main_tags:
                    more_factor *= (1 + eff_value / 100)
                    notes.append(f"Inspiration max charges -> x{1 + eff_value/100:.3f} MORE elemental damage")
            elif m.target == "CritChancePerInspirationCharge":
                crit_inc_sum += eff_value
                notes.append(f"Inspiration max charges -> +{eff_value:.0f}% INC crit chance")
            continue

        if m.target == "Damage":
            if not _mod_applies(m, main_tags):
                continue
            if m.type == "INC":
                inc_sum += eff_value
            else:
                more_factor *= (1 + eff_value / 100)
        elif m.target == "CritChance":
            if m.type == "INC":
                crit_inc_sum += eff_value
            else:
                crit_more_factor *= (1 + eff_value / 100)
        elif m.target == "CastSpeed":
            if m.type == "INC":
                cast_inc_sum += eff_value
            else:
                cast_more_factor *= (1 + eff_value / 100)
        # Penetration, mana cost, etc. -> not used in this MVP pass

    # 3. Crit
    base_crit = level_data.get("critChance", 5) or 5
    crit_chance = base_crit * (1 + crit_inc_sum / 100) * crit_more_factor
    crit_chance = max(0.0, min(100.0, crit_chance))
    avg_crit_factor = 1 + (crit_chance / 100) * (DEFAULT_CRIT_MULTI - 1)

    # 4. Cast rate (with repeat multiplier: each cast fires 1+repeats hits)
    eff_cast_time = cast_time / ((1 + cast_inc_sum / 100) * cast_more_factor)
    casts_per_sec = 1.0 / eff_cast_time if eff_cast_time > 0 else 0
    hits_per_cast = 1 + pool.extra_repeats
    cast_rate = casts_per_sec * hits_per_cast
    if pool.extra_repeats:
        notes.append(f"Spell Echo / repeats: {hits_per_cast} hits per cast cycle "
                     f"({casts_per_sec:.2f} casts/s -> {cast_rate:.2f} hits/s)")

    # 5. Final composition
    inc_mult = 1 + inc_sum / 100
    avg_base = (total_min + total_max) / 2
    average_hit = avg_base * inc_mult * more_factor * avg_crit_factor
    dps = average_hit * cast_rate

    return DpsResult(
        base_min_total=total_min,
        base_max_total=total_max,
        inc_pct=inc_sum,
        more_factor=more_factor,
        crit_chance_pct=crit_chance,
        effective_crit_multi=avg_crit_factor,
        cast_rate=cast_rate,
        average_hit=average_hit,
        dps=dps,
        skipped_unknown_stats=pool.unknown_stats,
        notes=notes,
    )


def _linear_interp(levels: list, gem_level: int, actor_level: int, key: str, fallback) -> float:
    """Linear interpolation between adjacent level entries by levelRequirement."""
    if len(levels) <= 1:
        return round(fallback)
    cur = gem_level - 1
    nxt = min(cur + 1, len(levels) - 1)
    prev = nxt - 1
    prev_req = levels[prev].get("levelRequirement", actor_level - 1)
    next_req = levels[nxt].get("levelRequirement", actor_level + 1)
    prev_stat = levels[prev].get(key, 0)
    next_stat = levels[nxt].get(key, 0)
    if next_req == prev_req:
        return round(prev_stat)
    return round(prev_stat + (next_stat - prev_stat) * (actor_level - prev_req) / (next_req - prev_req))
