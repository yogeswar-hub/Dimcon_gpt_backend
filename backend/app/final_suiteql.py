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
# === MANDATORY FIELDS DETECTION (DYNAMIC KB-BASED) ===
# === NO HARDCODING - READS DIRECTLY FROM KB ===
# ==========================================================

def get_mandatory_fields_from_kb(user_query: str) -> tuple:
    """
    Try to retrieve mandatory fields from KB (JSONL format).
    Uses LLM to intelligently match query to record types.
    
    Returns: (found: bool, instruction: str)
    """
    # Step 1: Retrieve all mandatory field records from KB
    kb_result = retrieve_from_kb("mandatory_fields record_type", num_results=20)
    
    if not kb_result or "{" not in kb_result:
        logging.info("⚠️ No mandatory fields data in KB - using natural field selection")
        return (False, "")
    
    # Step 2: Parse all JSONL records
    all_records = []
    for line in kb_result.split('\n'):
        line = line.strip()
        if not line or not line.startswith('{'):
            continue
        
        try:
            data = json.loads(line)
            if 'record_type' in data and 'mandatory_fields' in data:
                all_records.append(data)
        except json.JSONDecodeError:
            continue
    
    if not all_records:
        logging.info("⚠️ No valid mandatory fields records found in KB")
        return (False, "")
    
    # Step 3: Use LLM to match query to record type
    record_type_list = [r.get('record_type') for r in all_records]
    
    matching_prompt = f"""You are a record type matcher. Given a user query and a list of available record types, determine which record type the user is asking about.

User Query: "{user_query}"

Available Record Types:
{chr(10).join(f"- {rt}" for rt in record_type_list)}

Instructions:
1. Analyze the user's query to understand what type of data they want
2. Match it to ONE of the available record types above
3. Consider synonyms (e.g., "items" = "Inventory Item", "invoices" = "Invoice", "orders" = "Sales Order" or "Purchase Order")
4. If the query mentions multiple possible types, pick the most specific one
5. If no record type matches, respond with "NONE"

Respond with ONLY the matching record type name (exactly as shown above) or "NONE".

Examples:
Query: "show me inventory items" → Inventory Item
Query: "list all customers" → Customer  
Query: "what are the open sales orders" → Open Sales Order
Query: "get employee data" → Employee
Query: "show invoices" → Invoice (if available)
Query: "show departments" → NONE (if not in list)

Your response (one line only):"""
    
    try:
        # Call LLM for matching
        response = bedrock_runtime.invoke_model(
            modelId=INFERENCE_PROFILE_ARN,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 50,
                "temperature": 0,
                "messages": [
                    {
                        "role": "user",
                        "content": matching_prompt
                    }
                ]
            })
        )
        
        response_body = json.loads(response['body'].read())
        matched_type = response_body['content'][0]['text'].strip()
        
        logging.info(f"🤖 LLM matched query to: '{matched_type}'")
        
        # Find the matching record
        if matched_type != "NONE":
            for record in all_records:
                if record.get('record_type') == matched_type:
                    fields = record.get('mandatory_fields', [])
                    if fields:
                        field_list = ', '.join(fields)
                        logging.info(f"✓ Found mandatory fields for '{matched_type}': {field_list}")
                        
                        instruction = f"""MANDATORY FIELDS FOR {matched_type.upper()} (STRICT - ONLY THESE FIELDS):

**YOU MUST INCLUDE EXACTLY THESE FIELDS IN YOUR SELECT CLAUSE:**
- id (always required)
- {field_list}

**YOU MUST NOT INCLUDE ANY OTHER FIELDS.**
Do not add: email, phone, address, status, date fields, or any other fields not listed above.

ONLY SELECT: id, {field_list}

These fields are NON-NEGOTIABLE and EXCLUSIVE. No additional fields permitted."""
                        return (True, instruction)
        
        logging.info(f"⚠️ LLM returned '{matched_type}' - no matching record found in KB")
        return (False, "")
        
    except Exception as e:
        logging.error(f"Error using LLM for matching: {e}")
        logging.info("⚠️ Falling back to natural field selection")
        return (False, "")


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
        logging.debug(f"Using cached URL components for {cache_key}")
        return URL_COMPONENT_CACHE[cache_key]
    
    try:
        # Construct query based on whether we have a table name
        if table_name and table_name != record_type:
            url_query = f"NetSuite URL structure for {record_type} record in {table_name} table"
        else:
            url_query = f"NetSuite URL structure for {record_type} record type"
        
        url_context = retrieve_from_kb(url_query, num_results=10)
        
        user_message = f"""
Based on this context about NetSuite URL structures:

{url_context}

For record type: {record_type}
{f'From table: {table_name}' if table_name else ''}

Provide the URL components in JSON format:
{{
  "app": "common or accounting or...?",
  "type": "entity or transaction or...?",
  "page": "custjob.nl or item.nl or...?"
}}

Return ONLY valid JSON, no explanation.
"""
        
        payload = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 300,
            "temperature": 0.1,
            "system": URL_GENERATION_PROMPT,
            "messages": [{"role": "user", "content": user_message}],
        }
        
        response = bedrock_runtime.invoke_model(
            modelId=INFERENCE_PROFILE_ARN,
            body=json.dumps(payload),
            contentType="application/json",
        )
        body = json.loads(response["body"].read())
        response_text = body["content"][0]["text"].strip()
        
        # Clean response
        response_text = re.sub(r"```(?:json)?", "", response_text).strip()
        
        url_components = json.loads(response_text)
        URL_COMPONENT_CACHE[cache_key] = url_components
        
        return url_components
    
    except Exception as e:
        logging.error(f"Failed to get URL components for {record_type}: {e}")
        return None


