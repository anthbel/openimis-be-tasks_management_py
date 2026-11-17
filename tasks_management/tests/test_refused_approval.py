"""An approved task whose service operation refuses, or whose update the entity no longer matches, fails with
the reason instead of showing as completed."""
import threading
import time

from django.core.exceptions import ValidationError
from django.db import connection, connections, transaction
from django.test import TestCase, TransactionTestCase

from core.signals import REGISTERED_SERVICE_SIGNALS
from core.test_helpers import create_test_interactive_user
from tasks_management.models import Task, TaskGroup
from tasks_management.services import (
    BaseService, CheckerLogicServiceMixin, TaskService, on_task_complete_service_handler,
)

WAIT_SECONDS = 10
WAITING_ON_A_LOCK = (
    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
    "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
)


class TaskGroupCodeService(BaseService, CheckerLogicServiceMixin):
    """A service on TaskGroup; ``refusal`` makes its create return an error result or raise."""

    OBJECT_TYPE = TaskGroup
    refusal = None

    def create(self, obj_data):
        if isinstance(self.refusal, Exception):
            raise self.refusal
        if self.refusal:
            return {"success": False, "message": "Failed to create TaskGroup", "detail": self.refusal}
        return super().create(obj_data)


class RefusedApprovalTest(TestCase):
    def setUp(self):
        self.user = create_test_interactive_user(username="tm_refused_approval")
        signal = REGISTERED_SERVICE_SIGNALS["task_service.complete_task"].after_service_signal
        signal.connect(on_task_complete_service_handler(TaskGroupCodeService), weak=False,
                       dispatch_uid="tm-refused-approval")
        self.addCleanup(signal.disconnect, dispatch_uid="tm-refused-approval")
        self.addCleanup(setattr, TaskGroupCodeService, "refusal", None)
        self.group = TaskGroup(code="TM-RA-A", completion_policy=TaskGroup.TaskGroupCompletionPolicy.ANY)
        self.group.save(user=self.user)

    def approve(self, result):
        self.assertTrue(result["success"], result)
        task_id = result["data"]["id"]
        completed = TaskService(self.user).complete_task({"id": task_id})
        self.assertTrue(completed["success"], completed)
        return Task.objects.get(id=task_id)

    def create_task(self):
        return TaskGroupCodeService(self.user).create_create_task(
            {"code": "TM-RA-NEW", "completion_policy": TaskGroup.TaskGroupCompletionPolicy.ANY})

    def assertFailedWith(self, task, reason):
        self.assertEqual(task.status, Task.Status.FAILED)
        self.assertIn(reason, task.json_ext["completion_error"])

    def test_a_create_the_service_refuses_with_an_error_result_fails_with_its_detail(self):
        TaskGroupCodeService.refusal = "the rule refuses"
        task = self.approve(self.create_task())
        self.assertFailedWith(task, "the rule refuses")
        self.assertFalse(TaskGroup.objects.filter(code="TM-RA-NEW").exists())

    def test_a_create_the_service_refuses_by_raising_fails_with_the_message(self):
        TaskGroupCodeService.refusal = ValidationError("the rule raises")
        task = self.approve(self.create_task())
        self.assertFailedWith(task, "the rule raises")
        self.assertFalse(TaskGroup.objects.filter(code="TM-RA-NEW").exists())

    def test_a_create_the_service_accepts_completes(self):
        task = self.approve(self.create_task())
        self.assertEqual(task.status, Task.Status.COMPLETED)
        self.assertNotIn("completion_error", task.json_ext or {})
        self.assertTrue(TaskGroup.objects.filter(code="TM-RA-NEW").exists())

    def test_an_update_approved_after_the_entity_changed_fails_and_keeps_the_change(self):
        result = TaskGroupCodeService(self.user).create_update_task({"id": str(self.group.id), "code": "TM-RA-B"})
        self.group.code = "TM-RA-C"
        self.group.save(user=self.user)
        task = self.approve(result)
        self.assertFailedWith(task, "code")
        self.assertEqual(TaskGroup.objects.get(id=self.group.id).code, "TM-RA-C")

    def test_an_update_approved_on_an_unchanged_entity_is_written(self):
        result = TaskGroupCodeService(self.user).create_update_task({"id": str(self.group.id), "code": "TM-RA-B"})
        task = self.approve(result)
        self.assertEqual(task.status, Task.Status.COMPLETED)
        self.assertEqual(TaskGroup.objects.get(id=self.group.id).code, "TM-RA-B")


class RefusedApprovalConcurrencyTest(TransactionTestCase):
    """Committed rows, removed on teardown in place of a flush: the approval of an update task waits for a write
    that holds the entity's row and compares the task with what that write left."""

    CODES = ("TM-RAC-A", "TM-RAC-B", "TM-RAC-C")

    def setUp(self):
        self.user = create_test_interactive_user(username="tm_refused_approval_lock")
        signal = REGISTERED_SERVICE_SIGNALS["task_service.complete_task"].after_service_signal
        signal.connect(on_task_complete_service_handler(TaskGroupCodeService), weak=False,
                       dispatch_uid="tm-refused-approval-lock")
        self.addCleanup(signal.disconnect, dispatch_uid="tm-refused-approval-lock")
        self.group = TaskGroup(code="TM-RAC-A", completion_policy=TaskGroup.TaskGroupCompletionPolicy.ANY)
        self.group.save(user=self.user)

    def _fixture_teardown(self):
        tasks = Task.objects.filter(business_event__startswith=f"{TaskGroupCodeService.__name__}.")
        Task.history.model.objects.filter(id__in=list(tasks.values_list("id", flat=True))).delete()
        tasks.delete()
        TaskGroup.history.model.objects.filter(code__in=self.CODES).delete()
        TaskGroup.objects.filter(code__in=self.CODES).delete()

    @staticmethod
    def wait_for_a_lock_waiter():
        deadline = time.monotonic() + WAIT_SECONDS
        with connection.cursor() as cursor:
            while time.monotonic() < deadline:
                cursor.execute("SELECT pg_stat_clear_snapshot()")
                cursor.execute(WAITING_ON_A_LOCK)
                if cursor.fetchone()[0]:
                    return True
                time.sleep(0.05)
        return False

    def test_an_update_approved_while_another_write_holds_the_entity_compares_with_that_write(self):
        created = TaskGroupCodeService(self.user).create_update_task({"id": str(self.group.id), "code": "TM-RAC-B"})
        self.assertTrue(created["success"], created)
        written, waited, results = threading.Event(), [], {}

        def first():
            with transaction.atomic():
                group = TaskGroup.objects.select_for_update().get(id=self.group.id)
                group.code = "TM-RAC-C"
                group.save(user=self.user)
                written.set()
                waited.append(self.wait_for_a_lock_waiter())

        def second():
            written.wait(WAIT_SECONDS)
            with transaction.atomic():
                results["second"] = TaskService(self.user).complete_task({"id": created["data"]["id"]})

        def in_thread(target):
            def body():
                try:
                    target()
                except Exception as exc:
                    results[target.__name__] = repr(exc)
                finally:
                    connections.close_all()

            return threading.Thread(target=body)

        threads = [in_thread(first), in_thread(second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(4 * WAIT_SECONDS)
            self.assertFalse(thread.is_alive())
        self.assertEqual(waited, [True], results)
        task = Task.objects.get(id=created["data"]["id"])
        self.assertEqual(task.status, Task.Status.FAILED, results)
        self.assertIn("code", task.json_ext["completion_error"])
        self.assertEqual(TaskGroup.objects.get(id=self.group.id).code, "TM-RAC-C")
