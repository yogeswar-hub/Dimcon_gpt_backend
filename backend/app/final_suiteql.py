import os
import json
import requests
import time
import logging
import boto3
import re

from requests_oauthlib import OAuth1
from dotenv import load_dotenv

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

# Cross-region inference profile ARN
INFERENCE_PROFILE_ARN = "arn:aws:bedrock:us-east-1::inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0"

# ===== Setup Auth for NetSuite SuiteQL API =====
auth = OAuth1(
    CONSUMER_KEY,
    CONSUMER_SECRET,
    TOKEN_ID,
    TOKEN_SECRET,
    signature_method='HMAC-SHA256',
    realm=ACCOUNT_ID,
    signature_type='AUTH_HEADER'
)

# ===== Initialize AWS Bedrock clients =====
bedrock_agent = boto3.client("bedrock-agent-runtime", region_name=BEDROCK_REGION)
bedrock_runtime = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)

SYSTEM_PROMPT = """You are an expert NetSuite SuiteQL query generator. Your role is to translate natural language queries into valid, executable SuiteQL statements using the NetSuite schema provided in the knowledge base context.

## Core Output Requirements

1. **Output Format**: Return ONLY the raw SQL query with no additional text, explanations, or formatting
2. **No Markdown**: Do NOT use code blocks, backticks, or ```sql markers
3. **No Comments**: Do not include SQL comments, formatting notes, or explanatory text
4. **Direct Execution**: The output must be immediately executable by the SuiteQL API

## Schema Adherence Rules

1. **Strict Schema Compliance**: Use ONLY table names, field names (internal IDs), and relationships explicitly defined in the provided schema
2. **No Hallucination**: Never invent or assume table names, field names, or relationships not present in the schema
3. **Exact Field Names**: Use the exact internal ID field names as specified in the schema documentation
4. **No Custom Fields**: Do not add or reference custom fields unless explicitly present in the schema
5. **Schema Validation**: If a requested field or table doesn't exist, use the closest valid alternative from the schema

## NetSuite Transaction Records - CRITICAL

NetSuite uses a unified `transaction` table for all transaction types. Follow these rules strictly:

1. **Single Table**: ALL transaction types (invoices, sales orders, purchase orders, vendor bills, cash sales, credit memos, etc.) are stored in the `transaction` table
2. **Record Type Filtering**: Filter by `recordtype = '<VALUE>'` to query specific transaction types
3. **Valid Record Types**: Use ONLY the recordtype values defined in `transaction_recordtypes.jsonl` from the knowledge base
4. **No Separate Tables**: NEVER use `FROM invoice`, `FROM salesorder`, or similar - ALWAYS use `FROM transaction`

Examples:
- For invoices: `SELECT * FROM transaction WHERE recordtype = 'invoice'`
- For sales orders: `SELECT * FROM transaction WHERE recordtype = 'salesorder'`
- For vendor bills: `SELECT * FROM transaction WHERE recordtype = 'vendorbill'`

## SuiteQL Syntax Requirements

1. **No LIMIT Clause**: Do not use LIMIT in queries
2. **No MySQL Syntax**: Avoid MySQL-specific syntax like backticks (`)
3. **Proper Joins**: Use explicit JOIN syntax with proper ON conditions
4. **Date Handling**: Use NetSuite's date functions and formats
5. **Case Sensitivity**: Follow NetSuite's case requirements for keywords and identifiers

## Error Handling and Retry Logic

When an error is provided:

1. **Analyze the Error**: Carefully review the error message to identify the root cause
2. **Generate Different Query**: Create a DIFFERENT SQL query that addresses the specific error
3. **Alternative Approaches**: Try alternative table structures, field names, or join strategies
4. **Progressive Simplification**: If complex queries fail, try simpler alternatives
5. **Never Repeat**: Do not return the same query that caused the error
6. **Schema Validation**: Cross-reference with the schema to ensure all elements exist

## Query Construction Best Practices

1. **Start Simple**: Begin with basic queries and add complexity only as needed
2. **Qualify Columns**: Use table aliases and qualified column names for clarity
3. **Proper Aliasing**: Use meaningful aliases for tables and derived columns
4. **Aggregation**: Use appropriate GROUP BY clauses when using aggregate functions
5. **Subqueries**: Use subqueries when necessary but prefer JOINs when possible
6. **NULL Handling**: Consider NULL values in WHERE clauses and JOINs

## Common NetSuite Patterns

1. **Customer Queries**: Join transaction table with customer/entity tables using entity_id
2. **Item Queries**: Join transaction lines with item table using item_id
3. **Date Ranges**: Use BETWEEN or >= AND <= for date filtering
4. **Status Filters**: Many records have status fields - check schema for valid values
5. **Subsidiary Filtering**: Multi-subsidiary accounts need subsidiary filters

## Quality Checklist

Before outputting your query, verify:
- [ ] All table names exist in the schema
- [ ] All field names match schema internal IDs exactly
- [ ] Transaction queries use `transaction` table with proper recordtype filter
- [ ] No markdown formatting or code blocks
- [ ] No LIMIT clause
- [ ] Proper JOIN conditions
- [ ] Valid SuiteQL syntax
- [ ] Query addresses the natural language request accurately

Remember: Your output should be production-ready SQL that can execute immediately without any modifications."""

