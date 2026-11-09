from django.db import migrations, models


class Migration(migrations.Migration):
    """A nullable column: a brief lock, no rewrite."""

    dependencies = [("shop", "0002_widen_qty_and_index")]
    operations = [migrations.AddField(model_name="order", name="note", field=models.TextField(null=True))]
