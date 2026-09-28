import wger.exercises.models.video
from django.db import migrations, models


class Migration(migrations.Migration):
    """
    0041 added source_url to ExerciseVideo but not to its simple_history
    table, so saving any video failed once history tried to record it.
    """

    dependencies = [
        ('exercises', '0041_exercisevideo_source_url'),
    ]

    operations = [
        migrations.AddField(
            model_name='historicalexercisevideo',
            name='source_url',
            field=models.URLField(blank=True, max_length=1000, verbose_name='Linked video'),
        ),
        migrations.AlterField(
            model_name='historicalexercisevideo',
            name='video',
            field=models.TextField(
                blank=True,
                max_length=100,
                validators=[wger.exercises.models.video.validate_video],
                verbose_name='Video',
            ),
        ),
    ]
