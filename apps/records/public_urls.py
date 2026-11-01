from django.urls import path

from .public_views import PublicIntakeView

urlpatterns = [
    path("requests/", PublicIntakeView.as_view()),
]
