"""Bounded decorative particles, paused when hidden, gaming or motion is reduced."""

import math
import os
import random
import time
import tkinter as tk
from tkinter import ttk
from game_overlay import TRANSPARENT, passthrough, move_overlay

EFFECTS = {
    "Выключены": "off",
    "Снег": "snow",
    "Кровавые потёки": "blood",
    "Тлеющие искры": "embers",
    "Звёздная пыль": "stars",
}


class ParticleScene:
    def __init__(self, canvas):
        self.canvas = canvas
        self.particles = []
        self.mode = None
        self.size = None
        self.random = random.Random()

    def clear(self):
        self.canvas.delete("particles")
        self.particles = []
        self.mode = None

    def draw(self, mode, intensity, dt=0.08, border_only=False):
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if width < 4 or height < 4:
            return
        count = (8, 16, 26)[max(0, min(2, intensity - 1))]
        if self.mode != mode or self.size != (width, height) or len(self.particles) != count:
            self.clear()
            self.mode = mode
            self.size = (width, height)
            for _ in range(count):
                x = self.random.uniform(4, width - 4)
                if border_only and self.random.random() < 0.75:
                    x = self.random.choice(
                        [
                            self.random.uniform(4, 22),
                            self.random.uniform(max(4, width - 22), width - 4),
                        ]
                    )
                self.particles.append(
                    {
                        "x": x,
                        "y": self.random.uniform(-height, height),
                        "speed": self.random.uniform(10, 32),
                        "r": self.random.uniform(1, 2.8),
                        "phase": self.random.uniform(0, 6.28),
                    }
                )
        self.canvas.delete("particles")
        for p in self.particles:
            p["y"] += p["speed"] * dt * (-1 if mode == "embers" else 1)
            p["phase"] += dt
            if p["y"] > height + 35:
                p["y"] = -35
                p["x"] = self.random.uniform(4, width - 4)
            if p["y"] < -height:
                p["y"] = height + 15
            x = p["x"] + math.sin(p["phase"]) * 5
            y = p["y"]
            r = p["r"]
            if border_only and 28 < x < width - 28 and y > 36:
                continue
            if mode == "blood":
                length = 12 + p["r"] * 9
                self.canvas.create_line(
                    x,
                    y - length,
                    x,
                    y,
                    fill="#8f203d",
                    width=r + 1,
                    capstyle="round",
                    tags="particles",
                )
                self.canvas.create_oval(
                    x - r, y - r, x + r, y + r, fill="#dc4161", outline="", tags="particles"
                )
            else:
                color = (
                    "#dcecf8" if mode == "snow" else "#ffac65" if mode == "embers" else "#b2a6ef"
                )
                self.canvas.create_oval(
                    x - r, y - r, x + r, y + r, fill=color, outline="", tags="particles"
                )
                if mode == "stars" and math.sin(p["phase"]) > 0.6:
                    self.canvas.create_line(x - 4, y, x + 4, y, fill=color, tags="particles")