SUMMARIZATION_PROMPT = """You are a data analysis assistant. Your task is to analyze NetSuite query results and provide clear, actionable summaries.

When given query results with aggregated statistics and sample data, you should:
1. Present the TOTAL statistics prominently (covering ALL records, not just the sample)
2. Identify key insights and patterns across the entire dataset
3. Provide meaningful breakdowns and distributions
4. Highlight important findings or anomalies
5. Present information in a clear, business-friendly format using tables or structured formatting

The statistics provided cover the ENTIRE dataset. Use these to give a comprehensive overview.
Be concise but comprehensive. Focus on what matters to business users."""

def retrieve_from_kb(query: str, num_results: int = 5) -> str:
    """Retrieve relevant schema documents from Knowledge Base"""
    response = bedrock_agent.retrieve(
        knowledgeBaseId=KB_ID,
        retrievalQuery={'text': query},
        retrievalConfiguration={
            'vectorSearchConfiguration': {
                'numberOfResults': num_results
            }
        }
    )
    
    # Combine all retrieved content
    context_parts = []
    for result in response.get('retrievalResults', []):
        context_parts.append(result['content']['text'])
    
    return "\n\n".join(context_parts)

def clean_sql_query(sql_query: str) -> str:
    """Remove markdown code blocks and extra formatting from SQL query"""
    # Remove ```sql and ``` markers
    sql_query = re.sub(r'^```sql\s*\n?', '', sql_query, flags=re.IGNORECASE)
    sql_query = re.sub(r'^```\s*\n?', '', sql_query)
    sql_query = re.sub(r'\n?```\s*$', '', sql_query)
    
    # Strip whitespace
    sql_query = sql_query.strip()
    
    return sql_query

def analyze_data_statistics(items: list) -> dict:
    """Analyze all records and compute comprehensive statistics"""
    logging.info(f"[final_suiteql] Analyzing {len(items)} records for statistics.")
    
    if not items:
        return {"total_records": 0}
    
    stats = {
        "total_records": len(items),
        "field_analysis": {},
        "numeric_fields": {},
        "date_fields": {},
        "categorical_fields": {}
    }
    
    # Get all unique fields
    all_fields = set()
    for item in items:
        all_fields.update(item.keys())
    
    # Analyze each field
    for field in all_fields:
        values = [item.get(field) for item in items if item.get(field) is not None]
        
        if not values:
            continue
        
        # Skip if values contain lists or dicts (complex types)
        if any(isinstance(v, (list, dict)) for v in values):
            continue
            
        # Check if numeric
        try:
            numeric_values = [float(v) for v in values if v != '' and v is not None]
            if numeric_values and len(numeric_values) > 0:
                stats["numeric_fields"][field] = {
                    "count": len(numeric_values),
                    "sum": round(sum(numeric_values), 2),
                    "avg": round(sum(numeric_values) / len(numeric_values), 2),
                    "min": round(min(numeric_values), 2),
                    "max": round(max(numeric_values), 2)
                }
        except (ValueError, TypeError):
            pass
        
        # Check if categorical (limited unique values)
        # Filter out complex types before creating set
        simple_values = [v for v in values if not isinstance(v, (list, dict))]
        try:
            unique_values = list(set(simple_values))
            if len(unique_values) <= 20:  # Reasonable number for categories
                value_counts = {}
                for v in simple_values:
                    str_v = str(v)  # Convert to string for consistent keys
                    value_counts[str_v] = value_counts.get(str_v, 0) + 1
                stats["categorical_fields"][field] = value_counts
        except TypeError:
            # Skip fields that still can't be processed
            continue
    
    return stats

