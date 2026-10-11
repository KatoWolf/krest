"""Lightweight theme gallery, without continuous decorative rendering."""

import tkinter as tk
from ui_fonts import UI_FONT


class AppearancePages:
    def build_appearance(self):
        page = self.page_bodies["appearance"]
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
            cell.grid(row=i // 3, column=i % 3, sticky="ew", padx=(0, 8), pady=(0, 8))
            grid.columnconfigure(i % 3, weight=1, uniform="themes")
            bar = tk.Canvas(cell, height=18, bg=palette["sidebar"], highlightthickness=0)
            bar.pack(fill="x")
            for n, key in enumerate(("accent", "text", "sub_text")):
                bar.create_oval(3 + n * 25, 2, 17 + n * 25, 16, fill=palette[key], outline="")
            tk.Label(
                cell,
                text=name + (" · Premium" if name in PREMIUM_THEMES else ""),
                bg=palette["sidebar"],
                fg=palette["text"],
                font=(UI_FONT, 9, "bold"),
            ).pack(anchor="w", pady=6)
            button = tk.Button(
                cell,
                text="Выбрать",
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
