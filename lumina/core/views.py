from django.views.generic import TemplateView


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


