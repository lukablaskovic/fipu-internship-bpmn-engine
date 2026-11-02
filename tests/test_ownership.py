import unittest
from types import SimpleNamespace
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.access import is_owner


class OwnershipTests(unittest.TestCase):
    def test_only_trusted_successors_grant_access_to_legacy_history(self):
        user = {"email": "owner@local.test", "student": {"id": 1, "process_instance_id": "new"}}
        owner = {"_owner_student_id": 1, "_owner_email": user["email"]}
        def instance(iid, **state):
            return SimpleNamespace(_id=iid, variables=state)
        new = instance("new", previous_instance_id="old", **owner)
        old = instance("old", previous_instance_id="foreign")
        foreign = instance("foreign", student_email=user["email"])
        model = SimpleNamespace(instances={i._id: i for i in (new, old, foreign)})
        lookup = lambda iid: model if iid in model.instances else None
        self.assertTrue(is_owner(user, new, lookup))
        self.assertTrue(is_owner(user, old, lookup))
        self.assertFalse(is_owner(user, foreign, lookup))
        user["student"]["id"] = 2
        self.assertFalse(is_owner(user, new, lookup))
        self.assertFalse(is_owner(user, old, lookup))
