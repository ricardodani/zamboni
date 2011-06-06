import collections
import itertools
import json
import os
import random
import time
from datetime import datetime, timedelta

from django.conf import settings
from django.db import models, transaction
from django.dispatch import receiver
from django.db.models import Q, Max, signals as dbsignals
from django.utils.translation import trans_real as translation

import caching.base as caching
import commonware.log
from tower import ugettext_lazy as _

import amo.models
import sharing.utils as sharing
from amo.decorators import use_master
from amo.fields import DecimalCharField
from amo.utils import (send_mail, urlparams, sorted_groupby, JSONEncoder,
                       slugify, to_language)
from amo.urlresolvers import get_outgoing_url, reverse
from addons.utils import ReverseNameLookup
from files.models import File
from reviews.models import Review
from stats.models import AddonShareCountTotal
from translations.fields import (TranslatedField, PurifiedField,
                                 LinkifiedField, Translation)
from translations.query import order_by_translation
from users.models import UserProfile, PersonaAuthor, UserForeignKey
from versions.compare import version_int
from versions.models import Version

from . import query, signals

log = commonware.log.getLogger('z.addons')


class AddonManager(amo.models.ManagerBase):

    def get_query_set(self):
        qs = super(AddonManager, self).get_query_set()
        qs = qs._clone(klass=query.IndexQuerySet)
        return qs.transform(Addon.transformer)

    def id_or_slug(self, val):
        if isinstance(val, basestring) and not val.isdigit():
            return self.filter(slug=val)
        return self.filter(id=val)

    def public(self):
        """Get public add-ons only"""
        return self.filter(self.valid_q([amo.STATUS_PUBLIC]))

    def unreviewed(self):
        """Get only unreviewed add-ons"""
        return self.filter(self.valid_q(amo.UNREVIEWED_STATUSES))

    def valid(self):
        """Get valid, enabled add-ons only"""
        return self.filter(self.valid_q(amo.LISTED_STATUSES))

    def valid_and_disabled(self):
        """Get valid, enabled and disabled add-ons."""
        statuses = list(amo.LISTED_STATUSES) + [amo.STATUS_DISABLED]
        return self.filter(_current_version__isnull=False,
                           status__in=statuses)

    def featured(self, app):
        """
        Filter for all featured add-ons for an application in all locales.
        """
        return self.valid().filter(feature__application=app.id)

    def category_featured(self):
        """Get all category-featured add-ons for ``app`` in all locales."""
        return self.filter(addoncategory__feature=True)

    def listed(self, app, *status):
        """
        Listed add-ons have a version with a file matching ``status`` and are
        not disabled.  Personas and self-hosted add-ons will be returned too.
        """
        if len(status) == 0:
            status = [amo.STATUS_PUBLIC]
        return self.filter(self.valid_q(status), appsupport__app=app.id)

    def valid_q(self, status=[], prefix=''):
        """
        Return a Q object that selects a valid Addon with the given statuses.

        An add-on is valid if not disabled and has a current version.
        ``prefix`` can be used if you're not working with Addon directly and
        need to hop across a join, e.g. ``prefix='addon__'`` in
        CollectionAddon.
        """
        if not status:
            status = [amo.STATUS_PUBLIC]

        def q(*args, **kw):
            if prefix:
                kw = dict((prefix + k, v) for k, v in kw.items())
            return Q(*args, **kw)

        return q(q(_current_version__isnull=False),
                 disabled_by_user=False, status__in=status)


