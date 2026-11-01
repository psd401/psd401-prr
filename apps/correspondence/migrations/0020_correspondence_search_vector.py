from django.contrib.postgres.search import SearchVectorField
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("correspondence", "0019_automaticmessagetemplate")]

    operations = [
        migrations.CreateModel(
            name="CorrespondenceSearchChunk",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("source", models.CharField(max_length=20)),
                ("chunk_index", models.PositiveIntegerField()),
                ("search_vector", SearchVectorField(editable=False)),
                (
                    "correspondence",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="search_chunks",
                        to="correspondence.correspondence",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="correspondencesearchchunk",
            constraint=models.UniqueConstraint(
                fields=("correspondence", "source", "chunk_index"),
                name="corr_search_chunk_unique",
            ),
        ),
    ]
