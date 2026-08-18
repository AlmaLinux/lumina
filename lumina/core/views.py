from django.conf import settings
from django.http import HttpResponse
from django.urls import reverse
from django.views.decorators.cache import cache_control
from django.views.generic import TemplateView


@cache_control(max_age=86400, public=True)
def robots_txt(request) -> HttpResponse:
    """What a crawler is allowed to fetch, and where the sitemap is.

    Two shapes, one switch. A deployment that is not the one meant to be found refuses everything;
    that is belt to the ``X-Robots-Tag`` braces in ``SearchIndexingMiddleware``, which is the half
    that actually keeps a URL out of an index rather than merely out of a crawl.

    Where indexing is allowed, the disallow list is the parts of the site that are either private
    or pointless to index: everything behind a login answers with a redirect, which wastes a crawl
    and teaches a crawler nothing.
    """
    if not settings.LUMINA_ALLOW_INDEXING:
        body = "User-agent: *\nDisallow: /\n"
    else:
        sitemap = request.build_absolute_uri(reverse("core:sitemap-index"))
        body = "\n".join([
            "User-agent: *",
            "Disallow: /admin/",
            "Disallow: /api/",
            "Disallow: /my/",
            "Disallow: /review/",
            "Disallow: /submit/",
            "Disallow: /oidc/",
            "",
            f"Sitemap: {sitemap}",
            "",
        ])
    return HttpResponse(body, content_type="text/plain; charset=utf-8")


class HomeView(TemplateView):
    template_name = "core/home.html"

    def get_context_data(self, **kwargs):
        from lumina.results.highlights import attach_headlines
        from lumina.results.models import RunType, TestRun
        from lumina.results.services import apply_alias_kinds
        from lumina.software.highlights import recently_confirmed, recently_validated

        ctx = super().get_context_data(**kwargs)
        public = TestRun.objects.public()
        # apply_alias_kinds resolves corrected machine kinds for the whole list
        # in one query; display_name reads them, and without it each row would
        # go to the alias table on its own.
        ctx["recent_validations"] = apply_alias_kinds(
            public.filter(run_type=RunType.validate.value)
            .select_related("alma_release")
            # The PASS/FAIL badge calls verdict(), which without this is an
            # EXISTS query per row.
            .prefetch_related("results")
            .order_by("-published_at")[:6]
        )
        # attach_headlines picks the few metrics a feed row shows. The template
        # used to loop every primary metric inline, which on a full benchmark
        # run is seventeen dotted identifiers run together as one paragraph.
        ctx["recent_benchmarks"] = attach_headlines(
            apply_alias_kinds(
                public.with_benchmarks()
                .select_related("alma_release")
                .prefetch_related("benchmarks")
                .order_by("-published_at")[:5]
            )
        )
        # The software half of the page. The catalog covers both now, so a home
        # page that only ever showed test runs and benchmarks was describing half
        # the site.
        ctx["recent_software"] = recently_validated()
        ctx["recent_confirmations"] = recently_confirmed()
        return ctx


