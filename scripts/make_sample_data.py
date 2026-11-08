"""生成示例 Excel 数据：data/boxes.xlsx、data/orders.xlsx。

用法：
    python scripts/make_sample_data.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.excel_io import build_template  # noqa: E402


def main() -> None:
    data_dir = ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    for kind, filename in (("cartons", "boxes.xlsx"), ("orders", "orders.xlsx")):
        buffer = build_template(None, kind)  # type: ignore[arg-type]
        target = data_dir / filename
        target.write_bytes(buffer.read())
        print(f"已生成 {target}")


if __name__ == "__main__":
    main()
