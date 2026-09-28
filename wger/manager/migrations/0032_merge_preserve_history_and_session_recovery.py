from django.db import migrations


class Migration(migrations.Migration):
    """
    Joins upstream's 0030_preserve_workout_history with the local
    0030_workoutlog_cardio_metrics -> 0031_workoutsessionrecovery branch.

    Both branches only touch disjoint columns (FK on_delete vs new cardio
    fields and a new table), so no operations are needed. The local names
    are already applied on the live gym and are kept unchanged.
    """

    dependencies = [
        ('manager', '0030_preserve_workout_history'),
        ('manager', '0031_workoutsessionrecovery'),
    ]

    operations = []
