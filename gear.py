"""
Per-slot gear model.

A Build's gear is a dict of {slot_name: GearItem}. Each GearItem has a list
of Modifier objects (parsed from human-readable stat lines via mod_parser)
that flow into the calc engine the same way unique-item mods do today.

Two flavours:
  - `unique` — actual unique item from PoB data (parsed via existing pipeline)
  - `rare` — synthetic "typical level-X rare" baseline (life/res/attr-heavy)

This lets a build that pulled, say, 3 unique items from poe.ninja still
benefit from rare-quality mods on the 7 empty slots — much closer to a
real character than treating unset slots as empty.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Iterable

import mod_parser
import pob_data
from stat_map import Modifier


# --- Slot constants ---
SLOT_HELMET = "helmet"
SLOT_BODY = "body"
SLOT_GLOVES = "gloves"
SLOT_BOOTS = "boots"
SLOT_BELT = "belt"
SLOT_AMULET = "amulet"
SLOT_RING_1 = "ring1"
SLOT_RING_2 = "ring2"
SLOT_WEAPON = "weapon"
SLOT_OFFHAND = "offhand"

ARMOUR_SLOTS = (SLOT_HELMET, SLOT_BODY, SLOT_GLOVES, SLOT_BOOTS)
JEWELRY_SLOTS = (SLOT_AMULET, SLOT_RING_1, SLOT_RING_2, SLOT_BELT)
WEAPON_SLOTS = (SLOT_WEAPON, SLOT_OFFHAND)

# Order is presentation order in the GUI.
ALL_SLOTS = (
    SLOT_WEAPON, SLOT_OFFHAND,
    SLOT_HELMET, SLOT_AMULET, SLOT_BODY, SLOT_GLOVES, SLOT_BOOTS,
    SLOT_BELT, SLOT_RING_1, SLOT_RING_2,
)

# PoB tags unique items with a fine-grained slot string (e.g. "sword", "shield",
# "quiver"). Map those to our generic slots.
PRIMARY_WEAPON_TAGS = {
    "sword", "mace", "staff", "axe", "bow", "wand", "dagger", "claw",
    "sceptre", "rune dagger", "fishing",
}
OFFHAND_TAGS = {"shield", "quiver"}


def map_unique_slot(unique_slot: str) -> str | None:
    """PoB unique slot tag -> Build slot. Returns None for slots we don't
    model yet (jewel, flask, tincture)."""
    if unique_slot in ARMOUR_SLOTS or unique_slot in JEWELRY_SLOTS:
        # body/helmet/gloves/boots/belt/amulet/ring all map directly except ring
        if unique_slot == "ring":
            return SLOT_RING_1  # caller can place the 2nd ring of the same
                                # unique into RING_2 if desired
        return unique_slot
    if unique_slot in PRIMARY_WEAPON_TAGS:
        return SLOT_WEAPON
    if unique_slot in OFFHAND_TAGS:
        return SLOT_OFFHAND
    return None


# ---------------------------------------------------------------------------
# GearItem model
# ---------------------------------------------------------------------------

ITEM_TYPE_UNIQUE = "unique"
ITEM_TYPE_RARE = "rare"
ITEM_TYPE_NONE = "none"


@dataclass
class GearItem:
    slot: str
    item_type: str                       # ITEM_TYPE_*
    display_name: str                    # "Mageblood", "Rare Helmet", "—"
    mods: list[Modifier] = field(default_factory=list)
    stat_lines: list[str] = field(default_factory=list)  # raw display lines
    unparsed_lines: list[str] = field(default_factory=list)


def empty_item(slot: str) -> GearItem:
    return GearItem(slot=slot, item_type=ITEM_TYPE_NONE,
                    display_name="—", mods=[], stat_lines=[])


def gear_item_from_unique(slot: str, unique: dict) -> GearItem:
    """Build a GearItem from a parsed unique record (pob_data.uniques.json)."""
    name = unique.get("name", "?")
    stat_lines = unique.get("stats") or []
    mods, unparsed = mod_parser.parse_node_stats(stat_lines)
    return GearItem(
        slot=slot,
        item_type=ITEM_TYPE_UNIQUE,
        display_name=name,
        mods=mods,
        stat_lines=list(stat_lines),
        unparsed_lines=unparsed,
    )


def gear_item_from_rare(slot: str) -> GearItem:
    """Synthetic 'typical endgame rare' for the slot. See SYNTHETIC_RARE_STATS
    for the per-slot baselines."""
    lines = SYNTHETIC_RARE_STATS.get(slot, [])
    mods, unparsed = mod_parser.parse_node_stats(lines)
    return GearItem(
        slot=slot,
        item_type=ITEM_TYPE_RARE,
        display_name=_RARE_DISPLAY_NAMES.get(slot, f"Rare {slot.title()}"),
        mods=mods,
        stat_lines=list(lines),
        unparsed_lines=unparsed,
    )


# ---------------------------------------------------------------------------
# Synthetic "typical endgame rare" baselines per slot
# ---------------------------------------------------------------------------

# Values target a level-90 character running yellow-to-red maps. These are
# conservative estimates of what a self-found rare with t1-t3 affixes provides;
# crafted/curated rares can roll significantly higher.

SYNTHETIC_RARE_STATS: dict[str, list[str]] = {
    SLOT_HELMET: [
        "+90 to maximum Life",
        "+35 to maximum Mana",
        "+30% to Cold Resistance",
        "+30% to Lightning Resistance",
        "+40 to Strength",
    ],
    SLOT_BODY: [
        "+110 to maximum Life",
        "+30% to Fire Resistance",
        "+30% to Cold Resistance",
        "+30% to Lightning Resistance",
        "+50 to Strength",
    ],
    SLOT_GLOVES: [
        "+80 to maximum Life",
        "+30% to Fire Resistance",
        "+30% to Cold Resistance",
        "+40 to Dexterity",
        "10% increased Attack Speed",
    ],
    SLOT_BOOTS: [
        "+80 to maximum Life",
        "+30% to Fire Resistance",
        "+30% to Lightning Resistance",
        "30% increased Movement Speed",
        "+40 to Dexterity",
    ],
    SLOT_BELT: [
        "+90 to maximum Life",
        "+30% to Fire Resistance",
        "+30% to Cold Resistance",
        "+40 to Strength",
    ],
    SLOT_AMULET: [
        "+60 to maximum Life",
        "+40 to maximum Mana",
        "+40 to Strength",
        "+40 to Intelligence",
        "+30% to Cold Resistance",
    ],
    SLOT_RING_1: [
        "+60 to maximum Life",
        "+40 to maximum Mana",
        "+30% to Cold Resistance",
        "+30% to Lightning Resistance",
        "+30 to Intelligence",
    ],
    SLOT_RING_2: [
        "+60 to maximum Life",
        "+40 to maximum Mana",
        "+30% to Fire Resistance",
        "+30% to Chaos Resistance",
        "+30 to Intelligence",
    ],
    # Weapon synthesis is intentionally light — a real build's weapon mods
    # vary wildly by archetype (spell vs attack vs bow). Generic +damage/cast
    # speed here gives the user some scaffolding without overstating power.
    SLOT_WEAPON: [
        "30% increased Spell Damage",
        "15% increased Cast Speed",
        "+30 to Intelligence",
    ],
    SLOT_OFFHAND: [
        "+70 to maximum Life",
        "+30% to Fire Resistance",
        "+30% to Cold Resistance",
        "15% increased Spell Damage",
    ],
}

_RARE_DISPLAY_NAMES: dict[str, str] = {
    SLOT_HELMET: "Rare Helmet",
    SLOT_BODY: "Rare Body Armour",
    SLOT_GLOVES: "Rare Gloves",
    SLOT_BOOTS: "Rare Boots",
    SLOT_BELT: "Rare Belt",
    SLOT_AMULET: "Rare Amulet",
    SLOT_RING_1: "Rare Ring",
    SLOT_RING_2: "Rare Ring",
    SLOT_WEAPON: "Rare Weapon",
    SLOT_OFFHAND: "Rare Shield",
}


# ---------------------------------------------------------------------------
# Population: take a list of unique-item dicts and produce a full gear dict
# ---------------------------------------------------------------------------

def populate_gear_from_uniques(
    unique_picks: Iterable[dict],
    *,
    fill_empty_with_rares: bool = True,
) -> dict[str, GearItem]:
    """Build a complete gear dict given a (possibly small) list of uniques.

    `unique_picks` is a list of unique-item records (as returned by
    pob_data.get_unique). The first unique that wants a given slot wins;
    subsequent uniques for the same slot are dropped (you can only wear
    one body armour). Two-handed weapons block the offhand and vice versa
    is left to the caller — for now, both slots are independent.

    With `fill_empty_with_rares=True`, every slot not claimed by a unique
    gets a synthetic rare baseline so the calc reflects "real character" mods.
    """
    gear: dict[str, GearItem] = {}
    for unique in unique_picks:
        if not isinstance(unique, dict):
            continue
        slot_tag = unique.get("slot")
        target_slot = map_unique_slot(slot_tag) if slot_tag else None
        if target_slot is None:
            continue
        # Special handling: a second "ring" unique can fall into ring2 if ring1
        # is already populated.
        if target_slot == SLOT_RING_1 and SLOT_RING_1 in gear:
            target_slot = SLOT_RING_2
        if target_slot in gear:
            continue  # slot already claimed (e.g., two body uniques)
        gear[target_slot] = gear_item_from_unique(target_slot, unique)

    if fill_empty_with_rares:
        for slot in ALL_SLOTS:
            if slot not in gear:
                gear[slot] = gear_item_from_rare(slot)
    return gear


def aggregate_mods(gear: dict[str, GearItem]) -> list[Modifier]:
    """Flatten every slot's mods into one list — the format calc expects."""
    out: list[Modifier] = []
    for item in gear.values():
        out.extend(item.mods)
    return out
