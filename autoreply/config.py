"""Settings: a typed schema whose values live in the database.

Everything the bot does is configurable from the admin panel. Values are
resolved as: database (saved from the panel) > environment variable > default.
Only a few bootstrap secrets (admin password, encryption key, HF token) are
environment-only - see ``Bootstrap``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_SYSTEM_PROMPT = """\
Sen {owner_name} ning Telegram akkauntida ishlaydigan shaxsiy AI yordamchisan.
{owner_name} hozir band yoki onlayn emas, shuning uchun unga yozgan odamlarga
sen javob berasan.

Qoidalar:
- Suhbatdoshning tilida javob ber (o'zbek, rus, ingliz va h.k.).
- Qisqa, tabiiy va samimiy yoz, xuddi oddiy chatdagidek.
- O'zingni {owner_name} deb tanishtirma. "Botmisan?" deb so'rashsa, rostini ayt:
  sen {owner_name} ning AI yordamchisisan.
- {owner_name} nomidan va'da berma; pul, to'lov, qarz, uchrashuv yoki muhim
  qarorlarni muhokama qilma va tasdiqlama - "{owner_name} o'zi javob beradi" de.
- Parol, kod, karta raqami kabi maxfiy ma'lumotlarni so'rama va berma.
- Bilmagan narsangni o'ylab topma.

{owner_name} haqida:
{owner_info}
"""

DEFAULT_AWAY_NOTICE = (
    "Assalomu alaykum! Hozir onlayn emasman. Xabaringizga vaqtincha "
    "AI yordamchim javob beradi, qaytgach o'zim ham ko'rib chiqaman. 🙂"
)

DEFAULT_DEBT_REPLY = (
    "Men {owner_name} ning AI yordamchisiman. Moliyaviy va qarz masalalarini "
    "muhokama qila olmayman. Xabaringizni {owner_name} ga yetkazdim - u o'zi javob beradi."
)

DEFAULT_FREE_MODELS = [
    "google/gemma-3-12b-it:free",
    "mistralai/mistral-small-3.2-24b-instruct:free",
    "meta-llama/llama-4-maverick:free",
]


@dataclass(frozen=True)
class Field:
    key: str
    type: str  # bool | int | float | str | text | list | secret | secret_list | time
    default: Any
    group: str
    label: str
    help: str = ""
    env: str | None = None
    restart: bool = False  # Telegram must reconnect to apply
    min: float | None = None
    max: float | None = None


GROUPS = {
    "ai": "Sun'iy intellekt",
    "behavior": "Xulq-atvor",
    "persona": "Shaxsiyat va matnlar",
    "owner": "Ikkinchi akkaunt",
    "channel": "Kanal e'lonlari",
    "telegram": "Telegram ulanishi",
}

F = Field
FIELDS: list[Field] = [
    # --- AI ---
    F("ai_enabled", "bool", True, "ai", "Avtojavob (AI)",
      "O'chirilsa, bot hech kimga javob bermaydi.", env="AWAY_ON_START"),
    F("openrouter_api_keys", "secret_list", [], "ai", "OpenRouter API kalitlari",
      "Bir nechta kalit: biri ishlamasa (limit, xato) keyingisi avtomatik ishlatiladi.",
      env="OPENROUTER_API_KEYS"),
    F("openrouter_model", "str", "google/gemma-3-27b-it:free", "ai", "Asosiy model",
      "OpenRouter'dagi istalgan model. Rasm uchun multimodal model tanlang."),
    F("openrouter_fallback_models", "list", DEFAULT_FREE_MODELS, "ai", "Zaxira modellar",
      "Asosiy model ishlamasa navbat bilan sinaladi."),
    F("vision_enabled", "bool", True, "ai", "Rasmlarni ko'rish",
      "Yuborilgan rasmlar AI ga ko'rsatiladi (multimodal model kerak)."),
    F("reasoning_disabled", "bool", True, "ai", "Thinking rejimini o'chirish",
      "Model 'o'ylash' bosqichisiz tez javob beradi."),
    F("temperature", "float", 0.7, "ai", "Ijodkorlik (temperature)", "", env="OPENROUTER_TEMPERATURE",
      min=0, max=2),
    F("max_tokens", "int", 600, "ai", "Javob uzunligi (token)", "", env="OPENROUTER_MAX_TOKENS",
      min=50, max=4000),
    F("llm_timeout", "float", 60.0, "ai", "So'rov vaqti chegarasi (s)", "", env="OPENROUTER_TIMEOUT",
      min=5, max=300),
    F("moonlight_enabled", "bool", True, "ai", "MoonLight API (oxirgi chora)",
      "Barcha OpenRouter kalitlari/modellari ishlamay qolsa, MoonLight javob beradi."),
    F("moonlight_base_url", "str", "https://abdulhodiy-mzwax.hf.space", "ai", "MoonLight API manzili",
      "mybackend Space manzili (/api/ai/ask). OpenAI-mos server bo'lsa .../v1 bilan yozing.",
      env="MOONLIGHT_BASE_URL"),
    F("moonlight_api_key", "secret", "", "ai", "MoonLight kaliti",
      "Space private bo'lsa HF token. Bo'sh qolsa serverning HF_TOKEN'i ishlatiladi.",
      env="MOONLIGHT_API_KEY"),
    F("moonlight_model", "str", "moonlight-v1", "ai", "MoonLight modeli", env="MOONLIGHT_MODEL"),
    F("fallback_reply_enabled", "bool", True, "ai", "Hech biri ishlamasa tayyor javob",
      "AI umuman javob bera olmasa, odam jim qolmasin - quyidagi matn yuboriladi."),
    F("fallback_reply", "text",
      "Hozir javob bera olmayapman. {owner_name} qaytgach o'zi yozadi 🙏", "ai",
      "Tayyor javob matni"),
    # --- behaviour ---
    F("reply_in_groups", "bool", False, "behavior", "Guruhlarda javob berish",
      "Faqat sizni belgilashsa yoki xabaringizga reply qilishsa."),
    F("ignore_bots", "bool", True, "behavior", "Botlarga javob bermaslik"),
    F("only_contacts", "bool", False, "behavior", "Faqat kontaktlarga javob"),
    F("mark_read", "bool", True, "behavior", "Xabarlarni o'qilgan qilish"),
    F("typing_enabled", "bool", True, "behavior", "\"Yozmoqda...\" ko'rsatish"),
    F("catch_up_on_start", "bool", True, "behavior", "Offline paytdagi xabarlarga javob",
      "Ishga tushganda o'qilmagan xabarlarga javob beradi."),
    F("whitelist", "list", [], "behavior", "Oq ro'yxat",
      "Bo'sh bo'lsa hammaga javob. Har qatorda @username yoki ID."),
    F("blacklist", "list", [], "behavior", "Qora ro'yxat", "Bularga hech qachon javob berilmaydi."),
    F("history_limit", "int", 20, "behavior", "AI ko'radigan xabarlar soni", min=2, max=100),
    F("debounce_seconds", "float", 4.0, "behavior", "Javobdan oldin kutish (s)",
      "Ketma-ket xabarlar yig'ilib, bitta javob beriladi.", min=0, max=120),
    F("owner_takeover_minutes", "float", 30.0, "behavior", "Siz yozganda jim turish (daqiqa)",
      min=0, max=1440),
    F("max_replies_per_hour", "int", 30, "behavior", "Bir chatga soatiga maks. javob", min=1, max=500),
    F("catch_up_max_age_hours", "float", 12.0, "behavior", "Offline xabarlar yoshi (soat)", min=1, max=168),
    F("typing_chars_per_second", "float", 15.0, "behavior", "Yozish tezligi (belgi/s)", min=1, max=200),
    F("max_typing_seconds", "float", 8.0, "behavior", "Maks. yozish kechikishi (s)", min=0, max=60),
    F("command_prefix", "str", ".ai", "behavior", "Buyruq prefiksi"),
    # --- persona ---
    F("owner_name", "str", "Akkaunt egasi", "persona", "Ismingiz"),
    F("owner_info", "text", "", "persona", "Siz haqingizda", "AI odamlarga shu ma'lumotdan foydalanadi."),
    F("system_prompt", "text", DEFAULT_SYSTEM_PROMPT, "persona", "Asosiy ko'rsatma (prompt)",
      "{owner_name} va {owner_info} avtomatik qo'yiladi."),
    F("away_notice_enabled", "bool", True, "persona", "\"AI javob beradi\" ogohlantirishi"),
    F("away_notice", "text", DEFAULT_AWAY_NOTICE, "persona", "Ogohlantirish matni"),
    F("away_notice_cooldown_hours", "float", 12.0, "persona", "Ogohlantirish oralig'i (soat)", min=0, max=720),
    F("debt_guard_enabled", "bool", True, "persona", "Qarz mavzusini himoyalash",
      "Qarz haqida yozilsa AI tayyor javob beradi va sizga darhol xabar qiladi."),
    F("debt_reply", "text", DEFAULT_DEBT_REPLY, "persona", "Qarz mavzusidagi javob"),
    # --- second account ---
    F("owner_account", "str", "", "owner", "Ikkinchi akkauntingiz",
      "@username yoki ID. Xulosalar shu yerga keladi va shu yerdan boshqarasiz."),
    F("notify_enabled", "bool", True, "owner", "Kim yozganini qisqa xabar qilish"),
    F("notify_only_when_online", "bool", True, "owner", "Faqat onlayn bo'lsam",
      "Ikkinchi akkauntingiz onlayn bo'lgandagina darhol xabar keladi."),
    F("notify_images", "bool", True, "owner", "Rasmlarni tahlil qilib yuborish"),
    F("remote_control_enabled", "bool", True, "owner", "Ikkinchi akkauntdan boshqarish",
      "Ism, rasm, kanal e'lonlari va hisobotlarni buyruq bilan boshqarish."),
    F("reports_enabled", "bool", True, "owner", "Muntazam hisobot"),
    F("report_interval_hours", "float", 5.0, "owner", "Hisobot oralig'i (soat)", min=0.5, max=168),
    F("report_when_empty", "bool", True, "owner", "Hech kim yozmasa ham hisobot"),
    # --- channel ---
    F("channel_posts_enabled", "bool", False, "channel", "Kunlik e'lon"),
    F("channel", "str", "", "channel", "Kanal", "@kanal_username (siz admin bo'lishingiz kerak)."),
    F("post_time", "time", "22:30", "channel", "E'lon vaqti (HH:MM)"),
    F("post_topics", "text", "", "channel", "Mavzular", "Har qatorda bitta mavzu - navbat bilan."),
    F("post_style", "text", "Qisqa, foydali, o'zbek tilida, 1-2 ta emoji bilan.", "channel",
      "E'lon uslubi"),
    F("timezone", "str", "Asia/Tashkent", "channel", "Vaqt mintaqasi", env="TZ"),
    # --- telegram ---
    F("api_id", "int", 0, "telegram", "API ID", "my.telegram.org dan olinadi.",
      env="TELEGRAM_API_ID", restart=True),
    F("api_hash", "secret", "", "telegram", "API Hash", env="TELEGRAM_API_HASH", restart=True),
    F("phone", "str", "", "telegram", "Telefon raqam", env="TELEGRAM_PHONE"),
    F("proxy_url", "secret", "", "telegram", "Proksi", "socks5://user:pass@host:port",
      env="TELEGRAM_PROXY", restart=True),
    F("keep_awake", "bool", True, "telegram", "Serverni doim uyg'oq saqlash",
      "Hugging Face bepul Space 48 soat harakatsizlikda uxlaydi. Yoqilsa, server har "
      "6 soatda o'ziga so'rov yuborib uyg'oq turadi."),
    F("device_model", "str", "AutoReply Server", "telegram", "Qurilma nomi", restart=True),
    F("app_version", "str", "1.0.0", "telegram", "Ilova versiyasi", restart=True),
    F("lang_code", "str", "uz", "telegram", "Til kodi", restart=True),
]
FIELD_MAP = {f.key: f for f in FIELDS}

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _env_value(field: Field) -> str | None:
    value = os.getenv(field.env or field.key.upper())
    if value is None and field.key == "openrouter_api_keys":
        value = os.getenv("OPENROUTER_API_KEY")  # single legacy key
    return value if value is not None and value.strip() != "" else None


def coerce(field: Field, value: Any) -> Any:
    """Convert a raw value (from env, JSON or a form) to the field's type."""
    t = field.type
    if t == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on", "ha"}
    if t in {"int", "float"}:
        try:
            num = int(float(value)) if t == "int" else float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field.label}: son kiriting") from exc
        if field.min is not None and num < field.min:
            raise ValueError(f"{field.label}: kamida {field.min}")
        if field.max is not None and num > field.max:
            raise ValueError(f"{field.label}: ko'pi bilan {field.max}")
        return num
    if t in {"list", "secret_list"}:
        if isinstance(value, str):
            value = re.split(r"[\n,]", value)
        return [str(v).strip() for v in value if str(v).strip()]
    if t == "time":
        value = str(value).strip()
        if not TIME_RE.match(value):
            raise ValueError(f"{field.label}: HH:MM formatida kiriting")
        h, m = value.split(":")
        return f"{int(h):02d}:{m}"
    if t == "str":
        return str(value).strip()
    return str(value)  # text / secret: keep as written


