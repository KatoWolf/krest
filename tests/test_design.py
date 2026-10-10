import os
import tkinter as tk
from tkinter import ttk

import pytest

from themes import THEMES
from ui_design import apply_style, RoundedCard, Disclosure

pytestmark = pytest.mark.skipif(not os.getenv("DISPLAY"), reason="GUI requires an X display")


@pytest.mark.parametrize("name", list(THEMES))
def test_hover_focus_and_dropdown_colours_follow_each_theme(name):
    root = tk.Tk()
    try:
        colours = THEMES[name]
        style = apply_style(root, colours)
        combo = ttk.Combobox(root, values=["Первый", "Второй"], state="readonly")
        combo.pack()
        root.update()
        for state in (("readonly", "active"), ("readonly", "focus"), ("active",), ("focus",)):
            assert style.lookup("TCombobox", "fieldbackground", state) == colours["sidebar"]
            assert style.lookup("TCombobox", "foreground", state) == colours["text"]
        for element in ("TCheckbutton", "TRadiobutton"):
            assert style.lookup(element, "background", ("active",)) == colours["card"]
            assert style.lookup(element, "foreground", ("active",)) == colours["text"]
        assert style.lookup("Treeview.Heading", "background", ("active",)) == colours["border"]
        root.tk.call("ttk::combobox::Post", str(combo))
        popup = root.tk.call("ttk::combobox::PopdownWindow", str(combo))
        listbox = str(popup) + ".f.l"
        assert root.tk.call(listbox, "cget", "-background") == colours["card"]
        assert root.tk.call(listbox, "cget", "-foreground") == colours["text"]
        root.tk.call("ttk::combobox::Unpost", str(combo))
    finally:
        root.destroy()


def test_cards_size_to_content_and_disclosures_expand_without_clipping():
    root = tk.Tk()
    try:
        root.geometry("700x600")
        colours = THEMES["Amethyst Modern"]
        apply_style(root, colours)
        card = RoundedCard(root, colours)
        card.pack(fill="x")
        ttk.Entry(card.inner).pack(fill="x")
        details = Disclosure(card.inner, "Параметры", colours)
        entry = ttk.Entry(details.body)
        entry.pack(fill="x", pady=20)
        root.update()
        collapsed = card.winfo_height()
        assert collapsed > 60 and not entry.winfo_ismapped()
        details.toggle()
        root.update()
        assert entry.winfo_ismapped() and card.winfo_height() > collapsed
        assert card.winfo_height() >= card.inner.winfo_reqheight() + 2 * card.padding
        details.toggle()
        root.update()
        assert card.winfo_height() == collapsed
    finally:
        root.destroy()


def test_indicator_has_no_idle_animation_and_stops_after_pulse():
    from guikrest import CrossOverlayController

    root = tk.Tk()
    indicator = CrossOverlayController()
    try:
        indicator.attach(root)
        assert indicator.job is None
        indicator.pulse(0.05)
        assert indicator.job is not None
        root.after(120, root.quit)
        root.mainloop()
        assert indicator.job is None
        assert indicator.root.state() == "withdrawn"
    finally:
        indicator.stop()
        root.destroy()
