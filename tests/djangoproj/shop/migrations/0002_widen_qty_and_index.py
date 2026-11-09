from django.db import migrations, models


class Migration(migrations.Migration):
    """Looks harmless in review: a wider column and an index. Both lock the live table."""

    dependencies = [("shop", "0001_initial")]
    operations = [
        migrations.AlterField(model_name="order", name="qty", field=models.BigIntegerField()),
        migrations.AddIndex(model_name="order", index=models.Index(fields=["email"], name="order_email_idx")),
    ]
