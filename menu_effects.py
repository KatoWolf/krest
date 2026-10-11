"""Theme gallery. Decorative particle effects have been retired."""

from ui_fonts import UI_FONT
import tkinter as tk


class AppearancePages:
    def build_appearance(self):
        page = self.page_bodies["appearance"]
        card = self.card(
            page,
            "Палитра вашего пространства",
            "Девять согласованных палитр для страниц, полей и диалогов. Тема применяется сразу; черновики сохраняются.",
        )
        from themes import THEMES, PREMIUM_THEMES
        from ui_design import contrasting_text

        grid = tk.Frame(card, bg=card.cget("bg"))
        grid.pack(fill="x", pady=(0, 10))
        cells = []
        for i, (name, palette) in enumerate(THEMES.items()):
            cell = tk.Frame(
                grid,
                bg=palette["sidebar"],
                padx=12,
                pady=12,
                highlightbackground=palette["border"],
                highlightthickness=1,
            )
            cells.append(cell)
            cell.grid(row=i // 3, column=i % 3, sticky="nsew", padx=(0, 8), pady=(0, 8))
            grid.columnconfigure(i % 3, weight=1, uniform="themes")
            bar = tk.Canvas(cell, width=80, height=18, bg=palette["sidebar"], highlightthickness=0)
            bar.pack(fill="x")
            for n, key in enumerate(("accent", "text", "sub_text")):
                bar.create_oval(3 + n * 25, 2, 17 + n * 25, 16, fill=palette[key], outline="")
            tk.Label(
                cell,
                text=name + (" · Premium" if name in PREMIUM_THEMES else ""),
                bg=palette["sidebar"],
                fg=palette["text"],
                font=(UI_FONT, 9, "bold"),
                wraplength=180,
                justify="left",
            ).pack(anchor="w", pady=6)
            button = tk.Button(
                cell,
                text="Выбрана" if name == self.theme_var.get() else "Выбрать",
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

        def reflow(event):
            columns = max(1, min(3, event.width // 230))
            if getattr(grid, "_columns", None) == columns:
                return
            grid._columns = columns
            for column in range(3):
                grid.columnconfigure(
                    column,
                    weight=1 if column < columns else 0,
                    uniform="themes" if column < columns else "",
                )
            for index, cell in enumerate(cells):
                cell.grid_configure(row=index // columns, column=index % columns)

        grid.bind("<Configure>", reflow)