def create_smart_sample(items: list, sample_size: int = 30) -> list:
    """Create a representative sample from all records"""
    
    if len(items) <= sample_size:
        return items
    
    # Take records from beginning, middle, and end
    step = len(items) // sample_size
    sample = []
    
    for i in range(0, len(items), step):
        if len(sample) < sample_size:
            sample.append(items[i])
    
    return sample

def generate_sql_with_cross_region(user_query: str, schema_context: str, error_context: str = None) -> str:
    """Generate SQL using cross-region inference profile"""
    
    if error_context:
        user_message = f"""Schema information from NetSuite knowledge base:

{schema_context}

Natural language query: {user_query}

Previous SQL query resulted in this error:
{error_context}

Please generate a DIFFERENT SQL query that avoids this error. Try a different approach or simplification.
Generate only the SQL query with no explanation, no markdown code blocks, and no additional text."""
    else:
        user_message = f"""Schema information from NetSuite knowledge base:

{schema_context}

Natural language query: {user_query}

Generate only the SQL query with no explanation, no markdown code blocks, and no additional text."""
    
    payload = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 2000,
        "temperature": 0.3 if error_context else 0,
        "system": SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": user_message
            }
        ]
    }
    
    response = bedrock_runtime.invoke_model(
        modelId=INFERENCE_PROFILE_ARN,
        body=json.dumps(payload),
        contentType="application/json"
    )
    
    response_body = json.loads(response["body"].read())
    raw_sql = response_body["content"][0]["text"].strip()
    
    # Clean the SQL query
    cleaned_sql = clean_sql_query(raw_sql)
    
    return cleaned_sql

def summarize_results(natural_query: str, sql_query: str, results: dict) -> str:
    """Summarize ALL query results using Claude with comprehensive statistics"""
    logging.info(f"[final_suiteql] Summarizing results for NLQ: {natural_query}")
    items = results.get('items', [])
    total_count = len(items)
    logging.info(f"[final_suiteql] Total records to summarize: {total_count}")
    
    if total_count == 0:
        return "No records found matching your query."
    
    # Analyze ALL records for statistics
    print("\n→ Analyzing all records for comprehensive statistics...")
    statistics = analyze_data_statistics(items)
    
    # Create a smart sample for context
    sample_items = create_smart_sample(items, sample_size=30)
    
    user_message = f"""Natural Language Query: {natural_query}

SQL Query Executed:
{sql_query}

COMPLETE DATASET STATISTICS (covering ALL {total_count} records):
{json.dumps(statistics, indent=2)}

Representative Sample Data ({len(sample_items)} records from across the dataset):
{json.dumps(sample_items, indent=2)}

IMPORTANT: The statistics above cover ALL {total_count} records. Please provide a comprehensive summary of the ENTIRE dataset, not just the sample. Focus on:

1. Overall summary of all {total_count} records
2. Key metrics and totals (use the statistics that cover all records)
3. Distributions and patterns across the complete dataset
4. Important findings and insights
5. Any notable anomalies or trends

Format your response in a clear, business-friendly way with appropriate structure and emphasis on the COMPLETE dataset."""
    
    payload = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 3000,
        "temperature": 0.1,
        "system": SUMMARIZATION_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": user_message
            }
        ]
    }
    
    response = bedrock_runtime.invoke_model(
        modelId=INFERENCE_PROFILE_ARN,
        body=json.dumps(payload),
        contentType="application/json"
    )
    
    response_body = json.loads(response["body"].read())
    summary = response_body["content"][0]["text"].strip()
    
    return summary

def nlq_to_sql(natural_query: str, error_context: str = None) -> str:
    """Convert natural language to SQL using KB retrieval + cross-region generation"""
    logging.info(f"[final_suiteql] Received NLQ: {natural_query}")
    # Step 1: Retrieve relevant schema from Knowledge Base
    logging.info("[final_suiteql] Retrieving schema from Knowledge Base...")
    schema_context = retrieve_from_kb(natural_query)
    logging.debug(f"[final_suiteql] Retrieved schema context: {schema_context}")

    # Step 2: Generate SQL using cross-region model
    if error_context:
        logging.info("[final_suiteql] Regenerating SQL with error context...")
        logging.debug(f"[final_suiteql] Error context: {error_context}")
    else:
        logging.info("[final_suiteql] Generating SQL with cross-region model...")

    sql_query = generate_sql_with_cross_region(natural_query, schema_context, error_context)
    logging.info(f"[final_suiteql] Generated SQL: {sql_query}")

    return sql_query

