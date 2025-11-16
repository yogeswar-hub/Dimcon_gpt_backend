import logging
import json
import time
import boto3
import os
from decimal import Decimal as decimal
from app.final_suiteql import process_query_with_retry, summarize_results
from app.charting import generate_chart_specs_for_suiteql, extract_axes_from_nlq_ai

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

WEBSOCKET_SESSION_TABLE_NAME = os.environ["WEBSOCKET_SESSION_TABLE_NAME"]
dynamodb_client = boto3.resource("dynamodb")
table = dynamodb_client.Table(WEBSOCKET_SESSION_TABLE_NAME)

def process_nl2sql_query(nlq: str, limit: int = None, notificator=None) -> dict:
    # Overall processing start time
    overall_start_time = time.time()
    logger.info(f"[suiteql_service] Started processing NLQ: {nlq} at {overall_start_time * 1000:.3f}ms")
    
    final_result = {
        "sql_query": None,
        "result": None,
        "natural_query": nlq,
        "summary": None,
        "chart_specs": None,
        "errors": {},
        "timing": {
            "overall_start": overall_start_time * 1000,
            "stages": {}
        }
    }
    
    try:
        # =============================================================
        # STAGE 1: QUERY PROCESSING (process_query_with_retry)
        # =============================================================
        stage1_start = time.time()
        final_result["timing"]["stages"]["query_start"] = stage1_start * 1000
        
        if notificator:
            notification_start = time.time()
            notificator.notify(json.dumps({
                "status": "QUERY_PROCESSING_START",
                "stage": "query",
                "message": "Starting SQL query generation and execution...",
                "timestamp": notification_start * 1000
            }).encode("utf-8"))
            notification_time = (time.time() - notification_start) * 1000
            logger.info(f"[TIMING] Query start notification sent in {notification_time:.3f}ms")
        
        query_result = process_query_with_retry(nlq, max_attempts=5, notificator=notificator)
        stage1_end = time.time()
        stage1_duration = (stage1_end - stage1_start) * 1000
        final_result["timing"]["stages"]["query_duration"] = stage1_duration
        
        logger.info(f"[TIMING] Query processing completed in {stage1_duration:.3f}ms")
        
        if query_result.get("success"):
            # Stage 1 Success
            data = query_result.get("data", {})
            items = data.get("items", [])
            # Format SQL query for display - remove \n and format nicely
            raw_sql = query_result.get("sql", "")
            formatted_sql = raw_sql.replace("\\n", "\n").replace("\n", "\n")
            final_result["sql_query"] = formatted_sql
            final_result["result"] = data
            
            if notificator:
                notification_start = time.time()
                notificator.notify(json.dumps({
                    "status": "QUERY_PROCESSING_SUCCESS",
                    "stage": "query",
                    "message": f"Successfully retrieved {len(items)} records from NetSuite",
                    "processing_time_ms": stage1_duration,
                    "timestamp": notification_start * 1000,
                    "data": {
                        "sql_query": formatted_sql,  # Use formatted SQL here
                        "totalResults": len(items),
                        "items": items
                    }
                }).encode("utf-8"))
                notification_time = (time.time() - notification_start) * 1000
                logger.info(f"[TIMING] Query success notification sent in {notification_time:.3f}ms")
            
            logger.info(f"[suiteql_service] Query stage completed successfully with {len(items)} records in {stage1_duration:.3f}ms")
            
        else:
            # Stage 1 Failure
            error_msg = query_result.get("message", "Query processing failed after multiple attempts")
            failed_sql = query_result.get("sql", "")  # Get the attempted SQL query
            final_result["errors"]["query"] = error_msg
            final_result["sql_query"] = failed_sql  # Store the failed SQL query

            if notificator:
                notification_start = time.time()
                notificator.notify(json.dumps({
                    "status": "ATTEMPT_FAILED",
                    "stage": "query",
                    "attempt": query_result.get("attempt", 1),
                    "message": f"Attempt {query_result.get('attempt', 1)} unsuccessful - {error_msg}",
                    "sql_query": failed_sql,
                    "timestamp": notification_start * 1000,
                    "error": error_msg
                }).encode("utf-8"))
                notification_time = (time.time() - notification_start) * 1000
                logger.info(f"[TIMING] Query failure notification sent in {notification_time:.3f}ms")

            logger.warning(f"[suiteql_service] Query stage failed in {stage1_duration:.3f}ms: {error_msg}")
            return {
                "error": error_msg,
                "sql_query": failed_sql,
                "timing": final_result["timing"]
            }
        
        # =============================================================
        # STAGE 2: SUMMARY GENERATION (summarize_results)
        # =============================================================
        stage2_start = time.time()
        final_result["timing"]["stages"]["summary_start"] = stage2_start * 1000
        
        if notificator:
            notification_start = time.time()
            notificator.notify(json.dumps({
                "status": "SUMMARY_PROCESSING_START",
                "stage": "summary",
                "message": "Generating business summary and insights...",
                "timestamp": notification_start * 1000
            }).encode("utf-8"))
            notification_time = (time.time() - notification_start) * 1000
            logger.info(f"[TIMING] Summary start notification sent in {notification_time:.3f}ms")
        
        try:
            summary = summarize_results(
                nlq,
                final_result["sql_query"],
                final_result["result"]
            )
            stage2_end = time.time()
            stage2_duration = (stage2_end - stage2_start) * 1000
            final_result["timing"]["stages"]["summary_duration"] = stage2_duration
            final_result["summary"] = summary
            
            logger.info(f"[TIMING] Summary generation completed in {stage2_duration:.3f}ms")
            
            if notificator:
                notification_start = time.time()
                notificator.notify(json.dumps({
                    "status": "SUMMARY_PROCESSING_SUCCESS",
                    "stage": "summary",
                    "message": "Business summary generated successfully",
                    "processing_time_ms": stage2_duration,
                    "timestamp": notification_start * 1000,
                    "data": {
                        "summary": summary
                    }
                }).encode("utf-8"))
                notification_time = (time.time() - notification_start) * 1000
                logger.info(f"[TIMING] Summary success notification sent in {notification_time:.3f}ms")
            
            logger.info(f"[suiteql_service] Summary stage completed successfully in {stage2_duration:.3f}ms")
            
        except Exception as e:
            # Stage 2 Failure
            stage2_end = time.time()
            stage2_duration = (stage2_end - stage2_start) * 1000
            final_result["timing"]["stages"]["summary_duration"] = stage2_duration
            
            error_msg = f"Failed to generate summary: {str(e)}"
            final_result["errors"]["summary"] = error_msg
            
            logger.warning(f"[TIMING] Summary generation failed in {stage2_duration:.3f}ms: {error_msg}")
            
            if notificator:
                notification_start = time.time()
                notificator.notify(json.dumps({
                    "status": "SUMMARY_PROCESSING_FAILED",
                    "stage": "summary",
                    "message": "Failed to generate business summary",
                    "processing_time_ms": stage2_duration,
                    "timestamp": notification_start * 1000,
                    "error": error_msg
                }).encode("utf-8"))
                notification_time = (time.time() - notification_start) * 1000
                logger.info(f"[TIMING] Summary failure notification sent in {notification_time:.3f}ms")
            
            logger.warning(f"[suiteql_service] Summary stage failed in {stage2_duration:.3f}ms: {error_msg}")
            # Continue processing - summary failure shouldn't stop the whole process
        
        # =============================================================
        # STAGE 3: CHART GENERATION (generate_chart_specs_for_suiteql)
        # =============================================================
        stage3_start = time.time()
        final_result["timing"]["stages"]["charts_start"] = stage3_start * 1000
        
        if notificator:
            notification_start = time.time()
            notificator.notify(json.dumps({
                "status": "CHART_PROCESSING_START",
                "stage": "charts",
                "message": "Generating data visualizations and charts...",
                "timestamp": notification_start * 1000
            }).encode("utf-8"))
            notification_time = (time.time() - notification_start) * 1000
            logger.info(f"[TIMING] Chart start notification sent in {notification_time:.3f}ms")
        
        try:
            # Use AI to extract axes
            x_axis, y_axis = extract_axes_from_nlq_ai(
                nlq,
                sql_query=final_result["sql_query"],
                items=final_result["result"]["items"]
            )
            chart_specs = generate_chart_specs_for_suiteql(
                result=final_result["result"],
                nlq=nlq,
                x_axis=x_axis,
                y_axis=y_axis
            )
            stage3_end = time.time()
            stage3_duration = (stage3_end - stage3_start) * 1000
            final_result["timing"]["stages"]["charts_duration"] = stage3_duration
            final_result["chart_specs"] = chart_specs
            
            logger.info(f"[TIMING] Chart generation completed in {stage3_duration:.3f}ms")
            
            if notificator:
                notification_start = time.time()
                notificator.notify(json.dumps({
                    "status": "CHART_PROCESSING_SUCCESS",
                    "stage": "charts",
                    "message": f"Generated {len(chart_specs)} chart{'s' if len(chart_specs) != 1 else ''} successfully",
                    "processing_time_ms": stage3_duration,
                    "timestamp": notification_start * 1000,
                    "data": {
                        "chart_specs": chart_specs,
                        "chart_count": len(chart_specs)
                    }
                }).encode("utf-8"))
                notification_time = (time.time() - notification_start) * 1000
                logger.info(f"[TIMING] Chart success notification sent in {notification_time:.3f}ms")
            
            logger.info(f"[suiteql_service] Chart stage completed successfully with {len(chart_specs)} charts in {stage3_duration:.3f}ms")
            
        except Exception as e:
            # Stage 3 Failure
            stage3_end = time.time()
            stage3_duration = (stage3_end - stage3_start) * 1000
            final_result["timing"]["stages"]["charts_duration"] = stage3_duration
            
            error_msg = f"Failed to generate charts: {str(e)}"
            final_result["errors"]["charts"] = error_msg
            
            logger.warning(f"[TIMING] Chart generation failed in {stage3_duration:.3f}ms: {error_msg}")
            
            if notificator:
                notification_start = time.time()
                notificator.notify(json.dumps({
                    "status": "CHART_PROCESSING_FAILED",
                    "stage": "charts",
                    "message": "Failed to generate data visualizations",
                    "processing_time_ms": stage3_duration,
                    "timestamp": notification_start * 1000,
                    "error": error_msg
                }).encode("utf-8"))
                notification_time = (time.time() - notification_start) * 1000
                logger.info(f"[TIMING] Chart failure notification sent in {notification_time:.3f}ms")
            
            logger.warning(f"[suiteql_service] Chart stage failed in {stage3_duration:.3f}ms: {error_msg}")
            # Continue processing - chart failure shouldn't stop the whole process
        
        # =============================================================
        # FINAL COMPLETION NOTIFICATION
        # =============================================================
        overall_end = time.time()
        overall_duration = (overall_end - overall_start_time) * 1000
        final_result["timing"]["overall_duration"] = overall_duration
        
        if notificator:
            completed_stages = []
            failed_stages = []
            
            if "query" not in final_result["errors"]:
                completed_stages.append("data retrieval")
            else:
                failed_stages.append("data retrieval")
                
            if "summary" not in final_result["errors"]:
                completed_stages.append("summary generation")
            else:
                failed_stages.append("summary generation")
                
            if "charts" not in final_result["errors"]:
                completed_stages.append("chart generation")
            else:
                failed_stages.append("chart generation")
            
            completion_message = f"Processing completed - {len(completed_stages)} of 3 stages successful"
            if failed_stages:
                completion_message += f" ({', '.join(failed_stages)} failed)"
            
            notification_start = time.time()
            notificator.notify(json.dumps({
                "status": "ALL_PROCESSING_COMPLETE",
                "message": completion_message,
                "total_processing_time_ms": overall_duration,
                "timestamp": notification_start * 1000,
                "completed_stages": completed_stages,
                "failed_stages": failed_stages,
                "has_errors": len(final_result["errors"]) > 0,
                "timing_breakdown": {
                    "query_ms": final_result["timing"]["stages"].get("query_duration", 0),
                    "summary_ms": final_result["timing"]["stages"].get("summary_duration", 0),
                    "charts_ms": final_result["timing"]["stages"].get("charts_duration", 0),
                    "total_ms": overall_duration
                }
            }).encode("utf-8"))
            notification_time = (time.time() - notification_start) * 1000
            logger.info(f"[TIMING] Final completion notification sent in {notification_time:.3f}ms")
        
        logger.info(f"[TIMING] Overall processing completed in {overall_duration:.3f}ms")
        return final_result
            
    except Exception as e:
        overall_end = time.time()
        overall_duration = (overall_end - overall_start_time) * 1000
        final_result["timing"]["overall_duration"] = overall_duration
        
        logger.error(f"[TIMING] Unexpected exception after {overall_duration:.3f}ms: {e}", exc_info=True)
        
        if notificator:
            notification_start = time.time()
            notificator.notify(json.dumps({
                "status": "PROCESSING_ERROR",
                "message": "An unexpected error occurred during processing",
                "processing_time_ms": overall_duration,
                "timestamp": notification_start * 1000,
                "error": str(e)
            }).encode("utf-8"))
            notification_time = (time.time() - notification_start) * 1000
            logger.info(f"[TIMING] Error notification sent in {notification_time:.3f}ms")
        
        return {"error": str(e), "timing": final_result["timing"]}
    
