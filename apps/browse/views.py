import logging
import collections

from django import http
from django.conf import settings
from django.db.models import Q
from django.http import HttpResponsePermanentRedirect
from django.shortcuts import get_object_or_404
from django.views.decorators.cache import cache_page

import elasticutils
import jingo
import product_details
from mobility.decorators import mobile_template
from tower import ugettext_lazy as _lazy

import amo.utils
from amo.models import manual_order
from addons.models import Addon, Category, AddonCategory
from amo.urlresolvers import reverse
from addons.views import BaseFilter
from translations.query import order_by_translation


search_log = logging.getLogger('z.es')


class ES(object):

    def __init__(self, type_):
        self.type = type_
        self.filters = {}
        self.in_ = {}
        self.query = {}
        self.fields = ['id']
        self.ordering = []
        self.start = 0
        self.stop = None
        self._results_cache = None

    def _clone(self):
        new = self.__class__(self.type)
        new.filters = dict(self.filters)
        new.in_ = dict(self.in_)
        new.query = dict(self.query)
        new.fields = list(self.fields)
        new.ordering = list(self.ordering)
        new.start = self.start
        new.stop = self.stop
        return new

    def values(self, *fields):
        new = self._clone()
        new.fields.extend(fields)
        return new

    def order_by(self, *fields):
        new = self._clone()
        for field in fields:
            if field.startswith('-'):
                new.ordering.append({field[1:]: 'desc'})
            else:
                new.ordering.append(field)
        return new

    def search(self, **kw):
        new = self._clone()
        new.query.update(kw)
        return new

    def filter(self, **kw):
        new = self._clone()
        for key, value in kw.items():
            if key.endswith('__in'):
                new.in_[key[:-4]] = value
            else:
                new.filters[key] = value
        return new

    def count(self):
        num = self._do_search().count
        self._results_cache = None
        return num

    def __getitem__(self, k):
        # TODO: validate numbers and ranges
        if isinstance(k, slice):
            self.start, self.stop = k.start, k.stop
            return self
        else:
            self.start, self.stop = k, k + 1
            return list(self)[0]

    def _do_search(self):
        if not self._results_cache:
            qs = {}
            if self.query:
                qs['query'] = {'term': self.query}

            if len(self.filters) + len(self.in_) > 1:
                qs['filter'] = {'and': []}
                and_ = qs['filter']['and']
                for key, value in self.filters.items():
                    and_.append({'term': {key: value}})
                for key, value in self.in_.items():
                    and_.append({'in': {key: value}})
            elif self.filters:
                qs['filter'] = {'term': self.filters}
            elif self.in_:
                qs['filter'] = {'in': self.in_}

            if self.fields:
                qs['fields'] = self.fields
            if self.start:
                qs['from'] = self.start
            if self.stop:
                qs['size'] = self.stop - self.start
            if self.order_by:
                qs['sort'] = self.ordering

            es = elasticutils.get_es()
            search_log.debug(qs)
            hits = es.search(qs, settings.ES_INDEX, self.type._meta.app_label)
            self._results_cache = SearchResults(self.type, hits)
        return self._results_cache

    def __iter__(self):
        return iter(self._do_search())


class SearchResults(object):

    def __init__(self, type, results):
        self.type = type
        self.took = results['took']
        search_log.debug('Query took %dms.' % self.took)
        self.count = results['hits']['total']
        ids = [r['fields']['id'] for r in results['hits']['hits']]
        self.objects = manual_order(self.type.objects, ids)
        self.results = results

    def __iter__(self):
        return iter(self.objects)

    def __len__(self):
        return len(self.objects)


languages = dict((lang.lower(), val)
                 for lang, val in product_details.languages.items())


def locale_display_name(locale):
    """
    Return (english name, native name) for the locale.

    Raises KeyError if the locale can't be found.
    """
    if not locale:
        raise KeyError

    if locale.lower() in languages:
        v = languages[locale.lower()]
        return v['English'], v['native']
    else:
        # Take out the regional portion and try again.
        hyphen = locale.rfind('-')
        if hyphen == -1:
            raise KeyError
        else:
            return locale_display_name(locale[:hyphen])


Locale = collections.namedtuple('Locale', 'locale display native dicts packs')


class AddonFilter(BaseFilter):
    opts = (('name', _lazy(u'Name')),
            ('updated', _lazy(u'Updated')),
            ('created', _lazy(u'Created')),
            ('popular', _lazy(u'Downloads')),
            ('users', _lazy(u'Users')),
            ('rating', _lazy(u'Rating')))


