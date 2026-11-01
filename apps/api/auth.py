# ABOUTME: Session-auth endpoints for the staff SPA: local login (staff-only),
# ABOUTME: logout, and current-user lookup alongside deployed Google SSO.
from django.contrib.auth import authenticate, login, logout
from django.http import Http404
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.configuration import runtime

from .identity import staff_roles
from .models import StaffIdentity
from .throttles import StaffLoginThrottle


def user_payload(user):
    return {
        "authenticated": True,
        "username": user.username,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "is_superuser": user.is_superuser,
        "roles": sorted(staff_roles(user)),
    }


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(max_length=4096, trim_whitespace=False)


class StaffUserSerializer(serializers.Serializer):
    authenticated = serializers.BooleanField()
    username = serializers.CharField()
    email = serializers.EmailField(allow_blank=True)
    first_name = serializers.CharField(allow_blank=True)
    last_name = serializers.CharField(allow_blank=True)
    is_superuser = serializers.BooleanField()
    roles = serializers.ListField(child=serializers.CharField())


class AuthDetailSerializer(serializers.Serializer):
    detail = serializers.CharField()


@method_decorator(csrf_protect, name="dispatch")
class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [StaffLoginThrottle]

    @extend_schema(
        request=LoginSerializer,
        responses={200: StaffUserSerializer, 400: AuthDetailSerializer},
    )
    def post(self, request):
        # The route exists in every mode now, because the mode is a setting
        # that changes without a redeploy. What decides is the store, read
        # on the request: outside local sign-in this endpoint is not there
        # as far as any caller can tell.
        if str(runtime.get("PRR_AUTH_MODE")).lower() != "local":
            raise Http404
        ser = LoginSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = authenticate(request,
                            username=ser.validated_data["username"],
                            password=ser.validated_data["password"])
        if user is None or not user.is_staff:
            return Response({"detail": "Invalid username or password."},
                            status=status.HTTP_400_BAD_REQUEST)
        login(request, user)
        StaffIdentity.objects.get_or_create(
            user=user,
            defaults={"source": StaffIdentity.Source.LOCAL},
        )
        return Response(user_payload(user))


class LogoutView(APIView):
    permission_classes = [IsAdminUser]

    @extend_schema(request=None, responses={200: AuthDetailSerializer})
    def post(self, request):
        logout(request)
        return Response({"detail": "Logged out."})


class MeView(APIView):
    permission_classes = [IsAdminUser]

    @method_decorator(ensure_csrf_cookie)
    @extend_schema(responses={200: StaffUserSerializer})
    def get(self, request):
        return Response(user_payload(request.user))
