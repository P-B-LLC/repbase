from django.db import models


class RepbaseUser(models.Model):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    username = models.CharField(max_length=150)

    email = models.EmailField(unique=True)
    height_cm = models.PositiveIntegerField(null=True, blank=True)
    weight_kg = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)


    def __str__(self):
        return f"{self.first_name} {self.last_name}"


class Session(models.Model):
    repbase_user = models.ForeignKey(RepbaseUser,on_delete=models.DO_NOTHING)
    start_datetime = models.DateTimeField(null=True)
    end_datetime = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def duration_seconds(self):
        if self.start_datetime is None or self.end_datetime is None:
            return None

        return (self.end_datetime - self.start_datetime).total_seconds()
