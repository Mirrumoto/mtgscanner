"""
gui.py — Tkinter GUI for MTG binder scanner.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
import threading
import json
import hashlib
import os
from io import BytesIO

import requests
from PIL import Image, ImageTk

import scanner_engine

GEMINI_TIER_MODEL_MAP = {
    "2.5": "gemini-2.5-flash",
    "3": "gemini-3-flash-preview",
}


class MTGScannerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("MTG Binder Scanner")
        self.root.geometry("980x720")
        self.scanning = False
        self.scan_thread = None
        self.cancel_event = None
        self.last_scan_output: Path | None = None
        self.collection_view_var = tk.StringVar(value="list")
        self.collection_rows: list[dict] = []
        self.collection_image_cache: dict[str, ImageTk.PhotoImage] = {}
        self.collection_photo_refs: list[ImageTk.PhotoImage] = []
        self.thumbnail_cache_dir = self._resolve_thumbnail_cache_dir()
        self.collection_sort_state: dict[str, bool] = {
            "Card": False,
            "Count": False,
            "Set": False,
            "Rarity": False,
            "Finish": False,
            "Price": True,
        }

        self._configure_styles()
        self._build_ui()

    def _configure_styles(self):
        """Configure a clean, modern ttk style."""
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("TNotebook", tabmargins=(8, 8, 8, 0))
        style.configure("TNotebook.Tab", padding=(16, 8), font=("Segoe UI", 10, "bold"))
        style.configure("TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Treeview", rowheight=28, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("Accent.TLabel", font=("Segoe UI", 18, "bold"), foreground="#1f7a3a")

    def _build_ui(self):
        """Construct the GUI layout with two tabs: Scanner and Collection."""
        # Create notebook (tabs)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Tab 1: Scanner
        self.scanner_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.scanner_tab, text="Scanner")
        self._build_scanner_tab()

        # Tab 2: Collection Viewer
        self.collection_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.collection_tab, text="Collection")
        self._build_collection_tab()

    def _build_scanner_tab(self):
        """Build the scanner control tab."""
        main_frame = ttk.Frame(self.scanner_tab, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(4, weight=1)

        # ── Folder Selection ──────────────────────────────────────────────
        folder_frame = ttk.LabelFrame(main_frame, text="Image Folder", padding="10")
        folder_frame.pack(fill=tk.X, padx=5, pady=5)
        folder_frame.columnconfigure(1, weight=1)

        self.folder_var = tk.StringVar()
        ttk.Entry(folder_frame, textvariable=self.folder_var, state="readonly").grid(
            row=0, column=1, sticky=(tk.W, tk.E), padx=5
        )
        ttk.Button(folder_frame, text="Browse", command=self._pick_folder).grid(
            row=0, column=2, padx=5
        )

        # ── Settings Frame ────────────────────────────────────────────────
        settings_frame = ttk.LabelFrame(main_frame, text="Settings", padding="10")
        settings_frame.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(settings_frame, text="Provider:").grid(row=0, column=0, sticky=tk.W)
        self.provider_var = tk.StringVar(value="openai")
        provider_combo = ttk.Combobox(
            settings_frame,
            textvariable=self.provider_var,
            values=["openai", "gemini"],
            state="readonly",
            width=15,
        )
        provider_combo.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=5)
        provider_combo.bind("<<ComboboxSelected>>", self._on_provider_changed)

        ttk.Label(settings_frame, text="Model:").grid(row=1, column=0, sticky=tk.W)
        self.model_var = tk.StringVar()
        self.model_combo = ttk.Combobox(
            settings_frame,
            textvariable=self.model_var,
            state="readonly",
            width=15,
        )
        self.model_combo.grid(row=1, column=1, sticky=(tk.W, tk.E), padx=5)
        self._update_model_combo()

        ttk.Label(settings_frame, text="Output File:").grid(row=2, column=0, sticky=tk.W)
        self.output_var = tk.StringVar(value="cards.json")
        ttk.Entry(settings_frame, textvariable=self.output_var).grid(
            row=2, column=1, sticky=(tk.W, tk.E), padx=5
        )

        # ── Debug checkbox ────────────────────────────────────────────────
        self.debug_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(settings_frame, text="Show Debug Messages", variable=self.debug_var).grid(
            row=3, column=0, columnspan=2, sticky=tk.W, pady=5
        )

        # ── Output Frame ──────────────────────────────────────────────────
        output_frame = ttk.LabelFrame(main_frame, text="Scan Output", padding="10")
        output_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        output_frame.columnconfigure(0, weight=1)
        output_frame.rowconfigure(0, weight=1)

        scrollbar = ttk.Scrollbar(output_frame)
        scrollbar.grid(row=0, column=1, sticky=(tk.N, tk.S))

        self.output_text = tk.Text(
            output_frame, height=15, width=80, yscrollcommand=scrollbar.set, state="disabled"
        )
        self.output_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        scrollbar.config(command=self.output_text.yview)

        # ── Button Frame ──────────────────────────────────────────────────
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill=tk.X, padx=5, pady=10)

        self.start_button = ttk.Button(
            button_frame, text="Start Scan", command=self._start_scan
        )
        self.start_button.pack(side=tk.LEFT, padx=5)

        self.cancel_button = ttk.Button(
            button_frame, text="Cancel", command=self._cancel_scan, state="disabled"
        )
        self.cancel_button.pack(side=tk.LEFT, padx=5)

        ttk.Button(button_frame, text="Clear Output", command=self._clear_output).pack(
            side=tk.LEFT, padx=5
        )

    def _build_collection_tab(self):
        """Build the collection viewer tab."""
        main_frame = ttk.Frame(self.collection_tab, padding="14")
        main_frame.pack(fill=tk.BOTH, expand=True)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(2, weight=1)

        # Top summary card
        header_frame = ttk.LabelFrame(main_frame, text="Collection Value", padding="12")
        header_frame.pack(fill=tk.X, padx=4, pady=(2, 8))

        ttk.Label(header_frame, text="Total Price", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        self.total_value_label = ttk.Label(header_frame, text="$0.00", style="Accent.TLabel")
        self.total_value_label.pack(side=tk.LEFT, padx=(10, 0))

        # Controls
        control_frame = ttk.Frame(main_frame)
        control_frame.pack(fill=tk.X, padx=4, pady=(0, 8))
        control_frame.columnconfigure(1, weight=1)

        ttk.Label(control_frame, text="JSON File:").grid(row=0, column=0, sticky=tk.W, padx=(0, 8))
        self.collection_file_var = tk.StringVar()
        ttk.Entry(control_frame, textvariable=self.collection_file_var, state="readonly").grid(
            row=0, column=1, sticky=(tk.W, tk.E), padx=(0, 8)
        )
        ttk.Button(control_frame, text="Browse", command=self._pick_collection_file).grid(
            row=0, column=2, padx=(0, 6)
        )
        ttk.Button(control_frame, text="Load", command=self._load_collection).grid(row=0, column=3, padx=(0, 6))
        ttk.Button(control_frame, text="Refresh", command=self._load_collection).grid(row=0, column=4)
        ttk.Radiobutton(
            control_frame,
            text="List",
            value="list",
            variable=self.collection_view_var,
            command=self._on_collection_view_changed,
        ).grid(row=0, column=5, padx=(16, 4))
        ttk.Radiobutton(
            control_frame,
            text="Grid",
            value="grid",
            variable=self.collection_view_var,
            command=self._on_collection_view_changed,
        ).grid(row=0, column=6, padx=(0, 4))

        # Collection Views
        self.collection_content_frame = ttk.Frame(main_frame)
        self.collection_content_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.collection_content_frame.columnconfigure(0, weight=1)
        self.collection_content_frame.rowconfigure(0, weight=1)

        table_frame = ttk.Frame(self.collection_content_frame)
        table_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.collection_table_frame = table_frame

        columns = ("Card", "Count", "Set", "Rarity", "Finish", "Price")
        self.collection_tree = ttk.Treeview(table_frame, columns=columns, height=20, show="headings")

        # Define column headings and widths
        self.collection_tree.heading("Card", text="Card", command=lambda: self._sort_collection_by("Card"))
        self.collection_tree.heading("Count", text="Count", command=lambda: self._sort_collection_by("Count"))
        self.collection_tree.heading("Set", text="Set", command=lambda: self._sort_collection_by("Set"))
        self.collection_tree.heading("Rarity", text="Rarity", command=lambda: self._sort_collection_by("Rarity"))
        self.collection_tree.heading("Finish", text="Finish", command=lambda: self._sort_collection_by("Finish"))
        self.collection_tree.heading("Price", text="Price (USD)", command=lambda: self._sort_collection_by("Price"))

        self.collection_tree.column("Card", width=300, anchor=tk.W)
        self.collection_tree.column("Count", width=65, anchor=tk.CENTER)
        self.collection_tree.column("Set", width=90, anchor=tk.CENTER)
        self.collection_tree.column("Rarity", width=130, anchor=tk.CENTER)
        self.collection_tree.column("Finish", width=120, anchor=tk.CENTER)
        self.collection_tree.column("Price", width=140, anchor=tk.E)

        # Add scrollbars
        vsb = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.collection_tree.yview)
        hsb = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.collection_tree.xview)
        self.collection_tree.configure(yscroll=vsb.set, xscroll=hsb.set)

        # Grid layout with scrollbars
        self.collection_tree.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        vsb.grid(row=0, column=1, sticky=(tk.N, tk.S))
        hsb.grid(row=1, column=0, sticky=(tk.W, tk.E))

        self.collection_grid_frame = ttk.Frame(self.collection_content_frame)
        self.collection_grid_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        self.collection_grid_frame.columnconfigure(0, weight=1)
        self.collection_grid_frame.rowconfigure(0, weight=1)

        self.collection_grid_canvas = tk.Canvas(self.collection_grid_frame, highlightthickness=0)
        self.collection_grid_canvas.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        self.collection_grid_scrollbar = ttk.Scrollbar(
            self.collection_grid_frame,
            orient=tk.VERTICAL,
            command=self.collection_grid_canvas.yview,
        )
        self.collection_grid_scrollbar.grid(row=0, column=1, sticky=(tk.N, tk.S))
        self.collection_grid_canvas.configure(yscrollcommand=self.collection_grid_scrollbar.set)

        self.collection_grid_inner = ttk.Frame(self.collection_grid_canvas)
        self.collection_grid_window = self.collection_grid_canvas.create_window(
            (0, 0),
            window=self.collection_grid_inner,
            anchor="nw",
        )
        self.collection_grid_inner.bind("<Configure>", self._on_collection_grid_configure)
        self.collection_grid_canvas.bind("<Configure>", self._on_collection_grid_canvas_configure)
        self._bind_collection_grid_mousewheel(self.collection_grid_canvas)
        self._bind_collection_grid_mousewheel(self.collection_grid_inner)

        self.collection_grid_frame.grid_remove()

        # Summary Footer
        footer_frame = ttk.Frame(main_frame)
        footer_frame.pack(fill=tk.X, padx=4, pady=(8, 4))
        self.summary_label = ttk.Label(
            footer_frame,
            text="Unique Cards: 0 | Total Copies: 0",
            font=("Segoe UI", 9),
        )
        self.summary_label.pack(side=tk.LEFT)

        initial_file = self._resolve_initial_cards_file()
        if initial_file:
            self.collection_file_var.set(str(initial_file))

    def _update_collection_headings(self, active_column: str | None = None):
        """Refresh heading labels and show sort direction on active column."""
        heading_labels = {
            "Card": "Card",
            "Count": "Count",
            "Set": "Set",
            "Rarity": "Rarity",
            "Finish": "Finish",
            "Price": "Price (USD)",
        }
        for column, label in heading_labels.items():
            heading_text = label
            if column == active_column:
                heading_text = f"{label} {'▼' if self.collection_sort_state[column] else '▲'}"
            self.collection_tree.heading(
                column,
                text=heading_text,
                command=lambda col=column: self._sort_collection_by(col),
            )

    def _on_collection_view_changed(self):
        """Switch between list and visual grid views."""
        self._render_collection_view()

    def _on_collection_grid_configure(self, event=None):
        """Keep canvas scrollregion in sync with the grid contents."""
        self.collection_grid_canvas.configure(scrollregion=self.collection_grid_canvas.bbox("all"))

    def _on_collection_grid_canvas_configure(self, event):
        """Stretch the inner grid container to the visible canvas width."""
        self.collection_grid_canvas.itemconfigure(self.collection_grid_window, width=event.width)
        if self.collection_view_var.get() == "grid" and self.collection_rows:
            self._render_collection_grid()

    def _bind_collection_grid_mousewheel(self, widget):
        """Bind mouse wheel scrolling for the collection grid canvas and tiles."""
        widget.bind("<MouseWheel>", self._on_collection_grid_mousewheel)
        widget.bind("<Button-4>", self._on_collection_grid_mousewheel)
        widget.bind("<Button-5>", self._on_collection_grid_mousewheel)

    def _on_collection_grid_mousewheel(self, event):
        """Scroll the collection grid with the mouse wheel."""
        if self.collection_view_var.get() != "grid":
            return None

        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        else:
            raw_delta = int(getattr(event, "delta", 0) or 0)
            if raw_delta == 0:
                return "break"
            delta = -1 * int(raw_delta / 120) if abs(raw_delta) >= 120 else (-1 if raw_delta > 0 else 1)

        self.collection_grid_canvas.yview_scroll(delta, "units")
        return "break"

    def _get_finish_display(self, raw_finish: str) -> str:
        raw_finish = str(raw_finish or "unknown").strip().lower()
        return {"foil": "Foil", "nonfoil": "Non-foil"}.get(raw_finish, "Unknown")

    def _resolve_thumbnail_cache_dir(self) -> Path:
        """Choose a persistent thumbnail cache location and ensure it exists."""
        local_appdata = os.getenv("LOCALAPPDATA")
        if local_appdata:
            cache_dir = Path(local_appdata) / "MTGBinderScanner" / "thumb_cache"
        else:
            cache_dir = Path.home() / ".mtg_binder_scanner" / "thumb_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir

    def _get_thumbnail_cache_path(self, image_url: str) -> Path:
        """Map an image URL to a stable on-disk thumbnail cache path."""
        digest = hashlib.sha256(image_url.encode("utf-8")).hexdigest()
        return self.thumbnail_cache_dir / f"{digest}.jpg"

    def _load_cached_thumbnail_image(self, cache_path: Path) -> Image.Image | None:
        """Load a cached thumbnail image from disk if present and valid."""
        if not cache_path.exists() or not cache_path.is_file():
            return None
        try:
            with Image.open(cache_path) as cached_image:
                return cached_image.convert("RGB")
        except Exception:
            try:
                cache_path.unlink(missing_ok=True)
            except OSError:
                pass
            return None

    def _write_cached_thumbnail_image(self, cache_path: Path, image: Image.Image) -> None:
        """Persist a thumbnail image to disk for reuse across launches."""
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            image.save(cache_path, format="JPEG", quality=88, optimize=True)
        except Exception:
            return

    def _extract_image_url(self, card_info: dict) -> str | None:
        """Pick the best available Scryfall image URL for collection display."""
        image_uris = card_info.get("image_uris") or {}
        if not isinstance(image_uris, dict):
            return None
        for key in ("small", "normal", "large", "png"):
            value = image_uris.get(key)
            if value:
                return str(value)
        return None

    def _build_placeholder_thumbnail(self, size: tuple[int, int] = (146, 204)) -> ImageTk.PhotoImage:
        """Create a simple placeholder image when no card art is available."""
        image = Image.new("RGB", size, color="#e6e6e6")
        return ImageTk.PhotoImage(image)

    def _get_card_thumbnail(self, image_url: str | None) -> ImageTk.PhotoImage:
        """Fetch and cache a thumbnail for a collection card."""
        cache_key = image_url or "__placeholder__"
        cached = self.collection_image_cache.get(cache_key)
        if cached is not None:
            return cached

        if not image_url:
            photo = self._build_placeholder_thumbnail()
            self.collection_image_cache[cache_key] = photo
            return photo

        try:
            cache_path = self._get_thumbnail_cache_path(image_url)
            image = self._load_cached_thumbnail_image(cache_path)

            if image is None:
                response = requests.get(image_url, timeout=10)
                response.raise_for_status()
                with Image.open(BytesIO(response.content)) as downloaded_image:
                    image = downloaded_image.convert("RGB")
                image.thumbnail((146, 204), Image.Resampling.LANCZOS)
                self._write_cached_thumbnail_image(cache_path, image)

            image.thumbnail((146, 204), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(image)
        except Exception:
            photo = self._build_placeholder_thumbnail()

        self.collection_image_cache[cache_key] = photo
        return photo

    def _precache_collection_images(self):
        """Warm the in-memory thumbnail cache for the current collection."""
        seen_urls: set[str | None] = set()
        for row in self.collection_rows:
            image_url = row.get("image_url")
            if image_url in seen_urls:
                continue
            seen_urls.add(image_url)
            self._get_card_thumbnail(image_url)

    def _render_collection_list(self):
        """Render collection entries in the list view."""
        for item in self.collection_tree.get_children():
            self.collection_tree.delete(item)

        self.collection_tree.grid_remove()
        try:
            for row in self.collection_rows:
                self.collection_tree.insert(
                    "",
                    tk.END,
                    values=(
                        row["name"],
                        row["count"],
                        row["set_code"],
                        row["rarity"],
                        row["finish_display"],
                        row["price_str"],
                    ),
                )
        finally:
            self.collection_tree.grid()

    def _render_collection_grid(self):
        """Render collection entries as a visual card grid."""
        for child in self.collection_grid_inner.winfo_children():
            child.destroy()

        self.collection_photo_refs = []
        for column in range(12):
            self.collection_grid_inner.columnconfigure(column, weight=0)

        available_width = max(self.collection_grid_canvas.winfo_width(), self.collection_grid_frame.winfo_width(), 1)
        tile_width = 190
        columns = max(1, available_width // tile_width)

        for column in range(columns):
            self.collection_grid_inner.columnconfigure(column, weight=1)

        for index, row in enumerate(self.collection_rows):
            tile = ttk.Frame(self.collection_grid_inner, padding=8, relief="solid", borderwidth=1)
            tile.grid(row=index // columns, column=index % columns, padx=8, pady=8, sticky="n")
            self._bind_collection_grid_mousewheel(tile)

            photo = self._get_card_thumbnail(row.get("image_url"))
            self.collection_photo_refs.append(photo)

            image_label = ttk.Label(tile, image=photo)
            image_label.pack()
            self._bind_collection_grid_mousewheel(image_label)

            name_label = ttk.Label(
                tile,
                text=row["name"],
                font=("Segoe UI", 9, "bold"),
                wraplength=146,
                justify=tk.CENTER,
            )
            name_label.pack(pady=(6, 0))
            self._bind_collection_grid_mousewheel(name_label)
            details_label = ttk.Label(
                tile,
                text=f"x{row['count']} • {row['set_code']} #{row['collector_number']}",
                font=("Segoe UI", 8),
                justify=tk.CENTER,
            )
            details_label.pack()
            self._bind_collection_grid_mousewheel(details_label)
            finish_label = ttk.Label(
                tile,
                text=f"{row['finish_display']} • {row['rarity']}",
                font=("Segoe UI", 8),
                justify=tk.CENTER,
            )
            finish_label.pack()
            self._bind_collection_grid_mousewheel(finish_label)
            price_label = ttk.Label(
                tile,
                text=row["price_str"],
                font=("Segoe UI", 8, "bold"),
                justify=tk.CENTER,
            )
            price_label.pack(pady=(0, 4))
            self._bind_collection_grid_mousewheel(price_label)

        self.collection_grid_inner.update_idletasks()
        self.collection_grid_canvas.configure(scrollregion=self.collection_grid_canvas.bbox("all"))

    def _render_collection_view(self):
        """Render the currently selected collection view mode."""
        mode = self.collection_view_var.get()
        if mode == "grid":
            self.collection_table_frame.grid_remove()
            self.collection_grid_frame.grid()
            self._render_collection_grid()
        else:
            self.collection_grid_frame.grid_remove()
            self.collection_table_frame.grid()
            self._render_collection_list()

    def _sort_collection_by(self, column: str):
        """Sort collection rows by selected column, toggling direction each click."""
        reverse = self.collection_sort_state.get(column, False)
        self.collection_sort_state[column] = not reverse

        if column == "Price":
            key_func = lambda row: float(row.get("price_value", 0.0))
        elif column == "Count":
            key_func = lambda row: int(row.get("count", 0))
        elif column == "Card":
            key_func = lambda row: str(row.get("name", "")).lower().strip()
        elif column == "Set":
            key_func = lambda row: str(row.get("set_code", "")).lower().strip()
        elif column == "Rarity":
            key_func = lambda row: str(row.get("rarity", "")).lower().strip()
        elif column == "Finish":
            key_func = lambda row: str(row.get("finish_display", "")).lower().strip()
        else:
            key_func = lambda row: str(row.get("name", "")).lower().strip()

        self.collection_rows.sort(key=key_func, reverse=reverse)
        self._render_collection_view()

        self._update_collection_headings(active_column=column)

    def _resolve_initial_cards_file(self) -> Path | None:
        """Find a sensible default cards JSON file location."""
        search_paths = [
            Path.cwd() / "cards.json",
            Path.home() / "cards.json",
            Path.home() / "Desktop" / "cards.json",
        ]
        for path in search_paths:
            if path.exists() and path.is_file():
                return path
        return None

    def _pick_collection_file(self):
        selected_file = filedialog.askopenfilename(
            title="Select cards.json file",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if selected_file:
            self.collection_file_var.set(selected_file)

    def _coerce_price(self, value) -> float:
        """Normalize price values to float."""
        if value is None:
            return 0.0
        try:
            return float(value)
        except (ValueError, TypeError):
            return 0.0

    def _load_collection(self):
        """Load and display the cards.json file."""
        self.collection_rows = []
        self.collection_photo_refs = []
        for item in self.collection_tree.get_children():
            self.collection_tree.delete(item)
        for child in self.collection_grid_inner.winfo_children():
            child.destroy()

        cards_file = self.collection_file_var.get().strip()
        if not cards_file:
            initial_file = self._resolve_initial_cards_file()
            if initial_file:
                cards_file = str(initial_file)
                self.collection_file_var.set(cards_file)

        if not cards_file:
            self._pick_collection_file()
            cards_file = self.collection_file_var.get().strip()
            if not cards_file:
                return

        # Load and parse JSON
        try:
            with open(cards_file, "r", encoding="utf-8") as f:
                cards_data = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError) as e:
            messagebox.showerror("Error", f"Failed to load cards.json: {e}")
            return

        if isinstance(cards_data, dict):
            card_entries = [entry for entry in cards_data.values() if isinstance(entry, dict)]
        elif isinstance(cards_data, list):
            card_entries = [entry for entry in cards_data if isinstance(entry, dict)]
        else:
            messagebox.showerror("Error", "Unsupported JSON format. Expected object or array.")
            return

        # Populate table
        total_value = 0.0
        unique_count = 0
        total_copies = 0
        rows: list[dict] = []

        for card_info in card_entries:
            try:
                name = card_info.get("name", "Unknown")
                set_code = str(card_info.get("set", "N/A") or "N/A").upper()
                collector_number = str(card_info.get("collector_number", "?") or "?")
                rarity = str(card_info.get("rarity", "N/A") or "N/A").title()
                try:
                    count = int(card_info.get("count", 1) or 1)
                except (TypeError, ValueError):
                    count = 1

                prices = card_info.get("prices", {})
                if not isinstance(prices, dict):
                    prices = {}
                price_usd = self._coerce_price(prices.get("usd"))

                raw_finish = str(card_info.get("finish", "unknown") or "unknown").strip().lower()
                finish_display = self._get_finish_display(raw_finish)
                image_url = self._extract_image_url(card_info)

                total_card_value = price_usd * count
                total_value += total_card_value
                unique_count += 1
                total_copies += count

                price_str = f"${price_usd:.2f}" if price_usd > 0 else "$0.00"
                rows.append({
                    "name": name,
                    "count": count,
                    "set_code": set_code,
                    "collector_number": collector_number,
                    "rarity": rarity,
                    "finish": raw_finish,
                    "finish_display": finish_display,
                    "price_value": price_usd,
                    "price_str": price_str,
                    "image_url": image_url,
                })
            except (KeyError, TypeError):
                # Skip malformed entries
                continue

        self.collection_rows = sorted(rows, key=lambda row: row["price_value"], reverse=True)
        self._precache_collection_images()
        self.collection_sort_state["Price"] = True
        self._render_collection_view()
        self._update_collection_headings(active_column="Price")

        # Update summary
        self.total_value_label.config(text=f"${total_value:.2f}")
        self.summary_label.config(
            text=f"Unique Cards: {unique_count} | Total Copies: {total_copies}"
        )
        self.root.update()

    def _pick_folder(self):
        folder = filedialog.askdirectory(title="Select Image Folder")
        if folder:
            self.folder_var.set(folder)

    def _on_provider_changed(self, event=None):
        self._update_model_combo()

    def _update_model_combo(self):
        provider = self.provider_var.get()
        if provider == "openai":
            models = ["gpt-4o", "gpt-4o-mini"]
            self.model_combo["values"] = models
            self.model_combo.set("gpt-4o")
        elif provider == "gemini":
            models = ["2.5 (gemini-2.5-flash)", "3 (gemini-3-flash-preview)"]
            self.model_combo["values"] = models
            self.model_combo.set("2.5 (gemini-2.5-flash)")

    def _get_selected_model(self) -> str | None:
        """Resolve the selected model to a full model ID."""
        provider = self.provider_var.get()
        model_display = self.model_var.get()

        if provider == "openai":
            return model_display
        elif provider == "gemini":
            # Extract tier from display string like "2.5 (gemini-2.5-flash)"
            if "2.5" in model_display:
                return GEMINI_TIER_MODEL_MAP["2.5"]
            elif "3" in model_display:
                return GEMINI_TIER_MODEL_MAP["3"]
        return None

    def _log(self, message: str, is_error: bool = False):
        """Append message to output text widget."""
        self.output_text.config(state="normal")
        if is_error:
            self.output_text.insert(tk.END, f"❌ {message}\n")
        else:
            self.output_text.insert(tk.END, f"✓ {message}\n")
        self.output_text.see(tk.END)
        self.output_text.config(state="disabled")
        self.root.update()

    def _on_card_identified(
        self,
        name: str,
        set_code: str,
        number: str,
        count: int,
        match_method: str,
        finish: str = "unknown",
        name_confidence: str = "unknown",
        set_confidence: str = "unknown",
        finish_confidence: str = "unknown",
        image_url: str = "",
    ):
        """Callback when a card is identified."""
        msg = f"{name} [{set_code} #{number}] (x{count}) [{match_method}] [finish={finish}]"
        if self.debug_var.get():
            msg += (
                f" [conf name={name_confidence} set={set_confidence} finish={finish_confidence}]"
            )
        self._log(msg, is_error=False)

    def _on_status(self, message: str):
        """Callback for status updates."""
        self._log(message, is_error=False)

    def _on_error(self, message: str, debug: bool = False):
        """Callback for errors. Only show if debug is enabled or if error is critical."""
        if self.debug_var.get() or debug:
            self._log(message, is_error=True)

    def _start_scan(self):
        """Start the scanning process."""
        folder = self.folder_var.get()
        if not folder:
            messagebox.showerror("Error", "Please select an image folder.")
            return

        output_file = self.output_var.get()
        if not output_file:
            output_file = "cards.json"

        # Resolve output path: if relative, use image folder as base
        output_path = Path(output_file)
        if not output_path.is_absolute():
            output_path = Path(folder) / output_file
        self.last_scan_output = output_path

        provider = self.provider_var.get()
        model = self._get_selected_model()

        self.scanning = True
        self.cancel_event = threading.Event()
        self.start_button.config(state="disabled")
        self.cancel_button.config(state="normal")
        self._clear_output()

        self._log(f"Starting scan: {folder}")
        self._log(f"Provider: {provider}, Model: {model}")
        self._log("=" * 80)

        # Run scan in background thread to avoid UI freezing
        self.scan_thread = threading.Thread(
            target=self._run_scan,
            args=(str(folder), str(output_path), provider, model),
            daemon=True,
        )
        self.scan_thread.start()

    def _run_scan(self, folder: str, output_path: str, provider: str, model: str | None):
        """Background thread function for scanning."""
        try:
            scanner_engine.scan_with_callbacks(
                image_folder=folder,
                output_path=output_path,
                provider=provider,
                vision_model=model,
                on_card_identified=self._on_card_identified,
                on_status=self._on_status,
                on_error=self._on_error,
                cancel_event=self.cancel_event,
            )
        except Exception as exc:
            self._on_error(f"Fatal error: {exc}", debug=True)
        finally:
            self.scanning = False
            self.root.after(0, self._scan_complete)

    def _scan_complete(self):
        """Called when scan finishes (from main thread)."""
        self.start_button.config(state="normal")
        self.cancel_button.config(state="disabled")
        self._log("=" * 80)
        self._log("Scan complete!")

        if self.last_scan_output and self.last_scan_output.exists():
            self.collection_file_var.set(str(self.last_scan_output))
            self._load_collection()

    def _cancel_scan(self):
        """Cancel the ongoing scan."""
        if self.cancel_event:
            self.cancel_event.set()
        self._log("Cancellation requested...")
        self.cancel_button.config(state="disabled")

    def _clear_output(self):
        """Clear the output text widget."""
        self.output_text.config(state="normal")
        self.output_text.delete(1.0, tk.END)
        self.output_text.config(state="disabled")


def main():
    root = tk.Tk()
    app = MTGScannerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
