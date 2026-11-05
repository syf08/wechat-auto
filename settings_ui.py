"""Windows 可视化配置助手：个性、功能开关和 DPAPI 加密 API Key。"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import secret_store
import user_settings

APP_TITLE = "微信自动回复 · 个性配置"
BG = "#f3f6fa"
CARD = "#ffffff"
TEXT = "#172033"
MUTED = "#667085"
ACCENT = "#2563eb"
HEADER = "#14324f"


class SettingsApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.profile_error = ""
        try:
            self.profile = user_settings.load_profile()
        except user_settings.ProfileError as exc:
            self.profile = user_settings.UserProfile()
            self.profile_error = str(exc)

        self.secret_path = secret_store.default_secret_path()
        self.secret_error = ""
        try:
            self.stored_secret = secret_store.load_secret()
        except secret_store.SecretStoreError as exc:
            self.stored_secret = ""
            self.secret_error = str(exc)
        self.env_key_present = bool(os.environ.get("DEEPSEEK_API_KEY", "").strip())

        self.name_var = tk.StringVar()
        self.length_var = tk.StringVar()
        self.humor_var = tk.IntVar()
        self.enthusiasm_var = tk.IntVar()
        self.directness_var = tk.IntVar()
        self.warmth_var = tk.IntVar()
        self.search_var = tk.BooleanVar()
        self.vision_var = tk.BooleanVar()
        self.vision_memory_var = tk.IntVar(value=8)
        self.history_limit_var = tk.IntVar(value=20)
        self.sticker_var = tk.BooleanVar()
        self.sticker_diversity_var = tk.IntVar(value=6)
        self.sticker_strictness_var = tk.StringVar(value="严格")
        self.sticker_min_gap_var = tk.IntVar(value=0)
        self.search_toggle_text = tk.StringVar()
        self.vision_toggle_text = tk.StringVar()
        self.sticker_toggle_text = tk.StringVar()
        self.api_key_var = tk.StringVar(value=self.stored_secret)
        self.show_key_var = tk.BooleanVar(value=False)
        self.show_key_text = tk.StringVar(value="显示 Key")
        self.sticker_catalog_text = self.profile.sticker_catalog
        self.slider_labels: dict[str, ttk.Label] = {}

        self._configure_window()
        self._configure_style()
        self._build_layout()
        self._load_profile_into_form(self.profile)
        self._refresh_secret_status()
        if self.profile_error:
            self._set_status(self.profile_error, error=True)

    def _configure_window(self) -> None:
        self.root.title(APP_TITLE)
        self.root.geometry("960x820")
        self.root.minsize(880, 720)
        self.root.configure(background=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

    def _configure_style(self) -> None:
        available = set(tkfont.families(self.root))
        family = next(
            (
                name
                for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI")
                if name in available
            ),
            "TkDefaultFont",
        )
        self.font_family = family
        self.root.option_add("*Font", (family, 10))

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED)
        style.configure(
            "Section.TLabel", background=CARD, foreground=TEXT, font=(family, 12, "bold")
        )
        style.configure(
            "Card.TLabelframe",
            background=CARD,
            bordercolor="#dde4ee",
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "Card.TLabelframe.Label",
            background=CARD,
            foreground=TEXT,
            font=(family, 11, "bold"),
        )
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 9))
        style.map("TNotebook.Tab", background=[("selected", CARD)])
        style.configure("TEntry", padding=7)
        style.configure("TCombobox", padding=6)
        style.configure("TCheckbutton", background=CARD, foreground=TEXT)
        style.layout("Toggle.TCheckbutton", style.layout("Toolbutton"))
        style.configure(
            "Toggle.TCheckbutton",
            padding=(12, 7),
            background="#f2f5f9",
            foreground=MUTED,
            bordercolor="#cbd5e1",
            relief="solid",
            borderwidth=1,
            font=(family, 10, "bold"),
        )
        style.map(
            "Toggle.TCheckbutton",
            background=[("selected", "#e8f7ee"), ("active", "#edf2f7")],
            foreground=[("selected", "#166534"), ("!selected", MUTED)],
            bordercolor=[("selected", "#86d3a5"), ("!selected", "#cbd5e1")],
        )
        style.configure(
            "Accent.TButton",
            padding=(18, 7),
            foreground="#ffffff",
            background=ACCENT,
            font=(family, 10, "bold"),
        )
        style.map(
            "Accent.TButton",
            background=[("active", "#1d4ed8"), ("pressed", "#1e40af")],
        )
        style.configure("Quiet.TButton", padding=(14, 9))
        style.configure("Horizontal.TScale", background=CARD)

    def _build_layout(self) -> None:
        header = tk.Frame(self.root, bg=HEADER, height=88)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(
            header,
            text="微信自动回复",
            bg=HEADER,
            fg="#ffffff",
            font=(self.font_family, 19, "bold"),
            anchor="w",
        ).pack(fill="x", padx=24, pady=(14, 0))
        tk.Label(
            header,
            text="把回复风格调成更像你自己的样子",
            bg=HEADER,
            fg="#c9d8e8",
            font=(self.font_family, 10),
            anchor="w",
        ).pack(fill="x", padx=24, pady=(1, 12))

        body = ttk.Frame(self.root, padding=(18, 14, 18, 10))
        body.pack(fill="both", expand=True)
        self.notebook = ttk.Notebook(body)

        self.profile_tab = ttk.Frame(self.notebook, padding=18, style="Card.TFrame")
        self.features_tab = ttk.Frame(self.notebook, style="Card.TFrame")
        self.api_tab = ttk.Frame(self.notebook, padding=18, style="Card.TFrame")
        self.notebook.add(self.profile_tab, text="个人信息与聊天风格")
        self.notebook.add(self.features_tab, text="功能开关")
        self.notebook.add(self.api_tab, text="API Key")

        self._build_profile_tab()
        self._build_features_tab()
        self._build_api_tab()
        self._build_footer(body)
        self.notebook.pack(fill="both", expand=True)

    def _build_profile_tab(self) -> None:
        self.profile_tab.columnconfigure(0, weight=5)
        self.profile_tab.columnconfigure(1, weight=6)
        ttk.Label(
            self.profile_tab,
            text="设置身份和说话方式。风格滑杆会实时影响生成的人设提示词。",
            style="Muted.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

        basic_card = ttk.LabelFrame(
            self.profile_tab, text="基本信息", padding=14, style="Card.TLabelframe"
        )
        basic_card.grid(row=1, column=0, sticky="nsew", padx=(0, 9))
        basic_card.columnconfigure(1, weight=1)
        basic_card.rowconfigure(3, weight=1)
        ttk.Label(basic_card, text="个人名字", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 12), pady=6
        )
        ttk.Entry(basic_card, textvariable=self.name_var).grid(row=0, column=1, sticky="ew", pady=6)

        ttk.Label(basic_card, text="回复长度", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 12), pady=6
        )
        ttk.Combobox(
            basic_card,
            textvariable=self.length_var,
            values=user_settings.REPLY_LENGTHS,
            state="readonly",
        ).grid(row=1, column=1, sticky="ew", pady=6)

        ttk.Label(basic_card, text="补充偏好", style="Card.TLabel").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 5)
        )
        self.custom_text = ScrolledText(
            basic_card,
            height=7,
            wrap="word",
            relief="solid",
            borderwidth=1,
            highlightthickness=0,
            font=(self.font_family, 10),
            undo=True,
        )
        self.custom_text.grid(row=3, column=0, columnspan=2, sticky="nsew")
        ttk.Label(
            basic_card,
            text="例如：回复更口语化、少用感叹号、不要连续追问。",
            style="Muted.TLabel",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(5, 0))

        style_card = ttk.LabelFrame(
            self.profile_tab, text="聊天风格", padding=14, style="Card.TLabelframe"
        )
        style_card.grid(row=1, column=1, sticky="nsew", padx=(9, 0))
        style_card.columnconfigure(0, weight=1)
        style_card.columnconfigure(1, weight=1)
        ttk.Label(
            style_card,
            text="数字越高，对应特征越明显。",
            style="Muted.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        items = [
            ("humor", "幽默感", self.humor_var, 1, 0),
            ("enthusiasm", "热情度", self.enthusiasm_var, 1, 1),
            ("directness", "直接程度", self.directness_var, 2, 0),
            ("warmth", "温柔度", self.warmth_var, 2, 1),
        ]
        for key, label, variable, row, column in items:
            card = ttk.LabelFrame(style_card, text=label, padding=10, style="Card.TLabelframe")
            card.grid(
                row=row,
                column=column,
                sticky="nsew",
                padx=(0, 6) if column == 0 else (6, 0),
                pady=(0, 10),
            )
            value_label = ttk.Label(card, text="0", style="Card.TLabel", anchor="e")
            value_label.pack(fill="x")
            scale = ttk.Scale(
                card,
                from_=0,
                to=100,
                variable=variable,
                command=lambda _value, k=key: self._slider_changed(k),
            )
            scale.pack(fill="x", pady=(5, 0))
            self.slider_labels[key] = value_label

        preview_header = ttk.Frame(self.profile_tab, style="Card.TFrame")
        preview_header.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 5))
        ttk.Label(preview_header, text="生成的人设提示词", style="Section.TLabel").pack(side="left")
        ttk.Label(
            preview_header,
            text="只影响语气，不覆盖安全与格式规则",
            style="Muted.TLabel",
        ).pack(side="right")

        self.preview_text = ScrolledText(
            self.profile_tab,
            height=7,
            wrap="word",
            relief="solid",
            borderwidth=1,
            highlightthickness=0,
            font=(self.font_family, 9),
            background="#f8fafc",
        )
        self.preview_text.grid(row=3, column=0, columnspan=2, sticky="nsew")
        self.preview_text.configure(state="disabled")
        self.profile_tab.rowconfigure(1, weight=1)
        self.profile_tab.rowconfigure(3, weight=1)
        self.name_var.trace_add("write", self._field_changed)
        self.length_var.trace_add("write", self._field_changed)
        self.custom_text.bind("<KeyRelease>", self._field_changed)

    def _build_features_tab(self) -> None:
        self.features_tab.columnconfigure(0, weight=1)
        self.features_tab.rowconfigure(0, weight=1)
        self.features_canvas = tk.Canvas(
            self.features_tab,
            background=CARD,
            borderwidth=0,
            highlightthickness=0,
        )
        self.features_scrollbar = ttk.Scrollbar(
            self.features_tab,
            orient="vertical",
            command=self.features_canvas.yview,
        )
        self.features_canvas.configure(yscrollcommand=self.features_scrollbar.set)
        self.features_canvas.grid(row=0, column=0, sticky="nsew")
        self.features_scrollbar.grid(row=0, column=1, sticky="ns")

        self.features_content = ttk.Frame(
            self.features_canvas,
            padding=(18, 12),
            style="Card.TFrame",
        )
        self.features_window = self.features_canvas.create_window(
            (0, 0),
            window=self.features_content,
            anchor="nw",
        )
        self.features_content.bind("<Configure>", self._refresh_features_scrollregion)
        self.features_canvas.bind("<Configure>", self._resize_features_content)

        parent = self.features_content
        ttk.Label(
            parent,
            text="设置在线能力和启动上下文。关闭在线功能后速度更快，也更少发送数据。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 10))

        history_card = ttk.LabelFrame(
            parent, text="启动历史上下文", padding=12, style="Card.TLabelframe"
        )
        history_card.pack(fill="x", pady=(0, 14))
        history_row = ttk.Frame(history_card, style="Card.TFrame")
        history_row.pack(fill="x")
        ttk.Label(history_row, text="启动时读取最近", style="Card.TLabel").pack(side="left")
        self.history_limit_spin = ttk.Spinbox(
            history_row,
            from_=0,
            to=50,
            increment=1,
            width=5,
            textvariable=self.history_limit_var,
            state="readonly",
        )
        self.history_limit_spin.pack(side="left", padx=(8, 5))
        ttk.Label(
            history_row,
            text="条历史记录（0 = 不读取，默认 20）",
            style="Muted.TLabel",
        ).pack(side="left")
        ttk.Label(
            history_card,
            text="数量越多，启动时读取越慢，模型请求携带的上下文也越多。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(7, 0))

        search_card = ttk.LabelFrame(
            parent, text="按需联网搜索", padding=12, style="Card.TLabelframe"
        )
        search_card.pack(fill="x", pady=(0, 14))
        self.search_toggle = ttk.Checkbutton(
            search_card,
            textvariable=self.search_toggle_text,
            variable=self.search_var,
            style="Toggle.TCheckbutton",
            command=self._refresh_toggle_labels,
        )
        self.search_toggle.pack(anchor="w")
        ttk.Label(
            search_card,
            text=(
                "天气、新闻、价格等实时问题可按需搜索；每次回复最多一次，"
                "短搜索词会发送给 DeepSeek Search API。"
            ),
            style="Card.TLabel",
            wraplength=760,
        ).pack(anchor="w", pady=(7, 0))

        vision_card = ttk.LabelFrame(
            parent, text="图片与表情包分析", padding=12, style="Card.TLabelframe"
        )
        vision_card.pack(fill="x", pady=(0, 14))
        self.vision_toggle = ttk.Checkbutton(
            vision_card,
            textvariable=self.vision_toggle_text,
            variable=self.vision_var,
            style="Toggle.TCheckbutton",
            command=self._refresh_toggle_labels,
        )
        self.vision_toggle.pack(anchor="w")
        ttk.Label(
            vision_card,
            text=(
                "开启后把新发图片或表情包交给视觉模型；关闭后只使用文字占位符，不发送截图。"
            ),
            style="Card.TLabel",
            wraplength=760,
        ).pack(anchor="w", pady=(7, 0))
        memory_row = ttk.Frame(vision_card, style="Card.TFrame")
        memory_row.pack(fill="x", pady=(10, 0))
        ttk.Label(memory_row, text="后续回复保留最近", style="Card.TLabel").pack(side="left")
        self.vision_memory_spin = ttk.Spinbox(
            memory_row,
            from_=0,
            to=20,
            increment=1,
            width=5,
            textvariable=self.vision_memory_var,
            state="readonly",
        )
        self.vision_memory_spin.pack(side="left", padx=(8, 5))
        ttk.Label(
            memory_row,
            text="张历史图片（0 = 不保留，图片仅存在内存中）",
            style="Muted.TLabel",
        ).pack(side="left")

        sticker_card = ttk.LabelFrame(
            parent, text="原生收藏贴纸", padding=12, style="Card.TLabelframe"
        )
        sticker_card.pack(fill="x")
        sticker_card.columnconfigure(0, weight=1)
        self.sticker_toggle = ttk.Checkbutton(
            sticker_card,
            textvariable=self.sticker_toggle_text,
            variable=self.sticker_var,
            style="Toggle.TCheckbutton",
            command=self._sticker_toggle_changed,
        )
        self.sticker_toggle.grid(row=0, column=0, sticky="w")
        ttk.Label(
            sticker_card,
            text="开启后，模型会按聊天语境从微信“自定义表情”收藏里选一张，在文字后发送。",
            style="Card.TLabel",
            wraplength=820,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(7, 6))
        policy_row = ttk.Frame(sticker_card, style="Card.TFrame")
        policy_row.grid(row=2, column=0, sticky="ew", pady=(0, 7))
        ttk.Label(policy_row, text="最近去重", style="Card.TLabel").pack(side="left")
        self.sticker_diversity_spin = ttk.Spinbox(
            policy_row,
            from_=0,
            to=30,
            increment=1,
            width=5,
            textvariable=self.sticker_diversity_var,
            state="readonly",
        )
        self.sticker_diversity_spin.pack(side="left", padx=(6, 4))
        ttk.Label(policy_row, text="张（0 = 关闭）；每张成功后至少间隔", style="Card.TLabel").pack(
            side="left"
        )
        self.sticker_gap_spin = ttk.Spinbox(
            policy_row,
            from_=0,
            to=20,
            increment=1,
            width=5,
            textvariable=self.sticker_min_gap_var,
            state="readonly",
        )
        self.sticker_gap_spin.pack(side="left", padx=(6, 4))
        ttk.Label(policy_row, text="条文字回复（0 = 不限制）", style="Muted.TLabel").pack(
            side="left"
        )
        strictness_row = ttk.Frame(sticker_card, style="Card.TFrame")
        strictness_row.grid(row=3, column=0, sticky="ew", pady=(0, 2))
        ttk.Label(strictness_row, text="表情包回复严格程度", style="Card.TLabel").pack(
            side="left"
        )
        self.sticker_strictness_combo = ttk.Combobox(
            strictness_row,
            textvariable=self.sticker_strictness_var,
            values=user_settings.STICKER_STRICTNESS_LEVELS,
            width=7,
            state="readonly",
        )
        self.sticker_strictness_combo.pack(side="left", padx=(6, 8))
        self.sticker_strictness_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._refresh_sticker_status()
        )
        ttk.Label(
            strictness_row,
            text="严格 = 保持原逻辑；宽松 = 返回 none 时再次宽松匹配",
            style="Muted.TLabel",
        ).pack(side="left")
        actions = ttk.Frame(sticker_card, style="Card.TFrame")
        actions.grid(row=4, column=0, sticky="ew")
        ttk.Button(
            actions,
            text="粘贴 / 编辑贴纸目录...",
            style="Quiet.TButton",
            command=self._edit_sticker_catalog,
        ).pack(side="left")
        self.sticker_count_label = ttk.Label(actions, text="", style="Muted.TLabel")
        self.sticker_count_label.pack(side="left", padx=(12, 0))
        self.sticker_status_label = ttk.Label(
            sticker_card,
            text="",
            style="Muted.TLabel",
            wraplength=820,
            justify="left",
        )
        self.sticker_status_label.grid(row=5, column=0, sticky="w", pady=(2, 0))
        self._bind_features_mousewheel()

    def _refresh_features_scrollregion(self, _event: tk.Event | None = None) -> None:
        self.features_canvas.configure(scrollregion=self.features_canvas.bbox("all"))
        if self.features_content.winfo_reqheight() > self.features_canvas.winfo_height():
            self.features_scrollbar.grid()
        else:
            self.features_scrollbar.grid_remove()

    def _resize_features_content(self, event: tk.Event) -> None:
        self.features_canvas.itemconfigure(self.features_window, width=event.width)
        self._refresh_features_scrollregion()

    def _bind_features_mousewheel(self) -> None:
        def on_wheel(event: tk.Event) -> str:
            if event.delta:
                self.features_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"

        def bind_tree(widget: tk.Misc) -> None:
            widget.bind("<MouseWheel>", on_wheel)
            for child in widget.winfo_children():
                bind_tree(child)

        bind_tree(self.features_content)

    def _build_api_tab(self) -> None:
        ttk.Label(
            self.api_tab,
            text="DeepSeek API Key",
            style="Section.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            self.api_tab,
            text="Key 会使用 Windows DPAPI 加密保存，不会写入 user_config.json。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(4, 14))

        key_row = ttk.Frame(self.api_tab, style="Card.TFrame")
        key_row.pack(fill="x")
        self.api_entry = ttk.Entry(key_row, textvariable=self.api_key_var, show="●")
        self.api_entry.pack(side="left", fill="x", expand=True)
        ttk.Checkbutton(
            key_row,
            textvariable=self.show_key_text,
            variable=self.show_key_var,
            command=self._toggle_key_visibility,
            style="Toggle.TCheckbutton",
        ).pack(side="left", padx=(12, 0))

        self.secret_status = ttk.Label(self.api_tab, text="", style="Muted.TLabel")
        self.secret_status.pack(anchor="w", pady=(12, 4))
        ttk.Label(
            self.api_tab,
            text="如果设置了 DEEPSEEK_API_KEY 环境变量，它的优先级高于本机加密文件。",
            style="Muted.TLabel",
            wraplength=760,
        ).pack(anchor="w", pady=(4, 16))

        ttk.Button(
            self.api_tab,
            text="清除本机加密 Key",
            style="Quiet.TButton",
            command=self._clear_secret,
        ).pack(anchor="w")

    def _build_footer(self, parent: ttk.Frame) -> None:
        footer = ttk.Frame(parent)
        # 先为底部操作栏保留完整高度，避免窗口较矮时“保存配置”文字被压成一条线。
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        self.status_label = ttk.Label(footer, text="修改后点击保存，并在下次启动时生效")
        self.status_label.pack(side="left")
        ttk.Button(footer, text="恢复默认", command=self._reset_defaults).pack(
            side="right", padx=(8, 0)
        )
        ttk.Button(footer, text="取消", command=self.root.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(footer, text="保存配置", style="Accent.TButton", command=self._save).pack(
            side="right"
        )

    def _load_profile_into_form(self, profile: user_settings.UserProfile) -> None:
        self.name_var.set(profile.name)
        self.length_var.set(profile.reply_length)
        self.humor_var.set(profile.humor)
        self.enthusiasm_var.set(profile.enthusiasm)
        self.directness_var.set(profile.directness)
        self.warmth_var.set(profile.warmth)
        self.search_var.set(profile.search_enabled)
        self.vision_var.set(profile.vision_enabled)
        self.vision_memory_var.set(profile.vision_memory_limit)
        self.history_limit_var.set(profile.history_limit)
        self.sticker_var.set(profile.sticker_enabled)
        self.sticker_diversity_var.set(profile.sticker_diversity_window)
        self.sticker_strictness_var.set(profile.sticker_strictness)
        self.sticker_min_gap_var.set(profile.sticker_min_reply_gap)
        self._refresh_toggle_labels()
        self.custom_text.delete("1.0", "end")
        self.custom_text.insert("1.0", profile.custom_instructions)
        self.sticker_catalog_text = profile.sticker_catalog
        self._sticker_toggle_changed(focus=False)
        self._slider_changed("humor")
        self._refresh_preview()

    def _form_profile(self) -> user_settings.UserProfile:
        return user_settings.normalize_profile(
            user_settings.UserProfile(
                name=self.name_var.get(),
                humor=self.humor_var.get(),
                enthusiasm=self.enthusiasm_var.get(),
                directness=self.directness_var.get(),
                warmth=self.warmth_var.get(),
                reply_length=self.length_var.get(),
                custom_instructions=self.custom_text.get("1.0", "end").strip(),
                search_enabled=self.search_var.get(),
                vision_enabled=self.vision_var.get(),
                vision_memory_limit=self.vision_memory_var.get(),
                history_limit=self.history_limit_var.get(),
                sticker_enabled=self.sticker_var.get(),
                sticker_diversity_window=self.sticker_diversity_var.get(),
                sticker_strictness=self.sticker_strictness_var.get(),
                sticker_min_reply_gap=self.sticker_min_gap_var.get(),
                sticker_catalog=self.sticker_catalog_text,
            )
        )

    def _slider_changed(self, _key: str) -> None:
        values = {
            "humor": self.humor_var.get(),
            "enthusiasm": self.enthusiasm_var.get(),
            "directness": self.directness_var.get(),
            "warmth": self.warmth_var.get(),
        }
        for key, value in values.items():
            self.slider_labels[key].configure(text=f"{value} / 100")
        self._refresh_preview()

    def _field_changed(self, *_args: object) -> None:
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        if not hasattr(self, "preview_text"):
            return
        text = user_settings.build_profile_prompt(self._form_profile())
        self.preview_text.configure(state="normal")
        self.preview_text.delete("1.0", "end")
        self.preview_text.insert("1.0", text)
        self.preview_text.configure(state="disabled")

    def _toggle_key_visibility(self) -> None:
        self.api_entry.configure(show="" if self.show_key_var.get() else "●")
        self.show_key_text.set("隐藏 Key" if self.show_key_var.get() else "显示 Key")

    def _refresh_toggle_labels(self) -> None:
        self.search_toggle_text.set("✓ 已开启" if self.search_var.get() else "○ 已关闭")
        self.vision_toggle_text.set("✓ 已开启" if self.vision_var.get() else "○ 已关闭")
        self.sticker_toggle_text.set("✓ 已开启" if self.sticker_var.get() else "○ 已关闭")
        if hasattr(self, "vision_memory_spin"):
            self.vision_memory_spin.configure(
                state="readonly" if self.vision_var.get() else "disabled"
            )
        if hasattr(self, "sticker_diversity_spin"):
            sticker_state = "readonly" if self.sticker_var.get() else "disabled"
            self.sticker_diversity_spin.configure(state=sticker_state)
            self.sticker_gap_spin.configure(state=sticker_state)
            self.sticker_strictness_combo.configure(state=sticker_state)

    def _sticker_toggle_changed(self, focus: bool = True) -> None:
        self._refresh_toggle_labels()
        self._refresh_sticker_status()
        if focus and self.sticker_var.get() and not self.sticker_catalog_text.strip():
            self.root.after_idle(self._edit_sticker_catalog)

    def _edit_sticker_catalog(self) -> None:
        dialog = StickerCatalogDialog(
            self.root,
            self.sticker_catalog_text,
            self.font_family,
        )
        self.root.wait_window(dialog.window)
        if dialog.result is not None:
            self.sticker_catalog_text = dialog.result
            self._set_status("贴纸目录已更新，点击“保存配置”后生效")
        self._refresh_sticker_status()

    def _refresh_sticker_status(self) -> None:
        if not hasattr(self, "sticker_status_label"):
            return
        raw = self.sticker_catalog_text
        try:
            entries = user_settings.parse_sticker_catalog(raw)
            format_error = ""
        except user_settings.StickerCatalogError as exc:
            entries = ()
            format_error = str(exc)

        if format_error:
            text = f"已保存的目录格式有误：{format_error}"
            color = "#b42318"
            count_text = "0 张"
        elif not self.sticker_var.get():
            text = (
                f"当前未启用；目录中已填写 {len(entries)} 张贴纸，不会参与回复。"
                if entries
                else "当前未启用，不会读取或发送收藏贴纸。"
            )
            color = MUTED
            count_text = f"{len(entries)} 张" if entries else "未配置"
        elif not entries:
            text = "已开启，但贴纸目录为空；请粘贴至少一行有效目录。"
            color = "#b42318"
            count_text = "0 张"
        else:
            mode = (
                "严格模式：选择器返回 none 时不补发。"
                if self.sticker_strictness_var.get() == "严格"
                else "宽松模式：返回 none 时再次宽松匹配；仍无合适贴纸则只发文字。"
            )
            text = f"格式正确；{mode}保存并重启后生效。"
            color = "#166534"
            count_text = f"{len(entries)} 张"
        self.sticker_status_label.configure(text=text, foreground=color)
        self.sticker_count_label.configure(text=count_text, foreground=color)

    def _refresh_secret_status(self) -> None:
        if self.secret_error:
            text = f"本机密钥读取失败：{self.secret_error}"
        elif self.secret_path.exists():
            text = f"已找到本机加密 Key：{self.secret_path.name}"
        else:
            text = "本机尚未保存 API Key"
        if self.env_key_present:
            text += "；同时检测到 DEEPSEEK_API_KEY 环境变量（优先使用）"
        self.secret_status.configure(text=text)

    def _clear_secret(self) -> None:
        if not self.secret_path.exists():
            self.api_key_var.set("")
            self._set_status("本机没有需要清除的加密 Key")
            return
        if not messagebox.askyesno("清除 API Key", "确定删除 config_secret.bin 吗？"):
            return
        try:
            self.secret_path.unlink()
        except OSError as exc:
            messagebox.showerror("清除失败", str(exc))
            return
        self.api_key_var.set("")
        self.secret_error = ""
        self._refresh_secret_status()
        self._set_status("本机加密 API Key 已删除", error=False)

    def _reset_defaults(self) -> None:
        if not messagebox.askyesno("恢复默认", "将表单恢复为默认设置，尚未保存的修改会丢失。"):
            return
        self._load_profile_into_form(user_settings.UserProfile())
        self._set_status("已恢复默认表单，点击保存后生效")

    def _save(self) -> None:
        profile = self._form_profile()
        if profile.sticker_enabled:
            try:
                sticker_entries = user_settings.parse_sticker_catalog(profile.sticker_catalog)
            except user_settings.StickerCatalogError as exc:
                self.notebook.select(self.features_tab)
                self._refresh_sticker_status()
                messagebox.showerror("贴纸目录格式错误", str(exc))
                self.root.after_idle(self._edit_sticker_catalog)
                return
            if not sticker_entries:
                self.notebook.select(self.features_tab)
                self._refresh_sticker_status()
                messagebox.showerror(
                    "贴纸目录为空",
                    "已开启原生收藏贴纸，请先在“功能开关”页粘贴至少一行贴纸目录。",
                )
                self.root.after_idle(self._edit_sticker_catalog)
                return
        try:
            profile_path = user_settings.save_profile(profile)
        except user_settings.ProfileError as exc:
            messagebox.showerror("保存失败", str(exc))
            return

        key = self.api_key_var.get().strip()
        key_saved = False
        if key:
            try:
                secret_store.save_secret(key)
                key_saved = True
            except secret_store.SecretStoreError as exc:
                messagebox.showerror("API Key 保存失败", str(exc))
                return

        self.profile = profile
        self.secret_error = ""
        self._refresh_secret_status()
        self._set_status(f"已保存到 {profile_path.name}，重启主程序后生效")
        extra = ""
        if key_saved:
            extra = "\n\nAPI Key 已用 Windows DPAPI 加密保存。"
        elif not secret_store.load_secret() and not self.env_key_present:
            extra = "\n\n注意：尚未设置 API Key，主程序暂时无法连接 DeepSeek。"
        messagebox.showinfo("配置已保存", f"个性化设置已保存。{extra}\n\n请重新启动自动回复程序。")

    def _set_status(self, text: str, error: bool = False) -> None:
        self.status_label.configure(text=text, foreground="#b42318" if error else MUTED)


class StickerCatalogDialog:
    """独立编辑贴纸目录，避免主配置页被大文本框挤压。"""

    def __init__(self, parent: tk.Tk, initial_text: str, font_family: str):
        self.result: str | None = None
        self.window = tk.Toplevel(parent)
        self.window.title("编辑原生收藏贴纸目录")
        self.window.geometry("840x700")
        self.window.minsize(720, 600)
        self.window.configure(background=BG)
        self.window.transient(parent)
        self.window.grab_set()
        self.window.protocol("WM_DELETE_WINDOW", self._cancel)
        self.window.bind("<Escape>", lambda _event: self._cancel())

        body = ttk.Frame(self.window, padding=(20, 16, 20, 14))
        body.pack(fill="both", expand=True)

        card = ttk.LabelFrame(
            body,
            text="粘贴格式",
            padding=14,
            style="Card.TLabelframe",
        )
        card.pack(fill="x", pady=(0, 12))
        ttk.Label(
            card,
            text="每行填写一张贴纸，严格用半角竖线 | 分成 3 栏。不要加 StickerSpec(...)、引号或括号。",
            style="Card.TLabel",
            wraplength=750,
            justify="left",
        ).pack(anchor="w")
        ttk.Label(
            card,
            text="贴纸ID | 适用语义 | 收藏序号",
            style="Card.TLabel",
            font=(font_family, 10, "bold"),
        ).pack(anchor="w", pady=(8, 4))
        ttk.Label(
            card,
            text=(
                "cat_thanks | 谢谢、感谢 | 13\n"
                "laugh_big | 大笑、哈哈哈、绷不住了 | 20"
            ),
            style="Muted.TLabel",
            font=(font_family, 9),
            justify="left",
        ).pack(anchor="w", pady=(0, 8))
        ttk.Label(
            card,
            text=(
                "ID 仅限小写英文字母、数字、下划线和短横线。序号从 0 开始，从“自定义表情”"
                "收藏面板最底部逐行向上编号，每行从左到右；以 # 开头的行是注释。"
            ),
            style="Muted.TLabel",
            wraplength=750,
            justify="left",
        ).pack(anchor="w")

        self.text = ScrolledText(
            body,
            height=16,
            wrap="none",
            relief="solid",
            borderwidth=1,
            highlightthickness=0,
            font=(font_family, 9),
            undo=True,
        )
        self.text.pack(fill="both", expand=True)
        self.text.insert("1.0", initial_text)

        self.status = ttk.Label(body, text="", foreground=MUTED, wraplength=780)
        self.status.pack(fill="x", pady=(8, 10))

        footer = ttk.Frame(body)
        footer.pack(fill="x")
        ttk.Button(footer, text="取消", command=self._cancel).pack(side="right", padx=(8, 0))
        ttk.Button(
            footer,
            text="保存贴纸目录",
            style="Accent.TButton",
            command=self._save,
        ).pack(side="right")

        self.window.after_idle(self.text.focus_set)
        self.window.lift()

    def _save(self) -> None:
        raw = self.text.get("1.0", "end").strip()
        try:
            entries = user_settings.parse_sticker_catalog(raw)
        except user_settings.StickerCatalogError as exc:
            self.status.configure(text=f"格式错误：{exc}", foreground="#b42318")
            self.text.focus_set()
            self.window.bell()
            return
        if not entries:
            self.status.configure(
                text="目录不能为空，请至少粘贴一行有效贴纸。",
                foreground="#b42318",
            )
            self.text.focus_set()
            self.window.bell()
            return
        self.result = raw
        self.window.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.window.destroy()


def launch() -> int:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"无法打开配置窗口：{exc}")
        print("请安装带 Tcl/Tk 的 Windows 官方 Python。")
        return 1
    SettingsApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(launch())
