import os
import json
import requests
import time
import logging
import boto3
import re
import pandas as pd
from datetime import datetime
from requests_oauthlib import OAuth1
from dotenv import load_dotenv
from app.prompts import SYSTEM_PROMPT, SUMMARIZATION_PROMPT, URL_GENERATION_PROMPT

logging.basicConfig(level=logging.INFO)

# ===== Load environment variables =====
load_dotenv()

ACCOUNT_ID = os.getenv("NETSUITE_ACCOUNT_ID")
CONSUMER_KEY = os.getenv("NETSUITE_CONSUMER_KEY")
CONSUMER_SECRET = os.getenv("NETSUITE_CONSUMER_SECRET")
TOKEN_ID = os.getenv("NETSUITE_TOKEN_ID")
TOKEN_SECRET = os.getenv("NETSUITE_TOKEN_SECRET")

# ===== Bedrock CONFIG =====
BEDROCK_REGION = "us-east-1"
KB_ID = "VRQ5QNZEW6"
INFERENCE_PROFILE_ARN = (
    "arn:aws:bedrock:us-east-1::inference-profile/"
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)

# ===== NetSuite Auth =====
auth = OAuth1(
    CONSUMER_KEY,
    CONSUMER_SECRET,
    TOKEN_ID,
    TOKEN_SECRET,
    signature_method="HMAC-SHA256",
    realm=ACCOUNT_ID,
    signature_type="AUTH_HEADER",
)

# ===== Bedrock Clients =====
bedrock_agent = boto3.client("bedrock-agent-runtime", region_name=BEDROCK_REGION)
bedrock_runtime = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

# ===== Cache =====
URL_COMPONENT_CACHE = {}

# ==========================================================
# === KNOWLEDGE BASE RETRIEVAL ===
# ==========================================================
def retrieve_from_kb(query: str, num_results: int = 5) -> str:
    response = bedrock_agent.retrieve(
        knowledgeBaseId=KB_ID,
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {"numberOfResults": num_results}
        },
    )
    context_parts = [r["content"]["text"] for r in response.get("retrievalResults", [])]
    return "\n\n".join(context_parts)


# ==========================================================
# === SQL CLEANUP ===
# ==========================================================
def clean_sql_query(sql_query: str) -> str:
    sql_query = re.sub(r"^```sql\s*\n?", "", sql_query, flags=re.IGNORECASE)
    sql_query = re.sub(r"^```\s*\n?", "", sql_query)
    sql_query = re.sub(r"\n?```\s*$", "", sql_query)
    return sql_query.strip()


