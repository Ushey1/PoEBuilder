"""End-to-end EHP smoke test."""
from build import Build, GemSocket, Modifier, validate
import calc
import defense


def divider(t): print(f"\n{'='*60}\n {t}\n{'='*60}")


# Sample mid-tier endgame gear/passive defensive mods.
DEFENSIVE_GEAR_MODS = [
    Modifier("MaximumLife", "BASE", 350, frozenset()),   # ~70 life per body/helm/boots/gloves/belt slot
    Modifier("MaximumEnergyShield", "BASE", 600, frozenset()),
    Modifier("MaximumLife", "INC", 40, frozenset()),     # rolled inc life suffixes
    Modifier("FireResist", "BASE", 130, frozenset()),    # spread across gear
    Modifier("ColdResist", "BASE", 130, frozenset()),
    Modifier("LightningResist", "BASE", 135, frozenset()),
    Modifier("ChaosResist", "BASE", 30, frozenset()),
    Modifier("Armour", "BASE", 800, frozenset()),        # rolled armour bases
    Modifier("Evasion", "BASE", 1500, frozenset()),
]

DEFENSIVE_PASSIVE_MODS = [
    Modifier("MaximumLife", "INC", 50, frozenset()),     # life passive cluster
    Modifier("MaximumEnergyShield", "INC", 80, frozenset()),
    Modifier("ColdResist", "BASE", 12, frozenset()),     # generic resist nodes
    Modifier("FireResist", "BASE", 12, frozenset()),
    Modifier("Armour", "INC", 30, frozenset()),
    Modifier("Evasion", "INC", 30, frozenset()),
]

# Offensive mods reused from the earlier test (so this build can do real damage too)
OFFENSIVE_GEAR_MODS = [
    Modifier("Damage", "INC", 100, frozenset({"Cold"})),
    Modifier("Damage", "INC", 60, frozenset({"Spell"})),
    Modifier("Damage", "INC", 40, frozenset({"Cold"})),
    Modifier("CastSpeed", "INC", 20, frozenset({"Spell"})),
    Modifier("CritChance", "INC", 250, frozenset()),
]
OFFENSIVE_PASSIVE_MODS = [
    Modifier("Damage", "INC", 150, frozenset({"Cold"})),
    Modifier("Damage", "INC", 80, frozenset({"Spell"})),
    Modifier("Damage", "INC", 30, frozenset({"Projectile"})),
    Modifier("CritChance", "INC", 120, frozenset()),
    Modifier("Damage", "MORE", 12, frozenset({"Cold"})),
]


# --- Build A: Discipline + Herald of Ice (CI-style ES setup) ---
build_disc = Build(
    char_class="Witch",
    ascendancy="Elementalist",
    level=90,
    strength=50,
    dexterity=50,
    intelligence=250,
    main_skill=GemSocket("FrostBolt", level=20, quality=20),
    supports=[
        GemSocket("SupportSpellEcho", level=20, quality=20),
        GemSocket("SupportControlledDestruction", level=20, quality=20),
        GemSocket("SupportHypothermia", level=20, quality=20),
        GemSocket("SupportAddedColdDamage", level=20, quality=20),
        GemSocket("SupportInspiration", level=20, quality=20),
    ],
    auras=[
        GemSocket("Discipline", level=20),       # +ES
        GemSocket("HeraldOfIce", level=20, quality=20),
    ],
    gear_mods=OFFENSIVE_GEAR_MODS + DEFENSIVE_GEAR_MODS,
    passive_mods=OFFENSIVE_PASSIVE_MODS + DEFENSIVE_PASSIVE_MODS,
)

divider("Build A: Discipline + HoI (ES-focused)")
print(validate(build_disc).report())
print()
print(calc.compute_dps(build_disc).report())
print()
print(defense.compute_ehp(build_disc).report())


# --- Build B: Determination + Grace (armour/evasion-focused) - currently no Det/Grace data emitted via aura stats so this is more conceptual ---
# (Determination/Grace data extraction reveals what stats they emit; we'll see)
build_det = Build(
    char_class="Witch",
    ascendancy="Elementalist",
    level=90,
    strength=50,
    dexterity=200,
    intelligence=100,
    main_skill=GemSocket("FrostBolt", level=20, quality=20),
    supports=build_disc.supports,
    auras=[
        GemSocket("Determination", level=20),  # armour
        GemSocket("Grace", level=20),          # evasion
    ],
    gear_mods=OFFENSIVE_GEAR_MODS + DEFENSIVE_GEAR_MODS,
    passive_mods=OFFENSIVE_PASSIVE_MODS + DEFENSIVE_PASSIVE_MODS,
)

divider("Build B: Determination + Grace (armour/evasion)")
print(validate(build_det).report())
print()
print(defense.compute_ehp(build_det).report())


# --- Build C: Discipline + Vitality (Vitality has an untranslated stat — should surface in the report) ---
build_vit = Build(
    char_class="Witch",
    ascendancy="Elementalist",
    level=90,
    strength=50,
    dexterity=50,
    intelligence=250,
    main_skill=GemSocket("FrostBolt", level=20, quality=20),
    supports=build_disc.supports,
    auras=[
        GemSocket("Discipline", level=20),
        GemSocket("Vitality", level=20),   # emits base_life_regeneration_rate_per_minute — untranslated
    ],
    gear_mods=OFFENSIVE_GEAR_MODS + DEFENSIVE_GEAR_MODS,
    passive_mods=OFFENSIVE_PASSIVE_MODS + DEFENSIVE_PASSIVE_MODS,
)

divider("Build C: Discipline + Vitality (should surface Vitality's untranslated stat)")
print(validate(build_vit).report())
print()
print(defense.compute_ehp(build_vit).report())
