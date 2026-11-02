"""Real HTTP/subprocess restart tests; only temporary SQLite and synthetic data.

Run: python -m unittest discover -s tests -v
"""
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def serve():
    # Never load a developer's .env or contact real connectors.
    from types import ModuleType
    env = ModuleType("env")
    env.DB = {"provider": "sqlite"}
    env.DS = {
        name: {"type": "http-connector", "url": "http://127.0.0.1:1"}
        for name in ("baserow", "emailjs", "pdf")
    }
    env.SYSTEM_VARS = {}
    sys.modules["env"] = env
    sys.path.insert(0, str(ROOT))
    import asyncio
    import server
    async def coordinator(request, path):
        if request.headers.get("X-Test-Role") == "other_student":
            return {"coordinator": False, "email": "other@local.test", "student": {"id": 2}}
        student = {"id": 1}
        if request.match_info.get("task_id") == "predavanje_dnevnika_student":
            student["process_instance_id"] = request.match_info["instance_id"]
        creating = request.method == "POST" and request.path.startswith("/model/")
        is_student = creating or request.headers.get("X-Test-Role") == "student"
        return {"coordinator": not is_student, "email": "test@local.test", "student": student}
    server.access.connector_read = coordinator
    os.environ["WORKFLOW_SERVICE_TOKEN"] = "x" * 32
    from bpmn_model import UserFormMessage, ServiceTask
    from aiohttp import web

    if Path("stub_connectors").exists():
        async def run_service(self, variables, instance_id):
            if Path("fail_step").exists() and Path("fail_step").read_text() == self._id:
                raise RuntimeError("Sensitive connector response must stay private")
            if self._id == "evidentiranje_odustajanja":
                if Path("fail_withdrawal").exists():
                    raise RuntimeError("Synthetic connector failure")
                variables["withdrawal_saved"] = True
                return True
            variables["connector_description"] = variables.get("detaljan_opis_zadatka")
            return True
        ServiceTask.run = run_service

    original_put = asyncio.Queue.put_nowait

    def put(self, item):
        # Drop only the volatile wakeup to force the pre-processing crash window.
        if isinstance(item, UserFormMessage) and Path("pause_delivery").exists():
            return
        original_put(self, item)

    asyncio.Queue.put_nowait = put
    web.run_app(server.run(), host="127.0.0.1", port=int(sys.argv[2]))


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        (self.directory / "models").mkdir()
        shutil.copy(ROOT / "tests/fixtures/correction.bpmn", self.directory / "models")
        self.database = self.directory / "database/database.sqlite"
        self.process = None
        self.addCleanup(self.stop)
        self.start()

    def start(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        output = open(self.directory / "engine.log", "w")
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--serve", str(port)],
            cwd=self.directory, stdout=output, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        output.close()
        for _ in range(200):
            if self.process.poll() is not None:
                self.fail((self.directory / "engine.log").read_text())
            try:
                if self.request("GET", "/model")[0] == 200:
                    return
            except (URLError, ConnectionError):
                pass
            time.sleep(0.025)
        self.fail("Engine did not start")

    def stop(self):
        if self.process is not None:
            self.process.kill()  # No graceful shutdown or queue draining.
            self.process.wait(timeout=5)
            self.process = None

    def restart(self):
        self.stop()
        self.start()

    def request(self, method, path, body=None, role="coordinator"):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(self.url + path, data=data, method=method,
                      headers={"Content-Type": "application/json", "X-Test-Role": role})
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, error.read().decode()

    def sql(self, statement, parameters=()):
        with sqlite3.connect(self.database) as connection:
            return connection.execute(statement, parameters).fetchall()

    def create(self):
        status, body = self.request("POST", "/model/correction.bpmn/instance")
        self.assertEqual(status, 200)
        self.instance_id = body["id"]
        self.wait_for("application")

    def submit(self, task, data, status=200):
        actual, body = self.request(
            "POST", f"/instance/{self.instance_id}/task/{task}/form", data)
        self.assertEqual(actual, status, body)
        if status == 200:
            self.assertEqual(body, {"status": "OK"})

    def wait_for(self, task, state="running"):
        for _ in range(200):
            status, body = self.request("GET", f"/instance/{self.instance_id}")
            if status == 200 and body["pending"] == [task] and body["state"] == state:
                return body
            time.sleep(0.01)
        self.fail(f"Did not reach {task}/{state}: {body}")

    def test_instance_keeps_definition_across_save_and_restart(self):
        self.create()
        first = self.instance_id
        path = self.directory / "models/correction.bpmn"
        path.write_text(path.read_text().replace("<bpmn:definitions ", '<bpmn:definitions processVersion="2" ', 1))
        self.create()
        self.restart()
        self.assertEqual(self.wait_for("application")["model"]["version"], 2)
        self.instance_id = first
        self.assertEqual(self.wait_for("application")["model"]["version"], 1)
        with urlopen(self.url + f"/instance/{first}/model") as response:
            self.assertNotIn(b'processVersion="2"', response.read())
        self.assertEqual(self.request("GET", "/engine")[1]["deployments"][0]["active"], 2)
        self.sql("DELETE FROM InstanceDefinition WHERE instance_id = ?", (first,))
        self.restart()
        self.assertIsNone(self.wait_for("application")["model"]["version"])

    def prepare_correction(self):
        self.create()
        self.submit("application", {
            "description": "original", "details": {"hours": 80}, "confirmed": True,
        })
        self.wait_for("review")
        self.submit("review", {"approved": "no"})
        self.wait_for("application")

    def assert_corrected(self):
        body = self.wait_for("review")
        self.assertEqual(body["variables"]["description"], "corrected")
        self.assertEqual(body["variables"]["details"], {"hours": 0, "note": None})
        self.assertIs(body["variables"]["confirmed"], False)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM PendingSubmission'), [(0,)])

    def correction(self):
        self.submit("application", {
            "description": "corrected", "details": {"hours": 0, "note": None},
            "confirmed": False,
        })

    def test_diagram_history_follows_recorded_visits_through_a_return_and_restart(self):
        self.prepare_correction()  # review rejected the application, which is now pending again.
        path = f"/instance/{self.instance_id}"
        for role in ("student", "coordinator"):
            status, body = self.request("GET", path, role=role)
            self.assertEqual(status, 200)
            self.assertEqual(body["completed_activity_ids"], ["application", "decision", "review", "start"])
            self.assertEqual(body["pending"], ["application"])
        self.assertEqual(self.request("GET", path, role="other_student")[0], 404)
        self.create()
        self.assertEqual(self.wait_for("application")["completed_activity_ids"], ["start"])
        self.restart()
        self.assertEqual(self.request("GET", path, role="student")[1]["completed_activity_ids"],
                         ["application", "decision", "review", "start"])

    def test_corrected_values_and_exact_waiting_task_survive_restart(self):
        self.prepare_correction()
        self.correction()
        self.assert_corrected()
        self.restart()
        self.assert_corrected()
        # A replayed correction must also drive the subsequent gateway correctly.
        self.submit("review", {"approved": "yes"})
        self.wait_for("diary")
        self.restart()
        body = self.wait_for("diary")
        self.assertEqual(body["variables"]["approved"], "yes")

    def test_restart_immediately_after_ack_before_queue_processing(self):
        self.prepare_correction()
        marker = self.directory / "pause_delivery"
        marker.touch()
        self.correction()
        self.stop()  # First action after the HTTP acknowledgement is SIGKILL.
        self.assertEqual(self.sql('SELECT COUNT(*) FROM PendingSubmission'), [(1,)])
        self.assertEqual(self.sql(
            "SELECT COUNT(*) FROM Event WHERE activity_id = 'application'"), [(1,)])
        marker.unlink()
        self.start()
        self.assert_corrected()
        self.restart()
        self.assert_corrected()
        self.assertEqual(self.sql(
            "SELECT COUNT(*) FROM Event WHERE activity_id = 'application'"), [(2,)])

    def test_failed_inbox_write_never_acknowledges_success(self):
        self.create()
        self.sql("""CREATE TRIGGER fail_inbox BEFORE INSERT ON PendingSubmission
                    BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END""")
        self.submit("application", {"description": "unsaved"}, status=503)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM PendingSubmission'), [(0,)])
        self.restart()
        self.assertNotIn("description", self.wait_for("application")["variables"])

    def test_failed_transition_keeps_inbox_and_previous_checkpoint(self):
        self.prepare_correction()
        self.sql("""CREATE TRIGGER fail_event BEFORE INSERT ON Event
                    WHEN NEW.activity_id = 'application'
                    BEGIN SELECT RAISE(ABORT, 'synthetic event failure'); END""")
        self.correction()  # Accepted into the durable inbox, processing then fails.
        self.wait_for("review", state="failed")
        self.assertEqual(self.sql('SELECT COUNT(*) FROM PendingSubmission'), [(1,)])
        self.assertEqual(self.sql(
            "SELECT COUNT(*) FROM Event WHERE activity_id = 'application'"), [(1,)])
        self.submit("review", {"approved": "yes"}, status=409)
        self.stop()
        self.sql('DROP TRIGGER fail_event')
        self.start()
        self.assert_corrected()

    def test_inbox_delete_failure_rolls_back_the_entire_transition(self):
        self.prepare_correction()
        self.sql("""CREATE TRIGGER fail_consumption BEFORE DELETE ON PendingSubmission
                    BEGIN SELECT RAISE(ABORT, 'synthetic delete failure'); END""")
        self.correction()
        self.wait_for("review", state="failed")
        self.assertEqual(self.sql('SELECT COUNT(*) FROM PendingSubmission'), [(1,)])
        self.assertEqual(self.sql(
            "SELECT COUNT(*) FROM Event WHERE activity_id = 'application'"), [(1,)])
        self.stop()
        self.sql('DROP TRIGGER fail_consumption')
        self.start()
        self.assert_corrected()

    def test_actual_internship_models_recover_application_and_diary(self):
        application = "ispunjavanje_prijavnice_student"
        diary = "predavanje_dnevnika_student"
        self.stop()
        (self.directory / "stub_connectors").touch()
        for model in ("strucna_praksa_A.bpmn", "strucna_praksa_B.bpmn"):
            with self.subTest(model=model):
                shutil.copy(ROOT / "models" / model, self.directory / "models")
                self.instance_id = model
                self.sql('INSERT INTO RunningInstance(instance_id, running) VALUES (?, 1)', (model,))
                self.sql("""INSERT INTO Event(model_name, instance_id, activity_id,
                            timestamp, pending, activity_variables) VALUES (?, ?, ?, ?, ?, ?)""",
                         (model, model, application, "2020-01-01 00:00:00",
                          json.dumps([application]), json.dumps({
                              "detaljan_opis_zadatka": "original",
                              "connector_description": "original",
                          })))
                self.start()
                self.wait_for(application)
                marker = self.directory / "pause_delivery"
                marker.touch()
                self.submit(application, {"detaljan_opis_zadatka": "corrected"})
                self.stop()
                marker.unlink()
                self.start()
                body = self.wait_for(diary)
                self.assertEqual(body["variables"]["detaljan_opis_zadatka"], "corrected")
                self.assertEqual(body["variables"]["connector_description"], "corrected")
                self.restart()
                body = self.wait_for(diary)
                self.assertEqual(body["variables"]["detaljan_opis_zadatka"], "corrected")
                self.assertEqual(body["variables"]["connector_description"], "corrected")
                self.stop()

    def test_finished_instances_are_not_restarted_but_remain_listed(self):
        self.create()
        self.submit("application", {"description": "done"})
        self.wait_for("review")
        self.submit("review", {"approved": "yes"})
        self.wait_for("diary")
        self.submit("diary", {})
        for _ in range(200):
            if self.sql('SELECT running FROM RunningInstance') == [(0,)]:
                break
            time.sleep(0.01)
        self.assertEqual(self.sql('SELECT running FROM RunningInstance'), [(0,)])
        events = self.sql('SELECT COUNT(*) FROM Event')
        self.restart()
        self.assertEqual(self.request("GET", f"/instance/{self.instance_id}")[1]["state"], "finished")
        self.assertEqual(self.sql('SELECT COUNT(*) FROM Event'), events)
        self.assertIn(self.instance_id, self.request("GET", "/model")[1]["results"][0]["instances"])

    def prepare_internship(self, model="A", task="ispunjavanje_prijavnice_student"):
        from uuid import uuid4
        self.stop()
        (self.directory / "stub_connectors").touch()
        filename = f"strucna_praksa_{model}.bpmn"
        shutil.copy(ROOT / "models" / filename, self.directory / "models")
        self.instance_id = str(uuid4())
        self.sql('INSERT INTO RunningInstance(instance_id, running) VALUES (?, 1)', (self.instance_id,))
        self.sql("""INSERT INTO Event(model_name, instance_id, activity_id,
                    timestamp, pending, activity_variables) VALUES (?, ?, ?, ?, ?, ?)""",
                 (filename, self.instance_id, task, "2020-01-01 00:00:00",
                  json.dumps([task]), json.dumps({"description": "preserved", "_owner_student_id": 1, "_owner_email": "test@local.test"})))
        self.start()
        body = self.wait_for(task)
        self.assertIn("odustajanje_od_prakse", body["available_events"])

    def withdraw(self, expected=200):
        status, body = self.request("POST", f"/instance/{self.instance_id}/event/odustajanje_od_prakse", {})
        self.assertEqual(status, expected, body)

    def wait_for_state(self, state):
        for _ in range(200):
            status, body = self.request("GET", f"/instance/{self.instance_id}")
            if status == 200 and body["state"] == state:
                return body
            time.sleep(0.01)
        self.fail(f"Did not reach {state}: {body}")

    def assert_withdrawn(self):
        body = self.wait_for_state("cancelled")
        self.assertEqual(body["pending"], [])
        self.assertEqual(body["available_events"], [])
        self.assertEqual(body["variables"]["description"], "preserved")
        self.assertTrue(body["variables"]["withdrawal_saved"])
        self.assertEqual(set(body["completed_activity_ids"]),
                         {"odustajanje_od_prakse", "evidentiranje_odustajanja", "end_event_odustajanje"})
        self.assertEqual(self.sql('SELECT running FROM RunningInstance WHERE instance_id=?', (self.instance_id,)), [(0,)])
        self.assertEqual(self.sql('SELECT COUNT(*) FROM PendingSubmission WHERE instance_id=?', (self.instance_id,)), [(0,)])

    def test_withdrawal_of_both_models_at_every_user_stage_survives_restart(self):
        stages = {
            "A": ["odabiranje_zadatka_student", "alociranje_profesor", "evaluacija_poslodavac", "ispunjavanje_prijavnice_student", "predavanje_dnevnika_student", "upis_ocjene"],
            "B": ["model_b_odobrenje_zadatka", "model_b_direktna_prijava_student", "ispunjavanje_prijavnice_student", "predavanje_dnevnika_student", "upis_ocjene"],
        }
        for model, tasks in stages.items():
            for task in tasks:
                with self.subTest(model=model, task=task):
                    self.prepare_internship(model, task)
                    marker = self.directory / "pause_delivery"
                    marker.touch()
                    self.withdraw()
                    self.withdraw()  # Lost acknowledgement retry must not duplicate the inbox.
                    self.submit(task, {}, status=409)
                    self.stop()
                    marker.unlink()
                    self.start()
                    self.assert_withdrawn()
                    self.withdraw()
                    self.submit(task, {}, status=409)
                    self.restart()
                    self.assert_withdrawn()
                    self.assertEqual(self.sql("SELECT COUNT(*) FROM Event WHERE instance_id=? AND activity_id='end_event_odustajanje'", (self.instance_id,)), [(1,)])

    def test_new_instances_split_into_internship_and_withdrawal(self):
        for model, task in (("A", "odabiranje_zadatka_student"), ("B", "model_b_odobrenje_zadatka")):
            with self.subTest(model=model):
                self.stop()
                (self.directory / "stub_connectors").touch()
                filename = f"strucna_praksa_{model}.bpmn"
                shutil.copy(ROOT / "models" / filename, self.directory / "models")
                self.start()
                status, body = self.request("POST", f"/model/{filename}/instance")
                self.assertEqual(status, 200, body)
                self.instance_id = body["id"]
                body = self.wait_for(task)
                self.assertEqual(body["available_events"], ["odustajanje_od_prakse"])
                self.restart()
                self.assertEqual(self.wait_for(task)["available_events"], ["odustajanje_od_prakse"])
                self.withdraw()
                body = self.wait_for_state("cancelled")
                self.assertEqual(body["pending"], [])
                self.assertEqual(body["available_events"], [])
                self.assertTrue(body["variables"]["withdrawal_saved"])

    def test_coordinator_recovers_pinned_application_without_resubmission(self):
        application = "ispunjavanje_prijavnice_student"
        for task, email in [("kreiranje_potvrde_profesor", False), ("slanje_potvrde_student_email", True)]:
            with self.subTest(task=task):
                self.prepare_internship(task=application)
                model = self.directory / "models/strucna_praksa_A.bpmn"
                self.sql("INSERT INTO InstanceDefinition(instance_id, xml) VALUES (?, ?)",
                         (self.instance_id, model.read_text()))
                self.restart()
                marker = self.directory / "fail_step"
                marker.write_text(task)
                self.submit(application, {"detaljan_opis_zadatka": "Synthetic application"})
                self.wait_for_state("failed")
                marker.unlink()
                model.write_text(model.read_text().replace('id="' + task + '"', 'id="changed"')
                                 .replace('Ref="' + task + '"', 'Ref="changed"'))
                self.restart()
                self.wait_for_state("failed")
                incidents = self.request("GET", "/engine")[1]["incidents"]
                self.assertEqual(len(incidents), 1)
                incident = incidents[0]
                self.assertEqual(incident["task_id"], task)
                self.assertNotIn("Sensitive", json.dumps(incident))
                path = f"/instance/{self.instance_id}/retry"
                body = {"attempt_id": incident["attempt_id"]}
                self.assertEqual(self.request("POST", path, body, role="student")[0], 403)
                if email:
                    self.assertEqual(self.request("POST", path, body)[0], 409)
                    body["confirm_resend"] = True
                self.assertEqual(self.request("POST", path, body)[0], 202)
                self.assertEqual(self.request("POST", path, body)[0], 409)
                state = self.wait_for("predavanje_dnevnika_student")
                self.assertEqual(state["variables"]["connector_description"], "Synthetic application")
                self.assertEqual(self.request("GET", "/engine")[1]["incidents"], [])
                self.assertEqual(self.sql("SELECT count(DISTINCT operation_key), count(*), sum(closed) "
                                          "FROM ConnectorAttempt WHERE instance_id=? AND task_id=?",
                                          (self.instance_id, task)), [(1, 2, 2)])
                self.assertEqual(self.sql("SELECT retried_by FROM ConnectorAttempt WHERE id > ? AND task_id=?",
                                          (incident["attempt_id"], task)), [("test@local.test",)])
                self.assertEqual(self.sql("SELECT count(*) FROM Event WHERE instance_id=? AND activity_id=?",
                                          (self.instance_id, "azuriranje_podataka_profesor")), [(1,)])

    def test_crash_after_email_delivery_does_not_resend_on_startup(self):
        self.prepare_internship()
        self.sql("""CREATE TRIGGER lose_ack BEFORE INSERT ON Event
                    WHEN NEW.activity_id = 'slanje_potvrde_student_email'
                    BEGIN SELECT RAISE(ABORT, 'synthetic checkpoint failure'); END""")
        self.submit("ispunjavanje_prijavnice_student", {})
        self.wait_for_state("failed")
        self.sql("DROP TRIGGER lose_ack")
        self.restart()
        self.wait_for_state("failed")
        incident, = self.request("GET", "/engine")[1]["incidents"]
        self.assertEqual(incident["connector"], "emailjs")
        path = f"/instance/{self.instance_id}/retry"
        self.assertEqual(self.request("POST", path, {"attempt_id": incident["attempt_id"]})[0], 409)
        self.assertEqual(self.sql("SELECT count(*) FROM ConnectorAttempt WHERE task_id=?",
                                  (incident["task_id"],)), [(1,)])

    def test_withdrawal_failure_can_be_retried_without_repeating_the_message(self):
        self.prepare_internship()
        marker = self.directory / "fail_withdrawal"
        marker.touch()
        self.withdraw()
        self.wait_for_state("failed")
        self.submit("ispunjavanje_prijavnice_student", {}, status=409)
        marker.unlink()
        self.withdraw()
        self.assert_withdrawn()
        self.assertEqual(self.sql("SELECT COUNT(*) FROM Event WHERE activity_id='odustajanje_od_prakse'"), [(1,)])

    def test_failed_message_checkpoint_keeps_durable_request_for_retry(self):
        self.prepare_internship()
        self.sql("""CREATE TRIGGER fail_withdrawal BEFORE INSERT ON Event
                    WHEN NEW.activity_id = 'odustajanje_od_prakse'
                    BEGIN SELECT RAISE(ABORT, 'synthetic event failure'); END""")
        self.withdraw()
        self.wait_for_state("failed")
        self.sql('DROP TRIGGER fail_withdrawal')
        self.withdraw()
        self.assert_withdrawn()

    def test_normal_completion_clears_withdrawal_branch_and_rejects_late_withdrawal(self):
        for model in ("A", "B"):
            self.prepare_internship(model, "upis_ocjene")
            self.submit("upis_ocjene", {"ocjena_unesena_studomat": "true"})
            body = self.wait_for_state("finished")
            self.assertEqual(body["pending"], [])
            self.assertEqual(body["available_events"], [])
            self.withdraw(expected=409)

    def test_invalid_and_duplicate_forms_are_not_accepted(self):
        self.create()
        self.submit("review", {}, status=409)
        self.submit("application", [], status=400)
        self.assertEqual(self.request("POST", "/instance/missing/task/application/form", {})[0], 404)
        (self.directory / "pause_delivery").touch()
        self.submit("application", {"description": "first"})
        self.submit("application", {"description": "second"}, status=409)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM PendingSubmission'), [(1,)])

    def test_rollback_only_offers_completed_tasks_on_the_current_path(self):
        self.create()
        path = f"/instance/{self.instance_id}/rollback"
        def options():
            status, data = self.request("GET", path)
            self.assertEqual(status, 200)
            return data, [t["id"] for t in data["targets"]]
        first, targets = options()
        self.assertEqual(targets, [])
        self.submit("application", {"description": "keep me"})
        self.wait_for("review")
        review, targets = options()
        self.assertEqual(targets, ["application"])
        for target in ["review", "diary", "decision", "missing"]:
            self.assertEqual(self.request("POST", path, {"target": target, "checkpoint": review["checkpoint"]})[0], 409)
        self.submit("review", {"approved": "no"})
        self.wait_for("application")
        self.assertEqual(options()[1], [])  # Natural return made review a future task.
        self.submit("application", {"description": "corrected"})
        self.wait_for("review")
        self.submit("review", {"approved": "yes"})
        self.wait_for("diary")
        diary, targets = options()
        self.assertEqual(targets, ["review", "application"])
        move = {"target": "application", "checkpoint": diary["checkpoint"]}
        self.assertEqual(self.request("GET", path, role="student")[0], 403)
        self.assertEqual(self.request("POST", path, move, role="student")[0], 403)
        self.assertEqual(self.request("POST", path, {**move, "checkpoint": review["checkpoint"]})[0], 409)
        self.assertEqual(self.request("POST", path, move)[0], 200)
        self.assertEqual(self.request("POST", path, move)[0], 409)
        self.submit("diary", {}, status=409)
        self.restart()
        data = self.wait_for("application")
        self.assertEqual(data["variables"]["description"], "corrected")
        self.assertEqual(data["variables"]["approved"], "yes")
        self.assertEqual(data["completed_activity_ids"], ["application", "decision", "review", "start"])
        self.assertEqual(options()[1], [])
        self.assertEqual(self.request("POST", path, {"checkpoint": options()[0]["checkpoint"], "target": "review"})[0], 409)
        moves = self.sql("SELECT activity_variables FROM Event WHERE activity_id='@rollback'")
        self.assertEqual(len(moves), 1)
        self.assertEqual(json.loads(moves[0][0])["actor"], "test@local.test")
        self.submit("application", {"description": "after move"})
        self.wait_for("review")
        (self.directory / "pause_delivery").touch()
        move["checkpoint"] = options()[0]["checkpoint"]
        self.submit("review", {"approved": "yes"})
        self.assertEqual(options()[1], [])
        self.assertEqual(self.request("POST", path, move)[0], 409)

    def test_rollback_from_failed_connector_reopens_form_and_preserves_withdrawal(self):
        task = "ispunjavanje_prijavnice_student"
        self.prepare_internship(task=task)
        marker = self.directory / "fail_step"
        marker.write_text("kreiranje_potvrde_profesor")
        self.submit(task, {"detaljan_opis_zadatka": "saved"})
        failed = self.wait_for_state("failed")
        self.assertNotIn("kreiranje_potvrde_profesor", failed["completed_activity_ids"])
        path = f"/instance/{self.instance_id}/rollback"
        opts = self.request("GET", path)[1]
        self.assertIn(task, [t["id"] for t in opts["targets"]])
        self.assertEqual(self.request("POST", path, {"target": task, "checkpoint": opts["checkpoint"]})[0], 200)
        self.assertEqual(self.request("GET", "/engine")[1]["incidents"], [])
        self.restart()
        data = self.wait_for(task)
        self.assertEqual(data["variables"]["detaljan_opis_zadatka"], "saved")
        self.assertIn("odustajanje_od_prakse", data["available_events"])
        marker.unlink()
        self.submit(task, {"detaljan_opis_zadatka": "edited"})
        self.wait_for("predavanje_dnevnika_student")

    def test_deletion_removes_pending_submissions(self):
        self.create()
        (self.directory / "pause_delivery").touch()
        self.submit("application", {"description": "delete me"})
        self.assertEqual(self.request("DELETE", f"/instance/{self.instance_id}")[0], 200)
        self.assertEqual(self.request("GET", f"/instance/{self.instance_id}")[0], 404)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM PendingSubmission'), [(0,)])
        self.restart()
        self.assertEqual(self.request("GET", f"/instance/{self.instance_id}")[0], 404)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        serve()
    else:
        unittest.main()
