import os
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Settings:
    domain: str
    audience: str
    anthropic_key: str = ""
    tavily_key: str = ""
    model: str = "claude-sonnet-4-5"
    max_steps: int = 8
    max_tool_calls: int = 12
    timeout_seconds: float = 90
    database: str = "data/research.sqlite3"
    provider: str = "anthropic"
    openai_key: str = ""
    gemini_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    gemini_model: str = "gemini-3.8-flash"

    def __post_init__(self):
        parsed = urlsplit("https://" + self.domain)
        if (
            not parsed.hostname
            or parsed.netloc != self.domain
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.port
        ):
            raise ValueError("AUTH0_DOMAIN must be a bare trusted tenant hostname")
        if self.provider not in {"anthropic", "openai", "gemini"}:
            raise ValueError("LLM_PROVIDER must be anthropic, openai or gemini")
        if not self.audience:
            raise ValueError("AUTH0_AUDIENCE is required")
        if not (
            1 <= self.max_steps <= 20
            and 1 <= self.max_tool_calls <= 30
            and 1 <= self.timeout_seconds <= 180
        ):
            raise ValueError("Execution limits outside supported ranges")

    @property
    def llm_key(self):
        return {
            "anthropic": self.anthropic_key,
            "openai": self.openai_key,
            "gemini": self.gemini_key,
        }[self.provider]

    def for_provider(self, provider=None):
        provider = provider or self.provider
        model = {"anthropic": self.model, "openai": self.openai_model, "gemini": self.gemini_model}[
            provider
        ]
        return replace(self, provider=provider, model=model)

    @classmethod
    def load(cls):
        load_dotenv(ROOT / ".env")
        return cls(
            domain=os.environ["AUTH0_DOMAIN"],
            audience=os.environ["AUTH0_AUDIENCE"],
            anthropic_key=os.getenv("ANTHROPIC_API_KEY", ""),
            provider=os.getenv("LLM_PROVIDER", "anthropic"),
            openai_key=os.getenv("OPENAI_API_KEY", ""),
            gemini_key=os.getenv("GEMINI_API_KEY", ""),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
            tavily_key=os.getenv("TAVILY_API_KEY", ""),
            model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
            max_steps=int(os.getenv("MAX_STEPS", "8")),
            max_tool_calls=int(os.getenv("MAX_TOOL_CALLS", "12")),
            timeout_seconds=float(os.getenv("TIMEOUT_SECONDS", "90")),
            database=os.getenv("DATABASE_PATH", str(ROOT / "data/research.sqlite3")),
        )
