# app/charting.py
import os
import json
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

# === Config ===
MODEL_ID = os.getenv("BEDROCK_CHART_MODEL", "anthropic.claude-3-haiku-20240307-v1:0")
BEDROCK_REGION = os.getenv("BEDROCK_REGION", "us-east-1")
USE_AI_FOR_CHARTS = os.getenv("USE_AI_FOR_CHARTS", "true").lower() == "true"

# Allowed chart types to avoid typos
_ALLOWED_TYPES = {
    "bar", "line", "pie", "doughnut", "radar",
    "area", "scatter", "bubble", "radialBar",
    "matrix",      # Heatmap
    "treemap",     # Treemap
    "sunburst",    # Sunburst
    "candlestick", # Financial
    "ohlc",        # Financial
    "wordcloud"    # Word Cloud
}


def suiteql_items_to_text_for_chart(items: List[dict]) -> str:
    """
    Convert SuiteQL rows into label:value lines so the model can infer a chart.
    Heuristic:
      - First non-numeric string → label
      - First numeric value     → value
    If a row has no such pair, include a short preview line.
    """
    if not items:
        return ""

    from decimal import Decimal as _Dec

    def _is_num(v: Any) -> bool:
        if isinstance(v, (int, float, _Dec)):
            return True
        if isinstance(v, str):
            try:
                float(v.replace(",", ""))
                return True
            except Exception:
                return False
        return False

    lines: List[str] = []
    for i, row in enumerate(items, 1):
        label, value = None, None

        # pick label: first non-numeric string
        for v in row.values():
            if isinstance(v, str) and v.strip() and not _is_num(v):
                label = v.strip()
                break

        # pick value: first numeric
        for v in row.values():
            if _is_num(v):
                try:
                    value = float(str(v).replace(",", ""))
                    break
                except Exception:
                    pass

        if label is not None and value is not None:
            lines.append(f"{label[:80]}: {value}")
        else:
            # fallback – short preview
            preview = ", ".join(f"{k}={row[k]}" for k in list(row.keys())[:4])
            lines.append(f"Row {i}: {preview}")

        if len(lines) >= 50:
            break

    # brief instruction for the model
    header = (
        "Summarize these label→value pairs as a Chart.js configuration.\n"
        "Return ONLY JSON (no markdown). Include: type, data (labels,datasets), options.\n"
        "Use bar/line for series; pie/doughnut for composition.\n"
    )
    return header + "\n".join(lines)


def wrap_chartjs_message(title: str, chart_spec: Dict[str, Any], height: int = 260) -> str:
    """
    Produce a chat message string containing a ```chartjs fenced block.
    This matches your frontend format exactly (schemaVersion + height included).
    """
    spec = dict(chart_spec) if isinstance(chart_spec, dict) else {}
    spec.setdefault("schemaVersion", 1)
    spec.setdefault("height", height)

    return f"""{title}

```chartjs
{json.dumps(spec, indent=2)}
```"""


def generate_chart_spec(
    text_content: str,
    preferred_type: Optional[str] = None
) -> Dict[str, Any]:
    """
    Generate a Chart.js spec from free-text or label:value lines.

    - Tries Bedrock Claude first (pure JSON requested).
    - Falls back to a deterministic rule-based generator if AI fails.
    - If preferred_type is provided (e.g., "pie", "bar", "line"), it is enforced.
    """
    # normalize the preferred type
    if preferred_type:
        preferred_type = preferred_type.lower().strip()
        if preferred_type not in _ALLOWED_TYPES:
            logger.warning(f"Unsupported preferred_type '{preferred_type}', ignoring.")
            preferred_type = None

    try:
        spec = _generate_with_claude(text_content, preferred_type)
        if spec:
            # Safety: enforce preferred type if the model picked something else
            if preferred_type:
                spec["type"] = preferred_type
            return spec
    except Exception as e:
        logger.warning(f"AI chart generation failed: {e}")

    # Fallback
    spec = _fallback_rule_based(text_content)
    if preferred_type:
        spec["type"] = preferred_type
    return spec


