import json
from http import HTTPStatus

import arrow
from django.db.models import Count, Q

from canvas_sdk.effects import Effect
from canvas_sdk.effects.launch_modal import LaunchModalEffect
from canvas_sdk.effects.simple_api import Response, JSONResponse
from canvas_sdk.handlers.application import Application
from canvas_sdk.handlers.simple_api import StaffSessionAuthMixin, SimpleAPI, api
from canvas_sdk.templates import render_to_string
from canvas_sdk.v1.data import Note, Staff
from canvas_sdk.v1.data.claim import ClaimQueue
from canvas_sdk.v1.data.note import NoteStates, NoteTypeCategories, NoteType

from notes_worklist.models import CustomStaff, SavedWorklistView


# Default state set when no state_filter is sent. Matches encounter_list's
# default behavior — the active worklist of in-progress and just-completed work.
OPEN_STATES = (
    NoteStates.NEW,
    NoteStates.UNLOCKED,
    NoteStates.PUSHED,
    NoteStates.UNDELETED,
    NoteStates.CONVERTED,
    NoteStates.NOSHOW,
)


class MyApplication(Application):
    """An embeddable application that can be registered to Canvas."""

    def on_open(self) -> Effect:
        """Handle the on_open event."""
        return LaunchModalEffect(
            content=render_to_string("templates/notes_worklist.html"),
            target=LaunchModalEffect.TargetType.PAGE,
        ).apply()


