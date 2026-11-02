import unittest
from types import SimpleNamespace
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.access import is_owner


class OwnershipTests(unittest.TestCase):
    def test_requires_matching_owner_checkpoint(self):
        user = {"email": "owner@local.test", "student": {"id": 1, "process_instance_id": "old"}}
        instance = SimpleNamespace(_id="old", variables={"student_email": user["email"]})
        self.assertFalse(is_owner(user, instance))
        instance.variables["_owner_student_id"] = 1
        self.assertFalse(is_owner(user, instance))
        instance.variables["_owner_email"] = user["email"]
        self.assertTrue(is_owner(user, instance))
        user["email"] = "other@local.test"
        self.assertFalse(is_owner(user, instance))
        user["email"] = instance.variables["_owner_email"]
        user["student"]["id"] = 2
        self.assertFalse(is_owner(user, instance))
