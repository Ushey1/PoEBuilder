"""
Stat-text parser. Converts human-readable PoE stat lines into Modifier objects.

PoB has hundreds of regex patterns in `Modules/ModParser.lua` to cover every
phrasing the game uses. We can't replicate all of that in one go, so:

  - Cover the most common patterns (INC / MORE / BASE / flat-added-damage)
  - Conditional clauses ("while X", "if Y") are stripped for MVP — the mod
    is applied unconditionally. This will over-state mods that only apply
    in specific situations, fix-it-later.
  - Triggered effects ("on hit", "on kill", "chance to") aren't modeled —
    return empty list and surface the untranslated line.

Same parser handles passive tree node stats AND unique item stats — both
ship as human-readable text.
"""
from __future__ import annotations
import re
from stat_map import Modifier


# ---------------------------------------------------------------------------
# Stat-name -> Modifier target translation
# ---------------------------------------------------------------------------
# Map the display phrasing (lowercase) to (target_name, scope_tags).
# tags=frozenset() means global. tags={"Cold"} means the mod scopes to Cold damage etc.
#
# Order matters for the longest-match: we try longer keys first to avoid
# "cold damage" matching "damage" before "cold damage".
STAT_NAME_MAP: dict[str, tuple[str, frozenset[str]]] = {
    # Pools
    "maximum life":                 ("MaximumLife", frozenset()),
    "maximum mana":                 ("MaximumMana", frozenset()),
    "maximum energy shield":        ("MaximumEnergyShield", frozenset()),
    "life":                         ("MaximumLife", frozenset()),
    "mana":                         ("MaximumMana", frozenset()),
    "energy shield":                ("MaximumEnergyShield", frozenset()),
    # Resistances
    "fire resistance":              ("FireResist", frozenset()),
    "cold resistance":              ("ColdResist", frozenset()),
    "lightning resistance":         ("LightningResist", frozenset()),
    "chaos resistance":             ("ChaosResist", frozenset()),
    "elemental resistances":        ("AllElementalResist", frozenset()),
    "all elemental resistances":    ("AllElementalResist", frozenset()),
    # Defenses
    "armour":                       ("Armour", frozenset()),
    "evasion rating":               ("Evasion", frozenset()),
    "evasion":                      ("Evasion", frozenset()),
    # Movement
    "movement speed":               ("MovementSpeed", frozenset()),
    # Crit
    "critical strike chance":       ("CritChance", frozenset()),
    "critical strike multiplier":   ("CritMulti", frozenset()),
    "global critical strike chance":     ("CritChance", frozenset()),
    "global critical strike multiplier": ("CritMulti", frozenset()),
    "spell critical strike chance":      ("CritChance", frozenset({"Spell"})),
    "spell critical strike multiplier":  ("CritMulti", frozenset({"Spell"})),
    "attack critical strike chance":     ("CritChance", frozenset({"Attack"})),
    # Cast / attack speed
    "cast speed":                   ("CastSpeed", frozenset()),
    "attack speed":                 ("AttackSpeed", frozenset()),
    # Damage (scoped variants)
    "damage":                       ("Damage", frozenset()),
    "spell damage":                 ("Damage", frozenset({"Spell"})),
    "attack damage":                ("Damage", frozenset({"Attack"})),
    "projectile damage":            ("Damage", frozenset({"Projectile"})),
    "melee damage":                 ("Damage", frozenset({"Melee"})),
    "area damage":                  ("Damage", frozenset({"Area"})),
    "cold damage":                  ("Damage", frozenset({"Cold"})),
    "fire damage":                  ("Damage", frozenset({"Fire"})),
    "lightning damage":             ("Damage", frozenset({"Lightning"})),
    "physical damage":              ("Damage", frozenset({"Physical"})),
    "chaos damage":                 ("Damage", frozenset({"Chaos"})),
    "elemental damage":             ("Damage", frozenset({"Elemental"})),
    "elemental damage with attack skills": ("Damage", frozenset({"Elemental", "Attack"})),
    "cold damage with attack skills":      ("Damage", frozenset({"Cold", "Attack"})),
    "fire damage with attack skills":      ("Damage", frozenset({"Fire", "Attack"})),
    "lightning damage with attack skills": ("Damage", frozenset({"Lightning", "Attack"})),
    # Attributes
    "strength":                     ("Strength", frozenset()),
    "dexterity":                    ("Dexterity", frozenset()),
    "intelligence":                 ("Intelligence", frozenset()),
    "all attributes":               ("AllAttributes", frozenset()),
    # Sustain / regen
    "mana regeneration rate":       ("ManaRegen", frozenset()),
    "life regeneration rate":       ("LifeRegen", frozenset()),
    # Area / accuracy / other generic
    "area of effect":               ("AreaOfEffect", frozenset()),
    "accuracy rating":              ("Accuracy", frozenset()),
    "accuracy":                     ("Accuracy", frozenset()),
    "global accuracy rating":       ("Accuracy", frozenset()),
    # Resistance caps ("maximum" cold resistance etc.)
    "maximum cold resistance":      ("ColdResistMax", frozenset()),
    "maximum fire resistance":      ("FireResistMax", frozenset()),
    "maximum lightning resistance": ("LightningResistMax", frozenset()),
    "maximum chaos resistance":     ("ChaosResistMax", frozenset()),
    "all maximum elemental resistances": ("AllElementalResistMax", frozenset()),
    # Charges
    "maximum power charges":        ("MaxPowerCharges", frozenset()),
    "maximum frenzy charges":       ("MaxFrenzyCharges", frozenset()),
    "maximum endurance charges":    ("MaxEnduranceCharges", frozenset()),
    "maximum charges":              ("MaxAllCharges", frozenset()),
    # Block / suppression
    "chance to block attack damage": ("BlockChance", frozenset()),
    "chance to block spell damage":  ("SpellBlockChance", frozenset()),
    "chance to block":               ("BlockChance", frozenset()),
    "chance to suppress spell damage": ("SpellSuppression", frozenset()),
    "block chance":                  ("BlockChance", frozenset()),
    # Defences scoped to gear slot
    "defences from equipped shield": ("ShieldDefences", frozenset()),
    "defences":                      ("Defences", frozenset()),  # generic, multiplies armour+evasion+es
    # Recovery / sustain
    "energy shield recharge rate":   ("ESRechargeRate", frozenset()),
    "stun and block recovery":       ("StunBlockRecovery", frozenset()),
    "stun recovery":                 ("StunRecovery", frozenset()),
    "block recovery":                ("BlockRecovery", frozenset()),
    "block and stun recovery":       ("StunBlockRecovery", frozenset()),  # alt phrasing
    # Stun mechanics
    "stun duration":                  ("StunDuration", frozenset()),
    "stun duration on enemies":       ("EnemyStunDuration", frozenset()),
    "enemy stun threshold":           ("EnemyStunThreshold", frozenset()),
    # Item rarity / quantity
    "rarity of items found":          ("ItemRarity", frozenset()),
    "quantity of items found":        ("ItemQuantity", frozenset()),
    # Mana cost / leech
    "mana cost":                      ("ManaCost", frozenset()),
    "mana cost of skills":            ("ManaCost", frozenset()),
    # Curse effect
    "effect of your curses":          ("CurseEffect", frozenset()),
    "curse effect":                   ("CurseEffect", frozenset()),
    # Aura effect
    "effect of non-curse auras from your skills": ("AuraEffect", frozenset()),
    "effect of auras on you":         ("AurasOnYou", frozenset()),
    "aura effect":                    ("AuraEffect", frozenset()),
}


