import os
from decimal import Decimal

try:
    from tableauhyperapi import HyperProcess, Telemetry, Connection, CreateMode, TableDefinition, SqlType, Inserter, TableName
    HYPER_AVAILABLE = True
except (ImportError, OSError):   # package missing, or its native library can't load on this machine
    HYPER_AVAILABLE = False


def _infer_sql_type(all_data: list, key: str) -> "SqlType":
    """
    Scan ALL rows for a given key to infer the safest Tableau SqlType.
    - If ANY value is a float or Decimal  → double()
    - If ALL non-null values are int only → big_int()
    - If mixed types or any string        → text()
    """
    has_float = False
    has_int = False
    has_str = False

    for row in all_data:
        val = row.get(key)
        if val is None or val == "":
            continue
        if isinstance(val, bool):
            has_str = True
        elif isinstance(val, float) or isinstance(val, Decimal):
            has_float = True
        elif isinstance(val, int):
            has_int = True
        else:
            try:
                float(str(val).replace(',', ''))
                has_float = True
            except ValueError:
                has_str = True

    if has_str:
        return SqlType.text()
    if has_float:
        return SqlType.double()
    if has_int:
        return SqlType.big_int()
    return SqlType.text()  # safe default for empty columns


def _flatten(row: dict, is_anomaly: bool) -> dict:
    """Make a record Tableau-friendly: errors list -> one text column, plus an is_anomaly flag."""
    out = {k: v for k, v in row.items() if k != "errors"}
    out["is_anomaly"] = "Yes" if is_anomaly else "No"
    out["errors"] = "; ".join(row.get("errors", [])) if is_anomaly else ""
    return out


LAST_ERROR = ""


def create_hyper_extract(validation_report, output_filename="financial_data.hyper"):
    """Returns the .hyper path, or None if there is no data or the Hyper engine can't run here
    (e.g. blocked by Application Control); the reason is left in LAST_ERROR."""
    global LAST_ERROR
    try:
        return _create_hyper_extract(validation_report, output_filename)
    except Exception as e:      # HyperException, blocked hyperd.exe, disk errors...
        LAST_ERROR = f"{type(e).__name__}: {str(e)[:200]}"
        return None


def _create_hyper_extract(validation_report, output_filename):
    """
    Takes the validated financial records (and anomalies) and generates
    a highly optimized Tableau .hyper database extract.
    """
    valid = validation_report.get("valid_records", [])
    anomalies = validation_report.get("anomalies", [])
    all_data = [_flatten(r, False) for r in valid] + [_flatten(r, True) for r in anomalies]

    if not all_data or not HYPER_AVAILABLE:
        return None

    os.makedirs("uploads", exist_ok=True)
    hyper_filepath = os.path.join("uploads", output_filename)

    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
        with Connection(endpoint=hyper.endpoint,
                        database=hyper_filepath,
                        create_mode=CreateMode.CREATE_AND_REPLACE) as connection:

            # Union of columns across ALL rows (rows can have different fields), first-seen order.
            current_keys = list(dict.fromkeys(k for row in all_data for k in row))
            table_name = TableName("Extract", "Extract")

            # Robust column type inference across ALL rows
            columns = [
                TableDefinition.Column(name=key, type=_infer_sql_type(all_data, key))
                for key in current_keys
            ]

            extract_table = TableDefinition(table_name=table_name, columns=columns)
            connection.catalog.create_schema("Extract")
            connection.catalog.create_table(extract_table)

            with Inserter(connection, extract_table) as inserter:
                for row in all_data:
                    row_data = []
                    for i, key in enumerate(current_keys):
                        val = row.get(key, None)
                        col_type = columns[i].type
                        if val is None or val == "":
                            row_data.append(None)
                        elif col_type == SqlType.double():
                            try:
                                row_data.append(float(str(val).replace(',', '')))
                            except Exception:
                                row_data.append(0.0)
                        elif col_type == SqlType.big_int():
                            try:
                                row_data.append(int(float(str(val).replace(',', ''))))
                            except Exception:
                                row_data.append(None)
                        else:
                            row_data.append(str(val))
                    inserter.add_row(row_data)
                inserter.execute()

    return hyper_filepath
