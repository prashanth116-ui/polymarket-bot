"""YAML/JSON configuration loader."""

import json
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

CONFIG_DIR = Path(__file__).parent
PROJECT_ROOT = CONFIG_DIR.parent


def load_env():
    """Load environment variables from .env file."""
    env_path = CONFIG_DIR / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        # Fall back to project root .env
        load_dotenv(PROJECT_ROOT / ".env")


def load_yaml(filename: str) -> dict:
    """Load a YAML config file from the config directory."""
    path = CONFIG_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def load_strategy_config(strategy_name: str) -> dict:
    """Load a strategy config from config/strategies/."""
    return load_yaml(f"strategies/{strategy_name}.yaml")


def load_category_config(category_name: str) -> dict:
    """Load a category config from config/categories/."""
    return load_yaml(f"categories/{category_name}.yaml")


def load_settings() -> dict:
    """Load the main settings.yaml."""
    return load_yaml("settings.yaml")


def load_json(filename: str) -> dict:
    """Load a JSON config file from the config directory."""
    path = CONFIG_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "r") as f:
        return json.load(f)


def get_env(key: str, default: Any = None, required: bool = False) -> Any:
    """Get an environment variable with optional default."""
    load_env()
    value = os.getenv(key, default)
    if required and value is None:
        raise ValueError(f"Required environment variable {key} is not set")
    return value
