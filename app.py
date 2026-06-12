"""
PoE Builder — desktop app.

Wraps the existing CLI pipeline (ninja_template / calc / defense / qol /
optimizer) in a CustomTkinter window. Single window with:
  - Top bar: searchable skill picker + playstyle selector + Calculate button
  - Tabbed view: Template / DPS / Defense / QoL / Optimizer
  - Status bar at the bottom

Network + calc work runs on a worker thread so the UI doesn't freeze.
"""
from __future__ import annotations

import contextlib
import json
import threading
import traceback
from io import StringIO
from pathlib import Path

import customtkinter as ctk

import pob_data
from build import validate, PLAYSTYLE_MAPPING, PLAYSTYLE_BOSSING, PLAYSTYLE_BALANCED
import calc
import defense
import qol
import optimizer
import ninja_template


SETTINGS_PATH = Path(__file__).parent / "data" / "cache" / "app_settings.json"


def _load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_settings(data: dict) -> None:
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass  # not fatal


# ---------------------------------------------------------------------------
# Theme / typography — Path of Building-style
# ---------------------------------------------------------------------------

FONT_FAMILY = "Segoe UI"
FONT_MONO = "Cascadia Code"  # falls back to Consolas if unavailable

# Neutral chrome (PoB is mostly black/grey)
COLOR_BG = "#000000"
COLOR_PANEL = "#1A1A1A"
COLOR_PANEL_HOVER = "#2A2A2A"
COLOR_BORDER = "#3A3A3A"
COLOR_TEXT = "#E0E0E0"
COLOR_TEXT_DIM = "#888888"

# Yellow is PoB's one chrome accent (selected tabs, primary button, focus border)
COLOR_HIGHLIGHT = "#FFFF77"
COLOR_HIGHLIGHT_HOVER = "#D0D060"
COLOR_HIGHLIGHT_TEXT = "#000000"  # text on yellow buttons

# Semantic / status colors — used sparingly inside content
COLOR_OK = "#33FF77"
COLOR_WARN = "#FFFF77"
COLOR_BAD = "#FF5555"

# Gem attribute colors (PoB convention: red=Str, green=Dex, blue=Int, white=multi)
COLOR_STR = "#E04030"
COLOR_DEX = "#33C055"
COLOR_INT = "#6080FF"
COLOR_MULTI = "#E0E0E0"

# pob_data.load_skills() encodes attribute as `color` (1=Str, 2=Dex, 3=Int).
GEM_COLOR_MAP = {1: COLOR_STR, 2: COLOR_DEX, 3: COLOR_INT}


_ACTIVE_SKILL_TAGS = {"Spell", "Attack", "Aura", "Warcry"}


def list_active_skill_names() -> list[str]:
    """Display names of every active skill (not support gems, not Vaal variants)."""
    return sorted(active_skill_color_map().keys())


def active_skill_color_map() -> dict[str, str]:
    """Map skill display-name -> hex color (PoB gem-color convention)."""
    skills = pob_data.load_skills()
    out: dict[str, str] = {}
    for key, data in skills.items():
        if not isinstance(data, dict):
            continue
        if data.get("support"):
            continue
        if key.startswith("Vaal"):
            continue
        name = data.get("name")
        if not name:
            continue
        tags = data.get("skillTypes") or {}
        if isinstance(tags, list):
            tags = set(tags)
        else:
            tags = {k for k, v in tags.items() if v}
        if not (_ACTIVE_SKILL_TAGS & tags):
            continue
        color = GEM_COLOR_MAP.get(data.get("color"), COLOR_MULTI)
        # First occurrence wins; duplicate names across variants are rare.
        out.setdefault(name, color)
    return out


# ---------------------------------------------------------------------------
# Searchable skill picker
# ---------------------------------------------------------------------------