class Addon(amo.models.OnChangeMixin, amo.models.ModelBase):
    STATUS_CHOICES = amo.STATUS_CHOICES.items()
    LOCALES = [(translation.to_locale(k).replace('_', '-'), v) for k, v in
               settings.LANGUAGES.items()]

    guid = models.CharField(max_length=255, unique=True, null=True)
    slug = models.CharField(max_length=30, unique=True)
    name = TranslatedField()
    default_locale = models.CharField(max_length=10,
                                      default=settings.LANGUAGE_CODE,
                                      db_column='defaultlocale')

    type = models.PositiveIntegerField(db_column='addontype_id')
    status = models.PositiveIntegerField(
        choices=STATUS_CHOICES, db_index=True, default=0)
    highest_status = models.PositiveIntegerField(
        choices=STATUS_CHOICES, default=0,
        help_text="An upper limit for what an author can change.",
        db_column='higheststatus')
    icon_type = models.CharField(max_length=25, blank=True,
                                 db_column='icontype')
    homepage = TranslatedField()
    support_email = TranslatedField(db_column='supportemail')
    support_url = TranslatedField(db_column='supporturl')
    description = PurifiedField(short=False)

    summary = LinkifiedField()
    developer_comments = PurifiedField(db_column='developercomments')
    eula = PurifiedField()
    privacy_policy = PurifiedField(db_column='privacypolicy')
    the_reason = TranslatedField()
    the_future = TranslatedField()

    average_rating = models.FloatField(max_length=255, default=0, null=True,
                                       db_column='averagerating')
    bayesian_rating = models.FloatField(default=0, db_index=True,
                                        db_column='bayesianrating')
    total_reviews = models.PositiveIntegerField(default=0,
                                                db_column='totalreviews')
    weekly_downloads = models.PositiveIntegerField(
            default=0, db_column='weeklydownloads', db_index=True)
    total_downloads = models.PositiveIntegerField(
            default=0, db_column='totaldownloads')
    hotness = models.FloatField(default=0, db_index=True)

    average_daily_downloads = models.PositiveIntegerField(default=0)
    average_daily_users = models.PositiveIntegerField(default=0)
    share_count = models.PositiveIntegerField(default=0, db_index=True,
                                              db_column='sharecount')
    last_updated = models.DateTimeField(db_index=True, null=True,
        help_text='Last time this add-on had a file/version update')
    ts_slowness = models.FloatField(db_index=True, null=True,
        help_text='How much slower this add-on makes browser ts tests. '
                  'Read as {addon.ts_slowness}% slower.')

    disabled_by_user = models.BooleanField(default=False, db_index=True,
                                           db_column='inactive')
    trusted = models.BooleanField(default=False)
    view_source = models.BooleanField(default=True, db_column='viewsource')
    public_stats = models.BooleanField(default=False, db_column='publicstats')
    prerelease = models.BooleanField(default=False)
    admin_review = models.BooleanField(default=False, db_column='adminreview')
    admin_review_type = models.PositiveIntegerField(
                                    choices=amo.ADMIN_REVIEW_TYPES.items(),
                                    default=amo.ADMIN_REVIEW_FULL)
    site_specific = models.BooleanField(default=False,
                                        db_column='sitespecific')
    external_software = models.BooleanField(default=False,
                                            db_column='externalsoftware')
    binary = models.BooleanField(default=False,
                            help_text="Does the add-on contain a binary?")
    dev_agreement = models.BooleanField(default=False,
                            help_text="Has the dev agreement been signed?")
    auto_repackage = models.BooleanField(default=True,
        help_text='Automatically upgrade jetpack add-on to a new sdk version?')

    nomination_message = models.TextField(null=True,
                                          db_column='nominationmessage')
    target_locale = models.CharField(
        max_length=255, db_index=True, blank=True, null=True,
        help_text="For dictionaries and language packs")
    locale_disambiguation = models.CharField(
        max_length=255, blank=True, null=True,
        help_text="For dictionaries and language packs")

    wants_contributions = models.BooleanField(default=False)
    paypal_id = models.CharField(max_length=255, blank=True)
    charity = models.ForeignKey('Charity', null=True)
    # TODO(jbalogh): remove nullify_invalid once remora dies.
    suggested_amount = DecimalCharField(
        max_digits=8, decimal_places=2, nullify_invalid=True, blank=True,
        null=True, help_text=_(u'Users have the option of contributing more '
                               'or less than this amount.'))

    total_contributions = DecimalCharField(max_digits=8, decimal_places=2,
                                           nullify_invalid=True, blank=True,
                                           null=True)

    annoying = models.PositiveIntegerField(
        choices=amo.CONTRIB_CHOICES, default=0,
        help_text=_(u"Users will always be asked in the Add-ons"
                     " Manager (Firefox 4 and above)"))
    enable_thankyou = models.BooleanField(default=False,
        help_text="Should the thankyou note be sent to contributors?")
    thankyou_note = TranslatedField()

    get_satisfaction_company = models.CharField(max_length=255, blank=True,
                                               null=True)
    get_satisfaction_product = models.CharField(max_length=255, blank=True,
                                               null=True)

    authors = models.ManyToManyField('users.UserProfile', through='AddonUser',
                                     related_name='addons')

    categories = models.ManyToManyField('Category', through='AddonCategory')

    dependencies = models.ManyToManyField('self', symmetrical=False,
                                          through='AddonDependency',
                                          related_name='addons')

    _current_version = models.ForeignKey(Version, related_name='___ignore',
            db_column='current_version', null=True, on_delete=models.SET_NULL)
    # This is for Firefox only.
    _backup_version = models.ForeignKey(Version, related_name='___backup',
            db_column='backup_version', null=True, on_delete=models.SET_NULL)
    _latest_version = None

    # This gets overwritten in the transformer.
    share_counts = collections.defaultdict(int)

    objects = AddonManager()

    class Meta:
        db_table = 'addons'

    def __unicode__(self):
        return u'%s: %s' % (self.id, self.name)

    def __init__(self, *args, **kw):
        super(Addon, self).__init__(*args, **kw)
        # Make sure all add-ons have a slug.  save() runs clean_slug.
        if self.id and not self.slug:
            self.save()
        self._first_category = {}

    def save(self, **kw):
        self.clean_slug()
        log.info('Setting new slug: %s => %s' % (self.id, self.slug))
        super(Addon, self).save(**kw)

    @use_master
    def clean_slug(self):
        if not self.slug:
            if not self.name:
                try:
                    name = Translation.objects.filter(id=self.name_id)[0]
                except IndexError:
                    name = str(self.id)
            else:
                name = self.name
            self.slug = slugify(name)[:27]
        if BlacklistedSlug.blocked(self.slug):
            self.slug += "~"
        qs = Addon.objects.values_list('slug', 'id')
        match = qs.filter(slug=self.slug)
        if match and match[0][1] != self.id:
            if self.id:
                prefix = '%s-%s' % (self.slug[:-len(str(self.id))], self.id)
            else:
                prefix = self.slug
            slugs = dict(qs.filter(slug__startswith='%s-' % prefix))
            slugs.update(match)
            for idx in range(len(slugs)):
                new = ('%s-%s' % (prefix, idx + 1))[:30]
                if new not in slugs:
                    self.slug = new
                    return

    @transaction.commit_on_success
    def delete(self, msg):
        from amo.helpers import absolutify
        delete_harder = self.highest_status or self.status
        if delete_harder:
            if self.guid:
                log.debug('Adding guid to blacklist: %s' % self.guid)
                BlacklistedGuid(guid=self.guid, comments=msg).save()
            log.debug('Deleting add-on: %s' % self.id)

            authors = [u.email for u in self.authors.all()]
            to = [settings.FLIGTAR]
            user = amo.get_user()
            user_str = "%s, %s (%s)" % (user.display_name or user.username,
                    user.email, user.id) if user else "Unknown"

            email_msg = u"""
            The following add-on was deleted.
            ADD-ON: %s
            URL: %s
            DELETED BY: %s
            ID: %s
            GUID: %s
            AUTHORS: %s
            TOTAL DOWNLOADS: %s
            AVERAGE DAILY USERS: %s
            NOTES: %s
            """ % (self.name, absolutify(self.get_url_path()), user_str,
                   self.id, self.guid, authors, self.total_downloads,
                   self.average_daily_users, msg)
            log.debug('Sending delete email for add-on %s' % self.id)
            subject = 'Deleting add-on %s (%d)' % (self.slug, self.id)

        rv = super(Addon, self).delete()

        # We want to ensure deletion before notification.
        if delete_harder:
            send_mail(subject, email_msg, recipient_list=to)

        return rv

    @classmethod
    def from_upload(cls, upload, platforms):
        from files.utils import parse_addon
        data = parse_addon(upload.path)
        fields = cls._meta.get_all_field_names()
        addon = Addon(**dict((k, v) for k, v in data.items() if k in fields))
        addon.status = amo.STATUS_NULL
        addon.default_locale = to_language(translation.get_language())
        addon.save()
        Version.from_upload(upload, addon, platforms)
        amo.log(amo.LOG.CREATE_ADDON, addon)
        log.debug('New addon %r from %r' % (addon, upload))
        return addon

    def flush_urls(self):
        urls = ['*/addon/%s/' % self.slug,  # Doesn't take care of api
                '*/addon/%s/developers/' % self.slug,
                '*/addon/%s/eula/*' % self.slug,
                '*/addon/%s/privacy/' % self.slug,
                '*/addon/%s/versions/*' % self.slug,
                '*/api/*/addon/%s' % self.slug,
                self.icon_url,
                self.thumbnail_url,
                ]
        urls.extend('*/user/%d/' % u.id for u in self.listed_authors)

        return urls

    def get_url_path(self, impala=False):
        if impala:
            return reverse('addons.i_detail', args=[self.slug])
        return reverse('addons.detail', args=[self.slug])

    def meet_the_dev_url(self):
        return reverse('addons.meet', args=[self.slug])

    @property
    def reviews_url(self):
        return reverse('reviews.list', args=[self.slug])

    def type_url(self):
        """The url for this add-on's AddonType."""
        return AddonType(self.type).get_url_path()

    def share_url(self):
        return reverse('addons.share', args=[self.slug])

    @amo.cached_property(writable=True)
    def listed_authors(self):
        return UserProfile.objects.filter(addons=self,
                addonuser__listed=True).order_by('addonuser__position')

    @classmethod
    def get_fallback(cls):
        return cls._meta.get_field('default_locale')

    @property
    def reviews(self):
        return Review.objects.filter(addon=self, reply_to=None)

    def get_category(self, app):
        if app in getattr(self, '_first_category', {}):
            return self._first_category[app]
        categories = list(self.categories.filter(application=app))
        return categories[0] if categories else None

    def language_ascii(self):
        return settings.LANGUAGES[translation.to_language(self.default_locale)]

    def get_version(self, backup_version=False):
        """
        Retrieves the latest version of an addon.
        firefox_limit: if specified the highest file up to but *not* including
                       this version will be found.
        """
        if self.type == amo.ADDON_PERSONA:
            return
        try:
            if self.status == amo.STATUS_PUBLIC:
                status = [self.status]
            elif self.status in (amo.STATUS_LITE,
                                 amo.STATUS_LITE_AND_NOMINATED):
                status = [amo.STATUS_PUBLIC, amo.STATUS_LITE,
                          amo.STATUS_LITE_AND_NOMINATED]
            else:
                status = amo.VALID_STATUSES

            status_list = ','.join(map(str, status))
            fltr = {'files__status__in': status}
            if backup_version:
                fltr['apps__application__id'] = amo.FIREFOX.id
                fltr['apps__min__version_int__lt'] = amo.FIREFOX.backup_version
            return self.versions.no_cache().filter(**fltr).extra(
                where=["""
                    NOT EXISTS (
                        SELECT 1 FROM versions as v2
                        INNER JOIN files AS f2 ON (f2.version_id = v2.id)
                        WHERE v2.id = versions.id
                        AND f2.status NOT IN (%s))
                    """ % status_list])[0]

        except (IndexError, Version.DoesNotExist):
            return None

    def update_version(self):
        "Returns true if we updated the field."
        backup = None
        current = self.get_version()
        if current:
            firefox_min = current.compatible_apps.get(amo.FIREFOX)
            if (firefox_min and
                firefox_min.min.version_int > amo.FIREFOX.backup_version):
                backup = self.get_version(backup_version=True)

        diff = [self._backup_version, backup, self._current_version, current]

        changed = False
        if self._backup_version != backup:
            self._backup_version = backup
            changed = True
        if self._current_version != current:
            self._current_version = current
            changed = True

        if changed:
            try:
                self.save()
                signals.version_changed.send(sender=self)
                log.debug(u'Version changed from backup: %s to %s, '
                          'current: %s to %s for addon %s'
                          % tuple(diff + [self]))
            except Exception, e:
                log.error(u'Could not save version changes backup: %s to %s, '
                          'current: %s to %s for addon %s (%s)' %
                          tuple(diff + [self, e]))

        return changed

    @property
    def latest_version(self):
        """Returns the absolutely newest version; status doesn't matter. """
        if self.type == amo.ADDON_PERSONA:
            return
        if not self._latest_version:
            try:
                self._latest_version = self.versions.order_by('-created').latest()
            except Version.DoesNotExist, e:
                pass

        return self._latest_version

    @property
    def current_version(self):
        "Returns the current_version field or updates it if needed."
        if self.type == amo.ADDON_PERSONA:
            return
        if not self._current_version:
            self.update_version()
        return self._current_version

    @property
    def backup_version(self):
        """Returns the backup version."""
        if not self._current_version:
            return
        return self._backup_version

    def get_icon_dir(self):
        return os.path.join(settings.ADDON_ICONS_PATH,
                            '%s' % (self.id / 1000))

    def get_icon_url(self, size):
        """
        Returns either the addon's icon url, or a default.
        """
        icon_type_split = []
        if self.icon_type:
            icon_type_split = self.icon_type.split('/')

        # Get the closest allowed size without going over
        if (size not in amo.ADDON_ICON_SIZES and
            size >= amo.ADDON_ICON_SIZES[0]):
            size = [s for s in amo.ADDON_ICON_SIZES if s < size][-1]
        elif size < amo.ADDON_ICON_SIZES[0]:
            size = amo.ADDON_ICON_SIZES[0]

        # Figure out what to return for an image URL
        if self.type == amo.ADDON_PERSONA:
            return self.persona.icon_url
        if not self.icon_type:
            if self.type == amo.ADDON_THEME:
                icon = amo.ADDON_ICONS[amo.ADDON_THEME]
                return settings.ADDON_ICON_BASE_URL + icon
            else:
                return '%s/%s-%s.png' % (settings.ADDON_ICONS_DEFAULT_URL,
                                         'default', size)
        elif icon_type_split[0] == 'icon':
            return '%s/%s-%s.png' % (settings.ADDON_ICONS_DEFAULT_URL,
                                     icon_type_split[1], size)
        else:
            return settings.ADDON_ICON_URL % (
                    self.id, size, int(time.mktime(self.modified.timetuple())))

    def update_status(self, using=None):
        if self.status == amo.STATUS_NULL or self.is_disabled:
            return

        def logit(reason, old=self.status):
            log.info('Changing add-on status [%s]: %s => %s (%s).'
                     % (self.id, old, self.status, reason))
            amo.log(amo.LOG.CHANGE_STATUS, self.get_status_display(), self)

        versions = self.versions.using(using)
        if not versions.exists():
            self.update(status=amo.STATUS_NULL)
            logit('no versions')
        elif not (versions.filter(files__isnull=False).exists()):
            self.update(status=amo.STATUS_NULL)
            logit('no versions with files')
        elif (self.status == amo.STATUS_PUBLIC and
             not versions.filter(files__status=amo.STATUS_PUBLIC).exists()):
            if versions.filter(files__status=amo.STATUS_LITE).exists():
                self.update(status=amo.STATUS_LITE)
                logit('only lite files')
            else:
                self.update(status=amo.STATUS_UNREVIEWED)
                logit('no reviewed files')

    @staticmethod
    def transformer(addons):
        if not addons:
            return

        addon_dict = dict((a.id, a) for a in addons)
        personas = [a for a in addons if a.type == amo.ADDON_PERSONA]
        addons = [a for a in addons if a.type != amo.ADDON_PERSONA]

        version_ids = filter(None, (a._current_version_id for a in addons))
        backup_ids = filter(None, (a._backup_version_id for a in addons))
        all_ids = set(version_ids) | set(backup_ids)
        versions = list(Version.objects.filter(id__in=all_ids).order_by()
                        .transform(Version.transformer))
        for version in versions:
            addon = addon_dict[version.addon_id]
            if addon._current_version_id == version.id:
                addon._current_version = version
            elif addon._backup_version_id == version.id:
                addon._backup_version = version
            version.addon = addon

        # Attach listed authors.
        q = (UserProfile.objects.no_cache()
             .filter(addons__in=addons, addonuser__listed=True)
             .extra(select={'addon_id': 'addons_users.addon_id',
                            'position': 'addons_users.position'}))
        q = sorted(q, key=lambda u: (u.addon_id, u.position))
        for addon_id, users in itertools.groupby(q, key=lambda u: u.addon_id):
            addon_dict[addon_id].listed_authors = list(users)

        for persona in Persona.objects.no_cache().filter(addon__in=personas):
            addon = addon_dict[persona.addon_id]
            addon.persona = persona
            addon.listed_authors = [PersonaAuthor(persona.display_username)]
            addon.weekly_downloads = persona.popularity

        # Personas need categories for the JSON dump.
        Category.transformer(personas)

        # Store creatured apps on the add-on.
        creatured = AddonCategory.creatured()
        for addon in addons:
            addon._creatured_apps = creatured.get(addon.id, [])

        # Attach sharing stats.
        sharing.attach_share_counts(AddonShareCountTotal, 'addon', addon_dict)

        # Attach previews.
        qs = Preview.objects.filter(addon__in=addons).order_by()
        qs = sorted(qs, key=lambda x: (x.addon_id, x.position, x.created))
        for addon, previews in itertools.groupby(qs, lambda x: x.addon_id):
            addon_dict[addon].all_previews = list(previews)

        # Attach _first_category for Firefox.
        for addon in addons:
            addon._first_category[amo.FIREFOX.id] = None
        qs = AddonCategory.objects.filter(
            addon__in=addon_dict, category__application=amo.FIREFOX.id)
        for cat in qs.select_related('category'):
            addon = addon_dict[cat.addon_id]
            addon._first_category[amo.FIREFOX.id] = cat.category


    @property
    def show_beta(self):
        return self.status == amo.STATUS_PUBLIC and self.current_beta_version

    @amo.cached_property
    def current_beta_version(self):
        """Retrieves the latest version of an addon, in the beta channel."""
        versions = self.versions.filter(files__status=amo.STATUS_BETA)

        if len(versions):
            return versions[0]

    @property
    def icon_url(self):
        return self.get_icon_url(32)

    @property
    def authors_other_addons(self):
        """
        Return other addons by the author(s) of this addon
        """
        return (MiniAddon.objects.valid().exclude(id=self.id)
                .filter(addonuser__listed=True,
                        authors__in=self.listed_authors).distinct())

    @property
    def contribution_url(self, lang=settings.LANGUAGE_CODE,
                         app=settings.DEFAULT_APP):
        return reverse('addons.contribute', args=[self.slug])

    @property
    def thumbnail_url(self):
        """
        Returns the addon's thumbnail url or a default.
        """
        try:
            preview = self.all_previews[0]
            return preview.thumbnail_url
        except IndexError:
            return settings.MEDIA_URL + '/img/icons/no-preview.png'

    def can_request_review(self):
        """Return the statuses an add-on can request."""
        if not File.objects.filter(version__addon=self):
            return ()
        if self.is_disabled or self.status in (amo.STATUS_PUBLIC,
                                               amo.STATUS_LITE_AND_NOMINATED):
            return ()
        elif self.status == amo.STATUS_NOMINATED:
            return (amo.STATUS_LITE,)
        elif self.status == amo.STATUS_UNREVIEWED:
            return (amo.STATUS_PUBLIC,)
        elif self.status == amo.STATUS_LITE:
            if self.days_until_full_nomination() == 0:
                return (amo.STATUS_PUBLIC,)
            else:
                # Still in preliminary waiting period...
                return ()
        else:
            return (amo.STATUS_LITE, amo.STATUS_PUBLIC)

    def days_until_full_nomination(self):
        """Returns number of days until author can request full review.

        If wait period is over or this doesn't apply at all, returns 0 days.
        An author must wait 10 days after submitting first LITE approval
        to request FULL.
        """
        if self.status != amo.STATUS_LITE:
            return 0
        # Calculate wait time from the earliest submitted version:
        qs = (File.objects.filter(version__addon=self, status=self.status)
              .order_by('created').values_list('datestatuschanged'))[:1]
        if qs:
            days_ago = datetime.now() - qs[0][0]
            if days_ago < timedelta(days=10):
                return 10 - days_ago.days
        return 0

    def is_persona(self):
        return self.type == amo.ADDON_PERSONA

    @property
    def is_disabled(self):
        """True if this Addon is disabled.

        It could be disabled by an admin or disabled by the developer
        """
        return self.status == amo.STATUS_DISABLED or self.disabled_by_user

    def is_selfhosted(self):
        return self.status == amo.STATUS_LISTED

    @property
    def is_under_review(self):
        return self.status in amo.STATUS_UNDER_REVIEW

    def is_unreviewed(self):
        return self.status in amo.UNREVIEWED_STATUSES

    def is_incomplete(self):
        return self.status == amo.STATUS_NULL

    @classmethod
    def featured(cls, app, lang):
        # Attach an instance of Feature to Addon so we get persistent
        # @cached_method caching through the request.
        if not hasattr(cls, '_feature'):
            cls._feature = Feature()
        features = cls._feature.by_app(app)
        return dict((id, locales) for id, locales in features.items()
                     if None in locales or lang in locales)

    @classmethod
    def featured_random(cls, app, lang):
        """
        Sort featured ids for app into those that support lang and
        those with no locale, then shuffle both groups.
        """
        if not hasattr(cls, '_feature'):
            cls._feature = Feature()
        features = cls._feature.by_app(app)
        no_locale, specific_locale = [], []
        for id, locales in features.items():
            if lang in locales:
                specific_locale.append(id)
            elif None in locales:
                no_locale.append(id)
        random.shuffle(no_locale)
        random.shuffle(specific_locale)
        return specific_locale + no_locale

    def is_featured(self, app, lang):
        """is add-on globally featured for this app and language?"""
        return self.id in Addon.featured(app, lang)

    def is_category_featured(self, app, lang):
        """Is add-on featured in any category for this app?"""
        return app.id in self._creatured_apps

    def has_full_profile(self):
        """Is developer profile public (completed)?"""
        return self.the_reason and self.the_future

    def has_profile(self):
        """Is developer profile (partially or entirely) completed?"""
        return self.the_reason or self.the_future

    @amo.cached_property(writable=True)
    def _creatured_apps(self):
        """Return a list of the apps where this add-on is creatured."""
        # This exists outside of is_category_featured so we can write the list
        # in the transformer and avoid repeated .creatured() calls.
        return AddonCategory.creatured().get(self.id, [])

    @amo.cached_property
    def tags_partitioned_by_developer(self):
        "Returns a tuple of developer tags and user tags for this addon."
        tags = self.tags.not_blacklisted()
        if self.is_persona:
            return models.query.EmptyQuerySet(), tags
        user_tags = tags.exclude(addon_tags__user__in=self.listed_authors)
        dev_tags = tags.exclude(id__in=[t.id for t in user_tags])
        return dev_tags, user_tags

    @amo.cached_property
    def compatible_apps(self):
        """Shortcut to get compatible apps for the current version."""
        # Search providers and personas don't list their supported apps.
        if self.type in amo.NO_COMPAT:
            return dict((app, None) for app in
                        amo.APP_TYPE_SUPPORT[self.type])
        if self.current_version:
            return self.current_version.compatible_apps
        else:
            return {}

    def accepts_compatible_apps(self):
        """True if this add-on lists compatible apps."""
        return self.type not in amo.NO_COMPAT

    def incompatible_latest_apps(self):
        """Returns a list of applications with which this add-on is
        incompatible (based on the latest version).

        """
        return [a for a, v in self.compatible_apps.items() if v and
                version_int(v.max.version) < version_int(a.latest_version)]

    @caching.cached_method
    def has_author(self, user, roles=None):
        """True if ``user`` is an author with any of the specified ``roles``.

        ``roles`` should be a list of valid roles (see amo.AUTHOR_ROLE_*). If
        not specified, has_author will return true if the user has any role.
        """
        if user is None:
            return False
        if roles is None:
            roles = dict(amo.AUTHOR_CHOICES).keys()
        return AddonUser.objects.filter(addon=self, user=user,
                                        role__in=roles).exists()

    @property
    def takes_contributions(self):
        # TODO(jbalogh): config.paypal_disabled
        return (self.status == amo.STATUS_PUBLIC and self.wants_contributions
                and (self.paypal_id or self.charity_id))

    @property
    def has_eula(self):
        return self.eula

    @classmethod
    def _last_updated_queries(cls):
        """
        Get the queries used to calculate addon.last_updated.
        """
        status_change = Max('versions__files__datestatuschanged')
        public = (Addon.uncached.filter(status=amo.STATUS_PUBLIC,
            versions__files__status=amo.STATUS_PUBLIC)
            .exclude(type=amo.ADDON_PERSONA).values('id')
            .annotate(last_updated=status_change))

        lite = (Addon.uncached.filter(status__in=amo.LISTED_STATUSES,
                                      versions__files__status=amo.STATUS_LITE)
                .values('id').annotate(last_updated=status_change))

        stati = amo.LISTED_STATUSES + (amo.STATUS_PUBLIC,)
        exp = (Addon.uncached.exclude(status__in=stati)
               .filter(versions__files__status__in=amo.VALID_STATUSES)
               .values('id')
               .annotate(last_updated=Max('versions__files__created')))

        listed = (Addon.uncached.filter(status=amo.STATUS_LISTED)
                  .values('id')
                  .annotate(last_updated=Max('versions__created')))

        personas = (Addon.uncached.filter(type=amo.ADDON_PERSONA)
                    .extra(select={'last_updated': 'created'}))
        return dict(public=public, exp=exp, listed=listed, personas=personas,
                    lite=lite)

    @amo.cached_property(writable=True)
    def all_categories(self):
        return list(self.categories.all())

    @amo.cached_property(writable=True)
    def all_previews(self):
        return list(self.previews.all())

    @property
    def app_categories(self):
        categories = sorted_groupby(order_by_translation(self.categories.all(),
                                                         'name'),
                                    key=lambda x: x.application_id)
        app_cats = []
        for app, cats in categories:
            app_cats.append((amo.APP_IDS[app], list(cats)))
        return app_cats

    def remove_locale(self, locale):
        """NULLify the add-on's strings in this locale."""
        ids = [getattr(self, f.attname) for f in self._meta.translated_fields]
        qs = Translation.objects.filter(id__in=filter(None, ids),
                                        locale=locale)
        qs.update(localized_string=None, localized_string_clean=None)


