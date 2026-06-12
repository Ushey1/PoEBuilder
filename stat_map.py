"""
Raw stat-name -> Modifier translation.

PoB's `src/Data/SkillStatMap.lua` is the canonical source — it maps every
in-game stat name (e.g. `spell_minimum_base_cold_damage`) to one or more
internal-modifier function calls (e.g. `skill("ColdMin", nil)`).

For the MVP we mirror that shape for the ~25 stats our Frostbolt build
actually emits. Each entry is a list of (target, mod_type, scope_tags)
tuples. Pattern matching handles common shapes (`<scope>_damage_+%`,
`<scope>_damage_+%_final`). When we later want full PoB coverage, we
extract SkillStatMap.lua into the same structure and merge it in.

Mod targets used:
  - "ColdMin" / "ColdMax" / "FireMin" / ... -> skill's base damage by type
  - "AddedColdMin" / "AddedColdMax" / ...   -> flat-added damage (scales by dmgEff)
  - "Damage"            -> INC/MORE damage (scope filters which skills it touches)
  - "CritChance"        -> INC/MORE crit
  - "CastSpeed"         -> INC/MORE cast speed
  - "AttackSpeed"       -> INC/MORE attack speed
  - "ManaCost"          -> INC/MORE mana cost
  - "EnemyColdResist"   -> reduce enemy cold resistance (penetration analog)
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field

DAMAGE_TYPES = {"physical", "fire", "cold", "lightning", "chaos"}

# Scope-prefix -> tag set required on the skill for the mod to apply.
# Skill must have ALL listed tags for the mod to count.
SCOPE_TAGS = {
    "spell": {"Spell"},
    "attack": {"Attack"},
    "projectile": {"Projectile"},
    "melee": {"Melee"},
    "area": {"Area"},
    "cold": {"Cold"},
    "fire": {"Fire"},
    "lightning": {"Lightning"},
    "physical": {"Physical"},
    "chaos": {"Chaos"},
    # "elemental" is special: matches any of Cold/Fire/Lightning
    "elemental": {"Elemental"},  # sentinel; resolver expands
}


@dataclass(frozen=True)
class Modifier:
    target: str           # "Damage", "ColdMin", "CritChance", "CastSpeed", ...
    type: str             # "BASE" | "INC" | "MORE"
    value: float
    tags: frozenset[str] = field(default_factory=frozenset)


# ---------------------------------------------------------------------------
# Explicit overrides (modeled on SkillStatMap.lua entries we care about)
# ---------------------------------------------------------------------------

# Base damage from the skill itself (NOT scaled by damageEffectiveness).
# We use scope_tags={Cold} so the mod is tied to the cold-damage bucket.
_BASE_DAMAGE = {
    "spell_minimum_base_cold_damage":   ("ColdMin", "BASE"),
    "spell_maximum_base_cold_damage":   ("ColdMax", "BASE"),
    "spell_minimum_base_fire_damage":   ("FireMin", "BASE"),
    "spell_maximum_base_fire_damage":   ("FireMax", "BASE"),
    "spell_minimum_base_lightning_damage": ("LightningMin", "BASE"),
    "spell_maximum_base_lightning_damage": ("LightningMax", "BASE"),
    "spell_minimum_base_physical_damage": ("PhysicalMin", "BASE"),
    "spell_maximum_base_physical_damage": ("PhysicalMax", "BASE"),
    "spell_minimum_base_chaos_damage":  ("ChaosMin", "BASE"),
    "spell_maximum_base_chaos_damage":  ("ChaosMax", "BASE"),
    "secondary_minimum_base_cold_damage": ("ColdMin", "BASE"),
    "secondary_maximum_base_cold_damage": ("ColdMax", "BASE"),
}

# Added damage from supports/auras/heralds.
# These are stored separately so the composer can multiply them by the skill's
# damageEffectiveness before adding to base.
_ADDED_DAMAGE = {
    "global_minimum_added_cold_damage": ("AddedColdMin", "BASE"),
    "global_maximum_added_cold_damage": ("AddedColdMax", "BASE"),
    "global_minimum_added_fire_damage": ("AddedFireMin", "BASE"),
    "global_maximum_added_fire_damage": ("AddedFireMax", "BASE"),
    "global_minimum_added_lightning_damage": ("AddedLightningMin", "BASE"),
    "global_maximum_added_lightning_damage": ("AddedLightningMax", "BASE"),
    "global_minimum_added_physical_damage": ("AddedPhysicalMin", "BASE"),
    "global_maximum_added_physical_damage": ("AddedPhysicalMax", "BASE"),
    "global_minimum_added_chaos_damage": ("AddedChaosMin", "BASE"),
    "global_maximum_added_chaos_damage": ("AddedChaosMax", "BASE"),
    # spell-scoped added flat (Herald of Ice etc.)
    "spell_minimum_added_cold_damage":  ("AddedColdMin", "BASE", {"Spell"}),
    "spell_maximum_added_cold_damage":  ("AddedColdMax", "BASE", {"Spell"}),
    "spell_minimum_added_fire_damage":  ("AddedFireMin", "BASE", {"Spell"}),
    "spell_maximum_added_fire_damage":  ("AddedFireMax", "BASE", {"Spell"}),
    "spell_minimum_added_lightning_damage":  ("AddedLightningMin", "BASE", {"Spell"}),
    "spell_maximum_added_lightning_damage":  ("AddedLightningMax", "BASE", {"Spell"}),
}

# Support-specific stat names (PoB names them with the support prefix).
# Each is treated as an unconditional MORE for MVP (we ignore the "if chilled"
# / "per charge" conditions — see calc.py composition rules for assumptions).
_SUPPORT_FINAL_MORES = {
    "support_controlled_destruction_spell_damage_+%_final": ("Damage", "MORE", {"Spell"}),
    "support_controlled_destruction_critical_strike_chance_+%_final": ("CritChance", "MORE", set()),
    "support_multicast_cast_speed_+%_final": ("CastSpeed", "MORE", {"Spell"}),
    "support_echo_damage_+%_final": ("Damage", "MORE", {"Spell"}),
    # MVP assumption: enemy is chilled (Frostbolt chills, Hypothermia self-chill)
    "support_hypothermia_damage_+%_vs_chilled_enemies_final": ("Damage", "MORE", set()),
    # Hypothermia's DoT line — irrelevant for hit DPS, ignored
    "support_hypothermia_cold_damage_over_time_+%_final": None,
    "support_inspiration_mana_cost_+%_final": ("ManaCost", "MORE", set()),
    # MVP assumption: max righteous charges (5) → 5x multiplier baked in by composer
    "elemental_damage_+%_final_per_righteous_charge": ("ElementalDamagePerInspirationCharge", "MORE", set()),
    "critical_strike_chance_+%_per_righteous_charge": ("CritChancePerInspirationCharge", "INC", set()),
    # Arcane Surge: MVP assumes the buff is up. In reality the buff procs after
    # 400 mana spent and lasts ~4s — for sustained casting it's effectively always on.
    "support_arcane_surge_spell_damage_+%_final_while_you_have_arcane_surge": ("Damage", "MORE", {"Spell"}),
    "support_arcane_surge_cast_speed_+%": ("CastSpeed", "INC", {"Spell"}),
    # Greater Multiple Projectiles: -26% MORE damage in exchange for +4 projectiles.
    # Modeling per-hit damage; the extra projectiles only help if multiple hit one target.
    "support_multiple_projectile_damage_+%_final": ("Damage", "MORE", {"Projectile"}),
    # Zealotry: aura that grants spell damage + spell crit chance.
    "spell_damage_aura_spell_damage_+%_final": ("Damage", "MORE", {"Spell"}),
    "spell_critical_strike_chance_+%": ("CritChance", "INC", {"Spell"}),
}

# Penetration / resistance reduction.
_PENETRATION = {
    "base_reduce_enemy_cold_resistance_%": ("EnemyColdResist", "BASE", set()),
    "base_reduce_enemy_fire_resistance_%": ("EnemyFireResist", "BASE", set()),
    "base_reduce_enemy_lightning_resistance_%": ("EnemyLightningResist", "BASE", set()),
    "cold_damage_does_not_check_enemy_resistance": None,  # boolean, MVP handles via crit pipeline if needed
}

# QoL stats — movement speed, ailment immunities, etc.
# Targets are consumed by qol.compute_qol(). Each *immunity* target is BASE
# with value 1 meaning fully immune. Multiple sources stack (max == 1).
_QOL = {
    # Movement speed (inc / more)
    "base_movement_velocity_+%":    ("MovementSpeed", "INC", set()),
    "movement_velocity_+%":         ("MovementSpeed", "INC", set()),
    # Ailment immunities (boolean flags)
    "cannot_be_frozen":   ("ImmuneToFreeze", "BASE", set()),
    "cannot_be_chilled":  ("ImmuneToChill", "BASE", set()),
    "cannot_be_shocked":  ("ImmuneToShock", "BASE", set()),
    "cannot_be_ignited":  ("ImmuneToIgnite", "BASE", set()),
    "cannot_be_stunned":  ("ImmuneToStun", "BASE", set()),
    "cannot_be_poisoned": ("ImmuneToPoison", "BASE", set()),
    "cannot_bleed":       ("ImmuneToBleed", "BASE", set()),
    "cannot_be_cursed":   ("ImmuneToCurse", "BASE", set()),
    # `immune_to_status_ailments` (Purity of Elements) = immune to all elemental
    # ailments: Freeze, Chill, Shock, Ignite (+ Scorch/Brittle/Sapped in newer
    # patches; not modeled separately for MVP). We emit one composite target
    # and let qol.py expand it across the four base ailments.
    "immune_to_status_ailments": ("ImmuneToElementalAilments", "BASE", set()),
}


# Defensive stats — pools, resistances, armour, evasion.
# Player-facing aura/gear stats. Targets are consumed by defense.compute_ehp().
_DEFENSIVE = {
    # Pools
    "base_maximum_life":          ("MaximumLife", "BASE", set()),
    "base_maximum_mana":          ("MaximumMana", "BASE", set()),
    "base_maximum_energy_shield": ("MaximumEnergyShield", "BASE", set()),
    "maximum_life_+%":            ("MaximumLife", "INC", set()),
    "maximum_mana_+%":            ("MaximumMana", "INC", set()),
    "maximum_energy_shield_+%":   ("MaximumEnergyShield", "INC", set()),
    # Armour / Evasion ratings (PoE has multiple stat names for the same thing)
    "base_armour":                          ("Armour", "BASE", set()),
    "base_physical_damage_reduction_rating": ("Armour", "BASE", set()),
    "base_evasion":                         ("Evasion", "BASE", set()),
    "base_evasion_rating":                  ("Evasion", "BASE", set()),
    "armour_+%":                            ("Armour", "INC", set()),
    "physical_damage_reduction_rating_+%":  ("Armour", "INC", set()),
    "evasion_rating_+%":                    ("Evasion", "INC", set()),
    # Aura-specific MORE multipliers
    "determination_aura_armour_+%_final":   ("Armour", "MORE", set()),
    "grace_aura_evasion_rating_+%_final":   ("Evasion", "MORE", set()),
    # Resistances (additive to your resist totals before cap)
    "base_fire_damage_resistance_%":      ("FireResist", "BASE", set()),
    "base_cold_damage_resistance_%":      ("ColdResist", "BASE", set()),
    "base_lightning_damage_resistance_%": ("LightningResist", "BASE", set()),
    "base_chaos_damage_resistance_%":     ("ChaosResist", "BASE", set()),
    "base_resist_all_elements_%":         ("AllElementalResist", "BASE", set()),
    # Other defensive niceties Discipline emits — keep but not used in EHP MVP
    "energy_shield_recharge_rate_+%": ("ESRecharge", "INC", set()),
}

# Stats we deliberately ignore for hit-DPS MVP (AoE, defensive flags, etc.).
_IGNORED = {
    "base_is_projectile",
    "always_pierce",
    "base_deal_no_damage",
    "base_skill_area_of_effect_+%",
    "active_skill_base_radius_+",
    "attack_minimum_added_cold_damage",
    "attack_maximum_added_cold_damage",
    "attack_minimum_added_fire_damage",
    "attack_maximum_added_fire_damage",
    "attack_minimum_added_lightning_damage",
    "attack_maximum_added_lightning_damage",
    "attack_minimum_added_physical_damage",
    "attack_maximum_added_physical_damage",
    "is_area_damage",
    "base_skill_show_average_damage_instead_of_dps",
    "display_skill_deals_secondary_damage",
    "damage_cannot_be_reflected",
    "skill_can_add_multiple_charges_per_action",
    "skill_override_pvp_scaling_time_ms",
    "active_skill_base_area_of_effect_radius",
    "additional_chance_to_freeze_chilled_enemies_%",
    "support_hypothermia_cold_damage_over_time_+%_final",   # DoT — not part of hit DPS
    "base_spell_repeat_count",   # echo's extra cast; doesn't directly multiply DPS
    "lose_all_righteous_charges_on_mana_use_threshold",
    "gain_righteous_charge_on_mana_spent_%",
    "physical_damage_%_to_add_as_cold",  # conversion — needs phys damage source; FB has none
    # Informational / non-DPS / phase-2 candidates from the Frostbolt template
    "support_arcane_surge_gain_buff_on_mana_use_threshold",   # proc threshold (informational)
    "support_arcane_surge_base_duration_ms",                  # buff duration (informational)
    "support_arcane_surge_mana_regeneration_rate_+%",         # sustain, not DPS
    "number_of_additional_projectiles",                       # multi-target only, not per-hit DPS
    "terrain_arrow_attachment_chance_reduction_+%",           # projectile mechanic, not DPS
    # immune_to_status_ailments moved to _QOL — handled by qol.py now
    "base_life_regeneration_rate_per_minute",                 # recovery, not hit DPS/EHP
    "frostmage_cost_equals_%_reserved_mana",                  # cost adjustment, not DPS
    "create_consecrated_ground_on_hit_%_vs_rare_or_unique_enemy",  # situational
    "automation_behaviour",                                   # trigger flag, informational
    # Malevolence: DoT-only MORE multiplier. Frostbolt is hit-based, so this
    # contributes 0 in our current model (no DoT DPS pipeline yet).
    "delirium_aura_damage_over_time_+%_final",
    "delirium_skill_effect_duration_+%",
    # Pride: enemy debuff (increases physical damage taken). Requires enemy-state
    # modeling we don't do; ignore until we add taken-multiplier support.
    "physical_damage_aura_nearby_enemies_physical_damage_taken_+%",
    "physical_damage_aura_nearby_enemies_physical_damage_taken_+%_max",
    # HeraldOfThunder timing/flag stats — irrelevant to hit DPS for the buffed skill.
    "never_shock",
    "display_herald_of_thunder_storm",
    "herald_of_thunder_pvp_scaling_time_uses_bolt_frequency",
    "base_skill_effect_duration",
    "herald_of_thunder_bolt_base_frequency",
    # Frostmage's damage stat is NOT ignored — it's handled as a special case in
    # calc.compute_dps because it needs max_mana and total_reservation at calc time.
}


# ---------------------------------------------------------------------------
# Pattern fallbacks for stats that follow regular shapes
# ---------------------------------------------------------------------------

# <scope>_damage_+%               → INC Damage, scope-tagged
_RE_INC = re.compile(r"^([a-z]+)_damage_\+%$")
# <scope>_damage_+%_final         → MORE Damage, scope-tagged
_RE_MORE = re.compile(r"^([a-z]+)_damage_\+%_final$")
# <scope>_damage_+%_final_if_*    → MORE Damage, condition ignored (always-apply)
_RE_MORE_COND = re.compile(r"^([a-z]+)_damage_\+%_final_if_\w+$")
# critical_strike_chance_+%
_RE_CRIT_INC = re.compile(r"^critical_strike_chance_\+%$")
# critical_strike_chance_+%_final
_RE_CRIT_MORE = re.compile(r"^critical_strike_chance_\+%_final$")
# <scope>_cast_speed_+% / _final
_RE_CAST_INC = re.compile(r"^([a-z]*)_?cast_speed_\+%$")
_RE_CAST_MORE = re.compile(r"^([a-z]*)_?cast_speed_\+%_final$")


def _scope_to_tags(scope: str) -> frozenset[str]:
    """Map a stat-name prefix to the tag set required on the skill.
    Empty string or 'base' means global (no scope filter).
    """
    if not scope or scope in ("base", "global"):
        return frozenset()
    if scope in SCOPE_TAGS:
        return frozenset(SCOPE_TAGS[scope])
    # Treat unknown prefixes as informational (no scope filter).
    return frozenset()


def translate_stat(stat_name: str, value: float) -> list[Modifier]:
    """Return zero or more Modifier objects derived from a raw stat name + value."""
    if value == 0:
        # Stats reporting zero have no effect; cheaper to drop here than to chase later.
        return []

    if stat_name in _IGNORED:
        return []

    # 1. Explicit overrides
    for table in (_BASE_DAMAGE, _ADDED_DAMAGE, _SUPPORT_FINAL_MORES, _PENETRATION, _DEFENSIVE, _QOL):
        entry = table.get(stat_name)
        if entry is None:
            continue
        if entry is False or entry is None:
            return []
        if isinstance(entry, tuple):
            if len(entry) == 2:
                target, mtype = entry
                tags = frozenset()
            else:
                target, mtype, tags = entry
                tags = frozenset(tags)
            return [Modifier(target=target, type=mtype, value=value, tags=tags)]

    # 2. Pattern matches
    if (m := _RE_CRIT_MORE.match(stat_name)):
        return [Modifier("CritChance", "MORE", value)]
    if (m := _RE_CRIT_INC.match(stat_name)):
        return [Modifier("CritChance", "INC", value)]
    if (m := _RE_MORE_COND.match(stat_name)):
        return [Modifier("Damage", "MORE", value, _scope_to_tags(m.group(1)))]
    if (m := _RE_MORE.match(stat_name)):
        return [Modifier("Damage", "MORE", value, _scope_to_tags(m.group(1)))]
    if (m := _RE_INC.match(stat_name)):
        return [Modifier("Damage", "INC", value, _scope_to_tags(m.group(1)))]
    if (m := _RE_CAST_MORE.match(stat_name)):
        return [Modifier("CastSpeed", "MORE", value, _scope_to_tags(m.group(1)))]
    if (m := _RE_CAST_INC.match(stat_name)):
        return [Modifier("CastSpeed", "INC", value, _scope_to_tags(m.group(1)))]

    # 3. Unknown
    return []