# Pre-sort keys by length descending for greedy longest-match.
_SORTED_STAT_KEYS = sorted(STAT_NAME_MAP.keys(), key=len, reverse=True)


def _lookup_stat(text: str) -> tuple[str, frozenset[str]] | None:
    """Find the best-matching stat name in `text` (already-lowercased and trimmed).
    Returns (target, tags) or None if nothing recognized.
    """
    text = text.strip()
    if text in STAT_NAME_MAP:
        return STAT_NAME_MAP[text]
    # Otherwise try the sorted-longest-first list; require word-boundary match.
    for key in _SORTED_STAT_KEYS:
        if text == key:
            return STAT_NAME_MAP[key]
    return None


# ---------------------------------------------------------------------------
# Pattern handlers
# ---------------------------------------------------------------------------

# "12% increased Cold Damage"  / "X% reduced Y"
_RE_INC = re.compile(r"^(-?\d+(?:\.\d+)?)%\s+(increased|reduced)\s+(.+)$", re.IGNORECASE)
# "10% more Spell Damage"  / "X% less Y"
_RE_MORE = re.compile(r"^(-?\d+(?:\.\d+)?)%\s+(more|less)\s+(.+)$", re.IGNORECASE)
# "+50 to maximum Life"  /  "-10 to Y"
_RE_PLUS_FLAT = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s+to\s+(.+)$", re.IGNORECASE)
# "+30% to Cold Resistance"
_RE_PLUS_PCT = re.compile(r"^([+-]?\d+(?:\.\d+)?)%\s+to\s+(.+)$", re.IGNORECASE)
# "+X% chance to Suppress Spell Damage" / "+X% Chance to Block Attack Damage"
# We capture the whole "chance to X" so STAT_NAME_MAP entries like
# "chance to suppress spell damage" resolve directly.
_RE_PLUS_CHANCE = re.compile(
    r"^([+-]?\d+(?:\.\d+)?)%\s+(chance\s+to\s+.+)$", re.IGNORECASE
)
# "Adds 5 to 10 Cold Damage to Spells"  /  "Adds X to Y Z Damage"
_RE_ADDS = re.compile(
    r"^adds\s+(\d+(?:\.\d+)?)\s+to\s+(\d+(?:\.\d+)?)\s+([\w ]+?)\s+damage(?:\s+to\s+(\w+))?$",
    re.IGNORECASE,
)
# "Regenerate 1% of Life per second"  /  "Regenerate 12.5 Life per second"
_RE_REGEN_PCT = re.compile(
    r"^regenerate\s+(\d+(?:\.\d+)?)%\s+of\s+(\w+(?:\s+\w+)?)\s+per\s+second$",
    re.IGNORECASE,
)
_RE_REGEN_FLAT = re.compile(
    r"^regenerate\s+(\d+(?:\.\d+)?)\s+(\w+(?:\s+\w+)?)\s+per\s+second$",
    re.IGNORECASE,
)
# "Deal Triple/Double Damage with X" — Annihilating Light and a few uniques.
# Triple = 200% MORE, Double = 100% MORE, Quadruple = 300% MORE.
_RE_DEAL_MULTI = re.compile(
    r"^deal\s+(double|triple|quadruple)\s+damage\s+with\s+(.+)$",
    re.IGNORECASE,
)
_MULTI_TO_MORE = {"double": 100, "triple": 200, "quadruple": 300}
# "Minions deal 10% increased Damage"  /  "Minions have X"
_RE_MINION_PREFIX = re.compile(
    r"^minions\s+(?:deal|have|gain|are|recover|regenerate)\s+(.+)$",
    re.IGNORECASE,
)
# "Cannot be Frozen" / "Cannot be Chilled" / "Cannot Bleed" / "Cannot be Poisoned"
_RE_CANNOT_BE = re.compile(r"^cannot\s+be\s+(\w+)$", re.IGNORECASE)
_RE_CANNOT = re.compile(r"^cannot\s+(\w+)$", re.IGNORECASE)
_AILMENT_TO_TARGET = {
    "frozen": "ImmuneToFreeze",
    "chilled": "ImmuneToChill",
    "shocked": "ImmuneToShock",
    "ignited": "ImmuneToIgnite",
    "stunned": "ImmuneToStun",
    "poisoned": "ImmuneToPoison",
    "cursed": "ImmuneToCurse",
    "bleed": "ImmuneToBleed",  # for "Cannot Bleed"
}


