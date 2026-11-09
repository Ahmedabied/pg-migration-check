from django.db import models


class Order(models.Model):
    email = models.CharField(max_length=200)
    qty = models.BigIntegerField()
    note = models.TextField(null=True)

    class Meta:
        indexes = [models.Index(fields=["email"], name="order_email_idx")]
