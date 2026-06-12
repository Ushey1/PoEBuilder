"""End-to-end Frostbolt DPS smoke test."""
from build import Build, GemSocket, Modifier, validate
import calc


def divider(t): print(f"\n{'='*60}\n {t}\n{'='*60}")


# A reasonable Frostbolt-Ele gear/passive mod set (rough mid-tier endgame).
# Mods are kept simple — no conditional flags, every mod is always-on.
SAMPLE_GEAR_MODS = [
    Modifier("Damage", "INC", 100, frozenset({"Cold"})),     # +100% inc cold across rare gear
    Modifier("Damage", "INC", 60, frozenset({"Spell"})),      # +60% inc spell damage
    Modifier("Damage", "INC", 40, frozenset({"Cold"})),       # cold-damage suffixes
    Modifier("CastSpeed", "INC", 20, frozenset({"Spell"})),   # +20% cast speed on items
    Modifier("CritChance", "INC", 250, frozenset()),          # crit chance from items/diamond ring etc.
]

SAMPLE_PASSIVE_MODS = [
    Modifier("Damage", "INC", 150, frozenset({"Cold"})),       # cold passive nodes
    Modifier("Damage", "INC", 80, frozenset({"Spell"})),       # spell damage notables
    Modifier("Damage", "INC", 30, frozenset({"Projectile"})),  # projectile passive nodes
    Modifier("CritChance", "INC", 120, frozenset()),           # crit cluster
    Modifier("Damage", "MORE", 12, frozenset({"Cold"})),       # Frostweaver / similar notable
]


build = Build(
    char_class="Witch",
    ascendancy="Elementalist",
    level=90,
    main_skill=GemSocket("FrostBolt", level=20, quality=20),
    supports=[
        GemSocket("SupportSpellEcho", level=20, quality=20),
        GemSocket("SupportControlledDestruction", level=20, quality=20),
        GemSocket("SupportHypothermia", level=20, quality=20),
        GemSocket("SupportAddedColdDamage", level=20, quality=20),
        GemSocket("SupportInspiration", level=20, quality=20),
    ],
    auras=[
        GemSocket("Discipline", level=20),
        GemSocket("HeraldOfIce", level=20, quality=20),
    ],
    gear_mods=SAMPLE_GEAR_MODS,
    passive_mods=SAMPLE_PASSIVE_MODS,
)

divider("Validation")
print(validate(build).report())

divider("DPS")
print(calc.compute_dps(build).report())

# Counterfactual: drop Hatred-style aura that does nothing for cold spells —
# Hatred adds phys-as-cold; Frostbolt has no phys, so it should *not* change DPS.
build_with_hatred = Build(
    char_class="Witch",
    ascendancy="Elementalist",
    level=90,
    main_skill=GemSocket("FrostBolt", level=20, quality=20),
    supports=build.supports,
    auras=[
        GemSocket("Hatred", level=20),   # 50% reserve; should not help DPS
        GemSocket("HeraldOfIce", level=20, quality=20),
    ],
)
divider("Same build but Hatred instead of Discipline (Hatred shouldn't help DPS)")
print(validate(build_with_hatred).report())
print()
print(calc.compute_dps(build_with_hatred).report())