def addon_listing(request, addon_types, Filter=AddonFilter, default='popular'):
    # Set up the queryset and filtering for themes & extension listing pages.
    status = [amo.STATUS_PUBLIC, amo.STATUS_LITE,
              amo.STATUS_LITE_AND_NOMINATED]

    qs = (Addon.objects.listed(request.APP, *status)
          .filter(type__in=addon_types))

    filter = Filter(request, qs, 'sort', default)
    return filter.qs, filter


def _get_locales(addons):
    """Does the heavy lifting for language_tools."""
    # This is a generator so we can {% cache addons %} in the template without
    # running any of this code.
    for addon in addons:
        locale = addon.target_locale.lower()
        try:
            english, native = locale_display_name(locale)
            # Add the locale as a differentiator if we had to strip the
            # regional portion.
            if locale not in languages:
                native = '%s (%s)' % (native, locale)
            addon.locale_display, addon.locale_native = english, native
        except KeyError:
            english = u'%s (%s)' % (addon.name, locale)
            addon.locale_display, addon.locale_native = english, ''

    # We don't need the whole add-on so only store the parts in use.
    def slim(addon):
        return {'slug': addon.slug,
                'file_size': addon.current_version.all_files[0].size,
                'locale_disambiguation': addon.locale_disambiguation}

    locales = {}
    for locale, addons in amo.utils.sorted_groupby(addons, 'target_locale'):
        addons = list(addons)
        dicts = [slim(a) for a in addons if a.type == amo.ADDON_DICT]
        packs = [slim(a) for a in addons if a.type == amo.ADDON_LPAPP]
        addon = addons[0]
        locales[locale] = Locale(addon.target_locale, addon.locale_display,
                                 addon.locale_native, dicts, packs)

    for locale in sorted(locales.items(), key=lambda x: x[1].display):
        yield locale


# We never use the category, but this makes it
# uniform with the other type listings.
def language_tools(request, category=None):
    types = (amo.ADDON_DICT, amo.ADDON_LPAPP)
    addons = (Addon.objects.public()
              .filter(appsupport__app=request.APP.id, type__in=types,
                      target_locale__isnull=False).exclude(target_locale=''))
    locales = _get_locales(addons)
    return jingo.render(request, 'browse/language_tools.html',
                        {'locales': locales, 'addons': addons,
                         'search_cat': '%s,0' % amo.ADDON_DICT})


def themes(request, category=None):
    q = Category.objects.filter(application=request.APP.id,
                                type=amo.ADDON_THEME)
    categories = order_by_translation(q, 'name')

    addons, filter = addon_listing(request, [amo.ADDON_THEME])

    if category is not None:
        try:
            category = dict((c.slug, c) for c in categories)[category]
        except KeyError:
            raise http.Http404()
        addons = addons.filter(categories__id=category.id)

    themes = amo.utils.paginate(request, addons, count=addons.count())
    return jingo.render(request, 'browse/themes.html',
                        {'categories': categories,
                         'themes': themes, 'category': category,
                         'sorting': filter.field,
                         'sort_opts': filter.opts,
                         'search_cat': '%s,0' % amo.ADDON_THEME})


@mobile_template('browse/{mobile/}extensions.html')
def extensions(request, category=None, template=None):
    TYPE = amo.ADDON_EXTENSION

    if category is not None:
        q = Category.objects.filter(application=request.APP.id, type=TYPE)
        category = get_object_or_404(q, slug=category)

    if ('sort' not in request.GET and not request.MOBILE
        and category and category.count > 4):
        return category_landing(request, category)

    addons, filter = addon_listing(request, [TYPE])

    if category:
        addons = addons.filter(categories__id=category.id)

    addons = amo.utils.paginate(request, addons, count=addons.count())
    return jingo.render(request, template,
                        {'category': category, 'addons': addons,
                         'sorting': filter.field,
                         'sort_opts': filter.opts,
                         'search_cat': '%s,0' % TYPE})


