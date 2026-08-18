"""The sitemap, and what belongs in it.

Every published listing, because the detail pages are what this site is for and they are reachable
only by working through faceted browse pages. A crawler that never exhausts the facets never
reaches them, and a catalog whose entries cannot be found by searching for the machine is not
doing its job.

Only published rows. A draft is a 404 to anyone but its owner, and listing one would advertise a
URL that answers with an error.
"""
from __future__ import annotations

from django.contrib.sitemaps import Sitemap
from django.urls import reverse


class _Listings(Sitemap):
    changefreq = "weekly"
    priority = 0.8

    def lastmod(self, obj):
        return getattr(obj, "updated_at", None)


class SystemSitemap(_Listings):
    def items(self):
        from lumina.hardware.models import System

        return System.objects.filter(published=True).order_by("pk")


class ComponentSitemap(_Listings):
    def items(self):
        from lumina.hardware.models import Component

        return Component.objects.filter(published=True).order_by("pk")


class SoftwareSitemap(_Listings):
    def items(self):
        from lumina.software.models import Software

        return Software.objects.published().order_by("pk")


class StaticSitemap(Sitemap):
    """The pages that are not a listing: the landing page and the three browse pages.

    Named rather than discovered, because "every URL the router knows" would enrol the review
    queue, the API and the dashboard, and a sitemap is a recommendation rather than an inventory.
    """

    changefreq = "daily"
    priority = 1.0

    def items(self):
        return ["core:home", "hardware:systems", "hardware:components", "software:browse"]

    def location(self, item):
        return reverse(item)


SITEMAPS = {
    "static": StaticSitemap,
    "systems": SystemSitemap,
    "components": ComponentSitemap,
    "software": SoftwareSitemap,
}
