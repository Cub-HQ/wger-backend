from django.db import migrations, models
import wger.exercises.models.video


class Migration(migrations.Migration):
    dependencies = [("exercises", "0040_alter_exercise_license_author_and_more")]
    operations = [
        migrations.AlterField(model_name="exercisevideo", name="video", field=models.FileField(blank=True, upload_to=wger.exercises.models.video.exercise_video_upload_dir, validators=[wger.exercises.models.video.validate_video], verbose_name="Video")),
        migrations.AddField(model_name="exercisevideo", name="source_url", field=models.URLField(blank=True, max_length=1000, verbose_name="Linked video")),
    ]