@mobile_template('browse/{mobile/}extensions.html')
def es_extensions(request, category=None, template=None):
    TYPE = amo.ADDON_EXTENSION

    if category is not None:
        q = Category.objects.filter(application=request.APP.id, type=TYPE)
        category = get_object_or_404(q, slug=category)

    if ('sort' not in request.GET and not request.MOBILE
        and category and category.count > 4):
        return category_landing(request, category)

    opts = AddonFilter.opts
    sorting = request.GET['sort']
    sorting = sorting if sorting in dict(opts) else 'popular'
    # Translate user-facing sorts to the backend fields.
    sort_field = {'name': 'name',
                  'created': '-created',
                  'updated': '-last_updated',
                  'popular': '-weekly_downloads',
                  'users': '-average_daily_users',
                  'rating': '-bayesian_rating'}.get(sorting)

    qs = (ES(Addon).filter(type=TYPE, app=request.APP.id,
                           status__in=amo.REVIEWED_STATUSES)
          .order_by(sort_field))
    if category:
        qs = qs.filter(category=category.id)
    addons = amo.utils.paginate(request, qs)

    return jingo.render(request, template,
                        {'category': category, 'addons': addons,
                         'sorting': sorting, 'sort_opts': opts,
                         'search_cat': '%s,0' % TYPE})


class CategoryLandingFilter(BaseFilter):

    opts = (('featured', _lazy(u'Featured')),
            ('created', _lazy(u'Recently Added')),
            ('popular', _lazy(u'Top Downloads')),
            ('rating', _lazy(u'Top Rated')))

    def __init__(self, request, base, category, key, default):
        self.category = category
        self.ids = AddonCategory.creatured_random(category, request.LANG)
        super(CategoryLandingFilter, self).__init__(request, base, key,
                                                    default)

    def filter_featured(self):
        # Never fear this will get & with manual order and the base.
        return Addon.objects.all()

    def order_featured(self, filter):
        return manual_order(filter, self.ids, pk_name='addons.id')


def category_landing(request, category):
    base = (Addon.objects.listed(request.APP).exclude(type=amo.ADDON_PERSONA)
            .filter(categories__id=category.id))
    filter = CategoryLandingFilter(request, base, category,
                                   key='browse', default='featured')
    return jingo.render(request, 'browse/category_landing.html',
                        {'category': category, 'filter': filter,
                         'search_cat': '%s,0' % category.type})


def creatured(request, category):
    TYPE = amo.ADDON_EXTENSION
    q = Category.objects.filter(application=request.APP.id, type=TYPE)
    category = get_object_or_404(q, slug=category)
    ids = AddonCategory.creatured_random(category, request.LANG)
    addons = manual_order(Addon.objects.public(), ids)
    return jingo.render(request, 'browse/creatured.html',
                        {'addons': addons, 'category': category,
                         'sorting': 'featured'})


class PersonasFilter(BaseFilter):

    opts = (('up-and-coming', _lazy(u'Up & Coming')),
            ('created', _lazy(u'Recently Added')),
            ('popular', _lazy(u'Most Popular')),
            ('rating', _lazy(u'Top Rated')))

    def _filter(self, field):
        qs = Addon.objects
        if field == 'created':
            return (qs.order_by('-created')
                    .with_index(addons='created_type_idx'))
        elif field == 'popular':
            return (qs.order_by('-persona__popularity')
                    .with_index(personas='personas_popularity_idx'))
        elif field == 'rating':
            return (qs.order_by('-bayesian_rating')
                    .with_index(addons='rating_type_idx'))
        else:
            return (qs.order_by('-persona__movers')
                    .with_index(personas='personas_movers_idx'))


def personas_listing(request, category=None):
    # Common pieces using by browse and search.
    TYPE = amo.ADDON_PERSONA
    q = Category.objects.filter(application=request.APP.id,
                                type=TYPE)
    categories = order_by_translation(q, 'name')

    base = (Addon.objects.public().filter(type=TYPE)
            .extra(select={'_app': request.APP.id}))

    if category is not None:
        category = get_object_or_404(q, slug=category)
        base = base.filter(categories__id=category.id)

    filter = PersonasFilter(request, base, key='sort', default='up-and-coming')
    return categories, filter, base, category


def personas(request, category=None):
    categories, filter, base, category = personas_listing(request, category)

    # Pass the count from base instead of letting it come from
    # filter.qs.count() since that would join against personas.
    count = category.count if category else base.count()

    if 'sort' in request.GET or count < 5:
        template = 'grid.html'
    else:
        template = 'category_landing.html'

    addons = amo.utils.paginate(request, filter.qs, 30, count=count)
    if category:
        ids = AddonCategory.creatured_random(category, request.LANG)
        featured = manual_order(base, ids, pk_name="addons.id")
    else:
        ids = Addon.featured_random(request.APP, request.LANG)
        featured = manual_order(base, ids, pk_name="addons.id")

    is_homepage = category is None and 'sort' not in request.GET
    return jingo.render(request, 'browse/personas/' + template,
                        {'categories': categories, 'category': category,
                         'filter': filter, 'addons': addons,
                         'featured': featured, 'is_homepage': is_homepage,
                         'search_cat': 'personas'})


