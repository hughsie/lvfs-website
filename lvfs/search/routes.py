#!/usr/bin/python3
# -*- coding: utf-8 -*-
#
# Copyright (C) 2018 Richard Hughes <richard@hughsie.com>
#
# SPDX-License-Identifier: GPL-2.0+

from typing import List

from flask import Blueprint, request, render_template, flash, redirect, url_for
from flask_login import login_required

from sqlalchemy import func

from lvfs import db

from lvfs.components.models import ComponentGuid, ComponentKeyword, Component, ComponentIssue
from lvfs.firmware.models import Firmware
from lvfs.hash import _addr_hash
from lvfs.metadata.models import Remote
from lvfs.util import admin_login_required, _split_search_string, _get_client_address
from lvfs.vendors.models import Vendor

from .models import SearchEvent

bp_search = Blueprint('search', __name__, template_folder='templates')

@bp_search.route('/<int:search_event_id>/delete', methods=['POST'])
@login_required
@admin_login_required
def route_delete(search_event_id):
    ev = db.session.query(SearchEvent).filter(SearchEvent.search_event_id == search_event_id).first()
    if not ev:
        flash('No search found!', 'danger')
        return redirect(url_for('search.route_search'))
    db.session.delete(ev)
    db.session.commit()
    flash('Deleted search event', 'info')
    return redirect(url_for('analytics.route_search_history'))

def _add_search_event(ev):
    if db.session.query(SearchEvent).\
                        filter(SearchEvent.value == ev.value).\
                        filter(SearchEvent.addr == ev.addr).first():
        return
    db.session.add(ev)
    db.session.commit()

@bp_search.route('/firmware', methods=['GET', 'POST'])
@bp_search.route('/firmware/<int:max_results>', methods=['GET', 'POST'])
@login_required
def route_fw(max_results=100):

    if 'value' not in request.args:
        flash('No search value!', 'danger')
        return redirect(url_for('search.route_search'))
    keywords_unsafe = _split_search_string(request.args['value'])
    if not keywords_unsafe:
        keywords_unsafe = request.args['value'].split(' ')

    # never allow empty keywords
    keywords: List[str] = []
    for keyword in keywords_unsafe:
        if keyword:
            keywords.append(keyword)
    if not keywords:
        flash('No valid search value!', 'danger')
        return redirect(url_for('search.route_search'))

    # use keywords first
    fws = db.session.query(Firmware).join(Component).\
                           join(ComponentKeyword).\
                           filter(ComponentKeyword.value.in_(keywords)).\
                           distinct(ComponentKeyword.component_id).\
                           order_by(ComponentKeyword.component_id.desc(), Firmware.timestamp.desc()).\
                           limit(max_results).all()

    # try GUIDs
    if not fws:
        fws = db.session.query(Firmware).join(Component).\
                               join(ComponentGuid).\
                               filter(ComponentGuid.value.in_(keywords)).\
                               distinct(ComponentKeyword.component_id).\
                               order_by(ComponentKeyword.component_id.desc(), Firmware.timestamp.desc()).\
                               limit(max_results).all()

    # try version numbers
    if not fws:
        fws = db.session.query(Firmware).join(Component).\
                               filter(Component.version.in_(keywords)).\
                               distinct(ComponentKeyword.component_id).\
                               order_by(ComponentKeyword.component_id.desc(), Firmware.timestamp.desc()).\
                               limit(max_results).all()

    # try appstream ID
    if not fws:
        fws = db.session.query(Firmware).join(Component).\
                               filter(Component.appstream_id.startswith(keywords[0])).\
                               distinct(ComponentKeyword.component_id).\
                               order_by(ComponentKeyword.component_id.desc(), Firmware.timestamp.desc()).\
                               limit(max_results).all()

    # try CVE, e.g. CVE-2018-3646
    if not fws:
        fws = db.session.query(Firmware).join(Component).join(ComponentIssue).\
                               filter(func.lower(ComponentIssue.value).in_(keywords)).\
                               distinct(ComponentKeyword.component_id).\
                               order_by(ComponentKeyword.component_id.desc(), Firmware.timestamp.desc()).\
                               limit(max_results).all()

    # try filename (with hash)
    if not fws:
        fws = db.session.query(Firmware).\
                               filter(Firmware.filename.in_(keywords)).\
                               order_by(Firmware.timestamp.desc()).\
                               limit(max_results).all()

    # filter by ACL
    fws_safe: List[Firmware] = []
    for fw in fws:
        if fw.check_acl('@view'):
            fws_safe.append(fw)

    return render_template('firmware-search.html',
                           category='firmware',
                           state='search',
                           remote=None,
                           fws=fws_safe)

@bp_search.route('', methods=['GET', 'POST'])
@bp_search.route('/<int:max_results>', methods=['POST'])
def route_search(max_results=150):

    # no search results
    if 'value' not in request.args:
        return render_template('search.html',
                               mds=None,
                               search_size=-1,
                               keywords_good=[],
                               keywords_bad=[])

    # components that match
    keywords = _split_search_string(request.args['value'])
    ids = db.session.query(ComponentKeyword.component_id).\
                           filter(ComponentKeyword.value.in_(keywords)).\
                           group_by(ComponentKeyword.component_id).\
                           having(func.count() == len(keywords)).\
                           subquery()
    mds: List[Component] = []
    appstream_ids: List[str] = []
    vendors: List[Vendor] = []
    for md in db.session.query(Component).join(ids).\
                    join(Firmware).join(Remote).filter(Remote.is_public).\
                    order_by(Component.version.desc()).\
                    limit(max_results*4):
        if md.appstream_id in appstream_ids:
            continue
        mds.append(md)
        appstream_ids.append(md.appstream_id)
        if md.fw.vendor not in vendors:
            vendors.append(md.fw.vendor)

    # get any vendor information as a fallback
    keywords_good: List[ComponentKeyword] = []
    keywords_bad: List[ComponentKeyword] = []
    if mds:
        keywords_good.extend(keywords)
        search_method = 'FW'
    else:
        search_method = 'AND'

    # always add vendor results
    for vendor in db.session.query(Vendor).\
                        filter(Vendor.visible_for_search):
        for kw in keywords:
            if vendor.keywords:
                if kw in vendor.keywords:
                    if vendor not in vendors:
                        vendors.append(vendor)
                    if kw not in keywords_good:
                        keywords_good.append(kw)
                    break
            if vendor.display_name:
                if kw in _split_search_string(vendor.display_name):
                    if vendor not in vendors:
                        vendors.append(vendor)
                    if kw not in keywords_good:
                        keywords_good.append(kw)
                    break
    for kw in keywords:
        if not kw in keywords_good:
            keywords_bad.append(kw)

    # this seems like we're over-logging but I'd like to see how people are
    # searching so we can tweak the algorithm used
    _add_search_event(SearchEvent(value=request.args['value'],
                                  addr=_addr_hash(_get_client_address()),
                                  count=len(mds) + len(vendors),
                                  method=search_method))

    return render_template('search.html',
                           mds=mds[:max_results],
                           search_size=len(mds),
                           vendors=vendors,
                           keywords_good=keywords_good,
                           keywords_bad=keywords_bad)