class NotesWorklistApi(StaffSessionAuthMixin, SimpleAPI):
    """API for the Notes Worklist (filterable by any subset of NoteStates)."""

    @api.get("/encounters")
    def get_encounters(self) -> list[Response | Effect]:
        """Get paginated notes filtered by NoteStates and the usual worklist filters."""
        provider_ids = self.request.query_params.get("provider_ids")
        location_ids = self.request.query_params.get("location_ids")
        note_type_names = self.request.query_params.get("note_type_names")
        claim_queue_names = self.request.query_params.get("claim_queue_names")
        has_uncommitted_commands = self.request.query_params.get("has_uncommitted_commands") == "true"
        patient_search = self.request.query_params.get("patient_search")
        dos_start = self.request.query_params.get("dos_start")
        dos_end = self.request.query_params.get("dos_end")
        state_filter = self.request.query_params.get("state_filter")

        # Pagination parameters
        page = int(self.request.query_params.get("page", 1))
        page_size = int(self.request.query_params.get("page_size", 25))

        # Sorting parameters
        sort_by = self.request.query_params.get("sort_by", "dos")
        sort_direction = self.request.query_params.get("sort_direction", "asc")

        requested_states = self._resolve_states(state_filter)
        note_queryset = Note.objects.filter(current_state__state__in=requested_states)

        note_queryset = note_queryset.exclude(note_type_version__category__in=(NoteTypeCategories.MESSAGE,
                                                               NoteTypeCategories.LETTER,))

        if provider_ids:
            clean_ids = [pid.strip() for pid in provider_ids.split(',') if pid.strip()]
            if clean_ids:
                note_queryset = note_queryset.filter(provider__id__in=clean_ids)

        if location_ids:
            clean_location_ids = [lid.strip() for lid in location_ids.split(',') if lid.strip()]
            if clean_location_ids:
                note_queryset = note_queryset.filter(location__id__in=clean_location_ids)

        if note_type_names:
            clean_note_type_names = [name.strip() for name in note_type_names.split(',') if name.strip()]
            if clean_note_type_names:
                note_queryset = note_queryset.filter(note_type_version__name__in=clean_note_type_names)

        if claim_queue_names:
            clean_claim_queue_names = [cqn.strip() for cqn in claim_queue_names.split(',') if cqn.strip()]
            if clean_claim_queue_names:
                note_queryset = note_queryset.filter(claims__current_queue__name__in=clean_claim_queue_names)

        if patient_search:
            note_queryset = self._apply_patient_search(note_queryset, patient_search)

        if dos_start or dos_end:
            note_queryset = self._apply_dos_range_filter(note_queryset, dos_start, dos_end)

        # Add annotations
        note_queryset = note_queryset.annotate(
            staged_commands_count=Count(
                'commands',
                filter=Q(commands__state__in=('staged', 'in_review',))
            )
        )

        if has_uncommitted_commands:
            note_queryset = note_queryset.filter(staged_commands_count__gt=0)

        paginated_notes, total_count, total_pages = self._sort_and_paginate_database(
            note_queryset, sort_by, sort_direction, page, page_size
        )

        # Convert queryset to encounter data
        encounters = []
        for note in paginated_notes:
            claim_queue = note.get_claim().current_queue.name if note.get_claim() else None

            try:
                note_title = note.note_type_version.name or "Untitled Note"
            except:
                note_title = "Untitled Note"

            encounter_data = {
                "id": str(note.id),
                "dbid": note.dbid,
                "patient_name": (f"{note.patient.first_name} ({note.patient.nickname}) {note.patient.last_name}" if note.patient.nickname else f"{note.patient.first_name} {note.patient.last_name}" if note.patient else "Unknown Patient"),
                "patient_id": str(note.patient.id) if note.patient else None,
                "patient_dob": arrow.get(note.patient.birth_date).format(
                    "MMM DD, YYYY") if note.patient and note.patient.birth_date else "Unknown",
                "provider": note.provider.credentialed_name if note.provider else "Unknown Provider",
                "provider_id": str(note.provider.id) if note.provider else None,
                "note_title": note_title,
                "dos": arrow.get(note.datetime_of_service).format(
                    "MMM DD, YYYY") if note.datetime_of_service else "Unknown",
                "dos_iso": note.datetime_of_service.isoformat() if note.datetime_of_service else None,
                "uncommitted_commands": note.staged_commands_count,
                "claim_queue": claim_queue,
                "location": note.location.full_name if note.location else "Unknown Location",
                "location_id": str(note.location.id) if note.location else None,
                "created": note.created.isoformat() if note.created else None,
                "state": note.current_state.state if note.current_state else None,
            }
            encounters.append(encounter_data)

        return [JSONResponse({
            "encounters": encounters,
            "pagination": {
                "current_page": page,
                "total_pages": total_pages,
                "total_count": total_count,
                "page_size": page_size,
                "has_previous": page > 1,
                "has_next": page < total_pages,
            }
        }, status_code=HTTPStatus.OK)]

    @api.get("/providers")
    def get_providers(self) -> list[Response | Effect]:
        """Get list of providers who have notes."""
        logged_in_staff = self.request.headers["canvas-logged-in-user-id"]

        providers = [{"id": s.id, "name": s.credentialed_name}
                     for s in
                     Staff.objects.filter(active=True).order_by("first_name", "last_name")]


        return [JSONResponse({
            "logged_in_staff_id": logged_in_staff,
            "providers": providers
        }, status_code=HTTPStatus.OK)]

    @api.get("/locations")
    def get_locations(self) -> list[Response | Effect]:
        """Get practice locations that have notes in the requested state set.

        Respects the state_filter query param so the Location dropdown stays
        consistent with the current Status filter — picking "All Closed" only
        surfaces locations that actually have closed notes.
        """
        state_filter = self.request.query_params.get("state_filter")
        requested_states = self._resolve_states(state_filter)

        locations = [{"id": str(n.location.id), "name": n.location.full_name}
                     for n in
                     Note.objects.filter(current_state__state__in=requested_states)
                     .filter(location__isnull=False)
                     .order_by("location__full_name", "location__id")
                     .distinct("location__id", "location__full_name")]

        return [JSONResponse({
            "locations": locations
        }, status_code=HTTPStatus.OK)]

    @api.get("/note_types")
    def get_note_types(self) -> list[Response | Effect]:
        """Get list of note type names."""

        note_types = list(NoteType.objects.exclude(
            category__in=(
                NoteTypeCategories.MESSAGE,
                NoteTypeCategories.LETTER,
            )
        ).order_by("name").values("name").distinct())

        return [JSONResponse({
            "note_types": note_types
        }, status_code=HTTPStatus.OK)]

    @api.get("/claim_queues")
    def get_claim_queues(self) -> list[Response | Effect]:
        """Get list of claim queue names."""
        claim_queues = list(ClaimQueue.objects.values("name"))

        return [JSONResponse({
            "claim_queues": claim_queues
        }, status_code=HTTPStatus.OK)]

    # ---------------------------------------------------------------------
    # Saved Views CRUD
    # ---------------------------------------------------------------------

    @api.get("/views")
    def list_views(self) -> list[Response | Effect]:
        """Return saved views visible to the current user.

        Visible = own private views + any view marked visibility='shared'.
        The frontend uses this on page load to populate the Views dropdown.
        """
        staff_id = self.request.headers.get("canvas-logged-in-user-id", "")
        queryset = (
            SavedWorklistView.objects.filter(
                Q(creator__id=staff_id) | Q(visibility="shared")
            )
            .select_related("creator")
            .order_by("name")
        )
        views = [self._serialize_view(v, staff_id) for v in queryset]
        return [JSONResponse({"views": views}, status_code=HTTPStatus.OK)]

    @api.post("/views")
    def create_view(self) -> list[Response | Effect]:
        """Create a new saved view for the current user."""
        staff_id = self.request.headers.get("canvas-logged-in-user-id", "")
        payload = self._parse_view_payload()
        if payload is None:
            return [JSONResponse({"error": "invalid JSON body"}, status_code=HTTPStatus.BAD_REQUEST)]

        name = (payload.get("name") or "").strip()
        if not name:
            return [JSONResponse({"error": "name is required"}, status_code=HTTPStatus.BAD_REQUEST)]

        try:
            creator = CustomStaff.objects.get(id=staff_id)
        except CustomStaff.DoesNotExist:
            return [JSONResponse({"error": "current user not found"}, status_code=HTTPStatus.UNAUTHORIZED)]

        view = SavedWorklistView.objects.create(
            creator=creator,
            name=name,
            filters=payload.get("filters") or {},
            sort_by=(payload.get("sort_by") or "").strip(),
            sort_direction="desc" if payload.get("sort_direction") == "desc" else "asc",
            visibility="shared" if payload.get("visibility") == "shared" else "private",
        )
        return [JSONResponse({"view": self._serialize_view(view, staff_id)}, status_code=HTTPStatus.CREATED)]

    @api.put("/views")
    def update_view(self) -> list[Response | Effect]:
        """Rename / re-share an existing view. Only the creator can update."""
        staff_id = self.request.headers.get("canvas-logged-in-user-id", "")
        view_id = self.request.query_params.get("id", "")
        if not view_id:
            return [JSONResponse({"error": "id query param required"}, status_code=HTTPStatus.BAD_REQUEST)]
        payload = self._parse_view_payload()
        if payload is None:
            return [JSONResponse({"error": "invalid JSON body"}, status_code=HTTPStatus.BAD_REQUEST)]

        view = self._get_owned_view(view_id, staff_id)
        if view is None:
            return [JSONResponse({"error": "not found or not yours"}, status_code=HTTPStatus.NOT_FOUND)]

        if "name" in payload:
            name = (payload.get("name") or "").strip()
            if not name:
                return [JSONResponse({"error": "name cannot be empty"}, status_code=HTTPStatus.BAD_REQUEST)]
            view.name = name
        if "filters" in payload:
            view.filters = payload.get("filters") or {}
        if "sort_by" in payload:
            view.sort_by = (payload.get("sort_by") or "").strip()
        if "sort_direction" in payload:
            view.sort_direction = "desc" if payload.get("sort_direction") == "desc" else "asc"
        if "visibility" in payload:
            view.visibility = "shared" if payload.get("visibility") == "shared" else "private"
        view.save()

        return [JSONResponse({"view": self._serialize_view(view, staff_id)}, status_code=HTTPStatus.OK)]

    @api.delete("/views")
    def delete_view(self) -> list[Response | Effect]:
        """Delete a view. Only the creator can delete."""
        staff_id = self.request.headers.get("canvas-logged-in-user-id", "")
        view_id = self.request.query_params.get("id", "")
        if not view_id:
            return [JSONResponse({"error": "id query param required"}, status_code=HTTPStatus.BAD_REQUEST)]

        view = self._get_owned_view(view_id, staff_id)
        if view is None:
            return [JSONResponse({"error": "not found or not yours"}, status_code=HTTPStatus.NOT_FOUND)]
        view.delete()
        return [JSONResponse({"deleted": view_id}, status_code=HTTPStatus.OK)]

    # ---------------------------------------------------------------------
    # Saved Views helpers
    # ---------------------------------------------------------------------

    def _parse_view_payload(self) -> dict | None:
        """Parse the JSON body of a view create/update request."""
        body = getattr(self.request, "body", None)
        if body is None:
            return None
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        try:
            payload = json.loads(body or "{}")
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _get_owned_view(self, view_id: str, staff_id: str) -> SavedWorklistView | None:
        """Look up a view by id, returning None if not found or not owned by staff_id."""
        try:
            view = SavedWorklistView.objects.select_related("creator").get(id=view_id)
        except SavedWorklistView.DoesNotExist:
            return None
        return view if view.creator and str(view.creator.id) == str(staff_id) else None

    def _serialize_view(self, view: SavedWorklistView, current_staff_id: str) -> dict:
        """Build the JSON payload for a single view."""
        creator_name = view.creator.credentialed_name if view.creator else ""
        return {
            "id": str(view.id),
            "name": view.name,
            "visibility": view.visibility,
            "filters": view.filters,
            "sort_by": view.sort_by,
            "sort_direction": view.sort_direction,
            "creator_name": creator_name,
            "is_mine": bool(view.creator and str(view.creator.id) == str(current_staff_id)),
        }

    def _resolve_states(self, state_filter: str | None) -> list:
        """Parse a comma-separated NoteState name list (e.g. "NEW,UNLOCKED").

        Unknown names are silently dropped so a stale client sending a
        removed state doesn't break the request. Falls back to OPEN_STATES
        (matching encounter_list's default) when the param is empty or absent.
        """
        if state_filter:
            return [
                getattr(NoteStates, name.strip())
                for name in state_filter.split(",")
                if name.strip() and hasattr(NoteStates, name.strip())
            ]
        return list(OPEN_STATES)

    def _get_sort_fields(self, sort_by: str) -> list[str]:
        """Map frontend sort field names to database field names, returning a list of fields."""
        sort_mapping = {
            "patientName": ["patient__first_name", "patient__last_name"],
            "state": ["current_state__state"],  # Sorts by 3-letter DB code (alphabetical).
            "provider": ["provider__first_name", "provider__last_name"],
            "location": ["location__full_name"],
            "noteTitle": ["note_type_version__name"],
            "dos": ["datetime_of_service"],
            "uncommittedCommands": ["staged_commands_count"],
            "claimQueue": ["claims__current_queue__name"],
            "created": ["created"]
        }
        return sort_mapping.get(sort_by, ["created"])

    def _apply_patient_search(self, note_queryset, patient_search: str):
        """Apply patient name search across first, last, and nickname fields."""
        normalized_search = patient_search.strip()
        if not normalized_search:
            return note_queryset

        search_terms = normalized_search.split()

        search_filter = (
            Q(patient__first_name__icontains=normalized_search)
            | Q(patient__last_name__icontains=normalized_search)
            | Q(patient__nickname__icontains=normalized_search)
        )

        if len(search_terms) >= 2:
            first_term = search_terms[0]
            last_term = search_terms[-1]
            search_filter |= (
                (Q(patient__first_name__icontains=first_term) | Q(patient__nickname__icontains=first_term))
                & Q(patient__last_name__icontains=last_term)
            )
        elif len(search_terms) == 1:
            single_term = search_terms[0]
            search_filter |= Q(patient__first_name__icontains=single_term)
            search_filter |= Q(patient__last_name__icontains=single_term)
            search_filter |= Q(patient__nickname__icontains=single_term)

        return note_queryset.filter(search_filter)

    def _apply_dos_range_filter(self, note_queryset, dos_start: str | None, dos_end: str | None):
        """Filter notes by date of service range."""
        try:
            parsed_start = arrow.get(dos_start).date() if dos_start else None
        except (arrow.parser.ParserError, TypeError, ValueError):
            parsed_start = None

        try:
            parsed_end = arrow.get(dos_end).date() if dos_end else None
        except (arrow.parser.ParserError, TypeError, ValueError):
            parsed_end = None

        if parsed_start:
            note_queryset = note_queryset.filter(datetime_of_service__date__gte=parsed_start)

        if parsed_end:
            note_queryset = note_queryset.filter(datetime_of_service__date__lte=parsed_end)

        return note_queryset

    def _sort_and_paginate_database(self, note_queryset, sort_by, sort_direction, page, page_size):
        """Sort and paginate using database sorting."""
        sort_fields = self._get_sort_fields(sort_by)
        if sort_direction == "desc":
            sort_fields = [f"-{field}" for field in sort_fields]

        note_queryset = note_queryset.order_by(*sort_fields)
        total_count = note_queryset.count()
        paginated_notes, _, _ = self._apply_pagination(list(note_queryset), page, page_size)
        return paginated_notes, total_count, (total_count + page_size - 1) // page_size

    def _apply_pagination(self, items, page, page_size):
        """Apply pagination to a list of items."""
        total_count = len(items)
        total_pages = (total_count + page_size - 1) // page_size
        
        # Validate page number
        if page < 1:
            page = 1
        elif page > total_pages and total_pages > 0:
            page = total_pages
        
        # Calculate offset and apply slicing
        offset = (page - 1) * page_size
        paginated_items = items[offset:offset + page_size]
        
        return paginated_items, total_count, total_pages
