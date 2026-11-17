"""导出后端 OpenAPI 契约到 `docs/api/openapi.json`。

前端会话按该文件生成 TypeScript 类型，避免导出脚本写入 `frontend/` 造成并行冲突。
在仓库根目录执行：
    .\\.venv312\\Scripts\\python.exe scripts\\export_openapi.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


def main() -> int:
    spec = create_app(Settings(_env_file=None)).openapi()
    target = ROOT / "docs" / "api" / "openapi.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"导出 {target}：{len(spec['paths'])} 条路径")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
