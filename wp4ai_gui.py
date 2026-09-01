import configparser
import json
import os
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import requests
from requests.auth import HTTPBasicAuth


APP_TITLE = "WP4AI 多站点发布控制台"
SITE_DB_FILENAME = "wp4ai_sites.db"
LEGACY_SITE_JSON_FILENAME = "wp4ai_sites.json"
WORKER_EXE_NAME = "wp4ai_generate_cli.exe"
WORKER_PY_NAME = "wp4ai_generate.py"
CONNECTION_TEST_TIMEOUT = 15
MAX_PARALLEL_MIN = 1
MAX_PARALLEL_MAX = 20
DEFAULT_MAX_PARALLEL = 3

MODE_MAP = {
    "full": "全流程（建分类 + 发产品）",
    "scan": "仅扫描建分类（--scan）",
    "update": "仅更新分类描述（--update）",
}
MODE_LABEL_TO_CODE = {v: k for k, v in MODE_MAP.items()}
MODE_TO_ARGS = {
    "full": [],
    "scan": ["--scan"],
    "update": ["--update"],
}


def _looks_like_sgcaptcha(payload: str) -> bool:
    text = (payload or "").lower()
    return (
        "sgcaptcha" in text
        or "/.well-known/sgcaptcha/" in text
        or "/.well-known/captcha/" in text
    )


def _redact_ai_error_text(value: object, limit: int = 220) -> str:
    """脱敏连接测试错误，防止代理回显完整 API key。"""
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+\b", "[REDACTED_API_KEY]", text)
    text = re.sub(
        r"(?i)(api[ _-]?key\s*[:=]\s*)[^\s,;]+",
        r"\1[REDACTED_API_KEY]",
        text,
    )
    return text[:limit]


def detect_runtime_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def write_startup_crash_log(exc_text: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"wp4ai_gui_crash_{stamp}.log"
    content = (
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Runtime Dir: {detect_runtime_dir()}\n"
        f"Python: {sys.version}\n\n"
        f"{exc_text}"
    )

    candidates = [
        Path(detect_runtime_dir()),
        Path(tempfile.gettempdir()) / "wp4ai_gui",
        Path.cwd(),
    ]
    for directory in candidates:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            out = directory / filename
            out.write_text(content, encoding="utf-8")
            return str(out)
        except Exception:
            continue
    return ""


def show_startup_error_dialog(message: str):
    try:
        err_root = tk.Tk()
        err_root.withdraw()
        messagebox.showerror(APP_TITLE, message, parent=err_root)
        err_root.destroy()
    except Exception:
        pass


@dataclass
class SiteConfig:
    site_id: str
    enabled: bool
    name: str
    products_dir: str
    wp_domain: str
    wp_username: str
    wp_password: str
    ai_api_key: str
    ai_base_url: str
    default_model: str
    bak_ai_api_key: str
    bak_ai_base_url: str
    bak_default_model: str
    seo_min_score: str
    seo_max_retries: str
    seo_system_prompt: str
    mode: str

    @staticmethod
    def new_default() -> "SiteConfig":
        return SiteConfig(
            site_id=str(uuid.uuid4()),
            enabled=True,
            name="新站点",
            products_dir="",
            wp_domain="https://example.com",
            wp_username="",
            wp_password="",
            ai_api_key="",
            ai_base_url="https://api.deepseek.com/v1",
            default_model="deepseek-chat",
            bak_ai_api_key="",
            bak_ai_base_url="",
            bak_default_model="",
            seo_min_score="65",
            seo_max_retries="5",
            seo_system_prompt="",
            mode="full",
        )


class RunnerThread(threading.Thread):
    def __init__(self, app: "WP4AIGui", sites: list[SiteConfig], max_parallel: int = 1):
        super().__init__(daemon=True)
        self.app = app
        self.sites = sites
        self.max_parallel = max(MAX_PARALLEL_MIN, int(max_parallel))
        self.stop_event = threading.Event()
        self.temp_files: list[str] = []
        self.process_lock = threading.Lock()
        self.stat_lock = threading.Lock()
        self.processes: dict[str, subprocess.Popen] = {}
        self.success = 0
        self.failed = 0
        self.skipped = 0

    def stop(self):
        self.stop_event.set()
        with self.process_lock:
            running = list(self.processes.values())
        for proc in running:
            self._terminate_process(proc)

    def emit(self, level: str, message: str):
        self.app.log_queue.put((level, message))

    def _inc_stat(self, field: str, count: int = 1):
        with self.stat_lock:
            setattr(self, field, getattr(self, field) + count)

    def _terminate_process(self, proc: subprocess.Popen):
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def run(self):
        try:
            worker_cmd = self.app.resolve_worker_command()
        except Exception as e:
            self.emit("error", f"❌ 无法定位执行引擎: {e}")
            self.app.log_queue.put(("runner_done", ""))
            return

        total = len(self.sites)
        self.emit("info", f"🚀 开始执行，共 {total} 个站点，最大并发 {self.max_parallel}。")

        jobs: list[tuple[int, int, SiteConfig]] = []
        for idx, site in enumerate(self.sites, start=1):
            valid, errors = self.app.validate_site(site)
            if not valid:
                self._inc_stat("skipped")
                self.emit(
                    "warn",
                    f"⚠️ [{idx}/{total}] 站点 [{site.name}] 配置不完整，已跳过: {'; '.join(errors)}"
                )
                continue
            jobs.append((idx, total, SiteConfig(**asdict(site))))

        if not jobs:
            self.emit("warn", "⚠️ 没有可执行站点。")
            self.emit(
                "info",
                f"\n🏁 执行结束: 成功 {self.success} / 失败 {self.failed} / 跳过 {self.skipped}"
            )
            self.app.log_queue.put(("runner_done", ""))
            return

        worker_count = min(self.max_parallel, len(jobs))
        job_queue: queue.Queue = queue.Queue()
        for job in jobs:
            job_queue.put(job)

        workers = []
        for worker_index in range(1, worker_count + 1):
            t = threading.Thread(
                target=self._worker_loop,
                args=(worker_index, worker_cmd, job_queue),
                daemon=True,
            )
            workers.append(t)
            t.start()

        for t in workers:
            t.join()

        self.cleanup_temp_files()
        self.emit(
            "info",
            f"\n🏁 执行结束: 成功 {self.success} / 失败 {self.failed} / 跳过 {self.skipped}"
        )
        self.app.log_queue.put(("runner_done", ""))

    def _worker_loop(self, worker_index: int, worker_cmd: list[str], job_queue: queue.Queue):
        while not self.stop_event.is_set():
            try:
                seq, total, site = job_queue.get_nowait()
            except queue.Empty:
                return

            try:
                self._run_one_site(worker_index, seq, total, site, worker_cmd)
            finally:
                job_queue.task_done()

    def _run_one_site(self, worker_index: int, seq: int, total: int, site: SiteConfig, worker_cmd: list[str]):
        if self.stop_event.is_set():
            self._inc_stat("skipped")
            self.emit("warn", f"⚠️ 站点 [{site.name}] 因停止指令未执行。")
            return

        self.emit("info", "\n==============================")
        self.emit("info", f"▶️ [W{worker_index}] [{seq}/{total}] 开始站点: {site.name}")
        self.emit("info", f"   产品目录: {site.products_dir}")
        self.emit("info", f"   运行模式: {MODE_MAP.get(site.mode, site.mode)}")

        try:
            temp_cfg = self.app.write_temp_config(site)
            self.temp_files.append(temp_cfg)
        except Exception as e:
            self._inc_stat("failed")
            self.emit("error", f"❌ 站点 [{site.name}] 临时配置写入失败: {e}")
            return

        cmd = list(worker_cmd) + MODE_TO_ARGS.get(site.mode, [])
        env = os.environ.copy()
        env["WP4AI_CONFIG"] = temp_cfg
        env["WP4AI_PRODUCTS_DIR"] = os.path.abspath(site.products_dir)

        proc = None
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=self.app.runtime_dir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )

            with self.process_lock:
                self.processes[site.site_id] = proc

            if proc.stdout:
                for line in proc.stdout:
                    if self.stop_event.is_set():
                        break
                    self.emit("raw", f"[{site.name}] {line.rstrip()}")

            if self.stop_event.is_set():
                self._terminate_process(proc)

            ret = proc.wait(timeout=20)
            if ret == 0 and not self.stop_event.is_set():
                self._inc_stat("success")
                self.emit("info", f"✅ 站点 [{site.name}] 执行成功。")
            elif self.stop_event.is_set():
                self._inc_stat("skipped")
                self.emit("warn", f"⏹ 站点 [{site.name}] 已停止。")
            else:
                self._inc_stat("failed")
                self.emit("error", f"❌ 站点 [{site.name}] 执行失败，退出码: {ret}")
        except Exception as e:
            self._inc_stat("failed")
            self.emit("error", f"❌ 站点 [{site.name}] 运行异常: {e}")
        finally:
            if proc:
                with self.process_lock:
                    self.processes.pop(site.site_id, None)

    def cleanup_temp_files(self):
        for fp in self.temp_files:
            try:
                if fp and os.path.exists(fp):
                    os.remove(fp)
            except Exception:
                pass
        self.temp_files.clear()