# ==========================================================
# === RECORD TYPE DETECTION - ENHANCED ===
# ==========================================================
def detect_record_type(sql_query: str, item: dict) -> tuple:
    """
    Detect the record type and source table from the SQL query and result item.
    Returns: (record_type, table_name)
    
    Strategy: Analyze the SELECT clause to determine which table the ID field comes from.
    The first table with an ID field in SELECT takes priority.
    """
    # If recordtype field exists in the result, use it (for transaction table)
    if "recordtype" in item:
        return (item["recordtype"].lower(), "transaction")
    
    sql_lower = sql_query.lower()
    
    # Extract SELECT clause
    select_match = re.search(r"select\s+(.*?)\s+from", sql_lower, re.DOTALL)
    if select_match:
        select_clause = select_match.group(1)
        
        # Look for patterns like: alias.fieldname AS id
        # This handles cases like: tl.item AS id, c.customerid AS id, etc.
        aliased_id_match = re.search(r"(\w+)\.(\w+)\s+as\s+id", select_clause)
        if aliased_id_match:
            alias = aliased_id_match.group(1)
            field_name = aliased_id_match.group(2)
            
            # Common field name to table mappings
            field_to_table = {
                'item': 'item',
                'customer': 'customer',
                'entity': 'customer',  # entity often refers to customer
                'vendor': 'vendor',
                'employee': 'employee',
                'account': 'account',
                'department': 'department',
                'class': 'class',
                'location': 'location',
                'subsidiary': 'subsidiary',
            }
            
            # Check if field name indicates a specific table
            if field_name in field_to_table:
                return (field_to_table[field_name], field_to_table[field_name])
            
            # Otherwise, try to find what table the alias refers to
            from_pattern = rf"from\s+(\w+)(?:\s+as)?\s+{alias}\b"
            from_match = re.search(from_pattern, sql_lower)
            if from_match:
                table_name = from_match.group(1)
                return (table_name, table_name)
            
            join_pattern = rf"join\s+(\w+)(?:\s+as)?\s+{alias}\b"
            join_match = re.search(join_pattern, sql_lower)
            if join_match:
                table_name = join_match.group(1)
                return (table_name, table_name)
        
        # Look for aliased id field (e.g., "c.id", "i.id", "t.id")
        # Find the FIRST occurrence - this is the primary record type
        id_alias_match = re.search(r"(\w+)\.id(?:\s|,|$)", select_clause)
        if id_alias_match:
            alias = id_alias_match.group(1)
            
            # Now find what table this alias refers to
            # Pattern 1: FROM table alias or FROM table AS alias
            from_pattern = rf"from\s+(\w+)(?:\s+as)?\s+{alias}\b"
            from_match = re.search(from_pattern, sql_lower)
            if from_match:
                table_name = from_match.group(1)
                # Special check: if this is transaction table, check for recordtype
                if table_name == "transaction":
                    recordtype_match = re.search(r"recordtype\s*=\s*['\"](\w+)['\"]", sql_lower)
                    if recordtype_match:
                        return (recordtype_match.group(1), "transaction")
                    return ("transaction", "transaction")
                return (table_name, table_name)
            
            # Pattern 2: JOIN table alias or JOIN table AS alias
            join_pattern = rf"join\s+(\w+)(?:\s+as)?\s+{alias}\b"
            join_match = re.search(join_pattern, sql_lower)
            if join_match:
                table_name = join_match.group(1)
                # For JOINs, still check if it's transaction with recordtype
                if table_name == "transaction":
                    recordtype_match = re.search(r"recordtype\s*=\s*['\"](\w+)['\"]", sql_lower)
                    if recordtype_match:
                        return (recordtype_match.group(1), "transaction")
                return (table_name, table_name)
        
        # Look for unaliased id field in SELECT
        if re.search(r"\bid\b", select_clause):
            # Find the main table in FROM clause (first table mentioned)
            from_match = re.search(r"from\s+(\w+)", sql_lower)
            if from_match:
                table_name = from_match.group(1)
                # Special handling for transaction table
                if table_name == "transaction":
                    # Look for recordtype in WHERE clause
                    recordtype_where = re.search(r"recordtype\s*(?:=|in)\s*[('\"](\w+)", sql_lower)
                    if recordtype_where:
                        return (recordtype_where.group(1), "transaction")
                    return ("transaction", "transaction")
                return (table_name, table_name)
    
    # Fallback: Extract the main table from FROM clause
    from_match = re.search(r"from\s+(\w+)", sql_lower)
    if from_match:
        table_name = from_match.group(1)
        return (table_name, table_name)
    
    return (None, None)


