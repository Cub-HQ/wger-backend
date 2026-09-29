# Additive, nullable only: no defaults, no data steps, existing values untouched (wger-gym#18).

import django.db.models.deletion
import wger.manager.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0029_userprofile_timezone'),
        ('manager', '0031_workoutsessionrecovery'),
    ]

    operations = [
        migrations.AddField(
            model_name='workoutlog',
            name='distance',
            field=models.DecimalField(
                blank=True,
                decimal_places=3,
                max_digits=8,
                null=True,
                validators=[wger.manager.validators.NullMinValueValidator(0)],
            ),
        ),
        migrations.AddField(
            model_name='workoutlog',
            name='distance_unit',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='+',
                to='core.repetitionunit',
            ),
        ),
        migrations.AddField(
            model_name='workoutlog',
            name='duration',
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text='Seconds',
                max_digits=7,
                null=True,
                validators=[wger.manager.validators.NullMinValueValidator(0)],
            ),
        ),
        migrations.AddField(
            model_name='workoutlog',
            name='level',
            field=models.DecimalField(
                blank=True,
                decimal_places=1,
                help_text='Unitless machine level, not incline or RiR',
                max_digits=4,
                null=True,
                validators=[wger.manager.validators.NullMinValueValidator(0)],
            ),
        ),
        migrations.AddField(
            model_name='workoutlog',
            name='max_speed',
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text='Maximum speed, independent of weight',
                max_digits=6,
                null=True,
                validators=[wger.manager.validators.NullMinValueValidator(0)],
            ),
        ),
        migrations.AddField(
            model_name='workoutlog',
            name='max_speed_unit',
            field=models.ForeignKey(
                blank=True,
                help_text='Speed unit only: 5 = km/h (default in the UI), 6 = mph',
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='+',
                to='core.weightunit',
            ),
        ),
    ]