def save_suiteql_query_record(connection_id, nlq, generated_sql, result_data, feedback="default", user_id=None):
    logger.info(f"[SuiteQL Save] Saving NLQ record for connection_id={connection_id}, user_id={user_id}, feedback={feedback}")
    logger.debug(f"[SuiteQL Save] NLQ: {nlq}")
    logger.debug(f"[SuiteQL Save] Generated SQL: {generated_sql}")
    logger.debug(f"[SuiteQL Save] Result Data: {result_data}")

    response = table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("ConnectionId").eq(connection_id),
        ProjectionExpression="MessagePartId"
    )
    existing_ids = [item["MessagePartId"] for item in response.get("Items", [])]
    next_id = max(existing_ids, default=0) + 1

    item = {
        "ConnectionId": connection_id,
        "MessagePartId": decimal(next_id),
        "NLQ": nlq,
        "GeneratedSQL": generated_sql,
        "ResultData": result_data,
        "Feedback": feedback,
    }
    if user_id:
        item["UserId"] = user_id

    table.put_item(Item=item)
    logger.info(f"[SuiteQL Save] Record saved with MessagePartId={next_id}")

def update_suiteql_feedback(connection_id, message_part_id, feedback, feedback_comment=None):
    update_expr = "SET Feedback = :f"
    expr_attr_vals = {":f": feedback}
    if feedback_comment is not None:
        update_expr += ", FeedbackComment = :fc"
        expr_attr_vals[":fc"] = feedback_comment
    table.update_item(
        Key={
            "ConnectionId": connection_id,
            "MessagePartId": decimal(message_part_id)
        },
        UpdateExpression=update_expr,
        ExpressionAttributeValues=expr_attr_vals
    )
    logger.info(f"[SuiteQL Update] Feedback updated for ConnectionId={connection_id}, MessagePartId={message_part_id} to '{feedback}', comment='{feedback_comment}'")