# ==========================================================
# === LLM: URL COMPONENT RETRIEVAL - ENHANCED ===
# ==========================================================
def get_url_components_from_llm(record_type: str, table_name: str = None) -> dict:
    """
    Get URL components from LLM with enhanced caching and table awareness.
    """
    cache_key = f"{table_name}:{record_type}" if table_name else record_type
    
    if cache_key in URL_COMPONENT_CACHE:
        logging.info(f"Using cached URL components for {cache_key}")
        return URL_COMPONENT_CACHE[cache_key]
    
    try:
        # Construct query based on whether we have a table name
        if table_name and table_name != record_type:
            url_query = f"NetSuite URL structure for {record_type} record in {table_name} table"
        else:
            url_query = f"NetSuite URL structure for {record_type} record type"
        
        url_context = retrieve_from_kb(url_query, num_results=10)
        
        user_message = f"""
Based on the following official NetSuite URL structure documentation:

{url_context}

Determine the correct module, submodule, and record file name (without .nl extension)
for the record type '{record_type}'{f" from the {table_name} table" if table_name else ""}.

Important context:
- If this is from the 'item' table, use item URL structure from Module 1
- If this is from the 'customer' or entity-related table, use entity URL structure from Module 1
- If this is from the 'transaction' table with recordtype='{record_type}', use transaction URL structure from Module 2
- If this is from the 'vendor' table, use vendor URL structure from Module 1
- If this is from the 'employee' table, use employee URL structure from Module 1
- If this is from the 'account', 'department', 'class', 'location', 'subsidiary' tables, use otherlists URL structure from Module 1

Search the documentation carefully and return the exact URL components.

Return ONLY valid JSON like:
{{
  "module": "<module>",
  "submodule": "<submodule>",
  "record_file": "<record_file>"
}}
"""
        payload = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 500,
            "temperature": 0,
            "system": URL_GENERATION_PROMPT,
            "messages": [{"role": "user", "content": user_message}],
        }
        response = bedrock_runtime.invoke_model(
            modelId=INFERENCE_PROFILE_ARN,
            body=json.dumps(payload),
            contentType="application/json",
        )
        response_body = json.loads(response["body"].read())
        model_output = response_body["content"][0]["text"].strip()
        
        # Clean markdown fences
        model_output = re.sub(
            r"^```(?:json)?", "", model_output.strip(), flags=re.IGNORECASE
        )
        model_output = re.sub(r"```$", "", model_output.strip())
        model_output = model_output.strip()
        
        components = json.loads(model_output)
        clean_data = {
            "module": components.get("module", "").strip(),
            "submodule": components.get("submodule", "").strip(),
            "record_file": components.get("record_file", "")
            .replace(".nl", "")
            .strip(),
        }
        
        URL_COMPONENT_CACHE[cache_key] = clean_data
        logging.info(f"Cached URL components for {cache_key}: {clean_data}")
        return clean_data
        
    except Exception as e:
        logging.error(f"Error getting URL components from LLM: {e}")
        return None


# ==========================================================
# === LOCAL URL CONSTRUCTION ===
# ==========================================================
def construct_netsuite_url_from_components(account_id: str, components: dict, record_id: str) -> str:
    if not components or not all(k in components for k in ("module", "submodule", "record_file")):
        return None
    return f"https://{account_id}.app.netsuite.com/app/{components['module']}/{components['submodule']}/{components['record_file']}.nl?id={record_id}"


# ==========================================================
# === ENRICH RESULTS WITH URL - ENHANCED ===
# ==========================================================
def enrich_results_with_urls(sql_query: str, results: dict) -> dict:
    if not results or "items" not in results:
        return results
    
    enriched_items = []
    total_items = len(results["items"])
    print(f"\n→ Generating NetSuite URLs for {total_items} records...")
    
    success_count = 0
    
    # Detect record type once from the first item (they should all be the same type)
    if results["items"]:
        first_item = results["items"][0]
        record_type, table_name = detect_record_type(sql_query, first_item)
        logging.info(f"Detected record_type='{record_type}', table_name='{table_name}' from SQL query")
        
        if record_type and table_name:
            # Get URL components once for all records
            components = get_url_components_from_llm(record_type, table_name)
            
            if components:
                logging.info(f"URL components for {table_name}: {components}")
                
                # Apply to all records
                for idx, item in enumerate(results["items"], 1):
                    enriched_item = {}
                    record_id = item.get("id") or item.get("internalid")
                    
                    # Copy all fields except 'links' and convert keys to camelCase
                    for key, value in item.items():
                        if key == "links":
                            continue  # Skip links field
                        # Convert snake_case to camelCase
                        camel_key = re.sub(r'_([a-z])', lambda m: m.group(1).upper(), key)
                        enriched_item[camel_key] = value
                    
                    # Add URL if we have an ID
                    if record_id:
                        url = construct_netsuite_url_from_components(ACCOUNT_ID, components, record_id)
                        if url:
                            enriched_item["netsuiteUrl"] = url
                            success_count += 1
                    
                    enriched_items.append(enriched_item)
                    
                    if total_items > 10 and idx % 10 == 0:
                        print(f"  Progress: {idx}/{total_items} URLs generated...")
            else:
                logging.error(f"Failed to get URL components for record_type={record_type}, table={table_name}")
                enriched_items = results["items"]
        else:
            logging.error(f"Could not detect record type from SQL query")
            enriched_items = results["items"]
    
    results["items"] = enriched_items
    print(f"✓ Successfully generated {success_count} NetSuite URLs")
    return results


