#!/usr/bin/python3
# -*- coding: utf-8 -*-
#
# Copyright (C) 2019-2020 Richard Hughes <richard@hughsie.com>
#
# SPDX-License-Identifier: GPL-2.0+
#
# pylint: disable=singleton-comparison

import datetime
from typing import Dict, Optional, Tuple

from lvfs import db, tq

from lvfs.dbutils import _execute_count_star

from lvfs.util import _get_datestr_from_datetime

from lvfs.firmware.models import Firmware
from lvfs.main.models import Client
from lvfs.metadata.models import Remote
from lvfs.vendors.models import Vendor

from .models import AnalyticFirmware, Analytic, AnalyticVendor, AnalyticUseragent, AnalyticUseragentKind

def _generate_stats_for_vendor(v: Vendor, datestr: int) -> None:

    # is datestr older than firmware
    if not v.ctime:
        return
    if datestr < _get_datestr_from_datetime(v.ctime - datetime.timedelta(days=1)):
        return

    # get all the firmware for a specific vendor
    fw_ids = [fw.firmware_id for fw in v.fws]
    if not fw_ids:
        return

    # count how many times any of the firmware files were downloaded
    cnt = _execute_count_star(db.session.query(Client).\
                    filter(Client.firmware_id.in_(fw_ids)).\
                    filter(Client.datestr == datestr))
    analytic = AnalyticVendor(vendor_id=v.vendor_id, datestr=datestr, cnt=cnt)
    print('adding %s:%s = %i' % (datestr, v.group_id, cnt))
    db.session.add(analytic)

def _generate_stats_for_firmware(fw: Firmware, datestr: int) -> None:

    # is datestr older than firmware
    if datestr < _get_datestr_from_datetime(fw.timestamp):
        return

    # count how many times any of the firmware files were downloaded
    cnt = _execute_count_star(db.session.query(Client).\
                    filter(Client.firmware_id == fw.firmware_id).\
                    filter(Client.datestr == datestr))
    analytic = AnalyticFirmware(firmware_id=fw.firmware_id, datestr=datestr, cnt=cnt)
    db.session.add(analytic)

def _get_app_from_ua(ua: str) -> str:
    # always exists
    return ua.split(' ')[0]

def _get_fwupd_from_ua(ua: str) -> str:
    for part in ua.split(' '):
        if part.startswith('fwupd/'):
            return part[6:]
    return 'Unknown'

def _get_lang_distro_from_ua(ua: str) -> Optional[Tuple[str, str]]:
    start = ua.find('(')
    end = ua.rfind(')')
    if start == -1 or end == -1:
        return None
    parts = ua[start+1:end].split('; ')
    if len(parts) != 3:
        return None
    return (parts[1], parts[2])

def _generate_stats_for_datestr(datestr: int) -> None:

    # update AnalyticVendor
    for analytic in db.session.query(AnalyticVendor).filter(AnalyticVendor.datestr == datestr):
        db.session.delete(analytic)
    db.session.commit()
    for v in db.session.query(Vendor):
        _generate_stats_for_vendor(v, datestr)
    db.session.commit()

    # update AnalyticFirmware
    for analytic in db.session.query(AnalyticFirmware).filter(AnalyticFirmware.datestr == datestr):
        db.session.delete(analytic)
    db.session.commit()
    for firmware_id, in db.session.query(Firmware.firmware_id)\
                                  .join(Remote)\
                                  .filter(Remote.name != 'deleted'):
        fw = db.session.query(Firmware)\
                       .filter(Firmware.firmware_id == firmware_id)\
                       .one()
        _generate_stats_for_firmware(fw, datestr)
    db.session.commit()

    # update AnalyticUseragent
    for agnt in db.session.query(AnalyticUseragent).filter(AnalyticUseragent.datestr == datestr):
        db.session.delete(agnt)
    db.session.commit()
    ua_apps: Dict[str, int] = {}
    ua_fwupds: Dict[str, int] = {}
    ua_distros: Dict[str, int] = {}
    ua_langs: Dict[str, int] = {}
    clients = db.session.query(Client.user_agent).\
                    filter(Client.datestr == datestr).all()
    for res in clients:
        ua = res[0]
        if not ua:
            continue

        # downloader app
        ua_app = _get_app_from_ua(ua)
        if ua_app not in ua_apps:
            ua_apps[ua_app] = 1
        else:
            ua_apps[ua_app] += 1

        # fwupd version
        ua_fwupd = _get_fwupd_from_ua(ua)
        if ua_fwupd not in ua_fwupds:
            ua_fwupds[ua_fwupd] = 1
        else:
            ua_fwupds[ua_fwupd] += 1

        # language and distro
        ua_lang_distro = _get_lang_distro_from_ua(ua)
        if ua_lang_distro:
            ua_lang = ua_lang_distro[0]
            ua_distro = ua_lang_distro[1]
            if ua_lang not in ua_langs:
                ua_langs[ua_lang] = 1
            else:
                ua_langs[ua_lang] += 1
            if ua_distro not in ua_distros:
                ua_distros[ua_distro] = 1
            else:
                ua_distros[ua_distro] += 1
    for ua in ua_apps:
        db.session.add(AnalyticUseragent(kind=int(AnalyticUseragentKind.APP),
                                         value=ua, datestr=datestr, cnt=ua_apps[ua]))
    for ua in ua_fwupds:
        db.session.add(AnalyticUseragent(kind=int(AnalyticUseragentKind.FWUPD),
                                         value=ua, datestr=datestr, cnt=ua_fwupds[ua]))
    for ua in ua_langs:
        db.session.add(AnalyticUseragent(kind=int(AnalyticUseragentKind.LANG),
                                         value=ua, datestr=datestr, cnt=ua_langs[ua]))
    for ua in ua_distros:
        db.session.add(AnalyticUseragent(kind=int(AnalyticUseragentKind.DISTRO),
                                         value=ua, datestr=datestr, cnt=ua_distros[ua]))
    db.session.commit()

    # update Analytic
    analytic = db.session.query(Analytic).filter(Analytic.datestr == datestr).first()
    if analytic:
        db.session.delete(analytic)
        db.session.commit()
    db.session.add(Analytic(datestr=datestr, cnt=len(clients)))
    db.session.commit()

    # for the log
    print('generated for %s' % datestr)

@tq.task(max_retries=3, default_retry_delay=600, task_time_limit=600)
def _async_generate_stats():
    datestr = _get_datestr_from_datetime(datetime.date.today() - datetime.timedelta(days=1))
    _generate_stats_for_datestr(datestr)
