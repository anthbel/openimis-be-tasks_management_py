from django.core.exceptions import PermissionDenied
from graphene.types.resolver import dict_or_attr_resolver

from core.apps import CoreConfig
from core.data_masking import MaskingClassStorage

TASK_MASKING_MODEL = "Task"
TASK_HISTORY_MASKING_MODEL = "HistoricalTask"


def may_see_masked(user):
    return user.has_perms(CoreConfig.gql_query_enable_viewing_masked_data_perms)


def registered_mask(masking_model):
    """The mask another module registers for ``masking_model``, or None when none is registered or it is off."""
    mask = MaskingClassStorage.get_masking_class(masking_model)
    return mask if mask is not None and mask.masking_enabled else None


def _context_may_see_masked(info):
    """Whether the user of the GraphQL request may see masked data, checked once per request."""
    context = getattr(info, "context", None)
    if context is None:
        return False
    if not hasattr(context, "_tasks_management_may_see_masked"):
        context._tasks_management_may_see_masked = may_see_masked(context.user)
    return context._tasks_management_may_see_masked


def mask_task(info, task, masking_model, entity_string):
    """Masks ``task`` in place, once per object, by the mask registered for ``masking_model`` unless the user of
    the request may see masked data. The mask is applied to the task with its ``entity_string`` attribute set
    to ``entity_string(task)``, so a mask may list ``entity_string`` among its fields or set it itself."""
    if getattr(task, "_data_mask_checked", False):
        return task
    mask = registered_mask(masking_model)
    if mask is not None and not _context_may_see_masked(info):
        task.entity_string = entity_string(task)
        mask.apply_mask(task)
    task._data_mask_checked = True
    return task


def masked_entity_string(info, task, masking_model, entity_string):
    """The task's entity string as the reader may see it."""
    mask_task(info, task, masking_model, entity_string)
    return task.entity_string if hasattr(task, "entity_string") else entity_string(task)


def masking_resolver(masking_model, entity_string):
    """A default field resolver masking the task first, so the type is masked on every path reaching it."""

    def resolve(attname, default_value, root, info, **args):
        mask_task(info, root, masking_model, entity_string)
        return dict_or_attr_resolver(attname, default_value, root, info, **args)

    return resolve


def check_entity_string_filter(user, masking_model):
    """Refuses the entity string filter to a user without the masked-data right while a mask is registered for
    ``masking_model``: the filter matches the stored entity strings, so its results would tell masked values."""
    if registered_mask(masking_model) is not None and not may_see_masked(user):
        raise PermissionDenied("filtering on entityString needs the right to view masked data")
