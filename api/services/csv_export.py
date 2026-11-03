"""Bounded, spreadsheet-safe exports, materialized before sending HTTP headers."""

import csv
import io
from fastapi import HTTPException, Response

EXPORT_LIMIT = 10000
CSV_RESPONSES = {
    200: {
        "description": "UTF-8 CSV attachment, limited to 10,000 authorized rows.",
        "content": {"text/csv": {"schema": {"type": "string", "format": "binary"}}},
    }
}


def csv_response(headers, rows, filename):
    if len(rows) > EXPORT_LIMIT:
        raise HTTPException(422, "Export exceeds 10000 rows; narrow the filters")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(headers)
    for row in rows:
        values = []
        for value in row:
            text = "" if value is None else str(value)
            # Quoting CSV delimiters alone does not stop spreadsheet formula evaluation.
            if text.startswith(("\t", "\r", "\n")) or text.lstrip().startswith(
                ("=", "+", "-", "@")
            ):
                text = "'" + text
            values.append(text)
        writer.writerow(values)
    return Response(
        "\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
