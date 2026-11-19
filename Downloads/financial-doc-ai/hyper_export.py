"""Write the validated rows to a Tableau .hyper extract."""
import os
from decimal import Decimal

try:
    from tableauhyperapi import (Connection, CreateMode, HyperProcess, Inserter, SqlType,
                                 TableDefinition, TableName, Telemetry)
    HYPER_AVAILABLE = True
except (ImportError, OSError):      # not installed, or its native library won't load here
    HYPER_AVAILABLE = False

LAST_ERROR = ""         # why the last export failed, if it did


def _column_type(rows: list, key: str):
    """Pick a Tableau type that fits every value in the column."""
    seen_float = seen_int = seen_text = False
    for row in rows:
        value = row.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            seen_text = True
        elif isinstance(value, (float, Decimal)):
            seen_float = True
        elif isinstance(value, int):
            seen_int = True
        else:
            try:
                float(str(value).replace(",", ""))
                seen_float = True
            except ValueError:
                seen_text = True

    if seen_text:
        return SqlType.text()
    if seen_float:
        return SqlType.double()
    if seen_int:
        return SqlType.big_int()
    return SqlType.text()       # column with no values at all


def _flatten(row: dict, is_anomaly: bool) -> dict:
    """Tableau wants flat columns: the error list becomes one text column plus a yes/no flag."""
    flat = {k: v for k, v in row.items() if k != "errors"}
    flat["is_anomaly"] = "Yes" if is_anomaly else "No"
    flat["errors"] = "; ".join(row.get("errors", [])) if is_anomaly else ""
    return flat


def _convert(value, column_type):
    if value is None or value == "":
        return None
    if column_type == SqlType.double():
        try:
            return float(str(value).replace(",", ""))
        except ValueError:
            return 0.0
    if column_type == SqlType.big_int():
        try:
            return int(float(str(value).replace(",", "")))
        except ValueError:
            return None
    return str(value)


def create_hyper_extract(validation_report, output_filename="financial_data.hyper"):
    """Returns the path of the .hyper file, or None if there is nothing to export or Hyper can't run here."""
    global LAST_ERROR
    try:
        return _write_extract(validation_report, output_filename)
    except Exception as error:      # Hyper can be blocked by policy, or the disk may be full
        LAST_ERROR = f"{type(error).__name__}: {str(error)[:200]}"
        return None


def _write_extract(validation_report, output_filename):
    rows = ([_flatten(r, False) for r in validation_report.get("valid_records", [])]
            + [_flatten(r, True) for r in validation_report.get("anomalies", [])])
    if not rows or not HYPER_AVAILABLE:
        return None

    os.makedirs("uploads", exist_ok=True)
    path = os.path.join("uploads", output_filename)

    # Rows can have different fields, so use the union of all of them as the columns.
    keys = list(dict.fromkeys(key for row in rows for key in row))
    columns = [TableDefinition.Column(name=key, type=_column_type(rows, key)) for key in keys]
    table = TableDefinition(table_name=TableName("Extract", "Extract"), columns=columns)

    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
        with Connection(endpoint=hyper.endpoint, database=path, create_mode=CreateMode.CREATE_AND_REPLACE) as connection:
            connection.catalog.create_schema("Extract")
            connection.catalog.create_table(table)
            with Inserter(connection, table) as inserter:
                for row in rows:
                    inserter.add_row([_convert(row.get(key), column.type) for key, column in zip(keys, columns)])
                inserter.execute()
    return path