class FilterableCombobox(ctk.CTkFrame):
    """Entry + filtered dropdown popup. Type to filter, click a row to select.

    Behaves like a combobox: callers read `.get()` for the current value.
    """
    MAX_RESULTS = 50

    def __init__(self, master, values: list[str], *,
                 width: int = 260, placeholder: str = "Type to filter...",
                 default: str = "",
                 value_colors: dict[str, str] | None = None,
                 **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.all_values = sorted(set(values))
        self.value_colors = value_colors or {}
        self.var = ctk.StringVar(value=default)

        self.entry = ctk.CTkEntry(
            self, textvariable=self.var, width=width, height=32,
            placeholder_text=placeholder, font=(FONT_FAMILY, 13),
            border_color=COLOR_BORDER, border_width=1,
            fg_color=COLOR_PANEL, text_color=COLOR_TEXT,
        )
        self.entry.pack(side="left")
        self.entry.bind("<KeyRelease>", self._on_type)
        self.entry.bind("<FocusIn>", lambda e: self._show_popup())
        self.entry.bind("<Down>", lambda e: self._focus_first_result())
        # Click anywhere outside the entry/popup -> close
        self.winfo_toplevel().bind("<Button-1>", self._on_global_click, add="+")

        self.popup: ctk.CTkToplevel | None = None
        self.scroll: ctk.CTkScrollableFrame | None = None
        self._row_buttons: list[ctk.CTkButton] = []

    ROW_HEIGHT = 30  # row button height + vertical padding
    POPUP_MAX_HEIGHT = 320
    POPUP_MIN_HEIGHT = 40

    def _show_popup(self):
        if self.popup is not None and self.popup.winfo_exists():
            return
        top = self.winfo_toplevel()
        self.popup = ctk.CTkToplevel(top)
        self.popup.overrideredirect(True)
        self.popup.configure(fg_color=COLOR_BORDER)  # acts as 1px border
        x = self.entry.winfo_rootx()
        y = self.entry.winfo_rooty() + self.entry.winfo_height() + 2
        self.popup.geometry(f"{self.entry.winfo_width()}x{self.POPUP_MIN_HEIGHT}+{x}+{y}")
        self.popup.transient(top)
        self.scroll = ctk.CTkScrollableFrame(self.popup, fg_color=COLOR_PANEL,
                                             corner_radius=0)
        self.scroll.pack(fill="both", expand=True, padx=2, pady=2)
        self._refresh_results()

    def _resize_popup(self, row_count: int):
        if self.popup is None:
            return
        h = min(self.POPUP_MAX_HEIGHT,
                max(self.POPUP_MIN_HEIGHT, row_count * self.ROW_HEIGHT + 12))
        x = self.entry.winfo_rootx()
        y = self.entry.winfo_rooty() + self.entry.winfo_height() + 2
        self.popup.geometry(f"{self.entry.winfo_width()}x{h}+{x}+{y}")

    def _hide_popup(self):
        if self.popup is not None:
            self.popup.destroy()
            self.popup = None
            self.scroll = None
            self._row_buttons = []

    def _on_global_click(self, event):
        if self.popup is None:
            return
        # Walk up the widget tree from the click target to see if it's inside us
        w = event.widget
        while w is not None:
            if w is self or w is self.popup:
                return
            w = getattr(w, "master", None)
        self._hide_popup()

    def _on_type(self, event):
        if self.popup is None:
            self._show_popup()
        else:
            self._refresh_results()

    def _refresh_results(self):
        if self.scroll is None:
            return
        for b in self._row_buttons:
            b.destroy()
        self._row_buttons = []
        query = self.var.get().strip().lower()
        if query:
            # rank: startswith first, then contains
            starts = [v for v in self.all_values if v.lower().startswith(query)]
            contains = [v for v in self.all_values
                        if query in v.lower() and not v.lower().startswith(query)]
            matches = (starts + contains)[: self.MAX_RESULTS]
        else:
            matches = self.all_values[: self.MAX_RESULTS]
        for m in matches:
            text_color = self.value_colors.get(m, COLOR_TEXT)
            btn = ctk.CTkButton(
                self.scroll, text=m, anchor="w", height=28,
                fg_color="transparent", hover_color=COLOR_PANEL_HOVER,
                text_color=text_color, font=(FONT_FAMILY, 12),
                command=lambda v=m: self._select(v),
            )
            btn.pack(fill="x", padx=2, pady=1)
            self._row_buttons.append(btn)
        if not matches:
            ctk.CTkLabel(self.scroll, text="(no matches)",
                         text_color=COLOR_TEXT_DIM,
                         font=(FONT_FAMILY, 12)).pack(pady=8)
            self._resize_popup(1)
        else:
            self._resize_popup(len(matches))

    def _focus_first_result(self):
        if self._row_buttons:
            self._row_buttons[0].focus_set()

    def _select(self, value: str):
        self.var.set(value)
        self._hide_popup()
        self.entry.icursor("end")

    def get(self) -> str:
        return self.var.get()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class PoEBuilderApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("PoE Builder")
        self.geometry("1180x800")
        self.configure(fg_color=COLOR_BG)

        # Realize the HWND before talking to DWM. Setting the dark-mode
        # attribute *before* the first paint avoids the brief light flash —
        # no withdraw/deiconify needed (that path was leaving the window
        # invisible on some machines).
        self.update_idletasks()
        self._apply_dark_titlebar()

        # ---- Top bar ----
        top = ctk.CTkFrame(self, fg_color=COLOR_PANEL, corner_radius=8, height=72)
        top.pack(side="top", fill="x", padx=14, pady=(14, 8))
        top.pack_propagate(False)

        ctk.CTkLabel(top, text="PoE Builder", font=(FONT_FAMILY, 18, "bold"),
                     text_color=COLOR_TEXT).pack(side="left", padx=(16, 24))

        self._settings = _load_settings()

        ctk.CTkLabel(top, text="Skill", font=(FONT_FAMILY, 12),
                     text_color=COLOR_TEXT_DIM).pack(side="left", padx=(0, 6))
        self.skill_color_map = active_skill_color_map()
        self.skill_choices = sorted(self.skill_color_map.keys())
        remembered = self._settings.get("last_skill")
        if remembered and remembered in self.skill_color_map:
            default_skill = remembered
        elif "Frostbolt" in self.skill_choices:
            default_skill = "Frostbolt"
        else:
            default_skill = self.skill_choices[0] if self.skill_choices else ""
        self.skill_picker = FilterableCombobox(
            top, values=self.skill_choices, default=default_skill, width=260,
            value_colors=self.skill_color_map,
        )
        self.skill_picker.pack(side="left", padx=(0, 18))

        ctk.CTkLabel(top, text="Playstyle", font=(FONT_FAMILY, 12),
                     text_color=COLOR_TEXT_DIM).pack(side="left", padx=(0, 6))
        _playstyles = [PLAYSTYLE_MAPPING, PLAYSTYLE_BOSSING, PLAYSTYLE_BALANCED]
        remembered_ps = self._settings.get("last_playstyle")
        ps_default = remembered_ps if remembered_ps in _playstyles else PLAYSTYLE_MAPPING
        self.playstyle_var = ctk.StringVar(value=ps_default)
        self.playstyle_dropdown = ctk.CTkOptionMenu(
            top,
            values=[PLAYSTYLE_MAPPING, PLAYSTYLE_BOSSING, PLAYSTYLE_BALANCED],
            variable=self.playstyle_var, width=140, height=32,
            fg_color=COLOR_BORDER, button_color=COLOR_BORDER,
            button_hover_color=COLOR_PANEL_HOVER,
            text_color=COLOR_TEXT, font=(FONT_FAMILY, 12),
            corner_radius=6,
            dropdown_fg_color=COLOR_PANEL,
            dropdown_hover_color=COLOR_PANEL_HOVER,
            dropdown_text_color=COLOR_TEXT,
        )
        self.playstyle_dropdown.pack(side="left", padx=(0, 18))

        self.calc_button = ctk.CTkButton(
            top, text="Calculate", command=self.on_calculate, width=120, height=32,
            fg_color=COLOR_HIGHLIGHT, hover_color=COLOR_HIGHLIGHT_HOVER,
            text_color=COLOR_HIGHLIGHT_TEXT,
            font=(FONT_FAMILY, 13, "bold"),
        )
        self.calc_button.pack(side="left", padx=(0, 16))

        # ---- Tab view ----
        # CTk only supports one text_color for all segmented buttons, so the
        # selected tab uses a dark amber instead of bright yellow so white text
        # remains readable.
        self.tabs = ctk.CTkTabview(
            self, fg_color=COLOR_PANEL, segmented_button_fg_color=COLOR_BG,
            segmented_button_selected_color="#8A7B2E",
            segmented_button_selected_hover_color="#A8923A",
            segmented_button_unselected_color=COLOR_PANEL,
            segmented_button_unselected_hover_color=COLOR_PANEL_HOVER,
            text_color=COLOR_TEXT,
        )
        self.tabs.pack(side="top", fill="both", expand=True, padx=14, pady=(0, 8))
        # "Gear" is special — it holds interactive controls, not a textbox.
        self.tab_names = ("Template", "Gear", "DPS", "Defense", "QoL", "Optimizer")
        self.text_tab_names = tuple(n for n in self.tab_names if n != "Gear")
        for name in self.tab_names:
            self.tabs.add(name)

        self.tab_text: dict[str, ctk.CTkTextbox] = {}
        for name in self.text_tab_names:
            tb = ctk.CTkTextbox(
                self.tabs.tab(name), font=(FONT_MONO, 12), wrap="none",
                fg_color=COLOR_BG, text_color=COLOR_TEXT,
                corner_radius=6,
            )
            tb.pack(fill="both", expand=True, padx=6, pady=6)
            self.tab_text[name] = tb
            self._show_placeholder(name)

        self._build_gear_tab()
        self.current_build = None
        self._gear_worker: threading.Thread | None = None

        # ---- Status bar ----
        status_bar = ctk.CTkFrame(self, fg_color=COLOR_PANEL, height=32, corner_radius=6)
        status_bar.pack(side="bottom", fill="x", padx=14, pady=(0, 14))
        status_bar.pack_propagate(False)
        self.status_label = ctk.CTkLabel(
            status_bar, text="Ready. Pick a skill, hit Calculate.",
            anchor="w", font=(FONT_FAMILY, 11), text_color=COLOR_TEXT_DIM,
        )
        self.status_label.pack(side="left", fill="x", expand=True, padx=14)

        self._worker: threading.Thread | None = None
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        self._persist_settings()
        self.destroy()

    def _persist_settings(self):
        _save_settings({
            "last_skill": self.skill_picker.get().strip(),
            "last_playstyle": self.playstyle_var.get().strip(),
        })

    def _emit_status(self, text: str, color: str = COLOR_TEXT_DIM):
        """Thread-safe status update — schedules on the Tk main loop."""
        self.after(0, self.set_status, text, color)

    def _apply_dark_titlebar(self):
        """Force the Windows 11 title bar dark via DWM (CTk's auto path can miss)."""
        import sys
        if not sys.platform.startswith("win"):
            return
        try:
            from ctypes import windll, byref, c_int, sizeof
            hwnd = windll.user32.GetParent(self.winfo_id())
            value = c_int(1)
            for attr in (20, 19):  # Win11, then pre-19041 Win10 attribute
                if windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, byref(value), sizeof(value)
                ) == 0:
                    return
        except Exception:
            pass  # not fatal; just leaves the OS default

    # ---- Status / output helpers ----
    def _show_placeholder(self, tab_name: str):
        msg = {
            "Template":  "Run a calculation to see the popular build template for this skill.",
            "DPS":       "DPS breakdown will appear here after Calculate.",
            "Defense":   "Life / ES / Resists / EHP breakdown will appear after Calculate.",
            "QoL":       "Movement speed + ailment immunities will appear after Calculate.",
            "Optimizer": "Pareto-ranked swap suggestions will appear after Calculate.",
        }.get(tab_name, "")
        self.set_tab_text(tab_name, msg)

    # ---- Gear tab ----
    def _build_gear_tab(self):
        """Populate the Gear tab's persistent chrome. Rows are filled in later
        by _refresh_gear_rows() once a build is loaded."""
        import gear as gear_module
        self._gear_module = gear_module
        parent = self.tabs.tab("Gear")

        top = ctk.CTkFrame(parent, fg_color="transparent", height=44)
        top.pack(side="top", fill="x", padx=6, pady=(6, 4))
        top.pack_propagate(False)

        self.gear_recompute_button = ctk.CTkButton(
            top, text="Recompute", command=self.on_gear_recompute,
            width=130, height=32,
            fg_color=COLOR_HIGHLIGHT, hover_color=COLOR_HIGHLIGHT_HOVER,
            text_color=COLOR_HIGHLIGHT_TEXT, font=(FONT_FAMILY, 13, "bold"),
            state="disabled",
        )
        self.gear_recompute_button.pack(side="left")

        self.gear_reset_button = ctk.CTkButton(
            top, text="Reset to template", command=self.on_gear_reset,
            width=160, height=32,
            fg_color=COLOR_PANEL, hover_color=COLOR_PANEL_HOVER,
            text_color=COLOR_TEXT, font=(FONT_FAMILY, 12),
            border_color=COLOR_BORDER, border_width=1,
            state="disabled",
        )
        self.gear_reset_button.pack(side="left", padx=(8, 0))

        self.gear_status_label = ctk.CTkLabel(
            top, text="Run Calculate first to populate gear slots.",
            font=(FONT_FAMILY, 11), text_color=COLOR_TEXT_DIM, anchor="w",
        )
        self.gear_status_label.pack(side="left", padx=14)

        self.gear_rows_frame = ctk.CTkScrollableFrame(
            parent, fg_color=COLOR_BG, corner_radius=6,
        )
        self.gear_rows_frame.pack(side="top", fill="both", expand=True,
                                  padx=6, pady=(0, 6))
        self.gear_pickers: dict[str, FilterableCombobox] = {}
        self._template_gear: dict | None = None  # snapshot for Reset

    def _slot_choices(self, slot: str) -> list[str]:
        """Picker options for a given slot: '(Empty)' + 'Rare X' + all matching uniques."""
        uniques = pob_data.load_uniques()
        gm = self._gear_module
        rare_label = f"Rare {slot.title()}"
        uniq_choices = sorted(
            uname for uname, u in uniques.items()
            if isinstance(u, dict) and gm.map_unique_slot(u.get("slot") or "") == slot
        )
        return ["(Empty)", rare_label] + uniq_choices

    def _current_item_label(self, item) -> str:
        if item is None or item.item_type == self._gear_module.ITEM_TYPE_NONE:
            return "(Empty)"
        return item.display_name

    def _refresh_gear_rows(self):
        """Rebuild the slot rows from self.current_build.gear."""
        for w in self.gear_rows_frame.winfo_children():
            w.destroy()
        self.gear_pickers = {}
        if not self.current_build or not self.current_build.gear:
            return
        gm = self._gear_module
        for slot in gm.ALL_SLOTS:
            item = self.current_build.gear.get(slot)
            row = ctk.CTkFrame(self.gear_rows_frame, fg_color=COLOR_PANEL,
                               corner_radius=6, height=44)
            row.pack(fill="x", padx=4, pady=3)
            row.pack_propagate(False)

            ctk.CTkLabel(row, text=slot, width=80, anchor="w",
                         font=(FONT_FAMILY, 12, "bold"),
                         text_color=COLOR_TEXT_DIM).pack(side="left", padx=(12, 6))

            tag_color = {gm.ITEM_TYPE_UNIQUE: "#AF6025",
                         gm.ITEM_TYPE_RARE: COLOR_HIGHLIGHT,
                         gm.ITEM_TYPE_NONE: COLOR_TEXT_DIM}.get(item.item_type if item else gm.ITEM_TYPE_NONE)
            tag_text = {gm.ITEM_TYPE_UNIQUE: "U",
                        gm.ITEM_TYPE_RARE: "R",
                        gm.ITEM_TYPE_NONE: "—"}.get(item.item_type if item else gm.ITEM_TYPE_NONE)
            ctk.CTkLabel(row, text=tag_text, width=20, font=(FONT_FAMILY, 11, "bold"),
                         text_color=tag_color).pack(side="left", padx=(0, 8))

            picker = FilterableCombobox(
                row,
                values=self._slot_choices(slot),
                default=self._current_item_label(item),
                width=360,
            )
            picker.pack(side="left", padx=(0, 6))
            self.gear_pickers[slot] = picker

            # Stat preview — first 2 mod lines if any
            preview = ""
            if item and item.stat_lines:
                preview = " | ".join(item.stat_lines[:2])
                if len(item.stat_lines) > 2:
                    preview += f"  (+{len(item.stat_lines) - 2})"
            ctk.CTkLabel(row, text=preview, anchor="w",
                         font=(FONT_FAMILY, 10), text_color=COLOR_TEXT_DIM
                         ).pack(side="left", padx=(8, 12), fill="x", expand=True)

        # Enable controls now that we have a build to act on
        self.gear_recompute_button.configure(state="normal")
        self.gear_reset_button.configure(state="normal")
        self.gear_status_label.configure(
            text="Tweak slots, then Recompute. DPS / Defense / QoL refresh; Optimizer is skipped.",
            text_color=COLOR_TEXT_DIM,
        )

    def _gear_from_pickers(self) -> dict:
        """Build a fresh {slot: GearItem} dict from current picker values."""
        gm = self._gear_module
        uniques = pob_data.load_uniques()
        new_gear: dict[str, "gear_module.GearItem"] = {}  # type: ignore[name-defined]
        for slot, picker in self.gear_pickers.items():
            val = picker.get().strip()
            if not val or val == "(Empty)":
                new_gear[slot] = gm.empty_item(slot)
            elif val.startswith("Rare "):
                new_gear[slot] = gm.gear_item_from_rare(slot)
            else:
                u = uniques.get(val)
                if u is None:
                    # Unknown text in entry — treat as empty.
                    new_gear[slot] = gm.empty_item(slot)
                else:
                    new_gear[slot] = gm.gear_item_from_unique(slot, u)
        return new_gear

    def on_gear_reset(self):
        if not self.current_build or self._template_gear is None:
            return
        # Apply the snapshot back to the live build and rebuild rows
        import copy
        self.current_build.gear = copy.deepcopy(self._template_gear)
        self._refresh_gear_rows()
        self.set_status("Gear reset to template. Click Recompute to apply.", COLOR_TEXT_DIM)

    def on_gear_recompute(self):
        if not self.current_build:
            return
        if self._gear_worker and self._gear_worker.is_alive():
            return
        self.current_build.gear = self._gear_from_pickers()
        self.gear_recompute_button.configure(state="disabled", text="Recomputing...")
        self.set_status("Recomputing with new gear...", COLOR_HIGHLIGHT)
        self._gear_worker = threading.Thread(
            target=self._run_gear_recompute, daemon=True,
        )
        self._gear_worker.start()

    def _run_gear_recompute(self):
        try:
            build = self.current_build
            dps_text = calc.compute_dps(build).report()
            ehp_text = defense.compute_ehp(build).report()
            qol_text = qol.compute_qol(build).report()
            self.after(0, self._apply_gear_recompute,
                       dps_text, ehp_text, qol_text, None)
        except Exception as exc:
            err = f"ERROR: {exc}\n\n{traceback.format_exc()}"
            self.after(0, self._apply_gear_recompute, err, err, err, str(exc))

    def _apply_gear_recompute(self, dps_text, ehp_text, qol_text, err):
        self.set_tab_text("DPS", dps_text)
        self.set_tab_text("Defense", ehp_text)
        self.set_tab_text("QoL", qol_text)
        self.gear_recompute_button.configure(state="normal", text="Recompute")
        if err:
            self.set_status(f"Recompute failed: {err}", COLOR_BAD)
        else:
            self.set_status("Gear updated. (Optimizer not re-run.)", COLOR_OK)

    def set_status(self, text: str, color: str = COLOR_TEXT_DIM):
        self.status_label.configure(text=text, text_color=color)
        self.update_idletasks()

    def set_tab_text(self, tab_name: str, text: str):
        tb = self.tab_text[tab_name]
        tb.configure(state="normal")
        tb.delete("1.0", "end")
        tb.insert("1.0", text)
        tb.configure(state="disabled")

    # ---- Calculate flow ----
    def on_calculate(self):
        if self._worker and self._worker.is_alive():
            self.set_status("Already calculating...", COLOR_WARN)
            return
        skill = self.skill_picker.get().strip()
        playstyle = self.playstyle_var.get().strip()
        if not skill:
            self.set_status("Pick a skill first.", COLOR_BAD)
            return
        if skill not in self.skill_choices:
            self.set_status(f"'{skill}' isn't a known skill. Try one from the dropdown.",
                            COLOR_BAD)
            return
        self._persist_settings()
        self.calc_button.configure(state="disabled", text="Calculating...")
        self.set_status(f"Fetching template for '{skill}'...", COLOR_HIGHLIGHT)
        for name in self.tab_names:
            self.set_tab_text(name, "...")
        self._worker = threading.Thread(
            target=self._run_pipeline, args=(skill, playstyle), daemon=True,
        )
        self._worker.start()

    def _run_pipeline(self, skill: str, playstyle: str):
        try:
            self._emit_status(f"Fetching template for '{skill}'...", COLOR_HIGHLIGHT)
            build, meta = ninja_template.template_for_skill(skill)
            build.playstyle = playstyle

            validation = validate(build)
            if not validation.valid:
                # Surface invalid-build state immediately so the user sees it
                # before the full calc completes.
                self._emit_status(
                    f"Build invalid: {validation.warnings[0] if validation.warnings else 'see Template tab'}",
                    COLOR_BAD,
                )

            buf = StringIO()
            with contextlib.redirect_stdout(buf):
                ninja_template.print_template_summary(build, meta)
                print()
                print(validation.report())
            template_text = buf.getvalue()

            if validation.valid:
                self._emit_status("Computing DPS / Defense / QoL...", COLOR_HIGHLIGHT)
            dps_text = calc.compute_dps(build).report()
            ehp_text = defense.compute_ehp(build).report()
            qol_text = qol.compute_qol(build).report()

            opt_buf = StringIO()
            with contextlib.redirect_stdout(opt_buf):
                baseline_invalid = not validation.valid
                if validation.valid:
                    self._emit_status("Optimizing: aura swaps...", COLOR_HIGHLIGHT)
                print("=" * 60)
                print(" Single-aura swaps")
                print("=" * 60)
                aura_swaps = optimizer.enumerate_aura_swaps(build, include_invalid=True)
                optimizer.print_top_swaps(aura_swaps, n=8)

                if validation.valid:
                    self._emit_status("Optimizing: aura subsets...", COLOR_HIGHLIGHT)
                print()
                print("=" * 60)
                print(" Multi-aura subsets (sizes 1, 2, 3)")
                print("=" * 60)
                aura_subsets = optimizer.enumerate_aura_subsets(
                    build, sizes=(1, 2, 3), valid_only=True
                )
                optimizer.print_top_subsets(
                    aura_subsets, n=6, baseline_was_invalid=baseline_invalid
                )

                print()
                if validation.valid:
                    self._emit_status("Optimizing: support swaps...", COLOR_HIGHLIGHT)
                print("=" * 60)
                print(" Support-gem swaps")
                print("=" * 60)
                support_swaps = optimizer.enumerate_support_swaps(
                    build, include_invalid=True
                )
                optimizer.print_top_swaps(support_swaps, n=8)
            opt_text = opt_buf.getvalue()

            cache_tag = {
                "hit": " [cached]",
                "miss": " [fresh fetch]",
                "bypass": " [no cache]",
            }.get(meta.get("cache_status", ""), "")
            if validation.valid:
                final_msg = f"Done. Sample size: {meta['total_characters_in_sample']} characters.{cache_tag}"
                final_color = COLOR_OK
            else:
                warn = validation.warnings[0] if validation.warnings else "see Template tab"
                final_msg = f"Done (build INVALID — {warn}).{cache_tag}"
                final_color = COLOR_BAD
            self.after(0, self._update_ui,
                       template_text, dps_text, ehp_text, qol_text, opt_text,
                       final_msg, final_color, build)
        except Exception as exc:
            err = f"ERROR: {exc}\n\n{traceback.format_exc()}"
            self.after(0, self._update_ui,
                       err, err, err, err, err,
                       f"Failed: {exc}", COLOR_BAD)

    def _update_ui(self, template_text, dps_text, ehp_text, qol_text, opt_text,
                   status_text, status_color, build=None):
        self.set_tab_text("Template", template_text)
        self.set_tab_text("DPS", dps_text)
        self.set_tab_text("Defense", ehp_text)
        self.set_tab_text("QoL", qol_text)
        self.set_tab_text("Optimizer", opt_text)
        self.set_status(status_text, status_color)
        self.calc_button.configure(state="normal", text="Calculate")
        if build is not None:
            self.current_build = build
            # Snapshot template gear for "Reset to template"
            import copy
            self._template_gear = copy.deepcopy(build.gear)
            self._refresh_gear_rows()


def main():
    app = PoEBuilderApp()
    app.mainloop()


if __name__ == "__main__":
    main()
