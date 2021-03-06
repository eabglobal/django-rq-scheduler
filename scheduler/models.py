from __future__ import unicode_literals
import importlib
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.templatetags.tz import utc
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _

import django_rq
from model_utils import Choices
from model_utils.models import TimeStampedModel


@python_2_unicode_compatible
class BaseJob(TimeStampedModel):

    name = models.CharField(_('name'), max_length=128, unique=True)
    callable = models.CharField(_('callable'), max_length=2048)
    enabled = models.BooleanField(_('enabled'), default=True)
    queue = models.CharField(_('queue'), max_length=16)
    job_id = models.CharField(
        _('job id'), max_length=128, editable=False, blank=True, null=True)
    scheduled_time = models.DateTimeField(_('scheduled time'))
    timeout = models.IntegerField(
        _('timeout'), blank=True, null=True,
        help_text=_(
            'Timeout specifies the maximum runtime, in seconds, for the job '
            'before it\'ll be considered \'lost\'. Blank uses the default '
            'timeout.'
        )
    )

    def __str__(self):
        return self.name

    def callable_func(self):
        path = self.callable.split('.')
        module = importlib.import_module('.'.join(path[:-1]))
        func = getattr(module, path[-1])
        if callable(func) is False:
            raise TypeError("'{}' is not callable".format(self.callable))
        return func

    def clean(self):
        self.clean_callable()
        self.clean_queue()

    def clean_callable(self):
        try:
            self.callable_func()
        except:
            raise ValidationError({
                'callable': ValidationError(
                    _('Invalid callable, must be importable'), code='invalid')
            })

    def clean_queue(self):
        queue_keys = settings.RQ_QUEUES.keys()
        if self.queue not in queue_keys:
            raise ValidationError({
                'queue': ValidationError(
                    _('Invalid queue, must be one of: {}'.format(
                        ', '.join(queue_keys))), code='invalid')
            })

    def is_scheduled(self):
        return self.job_id in self.scheduler()
    is_scheduled.short_description = _('is scheduled?')
    is_scheduled.boolean = True

    def save(self, **kwargs):
        self.unschedule()
        if self.enabled:
            self.schedule()
        super(BaseJob, self).save(**kwargs)

    def delete(self, **kwargs):
        self.unschedule()
        super(BaseJob, self).delete(**kwargs)

    def scheduler(self):
        return django_rq.get_scheduler(self.queue)

    def is_schedulable(self):
        if self.job_id:
            return False
        return self.enabled

    def schedule(self):
        if self.is_schedulable() is False:
            return False
        kwargs = {}
        if self.timeout:
            kwargs['timeout'] = self.timeout
        job = self.scheduler().enqueue_at(
            self.schedule_time_utc(), self.callable_func(),
            **kwargs
        )
        self.job_id = job.id
        return True

    def unschedule(self):
        if self.is_scheduled():
            self.scheduler().cancel(self.job_id)
        self.job_id = None
        return True

    def schedule_time_utc(self):
        return utc(self.scheduled_time)

    class Meta:
        abstract = True


class ScheduledJob(BaseJob):

    class Meta:
        verbose_name = _('Scheduled Job')
        verbose_name_plural = _('Scheduled Jobs')
        ordering = ('name', )


class RepeatableJob(BaseJob):

    UNITS = Choices(
        ('minutes', _('minutes')),
        ('hours', _('hours')),
        ('days', _('days')),
        ('weeks', _('weeks')),
    )

    interval = models.PositiveIntegerField(_('interval'))
    interval_unit = models.CharField(
        _('interval unit'), max_length=12, choices=UNITS, default=UNITS.hours
    )
    repeat = models.PositiveIntegerField(_('repeat'), blank=True, null=True)

    def interval_display(self):
        return '{} {}'.format(self.interval, self.get_interval_unit_display())

    def interval_seconds(self):
        kwargs = {
            self.interval_unit: self.interval,
        }
        return timedelta(**kwargs).total_seconds()

    def schedule(self):
        if self.is_schedulable() is False:
            return False
        kwargs = {
            'scheduled_time': self.schedule_time_utc(),
            'func': self.callable_func(),
            'interval': self.interval_seconds(),
            'repeat': self.repeat
        }
        if self.timeout:
            kwargs['timeout'] = self.timeout
        job = self.scheduler().schedule(**kwargs)
        self.job_id = job.id
        return True

    class Meta:
        verbose_name = _('Repeatable Job')
        verbose_name_plural = _('Repeatable Jobs')
        ordering = ('name', )