# ==========================================================
# === DATA ANALYSIS / STATISTICS ===
# ==========================================================
def analyze_data_statistics(items: list) -> dict:
    if not items:
        return {"total_records": 0}
    
    stats = {
        "total_records": len(items),
        "numeric_fields": {},
        "categorical_fields": {},
        "url_count": sum(1 for i in items if "netsuite_url" in i),
    }
    
    all_fields = set(k for item in items for k in item.keys() if k != "netsuite_url")
    
    for field in all_fields:
        values = [item.get(field) for item in items if item.get(field) is not None]
        if not values:
            continue
        
        try:
            numeric_values = [float(v) for v in values if str(v).replace(".", "", 1).replace("-", "", 1).isdigit()]
            if numeric_values:
                stats["numeric_fields"][field] = {
                    "count": len(numeric_values),
                    "sum": round(sum(numeric_values), 2),
                    "avg": round(sum(numeric_values) / len(numeric_values), 2),
                    "min": round(min(numeric_values), 2),
                    "max": round(max(numeric_values), 2),
                }
            elif len(set(values)) <= 20:
                value_counts = {str(v): values.count(v) for v in set(values)}
                stats["categorical_fields"][field] = value_counts
        except Exception:
            continue
    
    return stats


# ==========================================================
# === SQL GENERATION ===
# ==========================================================
def generate_sql_with_cross_region(user_query: str, schema_context: str, error_context: str = None) -> str:
    message = f"""Schema from KB:

{schema_context}

User Query: {user_query}

{f'Error previously: {error_context}' if error_context else ''}
Always include id field. For transaction queries, include recordtype field. Return only SQL."""
    
    payload = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 2000,
        "temperature": 0.2,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": message}],
    }
    
    response = bedrock_runtime.invoke_model(
        modelId=INFERENCE_PROFILE_ARN,
        body=json.dumps(payload),
        contentType="application/json",
    )
    body = json.loads(response["body"].read())
    return clean_sql_query(body["content"][0]["text"].strip())