# ==========================================================
# === ENRICH RESULTS WITH URLS ===
# ==========================================================
def enrich_results_with_urls(sql_query: str, query_result: dict) -> dict:
    """
    Enrich query results with NetSuite URLs for each record.
    """
    items = query_result.get("items", [])
    if not items:
        return query_result
    
    enriched_items = []
    
    for item in items:
        # Remove the links field from NetSuite API response
        if "links" in item:
            del item["links"]
        
        record_type, table_name = detect_record_type(sql_query, item)
        
        if not record_type:
            enriched_items.append(item)
            continue
        
        # Get URL components from LLM
        url_components = get_url_components_from_llm(record_type, table_name)
        
        if url_components and "id" in item:
            record_id = item["id"]
            
            # Build NetSuite URL
            app = url_components.get("app", "common")
            type_ = url_components.get("type", "entity")
            page = url_components.get("page", "custjob.nl")
            
            netsuite_url = f"https://{ACCOUNT_ID}.app.netsuite.com/app/{app}/{type_}/{page}?id={record_id}"
            
            # Add URL to item
            item["netsuiteUrl"] = netsuite_url
        
        enriched_items.append(item)
    
    query_result["items"] = enriched_items
    return query_result


# ==========================================================
# === DATA STATISTICS ANALYSIS ===
# ==========================================================
def analyze_data_statistics(items: list) -> dict:
    """
    Analyze dataset to provide statistics for summarization.
    """
    if not items:
        return {"total_records": 0}
    
    stats = {
        "total_records": len(items),
        "numeric_fields": {},
        "categorical_fields": {}
    }
    
    # Analyze each field
    if items:
        sample_item = items[0]
        for field in sample_item.keys():
            if field == "netsuiteUrl":
                continue
            
            values = [item.get(field) for item in items if item.get(field) is not None]
            if not values:
                continue
            
            try:
                # Try to convert to numeric
                numeric_values = []
                for v in values:
                    try:
                        numeric_values.append(float(v))
                    except (ValueError, TypeError):
                        pass
                
                if len(numeric_values) > len(values) * 0.5:  # More than 50% numeric
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
# === SQL GENERATION WITH KB-FIRST MANDATORY FIELDS ===
# ==========================================================
def generate_sql_with_cross_region(user_query: str, schema_context: str, error_context: str = None) -> str:
    """
    Generate SQL query with KB-first approach for mandatory fields.
    
    If mandatory fields found in KB: Enforce them strictly
    If not found: Use natural LLM field selection
    """
    # Try to get mandatory fields from KB
    found_mandatory, mandatory_instruction = get_mandatory_fields_from_kb(user_query)
    
    if found_mandatory:
        # STRICT MODE: Mandatory fields found in KB
        message = f"""Schema from KB:

{schema_context}

{mandatory_instruction}

User Query: {user_query}

{f'Error previously: {error_context}' if error_context else ''}

CRITICAL: You MUST include ALL the mandatory fields listed above in your SELECT clause.
Always include id field. For transaction queries, include recordtype field. 
Return only SQL."""
    else:
        # NATURAL MODE: No mandatory fields, use LLM's judgment
        message = f"""Schema from KB:

{schema_context}

User Query: {user_query}

{f'Error previously: {error_context}' if error_context else ''}

Include relevant fields based on the user's query. Always include id field. 
For transaction queries, include recordtype field. Return only SQL."""
    
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
# === RETRY LOGIC WITH MODE VISIBILITY ===
# ==========================================================
def process_query_with_retry(nlq: str, max_attempts: int = 5, notificator=None):
    attempted = []
    error_context = None
    
    # Check mode once before attempts
    found_mandatory, _ = get_mandatory_fields_from_kb(nlq)
    mode = "MANDATORY FIELDS MODE" if found_mandatory else "NATURAL SELECTION MODE"
    
    for attempt in range(1, max_attempts + 1):
        print(f"\n{'='*60}")
        print(f"Attempt {attempt}/{max_attempts} - {mode}")
        print(f"{'='*60}")
        
        # Add notification for attempt start if notificator exists
        if notificator:
            notificator.notify(json.dumps({
                "status": "ATTEMPT_START", 
                "attempt": attempt,
                "message": f"Processing attempt {attempt} of {max_attempts}...",
                "timestamp": time.time() * 1000
            }).encode("utf-8"))
        
        schema = retrieve_from_kb(nlq, num_results=5)
        sql = generate_sql_with_cross_region(nlq, schema, error_context)
        
        if sql in attempted:
            print("⚠️ Duplicate SQL generated, skipping...")
            continue
        
        attempted.append(sql)
        print(f"\n✓ Generated SQL:\n{sql}\n")
        
        result = run_suiteql_query(sql)
        
        if result["success"]:
            enriched = enrich_results_with_urls(sql, result["data"])
            return {"success": True, "sql": sql, "nlq": nlq, "data": enriched}
        else:
            error_context = result["error"]
            print(f"✗ Query failed: {error_context}")
            
            # Add notification for failed attempt if notificator exists
            if notificator:
                notificator.notify(json.dumps({
                    "status": "ATTEMPT_FAILED",
                    "attempt": attempt,
                    "message": f"Attempt {attempt} failed: {error_context}",
                    "sql_query": sql,
                    "timestamp": time.time() * 1000
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
# === UTILITY: KB HEALTH CHECK (UPDATED FOR JSONL) ===
# ==========================================================
def check_kb_mandatory_fields_coverage():
    """
    Utility function to check if mandatory fields are properly indexed in KB.
    Run this periodically to verify KB health.
    """
    print("\n" + "="*60)
    print("KNOWLEDGE BASE MANDATORY FIELDS CHECK")
    print("="*60)
    
    # Retrieve all mandatory field records from KB
    kb_result = retrieve_from_kb("mandatory_fields record_type", num_results=20)
    
    if not kb_result or "{" not in kb_result:
        print("\n✗ ERROR: No JSONL data found in KB")
        print("Make sure your mandatory_fields_latest.jsonl is uploaded and synced.")
        return
    
    # Parse all JSONL records
    all_records = []
    for line in kb_result.split('\n'):
        line = line.strip()
        if line.startswith('{'):
            try:
                data = json.loads(line)
                if 'record_type' in data and 'mandatory_fields' in data:
                    all_records.append(data)
            except json.JSONDecodeError:
                continue
    
    if not all_records:
        print("\n✗ ERROR: Found JSON data but couldn't parse records")
        return
    
    print(f"\n✓ SUCCESS: Found {len(all_records)} record types in KB")
    print("="*60)
    
    for i, record in enumerate(all_records, 1):
        record_type = record.get('record_type', 'Unknown')
        fields = record.get('mandatory_fields', [])
        field_count = len(fields)
        
        print(f"\n{i}. {record_type.upper()}: ✓ FOUND ({field_count} mandatory fields)")
        print(f"   Fields: {', '.join(fields)}")
    
    print("\n" + "="*60)
    print(f"Total: {len(all_records)} record types with mandatory fields")
    print("="*60)


# ==========================================================
# === MAIN LOOP ===
# ==========================================================
if __name__ == "__main__":
    print("=" * 60)
    print("NL2SQL Generator with NetSuite URL Support (KB-Powered)")
    print("KB-First Mandatory Fields with Natural Fallback")
    print(f"Account: {ACCOUNT_ID}")
    print(f"Model: {INFERENCE_PROFILE_ARN}")
    print("=" * 60)
    
    # Optional: Uncomment to run KB health check on startup
    # print("\nRunning KB Health Check...")
    # check_kb_mandatory_fields_coverage()
    # print("\nStarting query loop...\n")
    
    while True:
        nlq = input("\nEnter your natural language query (or 'exit' to quit, 'check' for KB health): ")
        
        if nlq.lower() == "exit":
            print("Goodbye!")
            break
        
        if nlq.lower() == "check":
            check_kb_mandatory_fields_coverage()
            continue
        
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