def _generate_with_claude(
    text_content: str,
    preferred_type: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Ask Claude (Bedrock) for a pure-JSON Chart.js configuration.
    Tolerates accidental code fences in the response.
    """
    import boto3

    bedrock = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

    type_hint = f"- Use chart type: {preferred_type}\n" if preferred_type else ""
    prompt = (
        "Generate a Chart.js configuration JSON from this text.\n"
        "- Return ONLY valid JSON (no markdown, no backticks)\n"
        "- Include: type, data (labels, datasets), options\n"
        "- Ensure numeric values are numbers, not strings\n"
        "- For each dataset, include a 'backgroundColor' array with distinct colors based on the data values or categories\n"
        "- You may use advanced chart types (matrix, treemap, sunburst, candlestick, ohlc, wordcloud) if the data fits. Use Chart.js plugins if needed.\n"
        f"{type_hint}\n"
        f"Text:\n{text_content[:3000]}\n\nJSON:"
    )

    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1000,
        "temperature": 0.2,
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

    # Tolerate accidental code fences
    if s.startswith("```"):
        # remove leading/backticks and optional language line
        s = s.strip("`").strip()
        if "\n" in s:
            s = s.split("\n", 1)[1].strip()

    # Also tolerate if the model prepended something like "json\n{...}"
    if s.lower().startswith("json"):
        s = s[4:].lstrip()

    spec = json.loads(s)  # may raise ValueError for invalid JSON — caught by caller
    if not isinstance(spec, dict) or "type" not in spec or "data" not in spec:
        raise ValueError("Invalid Chart.js spec from model")

    return spec


def _fallback_rule_based(text_content: str) -> Dict[str, Any]:
    """
    Heuristic fallback:
      - If we can find >=2 numbers, create a bar/line with those numbers.
      - Else if we can find percentages, make a pie.
      - Else return a tiny default doughnut so the UI still renders something.
    """
    import re
    from collections import Counter

    numbers = re.findall(r"\b\d+(?:\.\d+)?\b", text_content)
    percentages = re.findall(r"\b\d+(?:\.\d+)?%\b", text_content)

    # Numeric series → bar/line
    if numbers and len(numbers) >= 2:
        vals = [float(n) for n in numbers[:12]]
        chart_type = "line" if len(vals) > 6 else "bar"
        return {
            "type": chart_type,
            "data": {
                "labels": [f"Data {i+1}" for i in range(len(vals))],
                "datasets": [{
                    "label": "Values",
                    "data": vals
                }]
            },
            "options": {
                "responsive": True,
                "scales": {"y": {"beginAtZero": True}}
            }
        }

    # Composition → pie
    if percentages:
        vals = [float(p.replace("%", "")) for p in percentages[:8]]
        return {
            "type": "pie",
            "data": {
                "labels": [f"Category {i+1}" for i in range(len(vals))],
                "datasets": [{"label": "Mix", "data": vals}]
            },
            "options": {"responsive": True}
        }

    # Word frequency (very rough) → doughnut
    words = [w.lower().strip('.,!?()[]{}":;') for w in text_content.split() if len(w) > 3]
    if len(words) > 8:
        freq = Counter(words).most_common(6)
        return {
            "type": "doughnut",
            "data": {
                "labels": [w for w, _ in freq],
                "datasets": [{"data": [c for _, c in freq]}]
            },
            "options": {"responsive": True}
        }

    # Tiny default so the UI doesn't break
    return {
        "type": "doughnut",
        "data": {
            "labels": ["A", "B"],
            "datasets": [{"data": [60, 40]}]
        },
        "options": {"responsive": True}
    }


def suiteql_items_to_geojson(items: list) -> dict:
    features = []
    for row in items:
        lat = row.get("lat") or row.get("latitude")
        lng = row.get("lng") or row.get("longitude") or row.get("lon")
        if lat is not None and lng is not None:
            try:
                lat = float(lat)
                lng = float(lng)
            except Exception:
                continue
            feature = {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lng, lat]},
                "properties": {k: v for k, v in row.items() if k not in ["lat", "lng", "latitude", "longitude", "lon"]}
            }
            features.append(feature)
    return {"type": "FeatureCollection", "features": features}
