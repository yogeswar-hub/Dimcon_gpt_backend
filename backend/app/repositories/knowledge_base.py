import logging

from app.utils import get_bedrock_agent_client
from app.repositories.models.custom_bot_kb import (
    BedrockAgentGetKnowledgeBaseResponse,
    KnowledgeBase,
    KnowledgeBaseConfiguration,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def get_knowledge_base_info(
    knowledge_base_id: str | None,
) -> BedrockAgentGetKnowledgeBaseResponse:
    logger.info(
        f"[START] get_knowledge_base_info called with knowledge_base_id={knowledge_base_id}"
    )
    client = get_bedrock_agent_client()
    try:
        response = client.get_knowledge_base(knowledgeBaseId=knowledge_base_id)
        logger.info(f"[INFO] get_knowledge_base_info received response={response}")
        result = BedrockAgentGetKnowledgeBaseResponse(
            knowledge_base=KnowledgeBase(
                knowledge_base_configuration=KnowledgeBaseConfiguration(
                    type=response.get("knowledgeBase", {})
                    .get("knowledgeBaseConfiguration", {})
                    .get("type", "VECTOR")
                )
            )
        )
        logger.info(f"[END] get_knowledge_base_info returning {result}")
        return result
    except Exception as e:
        logger.error(f"[ERROR] get_knowledge_base_info failed: {e}")
        result = BedrockAgentGetKnowledgeBaseResponse(
            knowledge_base=KnowledgeBase(
                knowledge_base_configuration=KnowledgeBaseConfiguration(type="VECTOR")
            )
        )
        logger.info(f"[END] get_knowledge_base_info returning fallback {result}")
        return result
