from canvas_sdk.v1.data import Staff
from canvas_sdk.v1.data.base import CustomModel, ModelExtension
from django.db.models import (
    DO_NOTHING,
    DateTimeField,
    ForeignKey,
    Index,
    JSONField,
    TextField,
)


class CustomStaff(Staff, ModelExtension):
    """Proxy to register the Staff reverse relation under this plugin's namespace."""

    pass


class SavedWorklistView(CustomModel):
    """A user-saved filter+sort preset for the Notes Worklist.

    Each view captures a snapshot of the worklist's filter state and sort order.
    Visibility is binary in v1: `private` (creator-only) or `shared` (all staff
    in the practice). The shared_with_team_ids / shared_with_staff_ids columns
    are reserved for a future per-team / per-user sharing UI.
    """

    creator = ForeignKey(
        CustomStaff,
        to_field="dbid",
        on_delete=DO_NOTHING,
        related_name="saved_worklist_views",
    )
    name = TextField()
    filters = JSONField(default=dict)
    sort_by = TextField(default="")
    sort_direction = TextField(default="asc")
    visibility = TextField(default="private")
    # Reserved for future team/user-specific sharing. Empty for v1.
    shared_with_team_ids = TextField(default="")
    shared_with_staff_ids = TextField(default="")
    created_at = DateTimeField(auto_now_add=True)
    updated_at = DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            Index(fields=["creator"]),
            Index(fields=["visibility"]),
        ]
