import sys
import unittest

sys.path.insert(0, ".")

import time

from app.repositories.user import find_group_by_name_prefix, find_users_by_email_prefix
from tests.test_usecases.utils.user_factory import (
    create_test_user,
    delete_cognito_group,
    delete_cognito_user,
    store_group_in_cognito,
    store_user_in_cognito,
)


class TestUserRepository(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.user1 = create_test_user("test_user1")
        store_user_in_cognito(cls.user1)
        cls.user2 = create_test_user("test_user2")
        store_user_in_cognito(cls.user2)

        # Wait for reflecting the changes in the user pool
        time.sleep(3)

    @classmethod
    def tearDownClass(cls) -> None:
        delete_cognito_user(cls.user1)
        delete_cognito_user(cls.user2)

    def test_find_users_by_email_prefix(self):
        users = find_users_by_email_prefix("test_user1")
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0].email, self.user1.email)

    def test_find_users_by_email_prefix_no_users(self):
        users = find_users_by_email_prefix("non_existent_user")
        self.assertEqual(len(users), 0)

    def test_find_users_by_email_prefix_limit(self):
        users = find_users_by_email_prefix("tes", limit=1)
        self.assertEqual(len(users), 1)


class TestUserGroupRepository(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.group_name = "test_group"
        store_group_in_cognito(cls.group_name)

        # Wait for reflecting the changes in the user pool
        time.sleep(3)

    @classmethod
    def tearDownClass(cls) -> None:
        delete_cognito_group(cls.group_name)

    def test_search_group_by_group_name(self):
        groups = find_group_by_name_prefix("tes")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].name, self.group_name)


if __name__ == "__main__":
    unittest.main()
