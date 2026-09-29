# src/awards/models.py
"""Prizes/awards for an event -- organizer-curated recognition layered on top of the results.

A Prize is organizer-owned CONFIGURATION, not an integrity record. It records which recognitions
an event offers (overall or per-track, podium places or special awards) and, optionally, which
submission an organizer has awarded it to. It is deliberately NOT part of the signed, reproducible
normalization run: awarding a prize never changes the judged, signed ranking and adds no
cryptographic property to it. The public podium page DERIVES its ordering from the frozen, signed
result (see awards.podium / normalize.results.current_results); the models here only store the
prize catalogue and the organizer's explicit winner pointer.
"""
from django.db import models

from events.models import Event, Track
from submissions.models import Submission


class Prize(models.Model):
    """One prize/award offered by an event.

    `position` is the podium place this prize targets (1 = first place, 2 = second, ...); 0 marks a
    special, non-podium award (e.g. "Best Use of Data") whose winner is only ever set explicitly by
    an organizer. `track` null means an event-wide prize; a track means it is scoped to that track.
    `awarded_submission` is the organizer's explicit winner pointer (nullable): when it is empty a
    podium-targeting prize can still resolve its winner live from the frozen signed result. There is
    no cross-event DB constraint tying track/awarded_submission to `event`; that same-event
    invariant is enforced in the service layer (awards.services)."""

    ext_id = models.CharField(max_length=64, unique=True)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="prizes")
    track = models.ForeignKey(Track, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="prizes")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    position = models.PositiveSmallIntegerField(default=1)
    awarded_submission = models.ForeignKey(Submission, on_delete=models.SET_NULL, null=True,
                                           blank=True, related_name="prizes")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "prize"
        ordering = ["event_id", "track_id", "position", "id"]

    def __str__(self):
        return self.name
