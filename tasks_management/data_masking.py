import logging

from django.core.exceptions import FieldDoesNotExist, PermissionDenied
from django.db.models import JSONField
from graphene.types.resolver import dict_or_attr_resolver
from graphene.utils.str_converters import to_snake_case

from core.apps import CoreConfig
from core.data_masking import MaskingClassStorage

logger = logging.getLogger(__name__)

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


def _order_lookups(term):
    """The lookups of one ``orderBy`` term, as core's ordered connection applies it."""
    from core.schema import OrderedDjangoFilterConnectionField

    cleaned = to_snake_case(OrderedDjangoFilterConnectionField._filter_order_by(term) or "")
    return [part.lstrip("-+") for part in cleaned.split(",") if part]


def traverses_json_field(model, lookup):
    """Whether the ``__`` lookup ``lookup`` from ``model`` reads a JSON field of any model along the relations it
    follows, or names something that is not a field of them (an annotation, a generic relation)."""
    for part in lookup.split("__"):
        if model is None:
            return True
        try:
            field = model._meta.get_field(part)
        except FieldDoesNotExist:
            return True
        if isinstance(field, JSONField):
            return True
        if not field.is_relation:
            return False
        model = field.related_model
    return False


def unmasked_json_order_by(user, model, order_by, applies=True):
    """``order_by`` less, for a user without the masked-data right and while ``applies``, its terms whose path
    traverses a JSON field (``traverses_json_field``): JSON fields hold the copies of masked values, so such an
    ordering would tell them. The primary key orders last, so the order stays total and pages neither repeat
    nor skip rows."""
    if not order_by:
        return order_by
    terms = [order_by] if isinstance(order_by, str) else list(order_by)
    kept = terms
    if applies and not may_see_masked(user):
        kept = [term for term in terms if term == "?" or not any(
            traverses_json_field(model, lookup) for lookup in _order_lookups(term))]
        if len(kept) < len(terms):
            logger.info("orderBy %s of %s left out for a user without the masked-data right",
                        [term for term in terms if term not in kept], model.__name__)
    primary_key = model._meta.pk.name
    if primary_key not in [lookup for term in kept if term != "?" for lookup in _order_lookups(term)]:
        kept = [*kept, primary_key]
    return kept


def unmasked_task_order_by(user, model, order_by):
    """``unmasked_json_order_by`` while a mask is registered for ``model`` (Task or HistoricalTask)."""
    return unmasked_json_order_by(user, model, order_by, applies=registered_mask(model.__name__) is not None)
