"""统一文件位置，不依赖终端或 IDE 的当前工作目录。"""

from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
ENV_FILE = BACKEND_DIR / ".env"
MEMORY_DIR = PROJECT_ROOT / "memory"
FRONTEND_DIR = PROJECT_ROOT / "frontend"