@dataclass
class Bootstrap:
    """Environment-only values that the panel never shows or changes."""

    admin_username: str
    admin_password: str
    secret_key: str
    hf_token: str
    state_repo: str
    data_dir: Path
    sessions_dir: Path
    session_name: str
    string_session: str
    port: int
    log_level: str
    proxy_secret: str = ""
    allow_direct: bool = False  # also allow the panel directly on the HF Space URL

    @classmethod
    def from_env(cls, env_file: str | os.PathLike | None = None) -> "Bootstrap":
        load_dotenv(env_file or BASE_DIR / ".env", override=False)

        def path(name: str, default: Path) -> Path:
            value = os.getenv(name, "").strip()
            if not value:
                return default
            p = Path(value)
            return p if p.is_absolute() else BASE_DIR / p

        # Hugging Face persistent storage is mounted at /data when enabled.
        persistent = Path("/data")
        default_data = persistent if persistent.is_dir() and os.access(persistent, os.W_OK) \
            else BASE_DIR / "data"
        data_dir = path("DATA_DIR", default_data)
        return cls(
            admin_username=os.getenv("ADMIN_USERNAME", "abu").strip() or "abu",
            admin_password=os.getenv("ADMIN_PASSWORD", ""),
            secret_key=os.getenv("SECRET_KEY", ""),
            hf_token=os.getenv("HF_TOKEN", "").strip(),
            state_repo=os.getenv("STATE_REPO", "").strip(),
            data_dir=data_dir,
            sessions_dir=path("SESSIONS_DIR", data_dir / "sessions"),
            session_name=os.getenv("SESSION_NAME", "account").strip() or "account",
            string_session=os.getenv("TELEGRAM_STRING_SESSION", "").strip(),
            port=int(os.getenv("PORT", "7860")),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip() or "INFO",
            proxy_secret=os.getenv("PROXY_SECRET", "").strip(),
            allow_direct=os.getenv("ALLOW_DIRECT_PANEL", "").strip().lower() in {"1", "true", "yes"},
        )


