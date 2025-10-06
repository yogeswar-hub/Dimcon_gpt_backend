import json
import logging
import os
from datetime import datetime, date
from decimal import Decimal as decimal
from queue import SimpleQueue
from threading import Thread
from typing import BinaryIO, Literal, TypedDict

import boto3
from app.agents.tools.agent_tool import ToolRunResult
from app.auth import verify_token
from app.charting import (
    USE_AI_FOR_CHARTS,
    generate_chart_spec,
    suiteql_items_to_text_for_chart,
    wrap_chartjs_message,
)
from app.repositories.conversation import RecordNotFoundError
from app.routes.schemas.conversation import ChatInput
from app.stream import OnStopInput, OnThinking
from app.usecases.chat import chat
from app.user import User
from app.suiteql_service import process_nl2sql_query
from boto3.dynamodb.conditions import Attr, Key

def _json_default(obj):
    # Safe JSON serialization for common SuiteQL types
    if isinstance(obj, decimal):
        return float(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    return str(obj)

WEBSOCKET_SESSION_TABLE_NAME = os.environ["WEBSOCKET_SESSION_TABLE_NAME"]

dynamodb_client = boto3.resource("dynamodb")
table = dynamodb_client.Table(WEBSOCKET_SESSION_TABLE_NAME)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class _NotifyCommand(TypedDict):
    type: Literal["notify"]
    payload: bytes | BinaryIO


class _FinishCommand(TypedDict):
    type: Literal["finish"]


_Command = _NotifyCommand | _FinishCommand


class NotificationSender:
    def __init__(self, endpoint_url: str, connection_id: str) -> None:
        self.commands = SimpleQueue[_Command]()
        self.endpoint_url = endpoint_url
        self.connection_id = connection_id

    def run(self):
        gatewayapi = boto3.client(
            "apigatewaymanagementapi",
            endpoint_url=self.endpoint_url,
        )

        while True:
            command = self.commands.get()
            if command["type"] == "notify":
                try:
                    gatewayapi.post_to_connection(
                        ConnectionId=self.connection_id,
                        Data=command["payload"],
                    )
                except (
                    gatewayapi.exceptions.GoneException,
                    gatewayapi.exceptions.ForbiddenException,
                ) as e:
                    logger.exception(
                        f"Shutdown the notification sender due to an exception: {e}"
                    )
                    break
                except Exception as e:
                    logger.exception(f"Failed to send notification: {e}")

            elif command["type"] == "finish":
                break

    def finish(self):
        self.commands.put({"type": "finish"})

    def notify(self, payload: bytes | BinaryIO):
        self.commands.put({"type": "notify", "payload": payload})

    def on_stream(self, token: str):
        payload = json.dumps(
            dict(status="STREAMING", completion=token),
            default=_json_default,
        ).encode("utf-8")
        self.notify(payload=payload)

    def on_stop(self, arg: OnStopInput):
        payload = json.dumps(
            dict(
                status="STREAMING_END",
                completion="",
                stop_reason=arg["stop_reason"],
                token_count=dict(
                    input=arg["input_token_count"],
                    output=arg["output_token_count"],
                    cache_read_input=arg["cache_read_input_count"],
                    cache_write_input=arg["cache_write_input_count"],
                ),
                price=arg["price"],
            ),
            default=_json_default,
        ).encode("utf-8")
        self.notify(payload=payload)

    def on_agent_thinking(self, tool_use: OnThinking):
        payload = json.dumps(
            dict(
                status="AGENT_THINKING",
                log={tool_use["tool_use_id"]: {"name": tool_use["name"], "input": tool_use["input"]}},
            ),
            default=_json_default,
        ).encode("utf-8")
        self.notify(payload=payload)

    def on_agent_tool_result(self, run_result: ToolRunResult):
        self.notify(
            payload=json.dumps(
                dict(
                    status="AGENT_TOOL_RESULT",
                    result={"toolUseId": run_result["tool_use_id"], "status": run_result["status"]},
                ),
                default=_json_default,
            ).encode("utf-8")
        )

        for related_document in run_result["related_documents"]:
            self.notify(
                payload=json.dumps(
                    dict(
                        status="AGENT_RELATED_DOCUMENT",
                        result={
                            "toolUseId": run_result["tool_use_id"],
                            "relatedDocument": related_document.to_schema().model_dump(by_alias=True),
                        },
                    ),
                    default=_json_default,
                ).encode("utf-8")
            )

    def on_reasoning(self, token: str):
        payload = json.dumps(
            dict(status="REASONING", completion=token),
            default=_json_default,
        ).encode("utf-8")
        self.notify(payload=payload)


def process_chat_input(
    user: User,
    chat_input: ChatInput,
    notificator: NotificationSender,
) -> dict:
    """Process chat input and send the message to the client."""
    logger.info(f"Received chat input: {chat_input}")

    try:
        chat(
            user=user,
            chat_input=chat_input,
            on_stream=lambda token: notificator.on_stream(token=token),
            on_stop=lambda arg: notificator.on_stop(arg=arg),
            on_thinking=lambda tool_use: notificator.on_agent_thinking(tool_use=tool_use),
            on_tool_result=lambda run_result: notificator.on_agent_tool_result(run_result=run_result),
            on_reasoning=lambda token: notificator.on_reasoning(token=token),
        )

        return {"statusCode": 200, "body": "Message sent."}

    except RecordNotFoundError:
        if chat_input.bot_id:
            return {
                "statusCode": 404,
                "body": json.dumps(dict(status="ERROR", reason=f"bot {chat_input.bot_id} not found.")),
            }
        else:
            return {
                "statusCode": 400,
                "body": json.dumps(dict(status="ERROR", reason="Invalid request.")),
            }

    except Exception as e:
        logger.exception(f"Failed to run stream handler: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps(dict(status="ERROR", reason=f"Failed to run stream handler: {e}")),
        }


def handler(event, context):
    logger.info(f"Received event: {event}")
    route_key = event["requestContext"]["routeKey"]

    if route_key == "$connect":
        return {"statusCode": 200, "body": "Connected."}
    elif route_key == "$disconnect":
        return {"statusCode": 200, "body": "Disconnected."}

    connection_id = event["requestContext"]["connectionId"]
    domain_name = event["requestContext"]["domainName"]
    stage = event["requestContext"]["stage"]
    endpoint_url = f"https://{domain_name}/{stage}"
    notificator = NotificationSender(endpoint_url=endpoint_url, connection_id=connection_id)

    now = datetime.now()
    expire = int(now.timestamp()) + 60 * 2
    body = json.loads(event["body"])
    step = body.get("step")
    token = body.get("token")

    notification_thread = Thread(target=lambda: notificator.run(), daemon=True)
    notification_thread.start()

    # Give thread time to initialize
    import time
    time.sleep(0.1)

    try:
        if step == "SUITEQL":
            nlq = body.get("nlq")
            logger.info(f"SuiteQL route called with NLQ: {nlq}")

            # Store connectionId in DynamoDB for this SuiteQL session
            table.put_item(
                Item={
                    "ConnectionId": connection_id,
                    "MessagePartId": decimal(0),
                    "Step": "SUITEQL",
                    "NLQ": nlq,
                    "expire": expire,
                }
            )
            logger.info(f"Stored connectionId {connection_id} for SUITEQL step.")

            notificator.notify(json.dumps({
                "status": "PROCESSING",
                "message": "Processing SuiteQL query..."
            }).encode("utf-8"))

            result = process_nl2sql_query(nlq)
            logger.info(
                "SuiteQL detailed result: %s",
                json.dumps(result, indent=2, default=_json_default),
            )

            if "error" in result:
                error_payload = {
                    "status": "ERROR",
                    "nlq": nlq,
                    "error": result["error"],
                    "message": "SuiteQL query failed.",
                }
                notificator.notify(json.dumps(error_payload, default=_json_default).encode("utf-8"))
                logger.warning(f"SuiteQL error returned to client: {result['error']}")
                return {"statusCode": 500, "body": json.dumps(error_payload)}

            items = result.get("result", {}).get("items", []) or []
            text_for_chart = suiteql_items_to_text_for_chart(items)

            # Detect requested chart type from NLQ
            requested_type = None
            for chart_type in [
                "bar", "line", "pie", "doughnut", "radar", "area", "scatter", "bubble",
                "radialBar", "matrix", "treemap", "sunburst", "candlestick", "ohlc", "wordcloud"
            ]:
                if chart_type in (nlq or "").lower():
                    requested_type = chart_type
                    break

            chart_specs = []
            used_types = set()

            if items and text_for_chart and len(text_for_chart.strip()) > 0:
                # First chart: user requested type (if any)
                if requested_type:
                    try:
                        chart_spec = generate_chart_spec(text_for_chart, preferred_type=requested_type)
                        if chart_spec and chart_spec.get("type"):
                            chart_specs.append(chart_spec)
                            used_types.add(chart_spec["type"])
                    except Exception as e:
                        logger.warning(f"Chart generation failed for requested type {requested_type}: {e}")

                # Next two charts: AI picks best types, but avoid duplicates
                ai_types = [t for t in [
                    "bar", "line", "pie", "doughnut", "radar", "area", "scatter", "bubble",
                    "radialBar", "matrix", "treemap", "sunburst", "candlestick", "ohlc", "wordcloud"
                ] if t not in used_types]
                for _ in range(3 - len(chart_specs)):
                    for ai_type in ai_types:
                        try:
                            chart_spec = generate_chart_spec(text_for_chart, preferred_type=ai_type)
                            if chart_spec and chart_spec.get("type") and chart_spec["type"] not in used_types:
                                chart_specs.append(chart_spec)
                                used_types.add(chart_spec["type"])
                                break
                        except Exception as e:
                            logger.warning(f"Chart generation failed for AI type {ai_type}: {e}")

                # Pad with None if less than 3 charts
                while len(chart_specs) < 3:
                    chart_specs.append(None)
            else:
                chart_specs = []  # No charts if no informative data

            notification_payload = {
                "status": "SUITEQL_RESULT",
                "nlq": nlq,
                "result": {
                    "sql_query": result.get("sql_query"),
                    "items": items,
                    "totalResults": len(items),
                },
                "summary": result.get("summary"),
                "message": f"SuiteQL query processed. Showing all {len(items)} records.",
                "chart_specs": chart_specs
            }

            notificator.notify(json.dumps(notification_payload, default=_json_default).encode("utf-8"))
            logger.info("SuiteQL result notification sent.")
            return {"statusCode": 200, "body": "SuiteQL processed."}

        if step == "START":
            try:
                decoded = verify_token(token)
            except Exception as e:
                logger.exception(f"Invalid token: {e}")
                notificator.notify(
                    json.dumps({"status": "ERROR", "reason": "Invalid token."}).encode("utf-8")
                )
                return {
                    "statusCode": 403,
                    "body": json.dumps(dict(status="ERROR", reason="Invalid token.")),
                }

            user_id = decoded["sub"]
            table.put_item(
                Item={
                    "ConnectionId": connection_id,
                    "MessagePartId": decimal(0),
                    "UserId": user_id,
                    "expire": expire,
                }
            )
            notificator.notify(json.dumps({"status": "SESSION_STARTED", "message": "Session started."}).encode("utf-8"))
            logger.info("Session started notification sent.")
            return {"statusCode": 200, "body": "Session started."}

        elif step == "END":
            decoded = verify_token(token)
            user = User.from_decoded_token(decoded)
            response = table.query(
                KeyConditionExpression=Key("ConnectionId").eq(connection_id),
                FilterExpression=Attr("UserId").exists(),
            )
            user_id = response["Items"][0]["UserId"]
            message_parts = []
            last_evaluated_key = None

            while True:
                if last_evaluated_key:
                    response = table.query(
                        KeyConditionExpression=Key("ConnectionId").eq(connection_id)
                        & Key("MessagePartId").gte(1),
                        ExclusiveStartKey=last_evaluated_key,
                    )
                else:
                    response = table.query(
                        KeyConditionExpression=Key("ConnectionId").eq(connection_id)
                        & Key("MessagePartId").gte(1),
                    )

                message_parts.extend(response["Items"])
                if "LastEvaluatedKey" in response:
                    last_evaluated_key = response["LastEvaluatedKey"]
                else:
                    break

            logger.info(f"Number of message chunks: {len(message_parts)}")
            message_parts.sort(key=lambda x: x["MessagePartId"])
            full_message = "".join(item["MessagePart"] for item in message_parts)
            chat_input = ChatInput(**json.loads(full_message))
            result = process_chat_input(user=user, chat_input=chat_input, notificator=notificator)
            notificator.notify(json.dumps({"status": "MESSAGE_PROCESSED", "message": "Message processed."}).encode("utf-8"))
            logger.info("Message processed notification sent.")
            return result

        else:
            part_index = body["index"] + 1
            message_part = body["part"]
            table.put_item(
                Item={
                    "ConnectionId": connection_id,
                    "MessagePartId": decimal(part_index),
                    "MessagePart": message_part,
                    "expire": expire,
                }
            )
            notificator.notify(
                json.dumps({"status": "PART_RECEIVED", "message": f"Message part {part_index} received."}).encode("utf-8")
            )
            logger.info(f"Message part {part_index} notification sent.")
            return {"statusCode": 200, "body": "Message part received."}

    except Exception as e:
        logger.exception(f"Operation failed: {e}")
        notificator.notify(json.dumps({"status": "ERROR", "reason": str(e)}).encode("utf-8"))
        import time
        time.sleep(0.2)
        return {
            "statusCode": 500,
            "body": json.dumps({"status": "ERROR", "reason": str(e)}),
        }

    finally:
        import time
        time.sleep(0.3)
        notificator.finish()
        notification_thread.join(timeout=60)