# ==========================================================
# === SUITEQL EXECUTION ===
# ==========================================================
def run_suiteql_query(sql_query: str):
    url = f"https://{ACCOUNT_ID}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql"
    headers = {"Content-Type": "application/json", "Prefer": "transient"}
    try:
        r = requests.post(url, auth=auth, headers=headers, json={"q": sql_query})
        if r.status_code == 200:
            return {"success": True, "data": r.json()}
        return {"success": False, "error": f"{r.status_code}: {r.text}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ==========================================================
# === RETRY LOGIC ===
# ==========================================================
def process_query_with_retry(nlq: str, max_attempts: int = 5, notificator=None):
    attempted = []
    error_context = None

    for attempt in range(1, max_attempts + 1):
        # Professional attempt start message
        if notificator:
            notificator.notify(json.dumps({
                "status": "ATTEMPT_START",
                "attempt": attempt,
                "message": f"Processing attempt {attempt} of {max_attempts} - Analyzing your query and preparing data..."
            }).encode("utf-8"))

        print(f"\n{'='*60}\nAttempt {attempt}/{max_attempts}\n{'='*60}")

        schema = retrieve_from_kb(nlq)
        sql = generate_sql_with_cross_region(nlq, schema, error_context)
        # Format SQL as single line
        sql_single_line = " ".join(sql.split())

        if sql_single_line in attempted:
            print("⚠️ Duplicate SQL generated, skipping...")
            continue

        attempted.append(sql_single_line)
        print(f"\n✓ Generated SQL:\n{sql_single_line}\n")
        result = run_suiteql_query(sql_single_line)

        if result["success"]:
            enriched = enrich_results_with_urls(sql_single_line, result["data"])
            return {"success": True, "sql": sql_single_line, "nlq": nlq, "data": enriched}
        else:
            error_context = result["error"]
            print(f"✗ Query failed: {error_context}")
            
            # Professional failure message
            if notificator:
                notificator.notify(json.dumps({
                    "status": "ATTEMPT_FAILED",
                    "attempt": attempt,
                    "message": f"Attempt {attempt} unsuccessful - Trying alternative approach {attempt + 1}...",
                    "sql_query": sql_single_line,
                    "error": error_context
                }).encode("utf-8"))
    # ...existing code...

    # Professional final failure message
    if notificator:
        notificator.notify(json.dumps({
            "status": "ALL_ATTEMPTS_FAILED",
            "message": "Unable to retrieve the requested data after multiple attempts. Please try rephrasing your question or verify the data exists in NetSuite."
        }).encode("utf-8"))

    return {"success": False, "message": "Unable to fetch the information for your query."}


# ==========================================================
# === SUMMARY (LLM + Statistics) - STRUCTURED JSON ===
# ==========================================================
def summarize_results(nlq: str, sql_query: str, results: dict):
    items = results.get("items", [])
    if not items:
        return json.dumps({"error": "No records found"})
    
    stats = analyze_data_statistics(items)
    sample = items[:10]
    
    message = f"""
Natural Query: {nlq}

SQL Query:
{sql_query}

Dataset Statistics:
{json.dumps(stats, indent=2)}

Sample Records:
{json.dumps(sample, indent=2)}

Provide a business summary in exactly 200 words or less. Return ONLY valid JSON with this structure:

{{
  "summary": "Your 200-word business summary here as a single string with no special characters or line breaks"
}}

IMPORTANT:
- Maximum 200 words
- Single continuous text in the summary field
- No markdown formatting
- No special characters like * # - |
- No line breaks or newlines
- Plain text only
- Return ONLY the JSON object
"""
    
    payload = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 500,
        "temperature": 0.1,
        "system": SUMMARIZATION_PROMPT,
        "messages": [{"role": "user", "content": message}],
    }
    
    response = bedrock_runtime.invoke_model(
        modelId=INFERENCE_PROFILE_ARN,
        body=json.dumps(payload),
        contentType="application/json",
    )
    body = json.loads(response["body"].read())
    summary_text = body["content"][0]["text"].strip()
    
    # Clean any markdown artifacts
    summary_text = re.sub(r"```(?:json)?", "", summary_text)
    summary_text = summary_text.strip()
    
    # Parse and return JSON
    try:
        summary_json = json.loads(summary_text)
        return json.dumps(summary_json, separators=(',', ':'))  # No spaces
    except:
        # Fallback if LLM doesn't return valid JSON
        return json.dumps({"summary": summary_text.replace('\n', ' ')[:200]}, separators=(',', ':'))


# ==========================================================
# === DISPLAY RESULTS (CLEAN JSON) ===
# ==========================================================
def display_results(result):
    if result and "items" in result:
        items = result["items"]
        total = len(items)
        urls = sum(1 for i in items if "netsuiteUrl" in i)
        print(f"\n✓ Query returned {total} records ({urls} URLs generated)")
        print("=" * 60)
        
        for i, item in enumerate(items[:3], 1):
            print(f"\nRecord {i}:")
            # Print compact JSON without spaces
            print(json.dumps(item, separators=(',', ':')))
        
        if total > 3:
            print(f"\n... and {total-3} more records")
    else:
        print("\n✗ No data returned")


# ==========================================================
# === MAIN LOOP ===
# ==========================================================
if __name__ == "__main__":
    print("=" * 60)
    print("NL2SQL Generator with NetSuite URL Support (KB-Powered)")
    print(f"Account: {ACCOUNT_ID}")
    print(f"Model: {INFERENCE_PROFILE_ARN}")
    print("=" * 60)
    
    while True:
        nlq = input("\nEnter your natural language query (or 'exit'): ")
        if nlq.lower() == "exit":
            break
        
        # Clear URL cache for fresh query
        URL_COMPONENT_CACHE.clear()
        
        try:
            result = process_query_with_retry(nlq)
            if result["success"]:
                display_results(result["data"])
                print("\n" + "=" * 60)
                print("SUMMARY (JSON)")
                print("=" * 60)
                summary = summarize_results(result["nlq"], result["sql"], result["data"])
                print(summary)  # Already compact JSON
            else:
                print(result["message"])
        except Exception as e:
            logging.error(f"Unexpected error: {e}")
            print(f"✗ Unexpected error: {e}")