# Conditional / contextual / triggered clauses we strip-then-treat-as-always-on
# for MVP. This is deliberately broad — it over-applies conditional mods, which
# inflates DPS for builds that don't actually meet the condition. Real fix is
# per-stat condition modeling (phase 4e or so).
_COND_CLAUSES = re.compile(
    r"\s+("
    r"while\s+.+"           # while X
    r"|if\s+.+"             # if you / if the / if you've / if it (broad)
    r"|when\s+.+"           # when you / when the / when on / when X
    r"|during\s+.+"         # during X (Spiked Concoction's flask condition)
    r"|against\s+.+"
    r"|per\s+\w*\s*charges?(\s+\w+)?"  # per Endurance Charge / per Power Charge
    r"|of\s+\w+\s+skills?"
    r"|with\s+\w+(\s+\w+)?\s+skills?"
    r"|on\s+\w+(\s+\w+)?"   # on Hit / on Kill / on Low Life
    r"|recently"            # "Recently" trailing word
    r")$",
    re.IGNORECASE,
)


def _split_and_clauses(line: str) -> list[str]:
    """`+10 to Strength and Intelligence` -> [`+10 to Strength`, `+10 to Intelligence`]
    so each half maps to its own modifier. Same shape for `X% increased Attack
    and Cast Speed`. We only split when both halves resolve to known stat names
    via STAT_NAME_MAP — otherwise we'd corrupt natural-language uses of "and".
    """
    # Pattern: "<prefix> A and B" where A and B are recognized stat names.
    # Try splitting on " and " and checking if both halves end with a known stat.
    if " and " not in line.lower():
        return [line]

    # Heuristic: try the "+X to A and B" / "X% increased A and B" / "Y rate" shape.
    # Match "+X (to)?" or "X%? (increased|reduced|more|less)?" then "<A> and <B>"
    m = re.match(
        r"^([+-]?\d+(?:\.\d+)?%?)\s+(to\s+|increased\s+|reduced\s+|more\s+|less\s+)?(.+?)\s+and\s+(.+)$",
        line, re.IGNORECASE
    )
    if not m:
        return [line]
    qty, verb, a, b = m.group(1), m.group(2) or "", m.group(3).strip(), m.group(4).strip()
    # Resolve a as-is OR as a partial stat name (e.g. "Strength" vs "Cast Speed" with
    # shared suffix "Speed" in B). The simplest test: does (a + suffix-of-b) form a name?
    suffix_words = b.split()
    candidates_a = [a]
    # If b is multi-word (e.g., "Cast Speed"), try a + last word ("Attack Speed")
    if len(suffix_words) >= 2:
        candidates_a.append(f"{a} {suffix_words[-1]}")
    if not any(_lookup_stat(c.lower()) for c in candidates_a):
        return [line]
    if not _lookup_stat(b.lower()):
        return [line]
    # We have a valid split — synthesize two lines.
    a_full = candidates_a[1] if len(candidates_a) > 1 and _lookup_stat(candidates_a[1].lower()) else candidates_a[0]
    line_a = f"{qty} {verb}{a_full}".strip()
    line_b = f"{qty} {verb}{b}".strip()
    return [line_a, line_b]


