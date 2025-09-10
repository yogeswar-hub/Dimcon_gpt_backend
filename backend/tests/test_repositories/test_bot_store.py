import sys
import unittest

sys.path.insert(0, ".")


import logging
import time

from app.repositories.bot_store import (
    find_bots_by_query,
    find_bots_sorted_by_usage_count,
    find_random_bots,
    get_opensearch_client,
)
from app.repositories.models.custom_bot import BotMeta
from app.user import User
from opensearchpy import NotFoundError, OpenSearch
from tests.test_repositories.utils.bot_factory import (
    create_test_partial_shared_bot,
    create_test_pinned_public_bot,
    create_test_private_bot,
    create_test_public_bot,
)


class TestFindBotsWithOpenSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.basicConfig(level=logging.DEBUG)
        cls.logger = logging.getLogger("app")
        cls.logger.setLevel(logging.DEBUG)

        cls.client = get_opensearch_client()
        cls.index_name = "bot-test"  # Index name for testing

        cls.test_user1 = User(
            id="test-user1",
            name="test-user1",
            groups=["group1"],
            email="test-user1@example.com",
        )
        cls.test_user2 = User(
            id="test-user2",
            name="test-user2",
            groups=["group2"],
            email="test-user2@example.com",
        )
        cls.test_admin = User(
            id="admin-user",
            name="admin-user",
            groups=["Admin"],
            email="admin@example.com",
        )

        cls.test_bots = [
            create_test_private_bot(
                id="bot-user-1-private",
                is_starred=False,
                owner_user_id=cls.test_user1.id,
                usage_count=3,
            ),
            create_test_private_bot(
                id="bot-user-2-private",
                is_starred=False,
                owner_user_id=cls.test_user2.id,
                usage_count=2,
            ),
            create_test_private_bot(
                id="bot-admin-private",
                is_starred=False,
                owner_user_id=cls.test_admin.id,
                usage_count=5,
            ),
            create_test_partial_shared_bot(
                id="bot-user-1-shared-to-user-2",
                is_starred=False,
                owner_user_id=cls.test_user1.id,
                allowed_cognito_users=[cls.test_user2.id],
                usage_count=10,
            ),
            create_test_partial_shared_bot(
                id="bot-user-2-shared-to-group-1",
                is_starred=False,
                owner_user_id=cls.test_user2.id,
                allowed_cognito_groups=["group1"],
                usage_count=11,
            ),
            create_test_public_bot(
                id="bot-user1-public",
                is_starred=True,
                owner_user_id=cls.test_user1.id,
                usage_count=100,
            ),
            create_test_pinned_public_bot(
                id="bot-user1-pinned-public",
                is_starred=True,
                owner_user_id=cls.test_user1.id,
                usage_count=1000,
            ),
        ]

        # Create index and index documents
        try:
            if not cls.client.indices.exists(index=cls.index_name):
                cls.client.indices.create(index=cls.index_name)
        except NotFoundError:
            cls.client.indices.create(index=cls.index_name)

        for bot in cls.test_bots:
            logging.debug(f"Indexing bot {bot.id}")
            body = {
                "BotId": bot.id,
                "Title": bot.title,
                "Description": bot.description,
                "CreateTime": bot.create_time,
                "LastUsedTime": bot.last_used_time,
                "SharedStatus": bot.shared_status,
                "PK": bot.owner_user_id,
                "SK": f"BOT#{bot.id}",
                "AllowedCognitoUsers": bot.allowed_cognito_users,
                "AllowedCognitoGroups": bot.allowed_cognito_groups,
                "SyncStatus": bot.sync_status,
                "BedrockKnowledgeBase": bot.bedrock_knowledge_base,
                "UsageStats": bot.usage_stats.model_dump(),
            }

            if bot.shared_scope != "private":
                body["SharedScope"] = bot.shared_scope

            cls.client.index(
                index=cls.index_name,
                body=body,
            )

        # Refresh index not working, so sleep for a while
        # Ref: https://github.com/opensearch-project/opensearch-py/issues/646
        # cls.client.indices.refresh(index=cls.index_name)

        logging.info("Waiting for indexing...")
        indexed = False
        while not indexed:
            try:
                response = cls.client.search(
                    index=cls.index_name, body={"query": {"match_all": {}}}
                )
            except NotFoundError:
                logging.info("Index not found. Waiting for indexing...")
                time.sleep(1)
                continue
            if response["hits"]["total"]["value"] == len(cls.test_bots):
                indexed = True
                logging.debug("Indexed Items:")
                for hit in response["hits"]["hits"]:
                    logging.debug(hit)
            else:
                logging.info("Waiting for indexing...")
                time.sleep(1)
        logging.info("Indexing done.")

    @classmethod
    def tearDownClass(cls):
        # Delete index after testing
        try:
            cls.client.indices.delete(index=cls.index_name)
        except NotFoundError:
            print(f"Index {cls.index_name} not found. Skipping deletion.")

    def test_find_bots_by_query_user1(self):
        result = find_bots_by_query(
            query="bot", user=self.test_user1, index_name=self.index_name
        )

        expected_bot_ids = {
            "bot-user-1-private",
            "bot-user-1-shared-to-user-2",  # Bot owner is user1
            "bot-user-2-shared-to-group-1",  # Bot owner is user2, but shared to group1
            "bot-user1-public",
            "bot-user1-pinned-public",
        }
        result_bot_ids = {bot.id for bot in result}

        logging.debug(f"Expected bot IDs: {expected_bot_ids}")
        logging.debug(f"Result bot IDs: {result_bot_ids}")

        self.assertEqual(result_bot_ids, expected_bot_ids)

    def test_find_bots_by_query_user2(self):
        result = find_bots_by_query(
            query="bot", user=self.test_user2, index_name=self.index_name
        )

        expected_bot_ids = {
            "bot-user-2-private",
            "bot-user-1-shared-to-user-2",  # Bot owner is user1, but shared to user2
            "bot-user-2-shared-to-group-1",  # Bot owner is user2
            "bot-user1-public",
            "bot-user1-pinned-public",
        }
        result_bot_ids = {bot.id for bot in result}

        logging.debug(f"Expected bot IDs: {expected_bot_ids}")
        logging.debug(f"Result bot IDs: {result_bot_ids}")

        self.assertEqual(result_bot_ids, expected_bot_ids)

    def test_find_bots_by_query_admin(self):
        result = find_bots_by_query(
            query="bot", user=self.test_admin, index_name=self.index_name
        )

        expected_bot_ids = {
            "bot-user-1-shared-to-user-2",  # Not shared to admin, but admin can see
            "bot-user-2-shared-to-group-1",  # Not shared to admin, but admin can see
            "bot-user1-public",
            "bot-admin-private",
            "bot-user1-pinned-public",
        }
        result_bot_ids = {bot.id for bot in result}

        logging.debug(f"Expected bot IDs: {expected_bot_ids}")
        logging.debug(f"Result bot IDs: {result_bot_ids}")

        self.assertEqual(result_bot_ids, expected_bot_ids)

    def test_find_bots_sorted_by_usage_count(self):
        result = find_bots_sorted_by_usage_count(
            user=self.test_user1, index_name=self.index_name
        )

        expected_bot_ids = [
            "bot-user1-pinned-public",  # 1000
            "bot-user1-public",  # 100
            "bot-user-2-shared-to-group-1",  # 11
            "bot-user-1-shared-to-user-2",  # 10
            "bot-user-1-private",  # 3
        ]
        result_bot_ids = [bot.id for bot in result]

        logging.debug(f"Expected bot IDs: {expected_bot_ids}")
        logging.debug(f"Result bot IDs: {result_bot_ids}")

        # Assert bot IDs are in the same order
        self.assertListEqual(result_bot_ids, expected_bot_ids)

    def test_find_random_bots(self):
        result = find_random_bots(user=self.test_user1, index_name=self.index_name)

        result_bot_ids = {bot.id for bot in result}

        logging.debug(f"Result bot IDs: {result_bot_ids}")

        # Assert array length is 5
        self.assertEqual(len(result), 5)


if __name__ == "__main__":
    unittest.main()