class MenuEffects:
    def __init__(self, app, header):
        self.app = app
        self.header = header
        self.scenes = [ParticleScene(header)]
        self.window = None
        self.job = None
        self.closed = False
        if os.name == "nt":
            self.window = tk.Toplevel(app)
            self.window.withdraw()
            self.window.overrideredirect(True)
            self.window.configure(bg=TRANSPARENT)
            self.window.attributes("-transparentcolor", TRANSPARENT)
            canvas = tk.Canvas(self.window, bg=TRANSPARENT, highlightthickness=0)
            canvas.pack(fill="both", expand=True)
            self.scenes.append(ParticleScene(canvas))
        self.last = time.monotonic()
        self.refresh()

    def add_preview(self, canvas):
        self.scenes.append(ParticleScene(canvas))
        self.refresh()

    def config(self):
        return {
            "mode": EFFECTS.get(self.app.effect_mode.get(), "off"),
            "intensity": int(self.app.effect_intensity.get()),
            "pause_in_game": self.app.effect_pause.get(),
        }

    def refresh(self):
        if self.closed:
            return
        if self.job:
            self.app.after_cancel(self.job)
            self.job = None
        for scene in self.scenes:
            if scene.canvas.winfo_exists():
                scene.clear()
        if self.window:
            self.window.withdraw()
        if self.config()["mode"] != "off":
            self.tick()

    def tick(self):
        self.job = None
        if self.closed or not self.header.winfo_exists():
            return
        cfg = self.config()
        if cfg["mode"] == "off":
            return
        reduced = getattr(self.app, "reduce_motion", None)
        motion = not (
            reduced.get()
            if reduced
            else self.app.config_mgr.snapshot()["accessibility"]["reduce_motion"]
        )
        active = self.app.state() == "normal" and self.app.winfo_viewable()
        game = self.app.runtime.performance.active == "game"
        paused = not motion or not active or (cfg["pause_in_game"] and game)
        now = time.monotonic()
        dt = min(0.15, now - self.last)
        self.last = now
        if paused:
            for scene in self.scenes:
                if scene.canvas.winfo_exists():
                    scene.clear()
            if self.window:
                self.window.withdraw()
        else:
            if self.window:
                if self.app.focus_displayof() is not None:
                    if not self.window.winfo_viewable():
                        self.window.update_idletasks()
                        passthrough(self.window)
                        self.window.deiconify()
                    move_overlay(
                        self.window,
                        self.app.winfo_rootx(),
                        self.app.winfo_rooty(),
                        self.app.winfo_width(),
                        self.app.winfo_height(),
                    )
                else:
                    self.window.withdraw()
            for scene in self.scenes:
                if scene.canvas.winfo_exists() and scene.canvas.winfo_viewable():
                    scene.draw(
                        cfg["mode"],
                        cfg["intensity"],
                        dt,
                        border_only=self.window is not None and scene is self.scenes[1],
                    )
        self.job = self.app.after(700 if paused else 80, self.tick)

    def close(self):
        self.closed = True
        if self.job:
            try:
                self.app.after_cancel(self.job)
            except tk.TclError:
                pass
            self.job = None
        if self.window:
            try:
                self.window.destroy()
            except tk.TclError:
                pass


class AppearancePages:
    def init_appearance(self):
        cfg = self.config_mgr.snapshot()["visual_effects"]
        self.effect_mode = tk.StringVar(
            self, value=next(k for k, v in EFFECTS.items() if v == cfg["mode"])
        )
        self.effect_intensity = tk.StringVar(self, value=str(cfg["intensity"]))
        self.effect_pause = tk.BooleanVar(self, value=cfg["pause_in_game"])
        self.effects = None
        for var in (self.effect_mode, self.effect_intensity, self.effect_pause):
            var.trace_add("write", self._effects_changed)

    def _effects_changed(self, *_):
        self._mark_dirty()
        if self.effects:
            self.effects.refresh()

    def build_appearance(self):
        page = self.page_bodies["appearance"]
        card = self.card(
            page,
            "Атмосфера Крест",
            "Спокойная, снежная или тёмная — выберите оформление под своё настроение.",
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
                font=("Segoe UI", 9, "bold"),
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
        card = self.card(
            page,
            "Движение и эффекты",
            "Лёгкая анимация в оформлении. Эффекты остаются по краям и не перехватывают нажатия.",
        )
        row = tk.Frame(card, bg=card.cget("bg"))
        row.pack(fill="x", pady=8)
        ttk.Combobox(
            row, textvariable=self.effect_mode, values=list(EFFECTS), state="readonly", width=23
        ).pack(side="left", padx=(0, 12))
        self.label(row, "Интенсивность", muted=True).pack(side="left", padx=(0, 8))
        ttk.Combobox(
            row,
            textvariable=self.effect_intensity,
            values=["1", "2", "3"],
            state="readonly",
            width=5,
        ).pack(side="left")
        ttk.Checkbutton(
            card, text="Приостанавливать эффекты в игровом профиле", variable=self.effect_pause
        ).pack(anchor="w", pady=6)
        preview = tk.Canvas(card, height=160, bg=self.theme["sidebar"], highlightthickness=0)
        preview.pack(fill="x", pady=12)
        preview.create_text(
            24,
            75,
            anchor="w",
            text="КРЕСТ  /  ВАША АТМОСФЕРА",
            fill=self.theme["sub_text"],
            font=("Segoe UI", 14, "bold"),
        )
        if self.effects:
            self.effects.add_preview(preview)
        self.label(
            card,
            "«Уменьшить движение» в настройках системы останавливает эффекты. Изменения сохраняются общей кнопкой «Сохранить».",
            muted=True,
            wraplength=650,
        ).pack(anchor="w")

    def effects_draft(self):
        return {
            "mode": EFFECTS[self.effect_mode.get()],
            "intensity": int(self.effect_intensity.get()),
            "pause_in_game": self.effect_pause.get(),
        }