@receiver(dbsignals.post_save, sender=Addon)
def update_name_table(sender, **kw):
    from . import cron
    addon = kw['instance']
    cron._build_reverse_name_lookup.delay({addon.name_id: addon.id},
                                          clear=True)


@receiver(dbsignals.pre_delete, sender=Addon)
def clear_name_table(sender, **kw):
    addon = kw['instance']
    ReverseNameLookup.delete(addon.id)


@receiver(signals.version_changed, dispatch_uid='version_changed')
def version_changed(sender, **kw):
    from . import tasks
    tasks.version_changed.delay(sender.id)


@Addon.on_change
def watch_status(old_attr={}, new_attr={}, instance=None,
                 sender=None, **kw):
    """Set nomination date if self.status asks for full review.

    The nomination date will only be set when the status of the addon changes.
    The nomination date cannot be reset, say, when a developer cancels their
    request for full review and re-requests full review.

    If a version is rejected after nomination, the developer has to upload a
    new version.
    """
    new_status = new_attr.get('status')
    if not new_status:
        return
    addon = instance
    stati = (amo.STATUS_NOMINATED, amo.STATUS_LITE_AND_NOMINATED)
    if new_status in stati and old_attr['status'] != new_status:
        try:
            latest = addon.versions.latest()
            if not latest.nomination:
                latest.update(nomination=datetime.now())
        except Version.DoesNotExist:
            pass


