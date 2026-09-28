from django.db import migrations, models
import wger.manager.validators


class Migration(migrations.Migration):
    dependencies = [("manager", "0029_alter_workoutsession_options_and_more")]
    operations = [
        migrations.AddField(model_name="workoutlog", name="average_speed", field=models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True, validators=[wger.manager.validators.NullMinValueValidator(0)])),
        migrations.AddField(model_name="workoutlog", name="pace", field=models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True, validators=[wger.manager.validators.NullMinValueValidator(0)])),
        migrations.AddField(model_name="workoutlog", name="incline", field=models.DecimalField(blank=True, decimal_places=2, max_digits=6, null=True, validators=[wger.manager.validators.NullMinValueValidator(0)])),
        migrations.AddField(model_name="workoutlog", name="calories", field=models.DecimalField(blank=True, decimal_places=2, max_digits=8, null=True, validators=[wger.manager.validators.NullMinValueValidator(0)])),
    ]