class Settings:
    """Live settings object shared by every component.

    Attribute access returns the current value (``settings.ai_enabled``).
    ``update()`` validates, persists and applies changes immediately.
    """

    def __init__(self, boot: Bootstrap, store=None):
        object.__setattr__(self, "boot", boot)
        object.__setattr__(self, "_store", store)
        values = {}
        saved = store.get_config_all() if store is not None else {}
        for field in FIELDS:
            if field.key in saved:
                raw = saved[field.key]
            else:
                raw = _env_value(field)
                if raw is None:
                    raw = field.default
            try:
                values[field.key] = coerce(field, raw)
            except ValueError:
                values[field.key] = coerce(field, field.default)
        object.__setattr__(self, "_values", values)

    def __getattr__(self, name: str) -> Any:
        values = object.__getattribute__(self, "_values")
        if name in values:
            return values[name]
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in FIELD_MAP:
            self._values[name] = coerce(FIELD_MAP[name], value)
        else:
            object.__setattr__(self, name, value)

    def update(self, changes: dict[str, Any]) -> list[str]:
        """Validate + persist. Returns keys that need a Telegram reconnect."""
        clean = {}
        for key, value in changes.items():
            field = FIELD_MAP.get(key)
            if field is None:
                raise ValueError(f"Noma'lum sozlama: {key}")
            clean[key] = coerce(field, value)
        restart = []
        for key, value in clean.items():
            if self._values.get(key) != value and FIELD_MAP[key].restart:
                restart.append(key)
            self._values[key] = value
            if self._store is not None:
                self._store.set_config(key, value)
        return restart

    def as_dict(self) -> dict[str, Any]:
        return dict(self._values)

    # --- derived values ---------------------------------------------------

    def render(self, template: str) -> str:
        return (
            template.replace("{owner_name}", self.owner_name or "Akkaunt egasi")
            .replace("{owner_info}", self.owner_info or "(qo'shimcha ma'lumot yo'q)")
        )

    @property
    def system_prompt_rendered(self) -> str:
        return self.render(self.system_prompt or DEFAULT_SYSTEM_PROMPT)

    @property
    def data_dir(self) -> Path:
        return self.boot.data_dir

    @property
    def sessions_dir(self) -> Path:
        return self.boot.sessions_dir

    @property
    def session_name(self) -> str:
        return self.boot.session_name

    @property
    def string_session(self) -> str:
        return self.boot.string_session

    @property
    def session_path(self) -> Path:
        return self.sessions_dir / f"{self.session_name}.session"

    @property
    def string_session_path(self) -> Path:
        return self.sessions_dir / f"{self.session_name}.string"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def system_version(self) -> str:
        return "Linux"

    @property
    def watchdog_interval(self) -> float:
        return 300.0

    @property
    def has_telegram_credentials(self) -> bool:
        return bool(self.api_id and self.api_hash)


def schema_for_panel() -> list[dict]:
    return [
        {
            "key": f.key, "type": f.type, "group": f.group, "label": f.label,
            "help": f.help, "restart": f.restart, "min": f.min, "max": f.max,
        }
        for f in FIELDS
    ]


def mask_secret(value: str) -> str:
    if not value:
        return ""
    return ("•" * 6) + value[-4:] if len(value) > 8 else "•" * 6


def panel_values(settings: Settings) -> dict[str, Any]:
    """Values safe to send to the browser: secrets are masked."""
    out = {}
    for f in FIELDS:
        value = getattr(settings, f.key)
        if f.type == "secret":
            out[f.key] = {"set": bool(value), "masked": mask_secret(value)}
        elif f.type == "secret_list":
            out[f.key] = [mask_secret(v) for v in value]
        else:
            out[f.key] = value
    return out


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
