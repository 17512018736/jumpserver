# -*- coding: utf-8 -*-
#
from collections import defaultdict
from functools import partial

from django.db.models.signals import m2m_changed
from django.db.models.signals import post_save, post_delete, pre_delete
from django.dispatch import receiver

from orgs.utils import tmp_to_org
from .models import Organization, OrganizationMember
from .hands import set_current_org, Node, get_current_org
from perms.models import (AssetPermission, ApplicationPermission)
from users.models import UserGroup, User
from common.const.signals import PRE_REMOVE, POST_REMOVE
from .caches import OrgResourceStatisticsCache


@receiver(post_save, sender=Organization)
def on_org_create_or_update(sender, instance=None, created=False, **kwargs):
    if instance:
        old_org = get_current_org()
        set_current_org(instance)
        node_root = Node.org_root()
        if node_root.value != instance.name:
            node_root.value = instance.name
            node_root.save()
        set_current_org(old_org)

    if instance and not created:
        instance.expire_cache()


def _remove_users(model, users, org):
    with tmp_to_org(org):
        if not isinstance(users, (tuple, list, set)):
            users = (users, )

        m2m_model = model.users.through
        reverse = model.users.reverse
        if reverse:
            m2m_field_name = model.users.field.m2m_reverse_field_name()
        else:
            m2m_field_name = model.users.field.m2m_field_name()
        relations = m2m_model.objects.filter(**{
            'user__in': users,
            f'{m2m_field_name}__org_id': org.id
        })

        object_id_users_id_map = defaultdict(set)

        m2m_field_attr_name = f'{m2m_field_name}_id'
        for relation in relations:
            object_id = getattr(relation, m2m_field_attr_name)
            object_id_users_id_map[object_id].add(relation.user_id)

        objects = model.objects.filter(id__in=object_id_users_id_map.keys())
        send_m2m_change_signal = partial(
            m2m_changed.send,
            sender=m2m_model, reverse=reverse, model=User, using=model.objects.db
        )

        for obj in objects:
            send_m2m_change_signal(
                instance=obj,
                pk_set=object_id_users_id_map[obj.id],
                action=PRE_REMOVE
            )

        relations.delete()

        for obj in objects:
            send_m2m_change_signal(
                instance=obj,
                pk_set=object_id_users_id_map[obj.id],
                action=POST_REMOVE
            )


def _clear_users_from_org(org, users):
    """
    清理用户在该组织下的相关数据
    """
    if not users:
        return

    models = (AssetPermission, ApplicationPermission, UserGroup)

    for m in models:
        _remove_users(m, users, org)


@receiver(m2m_changed, sender=OrganizationMember)
def on_org_user_changed(action, instance, reverse, pk_set, **kwargs):
    if action == 'post_remove':
        if reverse:
            user = instance
            org_pk_set = pk_set

            orgs = Organization.objects.filter(id__in=org_pk_set)
            for org in orgs:
                if not org.members.filter(id=user.id).exists():
                    _clear_users_from_org(org, user)
        else:
            org = instance
            user_pk_set = pk_set

            leaved_users = set(pk_set) - set(org.members.filter(id__in=user_pk_set).values_list('id', flat=True))
            _clear_users_from_org(org, leaved_users)


# 缓存相关
# -----------------------------------------------------

def refresh_user_amount_on_user_create_or_delete(user_id):
    org_ids = OrganizationMember.objects.distinct().filter(
        user_id=user_id
    ).values_list('org_id', flat=True)
    for org_id in org_ids:
        org_cache = OrgResourceStatisticsCache(org_id)
        org_cache.refresh_async('users_amount')


@receiver(post_save, sender=User)
def on_user_create(sender, instance, created, **kwargs):
    if created:
        refresh_user_amount_on_user_create_or_delete(instance.id)


@receiver(pre_delete, sender=User)
def on_user_delete(sender, instance, **kwargs):
    refresh_user_amount_on_user_create_or_delete(instance.id)


@receiver(m2m_changed, sender=OrganizationMember)
def on_org_user_changed(sender, action, instance, reverse, pk_set, **kwargs):
    if not action.startswith('POST'):
        return

    if reverse:
        org_ids = pk_set
    else:
        org_ids = [instance.id]

    for org_id in org_ids:
        org_cache = OrgResourceStatisticsCache(org_id)
        org_cache.refresh_async('users_amount')


@receiver(post_save, sender=UserGroup)
def on_user_group_create(sender, instance, created, **kwargs):
    if created:
        org_cache = OrgResourceStatisticsCache(instance.org_id)
        org_cache.refresh_async('groups_amount')


@receiver(pre_delete, sender=UserGroup)
def on_user_group_delete(sender, instance, **kwargs):
    org_cache = OrgResourceStatisticsCache(instance.org_id)
    org_cache.refresh_async('groups_amount')


@receiver(post_save, sender=AssetPermission)
def on_asset_perm_create(sender, instance, created, **kwargs):
    if created:
        org_cache = OrgResourceStatisticsCache(instance.org_id)
        org_cache.refresh_async('asset_perms_amount')


@receiver(pre_delete, sender=AssetPermission)
def on_asset_perm_delete(sender, instance, **kwargs):
    org_cache = OrgResourceStatisticsCache(instance.org_id)
    org_cache.refresh_async('asset_perms_amount')
