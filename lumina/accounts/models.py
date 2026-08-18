"""Accounts models: API tokens for CLI/test-suite access.

We don't subclass AbstractUser; Django's default User is sufficient because
Keycloak is the source of truth for identity. Groups are synced by
LuminaOIDCBackend on each login.

Tokens are stored hashed (SHA-256 of the raw value); the raw value is only
surfaced at creation time via ``ApiToken.issue``. ``resolve`` takes a raw
value, hashes it, and returns the active token or None.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from enum import StrEnum

from django.conf import settings
from django.db import models
from django.utils import timezone


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TokenScope(StrEnum):
    read = "read"
    submit = "submit"


class ApiTokenQuerySet(models.QuerySet["ApiToken"]):
    def active(self) -> ApiTokenQuerySet:
        now = timezone.now()
        return self.filter(revoked_at__isnull=True).filter(
            models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now)
        )


class DeviceAuthRequestQuerySet(models.QuerySet["DeviceAuthRequest"]):
    def pending(self) -> DeviceAuthRequestQuerySet:
        return self.filter(status="pending", expires_at__gt=timezone.now())


class ApiToken(models.Model):
    # Exposed as class attributes so callers can write ``ApiToken.SCOPE_READ``
    # without importing TokenScope separately. Single source of truth is the
    # TokenScope enum.
    SCOPE_READ = TokenScope.read.value
    SCOPE_SUBMIT = TokenScope.submit.value
    SCOPE_CHOICES = [(s.value, s.value.capitalize()) for s in TokenScope]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="api_tokens",
    )
    name = models.CharField(max_length=80, help_text="User-supplied label.")
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    scopes = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    # The machine this token was issued to, captured during the device flow.
    # Submissions whose results come from a different host are refused: a
    # leaked token then cannot be used to post results from anywhere else
    # without also forging the hostname in the report, which raises the
    # effort. Blank means unbound (admin-issued tokens).
    hostname = models.CharField(max_length=120, blank=True, db_index=True)

    objects = ApiTokenQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "API token"
        verbose_name_plural = "API tokens"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.user}: {self.name}"

    @classmethod
    def issue(
        cls,
        *,
        user,
        name: str,
        scopes: list[str] | None = None,
        ttl_seconds: int | None = None,
        hostname: str = "",
    ) -> tuple[ApiToken, str]:
        # 48 bytes -> 64 URL-safe characters. Longer than strictly needed
        # against brute force, but these travel in scripts and logs, so the
        # cost of extra length is nil and it removes any doubt.
        raw = secrets.token_urlsafe(48)
        if ttl_seconds is None:
            ttl_seconds = settings.LUMINA_API_TOKEN_TTL_SECONDS
        expires_at = (
            timezone.now() + timedelta(seconds=ttl_seconds) if ttl_seconds else None
        )
        instance = cls.objects.create(
            user=user,
            name=name,
            hostname=hostname[:120],
            token_hash=_hash_token(raw),
            scopes=scopes or [cls.SCOPE_READ],
            expires_at=expires_at,
        )
        return instance, raw

    @classmethod
    def resolve(cls, raw: str) -> ApiToken | None:
        try:
            return cls.objects.active().select_related("user").get(
                token_hash=_hash_token(raw)
            )
        except cls.DoesNotExist:
            return None

    def is_active(self) -> bool:
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and self.expires_at <= timezone.now():
            return False
        return True

    def has_scope(self, scope: str) -> bool:
        return scope in (self.scopes or [])

    def revoke(self) -> None:
        self.revoked_at = timezone.now()
        self.save(update_fields=["revoked_at"])


# Excludes I/O/0/1/U - ambiguous when read aloud or typed from a terminal.
USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXYZ23456789"
USER_CODE_LENGTH = 8


class DeviceAuthStatus(StrEnum):
    pending = "pending"
    approved = "approved"
    denied = "denied"


class DeviceAuthRequest(models.Model):
    """One `alma-cert register` attempt, following RFC 8628 semantics.

    The machine under test is usually headless, so it cannot run a browser
    redirect. Instead it shows a short user code that the operator enters on
    any device while signed in to Lumina. Approval is recorded here; the
    submit-scoped token is issued lazily on the next successful poll, so no
    raw token is ever stored at rest.
    """

    STATUS_CHOICES = [(s.value, s.value.capitalize()) for s in DeviceAuthStatus]

    device_code_hash = models.CharField(max_length=64, unique=True, db_index=True)
    user_code = models.CharField(max_length=16, db_index=True)
    client_name = models.CharField(max_length=80)
    # Reported by the requesting machine and carried onto the issued token,
    # which binds that token to this host. Shown on the approval page so the
    # operator can see which machine they are authorizing.
    hostname = models.CharField(max_length=120, blank=True)
    requester_ip = models.GenericIPAddressField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=DeviceAuthStatus.pending.value
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_device_requests",
    )
    token = models.OneToOneField(
        ApiToken,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="device_request",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    last_polled_at = models.DateTimeField(null=True, blank=True)

    objects = DeviceAuthRequestQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "device authorization request"
        verbose_name_plural = "device authorization requests"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"device request {self.user_code} ({self.status})"

    @staticmethod
    def _format_user_code(raw: str) -> str:
        return f"{raw[:4]}-{raw[4:]}"

    @classmethod
    def start(
        cls, *, client_name: str, ip: str | None = None, hostname: str = ""
    ) -> tuple[DeviceAuthRequest, str]:
        raw_device_code = secrets.token_urlsafe(32)
        ttl = settings.LUMINA_DEVICE_CODE_TTL_SECONDS
        for _ in range(10):
            code = cls._format_user_code(
                "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(USER_CODE_LENGTH))
            )
            if not cls.objects.pending().filter(user_code=code).exists():
                break
        else:  # pragma: no cover - astronomically unlikely
            raise RuntimeError("could not allocate an unused user code")
        instance = cls.objects.create(
            device_code_hash=_hash_token(raw_device_code),
            user_code=code,
            client_name=client_name[:80],
            hostname=hostname[:120],
            requester_ip=ip or None,
            expires_at=timezone.now() + timedelta(seconds=ttl),
        )
        return instance, raw_device_code

    @classmethod
    def resolve(cls, raw_device_code: str) -> DeviceAuthRequest | None:
        try:
            return cls.objects.select_related("approved_by", "token").get(
                device_code_hash=_hash_token(raw_device_code)
            )
        except cls.DoesNotExist:
            return None

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= timezone.now()

    def approve(self, *, by) -> None:
        self.status = DeviceAuthStatus.approved.value
        self.approved_by = by
        self.save(update_fields=["status", "approved_by"])

    def deny(self, *, by) -> None:
        self.status = DeviceAuthStatus.denied.value
        self.approved_by = by
        self.save(update_fields=["status", "approved_by"])

    def issue_token(self) -> str:
        """Mint the submit-scoped token for an approved request, once."""
        if self.status != DeviceAuthStatus.approved.value or self.approved_by is None:
            raise ValueError("Cannot issue a token for an unapproved request.")
        if self.token_id is not None:
            raise ValueError("A token has already been issued for this request.")
        token, raw = ApiToken.issue(
            user=self.approved_by,
            name=f"CLI: {self.client_name}",
            scopes=[ApiToken.SCOPE_SUBMIT],
            ttl_seconds=settings.LUMINA_CLI_TOKEN_TTL_SECONDS,
            # Carry the requesting machine onto the token so results can only
            # be posted from the host the operator actually authorized.
            hostname=self.hostname,
        )
        self.token = token
        self.save(update_fields=["token"])
        return raw
