"""Lightweight theme gallery, without continuous decorative rendering."""

import tkinter as tk
from tkinter import ttk
from ui_fonts import UI_FONT


class AppearancePages:
    def build_appearance(self):
        page = self.page_bodies["appearance"]
        from ui_relic import RelicEmblem, wordmark, theme_preview

        identity = self.card(
            page, "Реликт", "Холодный металл, спокойный фиолетовый и тонкие линии шипов."
        )
        RelicEmblem(identity, self.theme, 110).pack(side="right", padx=12)
        wordmark(identity, self.theme, 220).pack(anchor="w", pady=(10, 16))
        edge_var = tk.BooleanVar(
            value=self.config_mgr.snapshot()["accessibility"].get("relic_edges", True)
        )

        def toggle_edges():
            cfg = self.config_mgr.snapshot()
            cfg["accessibility"]["relic_edges"] = edge_var.get()
            self.config_mgr.save_config(cfg)
            # Rebuild with the current form state, as a normal theme change does.
            self.apply_theme(self.theme_var.get())

        ttk.Checkbutton(
            identity, text="Лозы по краям окна", variable=edge_var, command=toggle_edges
        ).pack(anchor="w")
        card = self.card(
            page,
            "Оформление",
            "Выберите палитру. Цвета применятся ко всем окнам Крест.",
        )
        from themes import THEMES, PREMIUM_THEMES
        from ui_design import contrasting_text

        grid = tk.Frame(card, bg=card.cget("bg"))
        grid.pack(fill="x", pady=(0, 10))
        for i, (name, palette) in enumerate(THEMES.items()):
            cell = tk.Frame(
                grid,
                bg=palette["sidebar"],
                padx=12,
                pady=12,
                highlightbackground=palette["border"],
                highlightthickness=1,
            )
            cell.grid(row=i // 2, column=i % 2, sticky="ew", padx=(0, 8), pady=(0, 8))
            grid.columnconfigure(i % 2, weight=1, uniform="themes")
            bar = theme_preview(cell, palette)
            bar.pack(fill="x")
            tk.Label(
                cell,
                text=name + (" · Premium" if name in PREMIUM_THEMES else ""),
                bg=palette["sidebar"],
                fg=palette["text"],
                font=(UI_FONT, 11, "bold"),
            ).pack(anchor="w", pady=6)
            button = tk.Button(
                cell,
                text="Выбрана" if self.theme_var.get() == name else "Выбрать",
                bg=palette["accent"],
                fg=contrasting_text(palette["accent"]),
                activebackground=palette["accent_hover"],
                activeforeground=contrasting_text(palette["accent_hover"]),
                relief="flat",
                bd=0,
                padx=10,
                pady=5,
                cursor="hand2",
                highlightthickness=0,
                command=lambda n=name: self.apply_theme(n),
            )
            button.pack(anchor="w")