@cache_page(60 * 60 * 24 * 365)
def legacy_redirects(request, type_, category=None, sort=None, format=None):
    type_slug = amo.ADDON_SLUGS.get(int(type_), 'extensions')
    if not category or category == 'all':
        url = reverse('browse.%s' % type_slug)
    else:
        cat = get_object_or_404(Category.objects, id=category)
        if format == 'rss':
            url = reverse('browse.%s.rss' % type_slug, args=[cat.slug])
        else:
            url = reverse('browse.%s' % type_slug, args=[cat.slug])
    mapping = {'updated': 'updated', 'newest': 'created', 'name': 'name',
               'weeklydownloads': 'popular', 'averagerating': 'rating'}
    if 'sort' in request.GET and request.GET['sort'] in mapping:
        url += '?sort=%s' % mapping[request.GET['sort']]
    elif sort in mapping:
        url += '?sort=%s' % mapping[sort]
    return HttpResponsePermanentRedirect(url)


class SearchToolsFilter(AddonFilter):
    opts = (('featured', _lazy(u'Featured')),
            ('name', _lazy(u'Name')),
            ('updated', _lazy(u'Updated')),
            ('created', _lazy(u'Created')),
            ('popular', _lazy(u'Downloads')),
            ('users', _lazy(u'Users')),
            ('rating', _lazy(u'Rating')))

    def filter(self, field):
        """Get the queryset for the given field."""
        # Ensure that we can combine distinct filters
        # (like the featured filter)
        this_filter = self._filter(field)
        if this_filter.query.distinct:
            base_qs = self.base_queryset.distinct()
        else:
            base_qs = self.base_queryset
        return this_filter & base_qs

    def filter_featured(self):
        # Featured search add-ons in all locales:
        featured_search = Q(
            type=amo.ADDON_SEARCH,
            feature__application=self.request.APP.id)

        # Featured in the search-tools category:
        featured_search_cat = Q(
            type__in=(amo.ADDON_EXTENSION, amo.ADDON_SEARCH),
            addoncategory__category__application=self.request.APP.id,
            addoncategory__category__slug='search-tools',
            addoncategory__feature=True)

        q = Addon.objects.valid().filter(
                        featured_search | featured_search_cat)

        # Need to make the query distinct because
        # one addon can be in multiple categories (see
        # addoncategory join above)
        return q.distinct()


class SearchExtensionsFilter(AddonFilter):
    opts = (('popular', _lazy(u'Most Popular')),
            ('created', _lazy(u'Recently Added')),)


def search_tools(request, category=None):
    """View the search tools page.

    The default landing page will show you both featured
    extensions and featured search Add-ons.  However, any
    other type of sorting on this page will not show extensions.

    Since it's uncommon for a category to have
    featured add-ons the default view for a category will land you
    on popular add-ons instead.  Note also that CSS will hide the
    sort-by-featured link.
    """
    APP, TYPE = request.APP, amo.ADDON_SEARCH
    qs = Category.objects.filter(application=APP.id, type=TYPE)
    categories = order_by_translation(qs, 'name')

    types = [TYPE]
    if category:
        # Category pages do not have features.
        # Sort by popular add-ons instead.
        default = 'popular'
    else:
        default = 'featured'
        # When the non-category page is featured, include extensions.
        if request.GET.get('sort', default) == 'featured':
            types.append(amo.ADDON_EXTENSION)

    addons, filter = addon_listing(request, types, SearchToolsFilter, default)

    if category:
        category = get_object_or_404(qs, slug=category)
        addons = addons.filter(categories__id=category.id)

    addons = amo.utils.paginate(request, addons)

    base = (Addon.objects.listed(request.APP, amo.STATUS_PUBLIC)
                         .filter(type=amo.ADDON_EXTENSION))
    sidebar_ext = SearchExtensionsFilter(request, base, 'sort', 'popular')

    return jingo.render(request, 'browse/search_tools.html',
                        {'categories': categories, 'category': category,
                         'addons': addons, 'filter': filter,
                         'search_extensions_filter': sidebar_ext})


@mobile_template('browse/{mobile/}featured.html')
def featured(request, category=None, template=None):
    ids = Addon.featured_random(request.APP, request.LANG)
    addons = manual_order(Addon.objects.exclude(type=amo.ADDON_PERSONA), ids)
    return jingo.render(request, template, {'addons': addons})
