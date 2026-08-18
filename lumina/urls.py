"""Top-level URL configuration.

Keeps the hardware/ prefix reserved so a future software/ app can mount
alongside without reshuffling public URLs.
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

# Mount mozilla-django-oidc routes only when the app is installed. Keycloak is the real
# authentication in production; the devstack settings drop the app because there is no
# Keycloak in compose.yaml.
_oidc_installed = "mozilla_django_oidc" in settings.INSTALLED_APPS

if _oidc_installed:
    _oidc_urls = [path("oidc/", include("mozilla_django_oidc.urls"))]
else:
    # A real password login, and *only* when OIDC is absent - production must never grow
    # one of these. Both routes take the names mozilla_django_oidc publishes
    # (``oidc_authentication_init``, ``oidc_logout``) so the base templates can link to
    # them unconditionally and neither world needs a template branch.
    #
    # This replaces a stub that redirected to /admin/login/, which looked fine because
    # the only account anybody tried it with was the seeded superuser. Django's admin
    # form refuses any account without ``is_staff``, so in devstack the seeded
    # ``reviewer`` - and every submitter or vendor account you would create to exercise
    # the submitter flows - could not log in at all. The password authenticated; the form
    # rejected it with "Please enter the correct username and password for a staff
    # account".
    _oidc_urls = [
        path(
            "oidc/authenticate/",
            auth_views.LoginView.as_view(template_name="core/devstack_login.html"),
            name="oidc_authentication_init",
        ),
        path(
            "oidc/logout/",
            auth_views.LogoutView.as_view(),
            name="oidc_logout",
        ),
    ]

urlpatterns = [
    path("admin/", admin.site.urls),
    *_oidc_urls,
    path("", include("lumina.core.urls")),
    path("hardware/", include("lumina.hardware.urls")),
    path("software/", include("lumina.software.urls")),
    path("submit/", include(("lumina.hardware.submit_urls", "submit"), namespace="submit")),
    path("vendors/", include("lumina.vendors.urls")),
    path("review/", include("lumina.review.urls")),
    path("api/v1/", include("lumina.api.urls")),
    path("my/", include("lumina.accounts.urls")),
    path("results/", include("lumina.results.urls")),
    path(
        "benchmarks/",
        include(("lumina.results.benchmark_urls", "benchmarks"), namespace="benchmarks"),
    ),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