@Addon.on_change
def watch_disabled(old_attr={}, new_attr={}, instance=None, sender=None, **kw):
    attrs = dict((k,v) for k, v in old_attr.items()
                 if k in ('disabled_by_user', 'status'))
    if Addon(**attrs).is_disabled and not instance.is_disabled:
        for f in File.objects.filter(version__addon=instance.id):
            f.unhide_disabled_file()
    if instance.is_disabled and not Addon(**attrs).is_disabled:
        for f in File.objects.filter(version__addon=instance.id):
            f.hide_disabled_file()


class MiniAddonManager(AddonManager):

    def get_query_set(self):
        qs = super(MiniAddonManager, self).get_query_set()
        return qs.only_translations()


class MiniAddon(Addon):
    """A smaller lightweight version of Addon suitable for the
    update script or other areas that don't need all the transforms.
    This class exists to give the addon a different key for cache machine."""

    objects = MiniAddonManager()

    class Meta:
        proxy = True


class Persona(caching.CachingMixin, models.Model):
    """Personas-specific additions to the add-on model."""
    addon = models.OneToOneField(Addon)
    persona_id = models.PositiveIntegerField(db_index=True)
    # name: deprecated in favor of Addon model's name field
    # description: deprecated, ditto
    header = models.CharField(max_length=64, null=True)
    footer = models.CharField(max_length=64, null=True)
    accentcolor = models.CharField(max_length=10, null=True)
    textcolor = models.CharField(max_length=10, null=True)
    author = models.CharField(max_length=32, null=True)
    display_username = models.CharField(max_length=32, null=True)
    submit = models.DateTimeField(null=True)
    approve = models.DateTimeField(null=True)

    movers = models.FloatField(null=True, db_index=True)
    popularity = models.IntegerField(null=False, default=0, db_index=True)
    license = models.ForeignKey('versions.License', null=True)

    objects = caching.CachingManager()

    class Meta:
        db_table = 'personas'

    def __unicode__(self):
        return unicode(self.addon.name)

    def flush_urls(self):
        urls = ['*/addon/%d/' % self.addon_id,
                '*/api/*/addon/%d' % self.addon_id,
                self.thumb_url,
                self.icon_url,
                self.preview_url,
                self.header_url,
                self.footer_url,
                self.update_url, ]

        return urls

    def _image_url(self, filename, ssl=True):
        base_url = (settings.PERSONAS_IMAGE_URL_SSL if ssl else
                    settings.PERSONAS_IMAGE_URL)
        units = self.persona_id % 10
        tens = (self.persona_id // 10) % 10
        return base_url % {
            'units': units, 'tens': tens, 'file': filename,
            'id': self.persona_id,
        }

    @amo.cached_property
    def thumb_url(self):
        """URL to Persona's thumbnail preview."""
        return self._image_url('preview.jpg')

    @amo.cached_property
    def icon_url(self):
        """URL to personas square preview."""
        return self._image_url('preview_small.jpg')

    @amo.cached_property
    def preview_url(self):
        """URL to Persona's big, 680px, preview."""
        return self._image_url('preview_large.jpg')

    @amo.cached_property
    def header_url(self):
        return self._image_url(self.header, ssl=False)

    @amo.cached_property
    def footer_url(self):
        return self._image_url(self.footer, ssl=False)

    @amo.cached_property
    def update_url(self):
        return settings.PERSONAS_UPDATE_URL % self.persona_id

    @amo.cached_property
    def json_data(self):
        """Persona JSON Data for Browser/extension preview."""
        hexcolor = lambda color: '#%s' % color
        addon = self.addon
        return json.dumps({
            'id': unicode(self.persona_id),  # Personas dislikes ints
            'name': addon.name,
            'accentcolor': hexcolor(self.accentcolor),
            'textcolor': hexcolor(self.textcolor),
            'category': (addon.all_categories[0].name if
                         addon.all_categories else ''),
            'author': self.author,
            'description': addon.description,
            'header': self.header_url,
            'footer': self.footer_url,
            'headerURL': self.header_url,
            'footerURL': self.footer_url,
            'previewURL': self.thumb_url,
            'iconURL': self.icon_url,
            'updateURL': self.update_url,
        }, separators=(',', ':'), cls=JSONEncoder)


class AddonCategory(caching.CachingMixin, models.Model):
    addon = models.ForeignKey(Addon)
    category = models.ForeignKey('Category')
    feature = models.BooleanField(default=False)
    feature_locales = models.CharField(max_length=255, default='', null=True)

    objects = caching.CachingManager()

    class Meta:
        db_table = 'addons_categories'
        unique_together = ('addon', 'category')

    def flush_urls(self):
        urls = ['*/addon/%d/' % self.addon_id,
                '*%s' % self.category.get_url_path(), ]
        return urls

    @classmethod
    def creatured_ids(cls):
        """ Returns a list of creatured ids """
        qs = cls.objects.filter(feature=True)
        f = lambda: list(qs.values_list('addon', 'category',
                                        'category__application',
                                        'feature_locales'))
        return caching.cached_with(qs, f, 'creatured')

    @classmethod
    def creatured(cls):
        """Get a dict of {addon: [app,..]} for all creatured add-ons."""
        rv = {}
        for addon, cat, catapp, locale in cls.creatured_ids():
            rv.setdefault(addon, []).append(catapp)
        return rv

    @classmethod
    def creatured_random(cls, category, lang):
        """
        Sort creatured ids for category into those that support lang and
        those with no locale, then shuffle both groups.
        """
        no_locale, specific_locale = [], []
        for addon, cat, catapp, locale in cls.creatured_ids():
            if cat != category.pk:
                continue
            if locale and lang in locale:
                specific_locale.append(addon)
            elif not locale:
                no_locale.append(addon)
        random.shuffle(no_locale)
        random.shuffle(specific_locale)
        return specific_locale + no_locale


class AddonRecommendation(models.Model):
    """
    Add-on recommendations. For each `addon`, a group of `other_addon`s
    is recommended with a score (= correlation coefficient).
    """
    addon = models.ForeignKey(Addon, related_name="addon_recommendations")
    other_addon = models.ForeignKey(Addon, related_name="recommended_for")
    score = models.FloatField()

    class Meta:
        db_table = 'addon_recommendations'
        ordering = ('-score',)

    @classmethod
    def scores(cls, addon_ids):
        """Get a mapping of {addon: {other_addon: score}} for each add-on."""
        d = {}
        q = (AddonRecommendation.objects.filter(addon__in=addon_ids)
             .values('addon', 'other_addon', 'score'))
        for addon, rows in sorted_groupby(q, key=lambda x: x['addon']):
            d[addon] = dict((r['other_addon'], r['score']) for r in rows)
        return d


class AddonType(amo.models.ModelBase):
    name = TranslatedField()
    name_plural = TranslatedField()
    description = TranslatedField()

    class Meta:
        db_table = 'addontypes'

    def __unicode__(self):
        return unicode(self.name)

    def get_url_path(self):
        try:
            type = amo.ADDON_SLUGS[self.id]
        except KeyError:
            return None
        return reverse('browse.%s' % type)


class AddonUser(caching.CachingMixin, models.Model):
    addon = models.ForeignKey(Addon)
    user = UserForeignKey()
    role = models.SmallIntegerField(default=amo.AUTHOR_ROLE_OWNER,
                                    choices=amo.AUTHOR_CHOICES)
    listed = models.BooleanField(_(u'Listed'), default=True)
    position = models.IntegerField(default=0)

    objects = caching.CachingManager()

    def __init__(self, *args, **kwargs):
        super(AddonUser, self).__init__(*args, **kwargs)
        self._original_role = self.role
        self._original_user_id = self.user_id

    class Meta:
        db_table = 'addons_users'

    def flush_urls(self):
        return self.addon.flush_urls() + self.user.flush_urls()


class AddonDependency(models.Model):
    addon = models.ForeignKey(Addon, related_name='addons_dependencies')
    dependent_addon = models.ForeignKey(Addon, related_name='dependent_on')

    class Meta:
        db_table = 'addons_dependencies'


class BlacklistedGuid(amo.models.ModelBase):
    guid = models.CharField(max_length=255, unique=True)
    comments = models.TextField(default='', blank=True)

    class Meta:
        db_table = 'blacklisted_guids'

    def __unicode__(self):
        return self.guid


class Category(amo.models.ModelBase):
    name = TranslatedField()
    slug = models.SlugField(max_length=50, help_text='Used in Category URLs.')
    description = TranslatedField()
    type = models.PositiveIntegerField(db_column='addontype_id')
    application = models.ForeignKey('applications.Application')
    count = models.IntegerField('Addon count', default=0)
    weight = models.IntegerField(default=0,
        help_text='Category weight used in sort ordering')
    misc = models.BooleanField(default=False)

    addons = models.ManyToManyField(Addon, through='AddonCategory')

    class Meta:
        db_table = 'categories'
        verbose_name_plural = 'Categories'

    def __unicode__(self):
        return unicode(self.name)

    def flush_urls(self):
        urls = ['*%s' % self.get_url_path(), ]
        return urls

    def get_url_path(self):
        try:
            type = amo.ADDON_SLUGS[self.type]
        except KeyError:
            type = amo.ADDON_SLUGS[amo.ADDON_EXTENSION]
        return reverse('browse.%s' % type, args=[self.slug])

    @staticmethod
    def transformer(addons):
        qs = (Category.uncached.filter(addons__in=addons)
              .extra(select={'addon_id': 'addons_categories.addon_id'}))
        cats = dict((addon_id, list(cs))
                    for addon_id, cs in sorted_groupby(qs, 'addon_id'))
        for addon in addons:
            addon.all_categories = cats.get(addon.id, [])


class Feature(amo.models.ModelBase):
    addon = models.ForeignKey(Addon)
    start = models.DateTimeField()
    end = models.DateTimeField()
    locale = models.CharField(max_length=10, default='', blank=True, null=True)
    application = models.ForeignKey('applications.Application')

    class Meta:
        db_table = 'features'

    def __unicode__(self):
        app = amo.APP_IDS[self.application.id].pretty
        return '%s (%s: %s)' % (self.addon.name, app, self.locale)

    @caching.cached_method
    def by_app(self, app):
        """
        Get a dict of featured add-ons for app.

        Returns: {addon_id: [featured locales]}
        """
        qs = Addon.objects.featured(app)
        vals = qs.extra(select={'locale': 'features.locale'})
        d = collections.defaultdict(list)
        for id, locale in vals.values_list('id', 'locale'):
            d[id].append(locale)
        return dict(d)


class Preview(amo.models.ModelBase):
    addon = models.ForeignKey(Addon, related_name='previews')
    filetype = models.CharField(max_length=25)
    thumbtype = models.CharField(max_length=25)
    caption = TranslatedField()

    position = models.IntegerField(default=0)

    class Meta:
        db_table = 'previews'
        ordering = ('position', 'created')

    def flush_urls(self):
        urls = ['*/addon/%d/' % self.addon_id,
                self.thumbnail_url,
                self.image_url, ]
        return urls

    def _image_url(self, url_template):
        if self.modified is not None:
            modified = int(time.mktime(self.modified.timetuple()))
        else:
            modified = 0
        return url_template % (self.id / 1000, self.id, modified)

    def _image_path(self, url_template):
        return url_template % (self.id / 1000, self.id)

    def as_dict(self, src=None):
        d = {'full': urlparams(self.image_url, src=src),
             'thumbnail': urlparams(self.thumbnail_url, src=src),
             'caption': unicode(self.caption)}
        return d

    @property
    def thumbnail_url(self):
        return self._image_url(settings.PREVIEW_THUMBNAIL_URL)

    @property
    def image_url(self):
        return self._image_url(settings.PREVIEW_FULL_URL)

    @property
    def thumbnail_path(self):
        return self._image_path(settings.PREVIEW_THUMBNAIL_PATH)

    @property
    def image_path(self):
        return self._image_path(settings.PREVIEW_FULL_PATH)


# Use pre_delete since we need to know what we want to delete in the fs.
@receiver(dbsignals.pre_delete, sender=Preview)
def delete_preview_handler(sender, instance, **kwargs):
    from . import tasks
    tasks.delete_preview_files.delay(instance.id)


class AppSupport(amo.models.ModelBase):
    """Cache to tell us if an add-on's current version supports an app."""
    addon = models.ForeignKey(Addon)
    app = models.ForeignKey('applications.Application')
    min = models.BigIntegerField("Minimum app version", null=True)
    max = models.BigIntegerField("Maximum app version", null=True)

    class Meta:
        db_table = 'appsupport'
        unique_together = ('addon', 'app')


class Charity(amo.models.ModelBase):
    name = models.CharField(max_length=255)
    url = models.URLField(verify_exists=False)
    paypal = models.CharField(max_length=255)

    class Meta:
        db_table = 'charities'

    @property
    def outgoing_url(self):
        if self.pk == amo.FOUNDATION_ORG:
            return self.url
        return get_outgoing_url(unicode(self.url))


class BlacklistedSlug(amo.models.ModelBase):
    name = models.CharField(max_length=255, unique=True, default='')

    class Meta:
        db_table = 'addons_blacklistedslug'

    def __unicode__(self):
        return self.name

    @classmethod
    def blocked(cls, slug):
        return slug.isdigit() or cls.objects.filter(name=slug).exists()


class FrozenAddon(models.Model):
    """Add-ons in this table never get a hotness score."""
    addon = models.ForeignKey(Addon)

    class Meta:
        db_table = 'frozen_addons'

    def __unicode__(self):
        return 'Frozen: %s' % self.addon_id


@receiver(dbsignals.post_save, sender=FrozenAddon)
def freezer(sender, instance, **kw):
    # Adjust the hotness of the FrozenAddon.
    if instance.addon_id:
        Addon.objects.get(id=instance.addon_id).update(hotness=0)
