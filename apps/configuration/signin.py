# ABOUTME: The one refusal that is not about a value being wrong — a sign-in
# ABOUTME: change that would leave no Application Admin able to get back in.
"""Nobody may lock the last administrator out of the building.

Every other refusal in this app is recoverable: an administrator who sets a
limit too low sets it again. A sign-in change is different. Point the hosted
domain at a domain the district's staff are not in, and there is no longer
anyone who can point it back — the fix requires signing in, and signing in is
what just broke. That is a shell-on-the-host incident, which is the thing
moving settings into the application was meant to end.

So the sign-in keys carry one extra check, and it differs from a Phase A
prerequisite in the way that matters: a prerequisite is asked only when a
switch is turned **on**, while this is asked on every change to a sign-in
key, including turning one off. Turning off break-glass while Google sign-in
is already broken is exactly the change that strands everyone.

The check is deliberately generous about what counts as a way back in. It
asks whether *some* Application Admin could sign in afterwards, by any of the
three doors:

* local mode, where a district password is the credential;
* Google mode, which needs a client ID, a client secret, and an admin whose
  address is in the Workspace domain that will be accepted;
* the break-glass Django admin route, which needs both of its switches on and
  an active superuser to use it.

It refuses only when all three are shut. What it cannot see — whether Google
will actually honour the client ID, whether anyone remembers the break-glass
password — is why the refusal names the door it is counting on.
"""

from django.contrib.auth import get_user_model

#: Changing any of these is checked against the lockout rule, in both
#: directions.
SIGN_IN_KEYS: frozenset[str] = frozenset(
    {
        "PRR_AUTH_MODE",
        "PRR_GOOGLE_OIDC_CLIENT_ID",
        "PRR_GOOGLE_OIDC_CLIENT_SECRET",
        "PRR_GOOGLE_HOSTED_DOMAIN",
        "PRR_GOOGLE_OIDC_TIMEOUT_SECONDS",
        "PRR_BREAK_GLASS_PASSWORD_AUTH",
        "PRR_ENABLE_ADMIN",
    }
)

_ADMIN_GROUP = "prr-role:admin"


def _password_usable(user) -> bool:
    return user.has_usable_password()


def _granted_admin_emails() -> set[str]:
    from apps.api.models import DirectStaffRoleGrant, StaffRole

    return {
        email.strip().lower()
        for email in DirectStaffRoleGrant.objects.filter(
            active=True, role=StaffRole.ADMIN
        ).values_list("email", flat=True)
        if email and email.strip()
    }


def _application_admins():
    """Active staff who hold the Application Admin role, however they hold it.

    Three ways to be one, and all three are application records rather than
    Google facts, which is why this stays answerable during a Google outage:
    a superuser, a member of the managed admin group, or the subject of a
    direct role grant (rule 12).
    """
    User = get_user_model()
    granted_emails = _granted_admin_emails()
    for user in User.objects.filter(is_active=True, is_staff=True):
        if (
            user.is_superuser
            or user.groups.filter(name=_ADMIN_GROUP).exists()
            or (user.email or "").strip().lower() in granted_emails
        ):
            yield user


def _google_admin_addresses() -> set[str]:
    """Every address Google sign-in would admit as an Application Admin.

    Narrower than :func:`_application_admins`, on purpose. A Google sign-in
    re-resolves roles at every login from Workspace group mappings and direct
    grants (``apps.api.identity.resolved_roles_for_email``); ``is_superuser``
    plays no part, and the managed admin group is rewritten from what Google
    says. So only two things count:

    * a direct grant, which counts even when no account exists yet:
      provisioning is just-in-time, and treating it as a lockout because the
      person has not signed in yet would refuse exactly the change that hands
      the district over to a new administrator;
    * an account whose admin group was written by a Google sign-in, while an
      admin group mapping still exists to write it again.

    What this cannot see is whether that person is still in the mapped group.
    The refusal names the door it is counting on for that reason.
    """
    from apps.api.models import GoogleGroupRoleMapping, StaffIdentity, StaffRole

    addresses = _granted_admin_emails()
    if not GoogleGroupRoleMapping.objects.filter(
        active=True, role=StaffRole.ADMIN
    ).exists():
        return addresses
    User = get_user_model()
    addresses.update(
        email.strip().lower()
        for email in User.objects.filter(
            is_active=True,
            is_staff=True,
            groups__name=_ADMIN_GROUP,
            staff_identity__source=StaffIdentity.Source.GOOGLE,
        ).values_list("email", flat=True)
        if email and email.strip()
    )
    return addresses


def _local_door_open() -> bool:
    return any(_password_usable(user) for user in _application_admins())


def _google_door_open(resolve, resolve_secret) -> list[str]:
    """What is missing for Google sign-in, as phrases to join into a sentence."""
    missing = []
    if not resolve("PRR_GOOGLE_OIDC_CLIENT_ID"):
        missing.append("no Google sign-in client ID is configured")
    if not resolve_secret("PRR_GOOGLE_OIDC_CLIENT_SECRET"):
        missing.append("no Google sign-in client secret is configured")
    domain = str(resolve("PRR_GOOGLE_HOSTED_DOMAIN") or "").strip().lower()
    if not domain:
        missing.append("no Workspace sign-in domain is configured")
    else:
        suffix = f"@{domain}"
        if not any(address.endswith(suffix) for address in _google_admin_addresses()):
            missing.append(
                f"no Application Admin has an address at {domain}"
            )
    return missing


def _break_glass_door_open(resolve) -> bool:
    if not resolve("PRR_BREAK_GLASS_PASSWORD_AUTH"):
        return False
    if not resolve("PRR_ENABLE_ADMIN"):
        return False
    User = get_user_model()
    return any(
        _password_usable(user)
        for user in User.objects.filter(is_active=True, is_superuser=True)
    )


def lockout_blocker(resolve, resolve_secret) -> str | None:
    """Why this sign-in change may not be saved, or ``None``.

    ``resolve`` and ``resolve_secret`` answer for the world the save would
    create, not the one it is leaving.
    """
    if str(resolve("PRR_AUTH_MODE") or "").lower() == "local":
        if _local_door_open():
            return None
        if _break_glass_door_open(resolve):
            return None
        return (
            "This change would leave nobody able to sign in: local sign-in "
            "would be the only mode, and no Application Admin has a usable "
            "password. Give an administrator a password first, or keep a "
            "working sign-in mode."
        )

    missing = _google_door_open(resolve, resolve_secret)
    if not missing:
        return None
    if _break_glass_door_open(resolve):
        return None
    return (
        "This change would leave nobody able to sign in: "
        + ", and ".join(missing)
        + ". Turn on the break-glass admin route and password sign-in first "
        "if you need to make this change anyway."
    )
