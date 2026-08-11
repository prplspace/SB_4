"""
Экспорт в Excel — раздел 3.6 ТЗ. pandas + openpyxl, отдельный лист на сеть,
плюс общий лист "Все". Шапка стилизована (насколько это уместно в Excel).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
from openpyxl.styles import Font, PatternFill

COLUMNS = [
    "address", "network", "asset_type", "source_platform", "collection_or_token",
    "role", "balance", "balance_checked_at", "extra_assets", "discord", "twitter",
    "times_skipped", "last_skipped_at", "first_seen", "last_seen",
]

HEADER_FILL = PatternFill(start_color="1A1A1A", end_color="1A1A1A", fill_type="solid")
HEADER_FONT = Font(color="E8E3D3", bold=True)


def _rows_to_df(rows: list[sqlite3.Row]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=COLUMNS)
    return pd.DataFrame([dict(r) for r in rows])[COLUMNS]


def export_to_excel(rows: list[sqlite3.Row], output_path: Path) -> Path:
    df = _rows_to_df(rows)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Все кошельки", index=False)

        for network, group in df.groupby("network"):
            sheet_name = str(network)[:31]  # лимит Excel на имя листа
            group.to_excel(writer, sheet_name=sheet_name, index=False)

        for sheet in writer.sheets.values():
            for cell in sheet[1]:
                cell.fill = HEADER_FILL
                cell.font = HEADER_FONT
            for col_cells in sheet.columns:
                length = max((len(str(c.value)) for c in col_cells if c.value is not None), default=10)
                sheet.column_dimensions[col_cells[0].column_letter].width = min(length + 2, 40)

    return output_path