class WP4AIGui:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1400x860")
        self.root.minsize(1200, 760)

        self.runtime_dir = self.detect_runtime_dir()
        self.db_fallback_reason = ""
        self.db_path = self.resolve_db_path()
        self.legacy_json_path = os.path.join(self.runtime_dir, LEGACY_SITE_JSON_FILENAME)
        self.log_queue: queue.Queue = queue.Queue()
        self.runner: RunnerThread | None = None
        self.connection_test_thread: threading.Thread | None = None

        self.sites: list[SiteConfig] = []
        self.current_site_id: str | None = None

        self._build_ui()
        self.init_database()
        self._load_sites()
        self._render_site_list()
        self._poll_log_queue()
        self._append_log("info", f"📦 程序目录: {self.runtime_dir}")
        self._append_log("info", f"🗃️ SQLite数据库: {self.db_path}")
        if self.db_fallback_reason:
            self._append_log("warn", self.db_fallback_reason)

    def detect_runtime_dir(self) -> str:
        return detect_runtime_dir()

    def _ensure_writable_dir(self, directory: Path) -> tuple[bool, str]:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return False, f"创建目录失败: {e}"

        probe = directory / f".wp4ai_write_probe_{os.getpid()}_{int(time.time() * 1000)}.tmp"
        try:
            probe.write_text("ok", encoding="utf-8")
            return True, ""
        except Exception as e:
            return False, f"写入测试失败: {e}"
        finally:
            try:
                if probe.exists():
                    probe.unlink()
            except Exception:
                pass

    def resolve_db_path(self) -> str:
        base = Path(self.runtime_dir)
        candidate = base / SITE_DB_FILENAME
        ok, reason = self._ensure_writable_dir(base)
        if ok:
            return str(candidate)

        fallback_dir = Path(tempfile.gettempdir()) / "wp4ai_gui"
        fallback_ok, fallback_reason = self._ensure_writable_dir(fallback_dir)
        if fallback_ok:
            self.db_fallback_reason = (
                f"⚠️ 程序目录不可写（{reason}），已自动切换数据库到临时目录: {fallback_dir}"
            )
            return str(fallback_dir / SITE_DB_FILENAME)

        raise RuntimeError(
            f"程序目录不可写（{reason}），且临时目录也不可写（{fallback_reason}）。"
        )

    def _db_connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def init_database(self):
        with self._db_connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sites (
                    site_id TEXT PRIMARY KEY,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    name TEXT NOT NULL,
                    products_dir TEXT NOT NULL,
                    wp_domain TEXT NOT NULL,
                    wp_username TEXT NOT NULL,
                    wp_password TEXT NOT NULL,
                    ai_api_key TEXT NOT NULL,
                    ai_base_url TEXT NOT NULL,
                    default_model TEXT NOT NULL,
                    bak_ai_api_key TEXT NOT NULL,
                    bak_ai_base_url TEXT NOT NULL,
                    bak_default_model TEXT NOT NULL,
                    seo_min_score TEXT NOT NULL DEFAULT '65',
                    seo_max_retries TEXT NOT NULL DEFAULT '5',
                    seo_system_prompt TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL DEFAULT 'full',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            site_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(sites)").fetchall()
            }
            if "seo_system_prompt" not in site_columns:
                conn.execute(
                    "ALTER TABLE sites ADD COLUMN seo_system_prompt TEXT NOT NULL DEFAULT ''"
                )

    def _db_get_setting(self, key: str, default_value: str) -> str:
        with self._db_connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
            if row and row["value"] is not None:
                return str(row["value"])
        return default_value

    def _db_set_setting(self, key: str, value: str):
        with self._db_connect() as conn:
            conn.execute("""
                INSERT INTO app_settings(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """, (key, value))

    def _load_max_parallel(self) -> int:
        raw = self._db_get_setting("max_parallel_sites", str(DEFAULT_MAX_PARALLEL))
        try:
            value = int(raw)
        except Exception:
            value = DEFAULT_MAX_PARALLEL
        return max(MAX_PARALLEL_MIN, min(MAX_PARALLEL_MAX, value))

    def _save_max_parallel(self):
        val = self._get_max_parallel()
        self._db_set_setting("max_parallel_sites", str(val))

    def _count_sites_in_db(self) -> int:
        with self._db_connect() as conn:
            row = conn.execute("SELECT COUNT(1) AS c FROM sites").fetchone()
            return int(row["c"]) if row else 0

    def _migrate_legacy_json_if_needed(self):
        if self._count_sites_in_db() > 0:
            return
        if not os.path.isfile(self.legacy_json_path):
            return

        try:
            raw = Path(self.legacy_json_path).read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else []
            migrated = [self._site_from_dict(item) for item in data if isinstance(item, dict)]
            if not migrated:
                return
            self.sites = migrated
            self._save_sites()
            self._append_log("info", f"✅ 已从旧配置文件迁移 {len(migrated)} 个站点到 SQLite。")
        except Exception as e:
            self._append_log("warn", f"⚠️ 旧配置迁移失败（不影响继续使用）: {e}")

    def _build_ui(self):
        container = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        container.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        left = ttk.Frame(container, width=320)
        right = ttk.Frame(container)
        container.add(left, weight=1)
        container.add(right, weight=4)

        self._build_left_panel(left)
        self._build_right_panel(right)

    def _build_left_panel(self, parent: ttk.Frame):
        ttk.Label(parent, text="站点列表", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(0, 6))

        self.var_max_parallel = tk.IntVar(value=DEFAULT_MAX_PARALLEL)

        self.site_listbox = tk.Listbox(parent, activestyle="none")
        self.site_listbox.pack(fill=tk.BOTH, expand=True)
        self.site_listbox.bind("<<ListboxSelect>>", self.on_select_site)

        btns = ttk.Frame(parent)
        btns.pack(fill=tk.X, pady=8)
        ttk.Button(btns, text="新增站点", command=self.on_add_site).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="复制站点", command=self.on_clone_site).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="删除站点", command=self.on_delete_site).pack(side=tk.LEFT)

        run_frame = ttk.LabelFrame(parent, text="执行")
        run_frame.pack(fill=tk.X, pady=(10, 0))

        parallel_row = ttk.Frame(run_frame)
        parallel_row.pack(fill=tk.X, padx=8, pady=(8, 4))
        ttk.Label(parallel_row, text="并发站点数").pack(side=tk.LEFT)
        self.max_parallel_spin = ttk.Spinbox(
            parallel_row,
            from_=MAX_PARALLEL_MIN,
            to=MAX_PARALLEL_MAX,
            textvariable=self.var_max_parallel,
            width=8
        )
        self.max_parallel_spin.pack(side=tk.LEFT, padx=(8, 6))
        ttk.Label(parallel_row, text=f"建议 1-{MAX_PARALLEL_MAX}").pack(side=tk.LEFT)

        self.btn_run_selected = ttk.Button(run_frame, text="运行选中站点", command=self.on_run_selected)
        self.btn_run_selected.pack(fill=tk.X, padx=8, pady=(6, 4))
        self.btn_run_all = ttk.Button(run_frame, text="运行全部启用站点", command=self.on_run_all_enabled)
        self.btn_run_all.pack(fill=tk.X, padx=8, pady=4)
        self.btn_stop_run = ttk.Button(run_frame, text="停止执行", command=self.on_stop_run, state=tk.DISABLED)
        self.btn_stop_run.pack(fill=tk.X, padx=8, pady=(4, 6))

        ttk.Label(
            run_frame,
            text="目录分配策略：并发执行时，每个站点必须绑定独立产品目录，禁止目录重复或父子目录重叠。",
            wraplength=280,
            foreground="#444444",
            justify=tk.LEFT
        ).pack(fill=tk.X, padx=8, pady=(0, 8))

    def _build_right_panel(self, parent: ttk.Frame):
        form_wrap = ttk.Frame(parent)
        form_wrap.pack(fill=tk.BOTH, expand=False)

        fields = ttk.LabelFrame(form_wrap, text="站点配置")
        fields.pack(fill=tk.X, padx=4, pady=(0, 8))

        self.var_enabled = tk.BooleanVar(value=True)
        self.var_name = tk.StringVar()
        self.var_products_dir = tk.StringVar()
        self.var_wp_domain = tk.StringVar()
        self.var_wp_username = tk.StringVar()
        self.var_wp_password = tk.StringVar()
        self.var_ai_api_key = tk.StringVar()
        self.var_ai_base_url = tk.StringVar()
        self.var_default_model = tk.StringVar()
        self.var_bak_ai_api_key = tk.StringVar()
        self.var_bak_ai_base_url = tk.StringVar()
        self.var_bak_default_model = tk.StringVar()
        self.var_seo_min_score = tk.StringVar(value="65")
        self.var_seo_max_retries = tk.StringVar(value="5")
        self.var_mode = tk.StringVar(value="full")
        self.seo_prompt_text = None

        row = 0
        ttk.Checkbutton(fields, text="启用该站点", variable=self.var_enabled).grid(row=row, column=0, sticky="w", padx=8, pady=6)
        row += 1

        self._grid_entry(fields, row, "站点名称", self.var_name)
        row += 1

        self._grid_entry(fields, row, "产品目录", self.var_products_dir, with_browse=True)
        row += 1

        self._grid_entry(fields, row, "站点域名（WP_DOMAIN）", self.var_wp_domain)
        row += 1
        self._grid_entry(fields, row, "站点账号（WP_USERNAME）", self.var_wp_username)
        row += 1
        self._grid_entry(fields, row, "站点密码（WP_PASSWORD）", self.var_wp_password, show="*")
        row += 1

        self._grid_entry(fields, row, "主AI密钥（AI_API_KEY）", self.var_ai_api_key, show="*")
        row += 1
        self._grid_entry(fields, row, "主AI地址（AI_BASE_URL）", self.var_ai_base_url)
        row += 1
        self._grid_entry(fields, row, "主AI模型（DEFAULT_MODEL）", self.var_default_model)
        row += 1

        self._grid_entry(fields, row, "备用AI密钥（BAK_AI_API_KEY）", self.var_bak_ai_api_key, show="*")
        row += 1
        self._grid_entry(fields, row, "备用AI地址（BAK_AI_BASE_URL）", self.var_bak_ai_base_url)
        row += 1
        self._grid_entry(fields, row, "备用AI模型（BAK_DEFAULT_MODEL）", self.var_bak_default_model)
        row += 1

        self._grid_entry(fields, row, "SEO最低分（SEO_MIN_SCORE）", self.var_seo_min_score)
        row += 1
        self._grid_entry(fields, row, "SEO重试次数（SEO_MAX_RETRIES）", self.var_seo_max_retries)
        row += 1

        ttk.Label(fields, text="自定义SEO系统提示词（留空使用默认）").grid(
            row=row, column=0, sticky="nw", padx=8, pady=6
        )
        prompt_frame = ttk.Frame(fields)
        prompt_frame.grid(row=row, column=1, sticky="nsew", padx=8, pady=6)
        prompt_frame.grid_columnconfigure(0, weight=1)
        prompt_frame.grid_rowconfigure(0, weight=1)
        self.seo_prompt_text = tk.Text(prompt_frame, height=8, wrap="word")
        self.seo_prompt_text.grid(row=0, column=0, sticky="nsew")
        prompt_scroll = ttk.Scrollbar(
            prompt_frame, orient=tk.VERTICAL, command=self.seo_prompt_text.yview
        )
        prompt_scroll.grid(row=0, column=1, sticky="ns")
        self.seo_prompt_text.configure(yscrollcommand=prompt_scroll.set)
        fields.grid_rowconfigure(row, weight=1)
        row += 1

        ttk.Label(fields, text="运行模式").grid(row=row, column=0, sticky="w", padx=8, pady=6)
        self.mode_combo = ttk.Combobox(
            fields,
            state="readonly",
            values=list(MODE_MAP.values()),
            width=42,
        )
        self.mode_combo.grid(row=row, column=1, sticky="we", padx=8, pady=6)
        self.mode_combo.bind("<<ComboboxSelected>>", self.on_mode_changed)
        self.mode_combo.set(MODE_MAP["full"])
        row += 1

        fields.grid_columnconfigure(1, weight=1)

        action_bar = ttk.Frame(parent)
        action_bar.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(action_bar, text="保存当前站点", command=self.on_save_current).pack(side=tk.LEFT, padx=(4, 6))
        ttk.Button(action_bar, text="保存全部配置", command=self.on_save_all).pack(side=tk.LEFT, padx=6)
        ttk.Button(action_bar, text="测试当前站点连接", command=self.on_test_connection).pack(side=tk.LEFT, padx=6)
        ttk.Button(action_bar, text="打开产品目录", command=self.on_open_products_dir).pack(side=tk.LEFT, padx=6)

        log_frame = ttk.LabelFrame(parent, text="运行日志")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))

        self.log_text = tk.Text(log_frame, wrap="none")
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        ys = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        ys.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.config(yscrollcommand=ys.set)

    def _grid_entry(self, parent, row: int, label: str, var: tk.StringVar, with_browse: bool = False, show: str | None = None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=6)
        entry = ttk.Entry(parent, textvariable=var, show=show if show else "")
        entry.grid(row=row, column=1, sticky="we", padx=8, pady=6)
        if with_browse:
            ttk.Button(parent, text="浏览", command=self.on_browse_products_dir).grid(row=row, column=2, sticky="e", padx=(0, 8), pady=6)

    def _poll_log_queue(self):
        try:
            while True:
                level, message = self.log_queue.get_nowait()
                if level == "runner_done":
                    self.runner = None
                    self._set_running_ui(False)
                else:
                    self._append_log(level, message)
        except queue.Empty:
            pass
        self.root.after(150, self._poll_log_queue)

    def _append_log(self, level: str, message: str):
        ts = time.strftime("%H:%M:%S")
        if level == "raw":
            text = f"{message}\n"
        else:
            text = f"[{ts}] {message}\n"
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)

    def _load_sites(self):
        self._migrate_legacy_json_if_needed()
        self.var_max_parallel.set(self._load_max_parallel())

        loaded: list[SiteConfig] = []
        try:
            with self._db_connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM sites ORDER BY sort_order ASC, updated_at DESC"
                ).fetchall()
                for row in rows:
                    loaded.append(self._site_from_dict(dict(row)))
        except Exception as e:
            self._append_log("error", f"❌ 读取 SQLite 站点配置失败: {e}")
            loaded = []

        self.sites = loaded
        if not self.sites:
            default = SiteConfig.new_default()
            self.sites = [default]
            self.current_site_id = default.site_id
            self._show_site(default)
            self.on_save_all(silent=True)
            return

        self.current_site_id = self.sites[0].site_id
        self._show_site(self.sites[0])

    def _save_sites(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        keep_ids = {site.site_id for site in self.sites}
        with self._db_connect() as conn:
            for idx, site in enumerate(self.sites):
                created_at = now
                row = conn.execute(
                    "SELECT created_at FROM sites WHERE site_id = ?",
                    (site.site_id,)
                ).fetchone()
                if row and row["created_at"]:
                    created_at = str(row["created_at"])

                conn.execute("""
                    INSERT INTO sites (
                        site_id, sort_order, enabled, name, products_dir,
                        wp_domain, wp_username, wp_password,
                        ai_api_key, ai_base_url, default_model,
                        bak_ai_api_key, bak_ai_base_url, bak_default_model,
                        seo_min_score, seo_max_retries, seo_system_prompt, mode,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(site_id) DO UPDATE SET
                        sort_order = excluded.sort_order,
                        enabled = excluded.enabled,
                        name = excluded.name,
                        products_dir = excluded.products_dir,
                        wp_domain = excluded.wp_domain,
                        wp_username = excluded.wp_username,
                        wp_password = excluded.wp_password,
                        ai_api_key = excluded.ai_api_key,
                        ai_base_url = excluded.ai_base_url,
                        default_model = excluded.default_model,
                        bak_ai_api_key = excluded.bak_ai_api_key,
                        bak_ai_base_url = excluded.bak_ai_base_url,
                        bak_default_model = excluded.bak_default_model,
                        seo_min_score = excluded.seo_min_score,
                        seo_max_retries = excluded.seo_max_retries,
                        seo_system_prompt = excluded.seo_system_prompt,
                        mode = excluded.mode,
                        updated_at = excluded.updated_at
                """, (
                    site.site_id, idx, 1 if site.enabled else 0, site.name, site.products_dir,
                    site.wp_domain, site.wp_username, site.wp_password,
                    site.ai_api_key, site.ai_base_url, site.default_model,
                    site.bak_ai_api_key, site.bak_ai_base_url, site.bak_default_model,
                    site.seo_min_score, site.seo_max_retries, site.seo_system_prompt,
                    site.mode, created_at, now
                ))

            if keep_ids:
                placeholders = ",".join("?" for _ in keep_ids)
                conn.execute(f"DELETE FROM sites WHERE site_id NOT IN ({placeholders})", tuple(keep_ids))
            else:
                conn.execute("DELETE FROM sites")

    def _site_from_dict(self, item: dict) -> SiteConfig:
        default = SiteConfig.new_default()
        data = asdict(default)
        for k, v in item.items():
            if k in data:
                data[k] = v
        if not data.get("site_id"):
            data["site_id"] = str(uuid.uuid4())
        mode = str(data.get("mode", "full")).strip().lower()
        if mode not in MODE_MAP:
            mode = "full"
        data["mode"] = mode
        data["enabled"] = bool(data.get("enabled", True))
        return SiteConfig(**data)

    def _render_site_list(self):
        self.site_listbox.delete(0, tk.END)
        selected_idx = 0
        for idx, site in enumerate(self.sites):
            prefix = "[x]" if site.enabled else "[ ]"
            label = f"{prefix} {site.name}"
            self.site_listbox.insert(tk.END, label)
            if site.site_id == self.current_site_id:
                selected_idx = idx

        if self.sites:
            self.site_listbox.selection_clear(0, tk.END)
            self.site_listbox.selection_set(selected_idx)
            self.site_listbox.activate(selected_idx)

    def _find_current_site(self) -> SiteConfig | None:
        if not self.current_site_id:
            return None
        for site in self.sites:
            if site.site_id == self.current_site_id:
                return site
        return None

    def _show_site(self, site: SiteConfig):
        self.var_enabled.set(site.enabled)
        self.var_name.set(site.name)
        self.var_products_dir.set(site.products_dir)
        self.var_wp_domain.set(site.wp_domain)
        self.var_wp_username.set(site.wp_username)
        self.var_wp_password.set(site.wp_password)
        self.var_ai_api_key.set(site.ai_api_key)
        self.var_ai_base_url.set(site.ai_base_url)
        self.var_default_model.set(site.default_model)
        self.var_bak_ai_api_key.set(site.bak_ai_api_key)
        self.var_bak_ai_base_url.set(site.bak_ai_base_url)
        self.var_bak_default_model.set(site.bak_default_model)
        self.var_seo_min_score.set(site.seo_min_score)
        self.var_seo_max_retries.set(site.seo_max_retries)
        self.seo_prompt_text.delete("1.0", tk.END)
        self.seo_prompt_text.insert("1.0", site.seo_system_prompt)
        self.var_mode.set(site.mode)
        self.mode_combo.set(MODE_MAP.get(site.mode, MODE_MAP["full"]))

    def _collect_form_to_site(self, site: SiteConfig):
        site.enabled = bool(self.var_enabled.get())
        site.name = self.var_name.get().strip() or "未命名站点"
        site.products_dir = self.var_products_dir.get().strip()
        site.wp_domain = self.var_wp_domain.get().strip()
        site.wp_username = self.var_wp_username.get().strip()
        site.wp_password = self.var_wp_password.get().strip()
        site.ai_api_key = self.var_ai_api_key.get().strip()
        site.ai_base_url = self.var_ai_base_url.get().strip()
        site.default_model = self.var_default_model.get().strip()
        site.bak_ai_api_key = self.var_bak_ai_api_key.get().strip()
        site.bak_ai_base_url = self.var_bak_ai_base_url.get().strip()
        site.bak_default_model = self.var_bak_default_model.get().strip()
        site.seo_min_score = self.var_seo_min_score.get().strip() or "65"
        site.seo_max_retries = self.var_seo_max_retries.get().strip() or "5"
        site.seo_system_prompt = self.seo_prompt_text.get("1.0", "end-1c").strip()
        mode_raw = self.var_mode.get().strip().lower()
        site.mode = mode_raw if mode_raw in MODE_MAP else "full"

    def validate_site(self, site: SiteConfig) -> tuple[bool, list[str]]:
        errs = []
        if not site.name.strip():
            errs.append("站点名称为空")
        if not site.products_dir.strip():
            errs.append("产品目录为空")
        elif not os.path.isdir(site.products_dir):
            errs.append("产品目录不存在")
        if not site.wp_domain.strip():
            errs.append("站点域名为空")
        if not site.wp_username.strip():
            errs.append("站点账号为空")
        if not site.wp_password.strip():
            errs.append("站点密码为空")
        if not site.ai_api_key.strip():
            errs.append("主AI密钥为空")
        if not site.ai_base_url.strip():
            errs.append("主AI地址为空")
        if not site.default_model.strip():
            errs.append("主AI模型为空")

        try:
            int(site.seo_min_score.strip())
        except Exception:
            errs.append("SEO最低分不是整数")
        try:
            int(site.seo_max_retries.strip())
        except Exception:
            errs.append("SEO重试次数不是整数")

        backup_fields = (
            site.bak_ai_api_key.strip(),
            site.bak_ai_base_url.strip(),
            site.bak_default_model.strip(),
        )
        if any(backup_fields) and not all(backup_fields):
            errs.append("备用AI配置必须同时填写密钥、地址和模型")

        if site.mode not in MODE_MAP:
            errs.append("运行模式无效")

        return len(errs) == 0, errs

    def write_temp_config(self, site: SiteConfig) -> str:
        cp = configparser.ConfigParser(interpolation=None)
        cp["AI"] = {
            "AI_API_KEY": site.ai_api_key,
            "AI_BASE_URL": site.ai_base_url,
            "DEFAULT_MODEL": site.default_model,
            "BAK_AI_API_KEY": site.bak_ai_api_key,
            "BAK_AI_BASE_URL": site.bak_ai_base_url,
            "BAK_DEFAULT_MODEL": site.bak_default_model,
        }
        cp["WordPress"] = {
            "WP_DOMAIN": site.wp_domain,
            "WP_USERNAME": site.wp_username,
            "WP_PASSWORD": site.wp_password,
        }
        cp["SEO"] = {
            "SEO_MIN_SCORE": site.seo_min_score,
            "SEO_MAX_RETRIES": site.seo_max_retries,
            "SEO_SYSTEM_PROMPT": site.seo_system_prompt,
        }

        fd, temp_path = tempfile.mkstemp(prefix="wp4ai_cfg_", suffix=".ini")
        os.close(fd)
        with open(temp_path, "w", encoding="utf-8") as f:
            cp.write(f)
        return temp_path

    def resolve_worker_command(self) -> list[str]:
        runtime = self.runtime_dir
        worker_exe = os.path.join(runtime, WORKER_EXE_NAME)
        worker_py = os.path.join(runtime, WORKER_PY_NAME)

        if os.path.isfile(worker_exe):
            return [worker_exe]

        # 开发模式：优先使用当前 Python 解释器执行 .py
        if os.path.isfile(worker_py):
            return [sys.executable, worker_py]

        raise FileNotFoundError(
            f"未找到执行引擎：{worker_exe} 或 {worker_py}"
        )

    def on_select_site(self, _event=None):
        idxs = self.site_listbox.curselection()
        if not idxs:
            return
        idx = idxs[0]
        if idx < 0 or idx >= len(self.sites):
            return
        self._flush_current_form()
        self.current_site_id = self.sites[idx].site_id
        self._show_site(self.sites[idx])

    def _flush_current_form(self):
        site = self._find_current_site()
        if not site:
            return
        self._collect_form_to_site(site)

    def on_add_site(self):
        self._flush_current_form()
        site = SiteConfig.new_default()
        self.sites.append(site)
        self.current_site_id = site.site_id
        self._show_site(site)
        self._render_site_list()

    def on_clone_site(self):
        self._flush_current_form()
        current = self._find_current_site()
        if not current:
            return
        new_site = SiteConfig(**asdict(current))
        new_site.site_id = str(uuid.uuid4())
        new_site.name = f"{current.name}-复制"
        self.sites.append(new_site)
        self.current_site_id = new_site.site_id
        self._show_site(new_site)
        self._render_site_list()

    def on_delete_site(self):
        if len(self.sites) <= 1:
            messagebox.showwarning(APP_TITLE, "至少保留一个站点。")
            return
        current = self._find_current_site()
        if not current:
            return
        ok = messagebox.askyesno(APP_TITLE, f"确认删除站点 [{current.name}] 吗？")
        if not ok:
            return
        self.sites = [s for s in self.sites if s.site_id != current.site_id]
        self.current_site_id = self.sites[0].site_id
        self._show_site(self.sites[0])
        self._render_site_list()
        self.on_save_all(silent=True)

    def on_browse_products_dir(self):
        chosen = filedialog.askdirectory(title="选择产品目录")
        if chosen:
            self.var_products_dir.set(chosen)

    def on_open_products_dir(self):
        path = self.var_products_dir.get().strip()
        if not path:
            messagebox.showwarning(APP_TITLE, "请先填写产品目录。")
            return
        if not os.path.isdir(path):
            messagebox.showwarning(APP_TITLE, f"目录不存在: {path}")
            return
        try:
            os.startfile(path)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"打开目录失败: {e}")

    def _validate_connection_fields(self, site: SiteConfig) -> tuple[bool, list[str]]:
        errs = []
        if not site.wp_domain.strip():
            errs.append("站点域名不能为空")
        if not site.wp_username.strip():
            errs.append("站点账号不能为空")
        if not site.wp_password.strip():
            errs.append("站点密码不能为空")
        if not site.ai_api_key.strip():
            errs.append("主AI密钥不能为空")
        if not site.ai_base_url.strip():
            errs.append("主AI地址不能为空")
        if not site.default_model.strip():
            errs.append("主AI模型不能为空")
        backup_fields = (
            site.bak_ai_api_key.strip(),
            site.bak_ai_base_url.strip(),
            site.bak_default_model.strip(),
        )
        if any(backup_fields) and not all(backup_fields):
            errs.append("备用AI配置必须同时填写密钥、地址和模型")
        return len(errs) == 0, errs

    def _test_wp_connection(self, site: SiteConfig) -> tuple[bool, str]:
        url = site.wp_domain.rstrip("/") + "/wp-json/wp/v2/settings"
        try:
            resp = requests.get(
                url,
                auth=HTTPBasicAuth(site.wp_username, site.wp_password),
                timeout=CONNECTION_TEST_TIMEOUT,
            )
            if resp.status_code >= 400:
                return False, f"WordPress 连接失败，HTTP {resp.status_code}: {resp.text[:180]}"

            content_type = str(resp.headers.get("Content-Type", ""))
            body_head = (resp.text or "")[:180].replace("\n", " ").replace("\r", " ")
            if _looks_like_sgcaptcha(resp.text):
                return False, (
                    "WordPress API 被 SiteGround SGCaptcha/Anti-Bot 拦截。"
                    "请在主机/WAF 放行 wp-json 和 rest_route 请求。"
                )

            title = ""
            try:
                payload_raw = resp.json()
                payload = payload_raw if isinstance(payload_raw, dict) else {}
                title = str(payload.get("title", "")).strip()
            except Exception as e:
                return False, (
                    f"WordPress 返回非 JSON 响应，HTTP {resp.status_code}, "
                    f"Content-Type={content_type}, Body={body_head}, 异常={e}"
                )

            title_text = title if title else "未返回站点标题"
            return True, f"WordPress 连接成功，站点标题: {title_text}"
        except Exception as e:
            return False, f"WordPress 连接异常: {e}"

    def _test_ai_connection(
        self,
        label: str,
        base_url: str,
        api_key: str,
        model: str = "",
    ) -> tuple[bool, str]:
        url = base_url.rstrip("/") + "/models"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }
        try:
            resp = requests.get(url, headers=headers, timeout=CONNECTION_TEST_TIMEOUT)
            if resp.status_code >= 400:
                return False, (
                    f"{label} 连接失败，HTTP {resp.status_code}: "
                    f"{_redact_ai_error_text(resp.text)}"
                )
            model_count = 0
            try:
                payload_raw = resp.json()
                payload = payload_raw if isinstance(payload_raw, dict) else {}
                data = payload.get("data")
                if isinstance(data, list):
                    model_count = len(data)
                    model_ids = {
                        str(item.get("id", "")).strip()
                        for item in data
                        if isinstance(item, dict) and str(item.get("id", "")).strip()
                    }
                    if model and model_ids and model not in model_ids:
                        return False, (
                            f"{label} 地址可连接，但模型 [{model}] 不在该地址返回的可用模型列表中；"
                            "请检查模型名称与 base URL 是否属于同一服务商。"
                        )
            except Exception:
                model_count = 0
            return True, f"{label} 连接成功，可用模型数: {model_count}"
        except Exception as e:
            return False, f"{label} 连接异常: {_redact_ai_error_text(e)}"

    def _run_connection_test(self, site: SiteConfig):
        self.log_queue.put(("info", f"🔎 开始测试站点连接: {site.name}"))

        wp_ok, wp_msg = self._test_wp_connection(site)
        self.log_queue.put(("info" if wp_ok else "error", f"{'✅' if wp_ok else '❌'} {wp_msg}"))

        ai_ok, ai_msg = self._test_ai_connection(
            "主AI", site.ai_base_url, site.ai_api_key, site.default_model
        )
        self.log_queue.put(("info" if ai_ok else "error", f"{'✅' if ai_ok else '❌'} {ai_msg}"))

        # 备用 AI 仅在配置完整时测试
        bak_key = site.bak_ai_api_key.strip()
        bak_url = site.bak_ai_base_url.strip()
        bak_model = site.bak_default_model.strip()
        bak_ok = True
        if bak_key and bak_url and bak_model:
            bak_ok, bak_msg = self._test_ai_connection(
                "备用AI", bak_url, bak_key, bak_model
            )
            self.log_queue.put(("info" if bak_ok else "error", f"{'✅' if bak_ok else '❌'} {bak_msg}"))
        else:
            if any((bak_key, bak_url, bak_model)):
                bak_ok = False
                self.log_queue.put(("error", "❌ 备用AI配置不完整，无法进行备用AI连接测试。"))
            else:
                self.log_queue.put(("warn", "⚠️ 未配置备用AI，已跳过备用AI连接测试。"))

        if wp_ok and ai_ok and bak_ok:
            self.log_queue.put(("info", "🎉 当前站点连接测试通过。"))
        else:
            self.log_queue.put(("warn", "⚠️ 当前站点连接测试未完全通过，请根据日志修正配置。"))

        self.connection_test_thread = None

    def on_test_connection(self):
        if self.connection_test_thread and self.connection_test_thread.is_alive():
            messagebox.showwarning(APP_TITLE, "连接测试正在进行中，请稍候。")
            return

        self._flush_current_form()
        site = self._find_current_site()
        if not site:
            return
        self._collect_form_to_site(site)

        valid, errs = self._validate_connection_fields(site)
        if not valid:
            messagebox.showwarning(APP_TITLE, "连接测试前请先完善配置：\n- " + "\n- ".join(errs))
            return

        snapshot = SiteConfig(**asdict(site))
        self.connection_test_thread = threading.Thread(
            target=self._run_connection_test,
            args=(snapshot,),
            daemon=True
        )
        self.connection_test_thread.start()

    def _get_max_parallel(self) -> int:
        raw = str(self.var_max_parallel.get()).strip()
        try:
            value = int(raw)
        except Exception:
            value = DEFAULT_MAX_PARALLEL
        value = max(MAX_PARALLEL_MIN, min(MAX_PARALLEL_MAX, value))
        self.var_max_parallel.set(value)
        return value

    def _validate_directory_allocation(self, targets: list[SiteConfig], max_parallel: int) -> tuple[bool, list[str]]:
        if max_parallel <= 1 or len(targets) <= 1:
            return True, []

        norm_map: dict[str, list[str]] = {}
        abs_paths: list[tuple[str, str]] = []
        for site in targets:
            path_raw = site.products_dir.strip()
            if not path_raw:
                continue
            abs_path = os.path.normcase(os.path.abspath(path_raw))
            norm_map.setdefault(abs_path, []).append(site.name)
            abs_paths.append((site.name, abs_path))

        issues: list[str] = []
        for p, names in norm_map.items():
            if len(names) > 1:
                issues.append(f"目录重复：{p} 被站点 {', '.join(names)} 共同使用")

        # 检测父子目录重叠，避免并发重命名/扫描冲突
        for i in range(len(abs_paths)):
            name_a, path_a = abs_paths[i]
            for j in range(i + 1, len(abs_paths)):
                name_b, path_b = abs_paths[j]
                if path_a == path_b:
                    continue
                try:
                    common = os.path.commonpath([path_a, path_b])
                    if common == path_a or common == path_b:
                        issues.append(f"目录重叠：站点 [{name_a}] 与 [{name_b}] 存在父子目录关系")
                except Exception:
                    continue

        return len(issues) == 0, issues

    def on_mode_changed(self, _event=None):
        raw = self.mode_combo.get().strip()
        mode = MODE_LABEL_TO_CODE.get(raw)
        if mode in MODE_MAP:
            self.var_mode.set(mode)

    def on_save_current(self):
        site = self._find_current_site()
        if not site:
            return
        self._collect_form_to_site(site)
        self._render_site_list()
        self.on_save_all(silent=False)

    def on_save_all(self, silent: bool = False):
        self._flush_current_form()
        try:
            self._save_sites()
            self._save_max_parallel()
            self._render_site_list()
            if not silent:
                self._append_log("info", "💾 配置已保存到 SQLite。")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"保存失败: {e}")

    def _set_running_ui(self, is_running: bool):
        state = tk.DISABLED if is_running else tk.NORMAL
        try:
            self.btn_run_selected.configure(state=state)
            self.btn_run_all.configure(state=state)
            self.max_parallel_spin.configure(state=state)
            self.btn_stop_run.configure(state=tk.NORMAL if is_running else tk.DISABLED)
        except Exception:
            pass
        if is_running:
            self._append_log("info", "⏳ 执行中，已锁定重复启动。")
        else:
            self._append_log("info", "✅ 执行器空闲。")

    def on_run_selected(self):
        if self.runner:
            messagebox.showwarning(APP_TITLE, "已有任务正在执行，请先停止或等待完成。")
            return
        self._flush_current_form()
        site = self._find_current_site()
        if not site:
            return
        self.on_save_all(silent=True)
        self.runner = RunnerThread(self, [site], max_parallel=1)
        self._set_running_ui(True)
        self.runner.start()

    def on_run_all_enabled(self):
        if self.runner:
            messagebox.showwarning(APP_TITLE, "已有任务正在执行，请先停止或等待完成。")
            return
        self._flush_current_form()
        self.on_save_all(silent=True)
        targets = [s for s in self.sites if s.enabled]
        if not targets:
            messagebox.showwarning(APP_TITLE, "没有启用的站点。")
            return
        max_parallel = self._get_max_parallel()
        ok, issues = self._validate_directory_allocation(targets, max_parallel)
        if not ok:
            msg = "并发运行前发现产品目录分配冲突：\n- " + "\n- ".join(issues)
            msg += "\n\n请为每个站点配置独立目录，或将并发站点数改为 1。"
            messagebox.showwarning(APP_TITLE, msg)
            self._append_log("warn", "⚠️ 并发运行被阻止：存在目录冲突。")
            return

        self.runner = RunnerThread(self, targets, max_parallel=max_parallel)
        self._set_running_ui(True)
        self.runner.start()

    def on_stop_run(self):
        if not self.runner:
            return
        self.runner.stop()
        self._append_log("warn", "⏹ 正在请求停止，请稍候...")


def main():
    try:
        root = tk.Tk()
        style = ttk.Style(root)
        try:
            style.theme_use("vista")
        except Exception:
            pass
        app = WP4AIGui(root)
        root.mainloop()
    except Exception:
        exc_text = traceback.format_exc()
        crash_log = write_startup_crash_log(exc_text)
        tail = exc_text.strip().splitlines()[-1] if exc_text.strip() else "未知异常"
        msg = f"GUI 启动失败：{tail}"
        if crash_log:
            msg += f"\n\n崩溃日志已保存：\n{crash_log}"
        show_startup_error_dialog(msg)
        return


if __name__ == "__main__":
    main()
