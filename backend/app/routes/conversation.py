from app.repositories.conversation import (
    change_conversation_title,
    delete_conversation_by_id,
    delete_conversation_by_user_id,
    find_conversation_by_user_id,
    find_related_document_by_id,
    find_related_documents_by_conversation_id,
    update_feedback,
)
from app.repositories.models.conversation import FeedbackModel
from app.routes.schemas.conversation import (
    ChatInput,
    ChatOutput,
    Conversation,
    ConversationMetaOutput,
    ConversationSearchResult,
    FeedbackInput,
    FeedbackOutput,
    NewTitleInput,
    ProposedTitle,
    RelatedDocument,
)
from app.usecases.chat import (
    chat,
    chat_output_from_message,
    fetch_conversation,
    propose_conversation_title,
    search_conversations as search_conversations_usecase,
)
from app.user import User
from fastapi import APIRouter, Request
import logging

logger = logging.getLogger(__name__)

router = APIRouter(tags=["conversation"])


@router.get("/health")
def health():
    logger.info(f"[START] health called")
    result = {"status": "ok"}
    logger.info(f"[END] health returning {result}")
    return result


@router.post("/conversation", response_model=ChatOutput)
def post_message(request: Request, chat_input: ChatInput):
    logger.info(f"[START] post_message called with chat_input={chat_input}")
    current_user: User = request.state.current_user

    conversation, message = chat(user=current_user, chat_input=chat_input)
    output = chat_output_from_message(conversation=conversation, message=message)
    logger.info(f"[END] post_message returning output={output}")
    return output


@router.get(
    "/conversation/{conversation_id}/related-documents",
    response_model=list[RelatedDocument],
)
def get_related_documents(
    request: Request, conversation_id: str
) -> list[RelatedDocument]:
    logger.info(
        f"[START] get_related_documents called with conversation_id={conversation_id}"
    )
    current_user: User = request.state.current_user

    related_documents = find_related_documents_by_conversation_id(
        user_id=current_user.id,
        conversation_id=conversation_id,
    )
    result = [related_document.to_schema() for related_document in related_documents]
    logger.info(f"[END] get_related_documents returning {result}")
    return result


@router.get(
    "/conversation/{conversation_id}/related-documents/{source_id}",
    response_model=RelatedDocument,
)
def get_related_document(
    request: Request, conversation_id: str, source_id: str
) -> RelatedDocument:
    logger.info(
        f"[START] get_related_document called with conversation_id={conversation_id}, source_id={source_id}"
    )
    current_user: User = request.state.current_user

    related_document = find_related_document_by_id(
        user_id=current_user.id,
        conversation_id=conversation_id,
        source_id=source_id,
    )
    result = related_document.to_schema()
    logger.info(f"[END] get_related_document returning {result}")
    return result


@router.get("/conversation/{conversation_id}", response_model=Conversation)
def get_conversation(request: Request, conversation_id: str):
    logger.info(f"[START] get_conversation called with conversation_id={conversation_id}")
    current_user: User = request.state.current_user

    output = fetch_conversation(current_user.id, conversation_id)
    logger.info(f"[END] get_conversation returning {output}")
    return output


@router.delete("/conversation/{conversation_id}")
def remove_conversation(request: Request, conversation_id: str):
    logger.info(f"[START] remove_conversation called with conversation_id={conversation_id}")
    current_user: User = request.state.current_user

    delete_conversation_by_id(current_user.id, conversation_id)
    logger.info(f"[END] remove_conversation completed")


@router.get("/conversations", response_model=list[ConversationMetaOutput])
def get_all_conversations(request: Request):
    logger.info(f"[START] get_all_conversations called")
    current_user: User = request.state.current_user

    conversations = find_conversation_by_user_id(current_user.id)
    output = [
        ConversationMetaOutput(
            id=conversation.id,
            title=conversation.title,
            create_time=conversation.create_time,
            model=conversation.model,
            bot_id=conversation.bot_id,
        )
        for conversation in conversations
    ]
    logger.info(f"[END] get_all_conversations returning {output}")
    return output


@router.delete("/conversations")
def remove_all_conversations(request: Request):
    logger.info(f"[START] remove_all_conversations called")
    delete_conversation_by_user_id(request.state.current_user.id)
    logger.info(f"[END] remove_all_conversations completed")


@router.get("/conversations/search", response_model=list[ConversationSearchResult])
def search_conversations(request: Request, query: str):
    logger.info(f"[START] search_conversations called with query={query}")
    current_user: User = request.state.current_user
    output = search_conversations_usecase(query, current_user)
    logger.info(f"[END] search_conversations returning {output}")
    return output


@router.patch("/conversation/{conversation_id}/title")
def patch_conversation_title(
    request: Request, conversation_id: str, new_title_input: NewTitleInput
):
    logger.info(
        f"[START] patch_conversation_title called with conversation_id={conversation_id}, new_title_input={new_title_input}"
    )
    current_user: User = request.state.current_user

    change_conversation_title(
        current_user.id, conversation_id, new_title_input.new_title
    )
    logger.info(f"[END] patch_conversation_title completed")


@router.get(
    "/conversation/{conversation_id}/proposed-title", response_model=ProposedTitle
)
def get_proposed_title(request: Request, conversation_id: str):
    logger.info(f"[START] get_proposed_title called with conversation_id={conversation_id}")
    current_user: User = request.state.current_user

    title = propose_conversation_title(current_user.id, conversation_id)
    result = ProposedTitle(title=title)
    logger.info(f"[END] get_proposed_title returning {result}")
    return result


@router.put(
    "/conversation/{conversation_id}/{message_id}/feedback",
    response_model=FeedbackOutput,
)
def put_feedback(request: Request, conversation_id: str, message_id: str, feedback_input: FeedbackInput):
    logger.info(f"[START] put_feedback called with conversation_id={conversation_id}, message_id={message_id}, feedback_input={feedback_input}")
    current_user: User = request.state.current_user

    update_feedback(
        user_id=current_user.id,
        conversation_id=conversation_id,
        message_id=message_id,
        feedback=FeedbackModel(
            thumbs_up=feedback_input.thumbs_up,
            category=feedback_input.category if feedback_input.category else "",
            comment=feedback_input.comment if feedback_input.comment else "",
        ),
    )
    result = FeedbackOutput(
        thumbs_up=feedback_input.thumbs_up,
        category=feedback_input.category if feedback_input.category else "",
        comment=feedback_input.comment if feedback_input.comment else "",
    )
    logger.info(f"[END] put_feedback returning {result}")
    return result
