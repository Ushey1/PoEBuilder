"""Quick exercise of the build model + validation."""
from build import Build, GemSocket, validate


def divider(title):
    print(f"\n{'=' * 60}\n {title}\n{'=' * 60}")


# --- Case 1: a sensible Frostbolt build ---
build_a = Build(
    char_class="Witch",
    ascendancy="Elementalist",
    level=90,
    main_skill=GemSocket("FrostBolt", level=20, quality=20),
    supports=[
        GemSocket("SupportSpellEcho", level=20),
        GemSocket("SupportControlledDestruction", level=20),
        GemSocket("SupportHypothermia", level=20),
        GemSocket("SupportAddedColdDamage", level=20),
        GemSocket("SupportColdPenetration", level=20),
    ],
    auras=[
        GemSocket("Discipline", level=20),     # 35% reserve
        GemSocket("HeraldOfIce", level=20),    # 25% reserve
    ],
    reservation_reduction_pct=0,
)

divider("Case A: reasonable Frostbolt build")
print(validate(build_a).report())


# --- Case 2: shove in a support that doesn't fit (attack-only?) ---
build_b = Build(
    char_class="Witch",
    ascendancy="Elementalist",
    level=90,
    main_skill=GemSocket("FrostBolt", level=20),
    supports=[
        GemSocket("SupportMeleePhysicalDamage", level=20),   # Attack/Melee-only — should reject
        GemSocket("SupportSpellEcho", level=20),
        GemSocket("SupportMinionDamage", level=20),    # Needs Minion-summoning skill
    ],
    auras=[],
)

divider("Case B: incompatible supports (should be rejected)")
print(validate(build_b).report())


# --- Case 3: over-reserved (Discipline + Hatred + Determination, no reduction) ---
build_c = Build(
    char_class="Witch",
    ascendancy="Elementalist",
    level=90,
    main_skill=GemSocket("FrostBolt", level=20),
    supports=[],
    auras=[
        GemSocket("Hatred", level=20),         # 50%
        GemSocket("Determination", level=20),  # 50%
        GemSocket("Discipline", level=20),     # 35%
    ],
    reservation_reduction_pct=0,
    min_free_mana_pct=35,
)

divider("Case C: over-reserved auras (should fail budget)")
print(validate(build_c).report())


# --- Case 4: same as C but with -30% reservation reduction ---
build_d = Build(
    char_class="Witch",
    ascendancy="Elementalist",
    level=90,
    main_skill=GemSocket("FrostBolt", level=20),
    supports=[],
    auras=[
        GemSocket("Hatred", level=20),
        GemSocket("Determination", level=20),
        GemSocket("Discipline", level=20),
    ],
    reservation_reduction_pct=30,
    min_free_mana_pct=35,
)

divider("Case D: same auras + 30% reservation reduction")
print(validate(build_d).report())
