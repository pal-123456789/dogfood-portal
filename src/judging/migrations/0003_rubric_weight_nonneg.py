# src/judging/migrations/0003_rubric_weight_nonneg.py
"""Non-negative rubric weights at the DB layer.

set_rubric_weights (judging/services.py) already rejects negative / NaN / inf / zero-sum weights,
and the admin form now validates the same -- but RubricWeight.weight is a plain FloatField an
operator could otherwise set negative straight through the admin, off the service path, silently
distorting the weighted 0..5 score every ranking rests on. This adds the deep DB backstop: weight
>= 0, refused even by a writer that bypasses the service (the same service + form + DB triple guard
the 1..5 ballot bound already uses). A single-row constraint cannot express the cross-row "weights
must not sum to zero" rule; that stays the service's job and the engine's den==0 guard. Non-destructive
(adds one constraint only); the seed installs weight=1.0, so no existing row can violate it.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("judging", "0002_ballot_versioning"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="rubricweight",
            constraint=models.CheckConstraint(condition=models.Q(weight__gte=0),
                                              name="ck_rubric_weight_nonneg"),
        ),
    ]
