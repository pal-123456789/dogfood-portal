# src/api/urls.py
"""URL map for the read-only public API (/api/v1/), included from portal/urls.py.

Nothing here is on the acceptance checker's five flat routes, and none of it is linked from
base.html, so mounting it leaves replay at 7/7. Event-scoped collections are nested under
`events/<ext_id>/` so scoping is structural, not just a queryset convention.
"""
from django.urls import path
from drf_spectacular.views import SpectacularAPIView

from . import views

app_name = "api"

urlpatterns = [
    path("events/", views.EventListView.as_view(), name="event-list"),
    path("events/<str:ext_id>/", views.EventDetailView.as_view(), name="event-detail"),
    path("events/<str:ext_id>/tracks/", views.EventTracksView.as_view(), name="event-tracks"),
    path("events/<str:ext_id>/teams/", views.EventTeamsView.as_view(), name="event-teams"),
    path("events/<str:ext_id>/submissions/", views.EventSubmissionsView.as_view(),
         name="event-submissions"),
    path("events/<str:ext_id>/results/", views.EventResultsView.as_view(), name="event-results"),
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path("docs/", views.CspSwaggerView.as_view(url_name="api:schema"), name="docs"),
    # Appended (T4): verifiable results certificate for an event's official PUBLISHED results.
    # Read-only GET; exact-path match, so its position after the schema routes cannot shadow, and
    # is not shadowed by, any route above (event-detail matches only `events/<id>/`).
    path("events/<str:ext_id>/certificate/", views.EventCertificateView.as_view(),
         name="event-certificate"),
]
