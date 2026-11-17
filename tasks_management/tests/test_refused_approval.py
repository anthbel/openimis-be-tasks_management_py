"""An approved task whose service operation refuses, or whose update the entity no longer matches, fails with
the reason instead of showing as completed."""
from django.core.exceptions import ValidationError
from django.test import TestCase

from core.signals import REGISTERED_SERVICE_SIGNALS
from core.test_helpers import create_test_interactive_user
from tasks_management.models import Task, TaskGroup
from tasks_management.services import (
    BaseService, CheckerLogicServiceMixin, TaskService, on_task_complete_service_handler,
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