def run_suiteql_query(sql_query: str):
    """Execute SuiteQL query against NetSuite API"""
    logging.info(f"[final_suiteql] Executing SuiteQL query: {sql_query}")
    url = f"https://{ACCOUNT_ID}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql"
    headers = {
        "Content-Type": "application/json",
        "Prefer": "transient"
    }
    payload = {"q": sql_query}
    max_retries = 3
    backoff_factor = 2

    for attempt in range(max_retries):
        try:
            logging.debug(f"[final_suiteql] Attempt {attempt+1}: POST {url} Payload: {payload}")
            response = requests.post(url, auth=auth, headers=headers, json=payload)
            logging.debug(f"[final_suiteql] Response status: {response.status_code}, Body: {response.text}")
            if response.status_code == 200:
                logging.info("[final_suiteql] SuiteQL query succeeded.")
                return {"success": True, "data": response.json()}
            else:
                error_msg = f"Status {response.status_code}: {response.text}"
                logging.error(f"[final_suiteql] SuiteQL API error - {error_msg}")
                return {"success": False, "error": error_msg}
        except requests.RequestException as e:
            logging.error(f"[final_suiteql] Request exception: {e}")
            if attempt < max_retries - 1:
                time.sleep(backoff_factor ** attempt)
            else:
                return {"success": False, "error": str(e)}

    logging.error("[final_suiteql] Max retries exceeded for SuiteQL query.")
    return {"success": False, "error": "Max retries exceeded"}

def process_query_with_retry(natural_query: str, max_attempts: int = 5):
    """Process a query with retry logic - maximum 5 attempts"""
    
    attempted_queries = []
    error_context = None
    
    for attempt in range(1, max_attempts + 1):
        print(f"\n{'='*60}")
        print(f"Attempt {attempt}/{max_attempts}")
        print(f"{'='*60}")
        
        # Generate SQL
        sql_query = nlq_to_sql(natural_query, error_context)
        
        # Check if we've already tried this exact query
        if sql_query in attempted_queries:
            print(f"\n⚠️  Warning: Generated the same SQL query as before. Skipping...")
            continue
        
        attempted_queries.append(sql_query)
        
        print(f"\n✓ Generated SQL (Attempt {attempt}):")
        print(sql_query)
        
        # Execute SQL
        print(f"\n→ Running SuiteQL query...")
        result = run_suiteql_query(sql_query)
        
        if result["success"]:
            return {"success": True, "data": result["data"], "sql": sql_query, "natural_query": natural_query}
        else:
            error_context = result["error"]
            print(f"\n✗ Query failed: {error_context}")
            
            if attempt < max_attempts:
                print(f"\n→ Retrying with error context...")
    
    # After 5 attempts, return failure
    return {"success": False, "message": "Unable to fetch the information for your query."}

def display_results(result):
    """Display query results with sample"""
    if result and "items" in result:
        total_records = len(result['items'])
        print(f"\n✓ Query returned {total_records} records")
        print("=" * 60)
        
        # Show first 3 records as sample
        print("\n📋 Sample Records (first 3 of {}):\n".format(total_records))
        for i, item in enumerate(result["items"][:3], 1):
            print(f"Record {i}:")
            print(json.dumps(item, indent=2))
            print()
        
        if total_records > 3:
            print(f"... and {total_records - 3} more records")
    else:
        print("\n✗ No data returned")

if __name__ == "__main__":
    print("=" * 60)
    print("NL2SQL Generator with Complete Dataset Summarization")
    print(f"Using: {INFERENCE_PROFILE_ARN}")
    print("=" * 60)

    while True:
        nlq = input("\nEnter your natural language query (or 'exit'): ")
        if nlq.lower() == "exit":
            break

        print("\n" + "=" * 60)
        print("PROCESSING QUERY")
        print("=" * 60)
        
        try:
            # Try the query with 5 attempts
            result = process_query_with_retry(nlq, max_attempts=5)
            
            if result["success"]:
                # Display sample records
                display_results(result["data"])
                
                # Generate and display comprehensive summary
                print("\n" + "=" * 60)
                print("COMPREHENSIVE SUMMARY (ALL RECORDS)")
                print("=" * 60)
                summary = summarize_results(result["natural_query"], result["sql"], result["data"])
                print("\n" + summary)
                print("\n" + "=" * 60)
            else:
                print(f"\n{'='*60}")
                print(result["message"])
                print(f"{'='*60}")
                
        except Exception as e:
            logging.error(f"Unexpected error: {e}")
            print(f"\n✗ An unexpected error occurred: {e}")