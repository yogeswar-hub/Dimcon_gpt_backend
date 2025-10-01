import json
import requests
import time
import logging
import boto3
from requests_oauthlib import OAuth1
from dotenv import load_dotenv
import os

load_dotenv()

ACCOUNT_ID = os.getenv("NETSUITE_ACCOUNT_ID")
CONSUMER_KEY = os.getenv("NETSUITE_CONSUMER_KEY")
CONSUMER_SECRET = os.getenv("NETSUITE_CONSUMER_SECRET")
TOKEN_ID = os.getenv("NETSUITE_TOKEN_ID")
TOKEN_SECRET = os.getenv("NETSUITE_TOKEN_SECRET")
BEDROCK_REGION = os.getenv("BEDROCK_REGION")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ===== Bedrock CONFIG =====
KB_ID = "VRQ5QNZEW6"   # Your knowledge base ID

# Cross-region inference profile ARN
INFERENCE_PROFILE_ARN = "arn:aws:bedrock:us-east-1::inference-profile/us.anthropic.claude-3-7-sonnet-20250219-v1:0"

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

SYSTEM_PROMPT = """You are a NL2SQL generator agent. You will be provided with relevant NetSuite schema information.

When a user provides a natural language query, convert it into a SQL query using the NetSuite schema contained in the provided context.

Rules:
- Output only the SQL query, with no explanation or description.
- Always generate a valid SQL query strictly following the schema in the knowledge base.
- Do not include comments, formatting notes, or extra text. Output only raw SQL.
- Follow the schema exactly as provided in the knowledge base.
- Use only the table names, field names, and relationships explicitly defined in the schema.
- Do not hallucinate or invent random table names or fields.
- Never generate queries using tables or fields outside the provided schema.
- If a requested field or table does not exist in the schema, do not invent it — instead generate a query using only valid schema elements.
- If the user sends an error message for a generated SQL query, carefully review the error and generate a corrected version of the query using the schema.
- If the user reports errors multiple times, provide multiple alternative corrected versions of the SQL query.
- Do not give the same query repeatedly if it causes errors. Always adjust the structure or syntax to resolve the issue.
- Ensure SQL syntax is compatible with NetSuite SuiteQL (e.g., no MySQL backticks).
- Your output must always be only the SQL query corresponding to the user's natural language query, with no extra information.
- Use the field names (mentioned as internal ID) exactly mentioned in the schema.
- Do not add any custom field names. Follow only the field names which are present in the schema in the knowledge base.
- If you get any error use that error response and generate the new sql query.
- Generate the SQL command in a way that it can be directly executed in the SuiteQL API.
- Dont use the LIMIT clause in the query.

Additional rules for transaction records:
- There is no separate table for INVOICE, CASH_SALE, VENDOR_BILL, etc. All of these are stored in the `transaction` table.
- When a query involves one of these subtypes, always use `transaction` as the table name and filter using `recordtype = '<VALUE>'`.
- The only valid values for `recordtype` are the ones listed in the knowledge base file `transaction_recordtypes.jsonl`.
- Always map natural language queries like "invoices", "vendor bills", "credit memos", "sales orders" to their corresponding `recordtype` values.
- Do not invent or use a `FROM <recordtype>` table — always `FROM transaction`.
"""

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

def generate_sql_with_cross_region(user_query: str, schema_context: str) -> str:
    """Generate SQL using cross-region inference profile"""
    
    user_message = f"""Schema information from NetSuite knowledge base:

{schema_context}

Natural language query: {user_query}

Generate only the SQL query with no explanation or additional text."""
    
    payload = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 2000,
        "temperature": 0,
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
    return response_body["content"][0]["text"].strip()

def nlq_to_sql(natural_query: str) -> str:
    logger.info("[final_suiteql] Starting NLQ to SQL conversion")
    logger.info(f"[final_suiteql] NLQ: {natural_query}")
    schema_context = retrieve_from_kb(natural_query)
    logger.info(f"[final_suiteql] Retrieved schema context: {schema_context[:200]}...")
    sql_query = generate_sql_with_cross_region(natural_query, schema_context)
    logger.info(f"[final_suiteql] Generated SQL: {sql_query}")
    return sql_query

def run_suiteql_query(sql_query: str):
    logger.info(f"[final_suiteql] Running SuiteQL query: {sql_query}")
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
            logger.info(f"[final_suiteql] Attempt {attempt+1} to run SuiteQL query.")
            response = requests.post(url, auth=auth, headers=headers, json=payload)
            logger.info(f"[final_suiteql][DEBUG] SuiteQL response status: {response.status_code}")
            logger.info(f"[final_suiteql][DEBUG] SuiteQL response text: {response.text}")
            
            if response.status_code == 200:
                result_json = response.json()
                items = result_json.get("items", [])
                logger.info(f"[final_suiteql] SuiteQL returned {len(items)} records.")
                for idx, item in enumerate(items, 1):
                    logger.info(f"[final_suiteql] Record {idx}: {json.dumps(item, indent=2)}")
                return result_json
            else:
                # Return error details instead of None
                error_response = {
                    "error": True,
                    "status_code": response.status_code,
                    "error_message": response.text,
                    "sql_query": sql_query
                }
                logger.error(f"[final_suiteql] SuiteQL API error {response.status_code}: {response.text}")
                return error_response
                
        except requests.RequestException as e:
            logger.error(f"[final_suiteql][DEBUG] Request exception: {e}", exc_info=True)
            if attempt < max_retries - 1:
                time.sleep(backoff_factor ** attempt)
            else:
                return {
                    "error": True,
                    "error_message": f"Request failed after {max_retries} attempts: {str(e)}",
                    "sql_query": sql_query
                }
    
    return {
        "error": True,
        "error_message": "Max retries exceeded",
        "sql_query": sql_query
    }

if __name__ == "__main__":
    print("=" * 60)
    print("NL2SQL Generator with Cross-Region Inference + SuiteQL Runner")
    print(f"Using: {INFERENCE_PROFILE_ARN}")
    print("=" * 60)

    while True:
        nlq = input("\nEnter your natural language query (or 'exit'): ")
        if nlq.lower() == "exit":
            break

        print("\n" + "-" * 60)
        print("Generating SQL query...")
        print("-" * 60)
        
        try:
            sql_query = nlq_to_sql(nlq)
            print("\n✓ Generated SQL:")
            print(sql_query)
            
            print("\n" + "-" * 60)
            print("Running SuiteQL query...")
            print("-" * 60)
            
            result = run_suiteql_query(sql_query)

            if result and "items" in result:
                print(f"\n✓ Query Results ({len(result['items'])} records):")
                print("=" * 60)
                for i, item in enumerate(result["items"], 1):
                    print(f"\nRecord {i}:")
                    print(json.dumps(item, indent=2))
            else:
                print("\n✗ No data or error running SuiteQL query.")
                
        except Exception as e:
            logger.error(f"Error: {e}")
            print(f"\n✗ An error occurred: {e}")