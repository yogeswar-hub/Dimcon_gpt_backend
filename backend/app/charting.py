import os
import json
from typing import List

MODEL_ID = os.getenv("BEDROCK_CHART_MODEL", "anthropic.claude-3-haiku-20240307-v1:0")
BEDROCK_REGION = os.getenv("BEDROCK_REGION", "us-east-1")

_ALLOWED_TYPES = {
    "bar", "line", "pie", "doughnut", "radar",
    "area", "scatter", "bubble", "radialBar",
    "matrix", "treemap", "sunburst", "candlestick", "ohlc", "wordcloud"
}

def extract_axes_from_nlq_ai(nlq: str, sql_query: str = None, items: List[dict] = None) -> tuple:
    import boto3
    bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
    prompt = (
        "Given the following NLQ, SQL query, and data preview, "
        "identify the most appropriate column names from the data for the chart's x-axis and y-axis.\n"
        "Return ONLY a JSON object with keys 'x_axis' and 'y_axis', where the values are actual column names from the data preview.\n"
        f"NLQ: {nlq}\n"
        f"SQL: {sql_query}\n"
        f"Data Preview: {json.dumps(items[:3], indent=2) if items else ''}\n"
        "JSON:"
    )

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 100,
        "temperature": 0.0,
        "messages": [{"role": "user", "content": prompt}],
    }

    resp = bedrock.invoke_model(
        modelId=MODEL_ID,
        body=json.dumps(body),
        contentType="application/json",
    )
    
    payload = json.loads(resp["body"].read())
    raw = (payload.get("content") or [{}])[0].get("text", "")
    s = raw.strip()

    if s.startswith("```"):
        s = s.strip("`").strip()
        if "\n" in s:
            s = s.split("\n", 1)[1].strip()
    if s.lower().startswith("json"):
        s = s[4:].lstrip()
    axes = json.loads(s)
    return axes.get("x_axis"), axes.get("y_axis")

def generate_chart_specs_for_suiteql(result: dict, nlq: str, x_axis: str = None, y_axis: str = None) -> list:
    items = result.get("items", []) or []
    if not x_axis or not y_axis:
        return []
    labels = [row.get(x_axis) for row in items if row.get(x_axis) is not None]
    values = [row.get(y_axis) for row in items if row.get(y_axis) is not None]
    chart_specs = []
    for chart_type in _ALLOWED_TYPES:
        chart_spec = {
            "type": chart_type,
            "labels": labels,
            "datasets": [{
                "label": y_axis,
                "data": values,
                "backgroundColor": [
                    "#4e73df", "#1cc88a", "#36b9cc", "#f6c23e", "#e74a3b",
                    "#858796", "#FF6384", "#36A2EB", "#FFCE56", "#9966FF"
                ]
            }],
            "schemaVersion": 1,
            "height": 260
        }
        chart_specs.append(chart_spec)
    return chart_specs