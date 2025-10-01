import logging
from app.final_suiteql import nlq_to_sql, run_suiteql_query

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

def process_nl2sql_query(nlq: str) -> dict:
    logger.info(f"[suiteql_service] Received NLQ: {nlq}")
    try:
        sql_query = nlq_to_sql(nlq)
        logger.info(f"[suiteql_service] Generated SQL: {sql_query}")
        result = run_suiteql_query(sql_query)
        logger.info(f"[suiteql_service] SuiteQL result: {result}")
        # Log result details
        if result is not None and "items" in result:
            logger.info(f"[suiteql_service] SuiteQL returned {len(result['items'])} records.")
            for idx, item in enumerate(result["items"], 1):
                logger.info(f"[suiteql_service] Record {idx}: {item}")
        else:
            logger.warning(f"[suiteql_service] No data or error in SuiteQL result: {result}")
        return {"sql_query": sql_query, "result": result}
    except Exception as e:
        logger.error(f"[suiteql_service] Error: {e}", exc_info=True)
        return {"error": str(e)}