def parse_stat_text(line: str) -> list[Modifier]:
    """Parse one stat text line into zero or more Modifier objects.

    Returns an empty list if the line doesn't match any handler. Callers
    should track which lines didn't parse so we can extend coverage.
    """
    text = line.strip().rstrip(".")

    # Try splitting "X and Y" first so each half routes through pattern handlers
    halves = _split_and_clauses(text)
    if len(halves) > 1:
        out = []
        for h in halves:
            out.extend(parse_stat_text(h))
        return out

    # ---- Patterns that NEED their trailing words run before the conditional
    # stripper (which would otherwise eat "per second" or "with X Skills") ----

    # "Regenerate X% of Pool per second"
    if m := _RE_REGEN_PCT.match(text):
        val, pool_name = float(m.group(1)), m.group(2).strip().lower()
        if pool_name in ("life", "mana", "energy shield"):
            target = {"life": "LifeRegenPct",
                      "mana": "ManaRegenPct",
                      "energy shield": "ESRegenPct"}[pool_name]
            return [Modifier(target, "BASE", val)]
        return []

    # "Regenerate X Pool per second" (flat)
    if m := _RE_REGEN_FLAT.match(text):
        val, pool_name = float(m.group(1)), m.group(2).strip().lower()
        if pool_name in ("life", "mana", "energy shield"):
            target = {"life": "LifeRegenFlat",
                      "mana": "ManaRegenFlat",
                      "energy shield": "ESRegenFlat"}[pool_name]
            return [Modifier(target, "BASE", val)]
        return []

    # "Deal Triple/Double Damage with X"
    if m := _RE_DEAL_MULTI.match(text):
        multi_word, scope = m.group(1).lower(), m.group(2).strip().lower()
        more_pct = _MULTI_TO_MORE[multi_word]
        tags: frozenset = frozenset()
        if "elemental" in scope:
            tags = frozenset({"Elemental"})
        elif "spell" in scope:
            tags = frozenset({"Spell"})
        elif "attack" in scope:
            tags = frozenset({"Attack"})
        return [Modifier("Damage", "MORE", more_pct, tags)]

    # ---- Now strip conditional clauses for the general patterns ----
    text = _COND_CLAUSES.sub("", text).strip()

    # "Cannot be X" / "Cannot Bleed" -> ailment immunity
    if m := _RE_CANNOT_BE.match(text):
        word = m.group(1).lower()
        target = _AILMENT_TO_TARGET.get(word)
        if target:
            return [Modifier(target, "BASE", 1)]
        return []
    if m := _RE_CANNOT.match(text):
        word = m.group(1).lower()
        target = _AILMENT_TO_TARGET.get(word)
        if target:
            return [Modifier(target, "BASE", 1)]
        return []

    # "Minions <verb> X" — recurse on the inner stat with Minion scope tag.
    if m := _RE_MINION_PREFIX.match(text):
        inner = m.group(1).strip()
        inner_mods = parse_stat_text(inner)
        return [
            Modifier(mod.target, mod.type, mod.value, mod.tags | frozenset({"Minion"}))
            for mod in inner_mods
        ]

    if m := _RE_ADDS.match(text):
        mn, mx, dmg_type, scope = m.group(1), m.group(2), m.group(3).strip().lower(), m.group(4)
        dmg_type_cap = dmg_type.capitalize()
        if dmg_type.lower() not in {"physical", "fire", "cold", "lightning", "chaos"}:
            return []
        target_min = f"Added{dmg_type_cap}Min"
        target_max = f"Added{dmg_type_cap}Max"
        tags = frozenset()
        if scope:
            scope_l = scope.lower()
            if scope_l in ("spells", "spell"):
                tags = frozenset({"Spell"})
            elif scope_l in ("attacks", "attack"):
                tags = frozenset({"Attack"})
        return [
            Modifier(target_min, "BASE", float(mn), tags),
            Modifier(target_max, "BASE", float(mx), tags),
        ]

    if m := _RE_INC.match(text):
        val, direction, name = float(m.group(1)), m.group(2).lower(), m.group(3).strip().lower()
        signed = val if direction == "increased" else -val
        target = _lookup_stat(name)
        if target is None:
            return []
        return [Modifier(target[0], "INC", signed, target[1])]

    if m := _RE_MORE.match(text):
        val, direction, name = float(m.group(1)), m.group(2).lower(), m.group(3).strip().lower()
        signed = val if direction == "more" else -val
        target = _lookup_stat(name)
        if target is None:
            return []
        return [Modifier(target[0], "MORE", signed, target[1])]

    if m := _RE_PLUS_CHANCE.match(text):
        val, name = float(m.group(1)), m.group(2).strip().lower()
        target = _lookup_stat(name)
        if target is None:
            return []
        return [Modifier(target[0], "BASE", val, target[1])]

    if m := _RE_PLUS_PCT.match(text):
        val, name = float(m.group(1)), m.group(2).strip().lower()
        target = _lookup_stat(name)
        if target is None:
            return []
        return [Modifier(target[0], "BASE", val, target[1])]

    if m := _RE_PLUS_FLAT.match(text):
        val, name = float(m.group(1)), m.group(2).strip().lower()
        target = _lookup_stat(name)
        if target is None:
            return []
        return [Modifier(target[0], "BASE", val, target[1])]

    return []


def parse_node_stats(stats: list[str]) -> tuple[list[Modifier], list[str]]:
    """Parse all stat lines of a node. Returns (modifiers, unparsed_lines)."""
    mods: list[Modifier] = []
    unparsed: list[str] = []
    for line in stats:
        parsed = parse_stat_text(line)
        if parsed:
            mods.extend(parsed)
        else:
            unparsed.append(line)
    return mods, unparsed
