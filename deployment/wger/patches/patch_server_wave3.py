#!/usr/bin/env python3
"""Apply #83 native cardio and linked-video fields to the public wger server source."""
from pathlib import Path
import sys

root = Path(sys.argv[1])


def replace(path: str, old: str, new: str) -> None:
    target = root / path
    text = target.read_text()
    if text.count(old) != 1:
        raise SystemExit(f"Expected one server wave-three anchor in {path}, found {text.count(old)}")
    target.write_text(text.replace(old, new))


replace(
    "wger/manager/models/log.py",
    """    weight_target = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        verbose_name='Weight target',
        validators=[NullMinValueValidator(0)],
        null=True,
        blank=True,
    )
    \"\"\"
    Target amount of weight
    \"\"\"
""",
    """    weight_target = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        verbose_name='Weight target',
        validators=[NullMinValueValidator(0)],
        null=True,
        blank=True,
    )
    \"\"\"
    Target amount of weight
    \"\"\"

    average_speed = models.DecimalField(max_digits=8, decimal_places=2, validators=[NullMinValueValidator(0)], null=True, blank=True)
    pace = models.DecimalField(max_digits=8, decimal_places=2, validators=[NullMinValueValidator(0)], null=True, blank=True)
    incline = models.DecimalField(max_digits=6, decimal_places=2, validators=[NullMinValueValidator(0)], null=True, blank=True)
    calories = models.DecimalField(max_digits=8, decimal_places=2, validators=[NullMinValueValidator(0)], null=True, blank=True)
""",
)
replace(
    "wger/manager/models/log.py",
    """        if self.repetitions is None and self.weight is None:
            raise ValidationError('Both repetitions and weight cannot be null at the same time.')
""",
    """        if self.repetitions is None and self.weight is None and all(
            value is None for value in (self.average_speed, self.pace, self.incline, self.calories)
        ):
            raise ValidationError('A workout log must contain at least one metric.')
""",
)
replace(
    "wger/manager/api/serializers.py",
    """            'weight',
            'weight_target',
            'rir',
""",
    """            'weight',
            'weight_target',
            'average_speed',
            'pace',
            'incline',
            'calories',
            'rir',
""",
)
replace(
    "wger/exercises/models/video.py",
    """    video = models.FileField(
        verbose_name='Video',
        upload_to=exercise_video_upload_dir,
        validators=[validate_video],
    )
    \"\"\"Uploaded video\"\"\"
""",
    """    video = models.FileField(
        verbose_name='Video',
        upload_to=exercise_video_upload_dir,
        validators=[validate_video],
        blank=True,
    )
    \"\"\"Uploaded video\"\"\"

    source_url = models.URLField(verbose_name='Linked video', max_length=1000, blank=True)
""",
)
replace(
    "wger/exercises/models/video.py",
    """    def get_absolute_url(self):
        \"\"\"
        Returns the video URL
        \"\"\"
        return self.video.url
""",
    """    def get_absolute_url(self):
        \"\"\"Returns the uploaded or linked video URL.\"\"\"
        return self.video.url if self.video else self.source_url

    def clean(self):
        super().clean()
        if bool(self.video) == bool(self.source_url):
            raise ValidationError(_('Provide exactly one uploaded or linked video.'))
""",
)
replace(
    "wger/exercises/api/serializers.py",
    """        fields = (
            'id',
            'uuid',
            'exercise',
            'exercise_uuid',
            'video',
            'is_main',""",
    """        fields = (
            'id',
            'uuid',
            'exercise',
            'exercise_uuid',
            'video',
            'source_url',
            'is_main',""",
)
replace(
    "wger/exercises/api/serializers.py",
    """class ExerciseVideoSerializer(serializers.ModelSerializer):
    \"\"\"
    ExerciseVideo serializer
    \"\"\"
""",
    """class ExerciseVideoSerializer(serializers.ModelSerializer):
    \"\"\"Serializer for uploaded and linked exercise videos.\"\"\"

    video = serializers.SerializerMethodField()

    def get_video(self, obj):
        return obj.get_absolute_url()
""",
)
replace(
    "wger/exercises/api/views.py",
    """    # the video is uploaded as a file, which JSON cannot carry
    parser_classes = (MultiPartParser,)
""",
    """    # Accept uploads and JSON-linked videos.
    parser_classes = [*ModelViewSet.parser_classes, MultiPartParser]
""",
)

migration = root / "wger/manager/migrations/0030_workoutlog_cardio_metrics.py"
migration.write_text('''from django.db import migrations, models\nimport wger.manager.validators\n\n\nclass Migration(migrations.Migration):\n    dependencies = [("manager", "0029_alter_workoutsession_options_and_more")]\n    operations = [\n        migrations.AddField(model_name="workoutlog", name="average_speed", field=models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True, validators=[wger.manager.validators.NullMinValueValidator(0)])),\n        migrations.AddField(model_name="workoutlog", name="pace", field=models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True, validators=[wger.manager.validators.NullMinValueValidator(0)])),\n        migrations.AddField(model_name="workoutlog", name="incline", field=models.DecimalField(blank=True, decimal_places=2, max_digits=6, null=True, validators=[wger.manager.validators.NullMinValueValidator(0)])),\n        migrations.AddField(model_name="workoutlog", name="calories", field=models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True, validators=[wger.manager.validators.NullMinValueValidator(0)])),\n    ]\n''')
video_migration = root / "wger/exercises/migrations/0041_exercisevideo_source_url.py"
video_migration.write_text('''from django.db import migrations, models\nimport wger.exercises.models.video\n\n\nclass Migration(migrations.Migration):\n    dependencies = [("exercises", "0040_alter_exercise_license_author_and_more")]\n    operations = [\n        migrations.AlterField(model_name="exercisevideo", name="video", field=models.FileField(blank=True, upload_to=wger.exercises.models.video.exercise_video_upload_dir, validators=[wger.exercises.models.video.validate_video], verbose_name="Video")),\n        migrations.AddField(model_name="exercisevideo", name="source_url", field=models.URLField(blank=True, max_length=1000, verbose_name="Linked video")),\n    ]\n''')
print("Applied native workout metrics and linked exercise videos")
