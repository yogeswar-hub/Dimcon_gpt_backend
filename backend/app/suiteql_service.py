import logging
from app.final_suiteql import process_query_with_retry, summarize_results

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

def process_nl2sql_query(nlq: str, limit: int = None) -> dict:
    """
    Process natural language query with optional result limiting and retry logic.
    Also returns a comprehensive summary of the results.
    
    Args:
        nlq: Natural language query
        limit: Maximum number of records to return (if specified)
    """
    logger.info(f"[suiteql_service] Received NLQ: {nlq}")
    try:
        result = process_query_with_retry(nlq, max_attempts=5)
        logger.info(f"[suiteql_service] Process query result: {result}")
        
        if result.get("success"):
            data = result.get("data", {})
            items = data.get("items", [])
            
            logger.info(f"[suiteql_service] SuiteQL returned {len(items)} records.")
            
            for idx, item in enumerate(items[:3], 1):
                logger.debug(f"[suiteql_service] Record {idx}: {item}")

            summary = summarize_results(
                result.get("natural_query"),
                result.get("sql"),
                data
            )

            return {
                "sql_query": result.get("sql"), 
                "result": data,
                "natural_query": result.get("natural_query"),
                "summary": summary
            }
        else:
            error_msg = result.get("message", "Query processing failed after multiple attempts")
            logger.warning(f"[suiteql_service] SuiteQL error: {error_msg}")
            return {"error": error_msg}
            
    except Exception as e:
        logger.error(f"[suiteql_service] Exception occurred: {e}", exc_info=True)
        return {"error": str(e)}