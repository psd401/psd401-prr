from rest_framework.throttling import AnonRateThrottle, UserRateThrottle

from apps.configuration import runtime


class StaffLoginThrottle(AnonRateThrottle):
    scope = "staff_login"

    def get_rate(self):
        return runtime.get("PRR_STAFF_LOGIN_RATE")


class PersonalAccessTokenCreateThrottle(UserRateThrottle):
    scope = "personal_token_create"

    def get_rate(self):
        return runtime.get("PRR_PERSONAL_TOKEN_CREATE_RATE")
