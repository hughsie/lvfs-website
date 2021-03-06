#!/usr/bin/python3
# -*- coding: utf-8 -*-
#
# Copyright (C) 2015-2020 Richard Hughes <richard@hughsie.com>
#
# SPDX-License-Identifier: GPL-2.0+
#
# pylint: disable=too-many-statements,too-many-locals,too-many-nested-blocks

import os
import gzip
import glob
import datetime

from collections import defaultdict
from typing import List, Tuple, Optional, Dict
from distutils.version import StrictVersion
from lxml import etree as ET

from jcat import JcatFile, JcatBlobSha1, JcatBlobSha256, JcatBlobKind

from lvfs import db, app, ploader, tq

from lvfs.components.models import Component, ComponentRequirement
from lvfs.firmware.models import Firmware
from lvfs.util import _get_settings, _xml_from_markdown, _event_log
from lvfs.verfmts.models import Verfmt

from .models import Remote

def _is_verfmt_supported_by_fwupd(md: Component, verfmt: Verfmt) -> bool:

    # fwupd version no specified
    if not verfmt.fwupd_version:
        return False

    # did the firmware specify >= a fwupd version
    req = md.find_req('id', 'org.freedesktop.fwupd')
    if not req:
        return False
    if req.compare != 'ge':
        return False

    # compare the version number for the protocol and the requirement
    try:
        if StrictVersion(req.version) >= StrictVersion(verfmt.fwupd_version):
            return True
    except ValueError as _:
        pass

    # failed
    return False

def _use_hex_release_version(md: Component) -> bool:
    if not md.version.isdigit():
        return False
    if not md.verfmt or md.verfmt.value == 'plain':
        return False
    return True

def _get_unique_requirements(mds: List[Component], kind: str):
    rqs_unique: Dict[str, ComponentRequirement] = {}
    for md in mds:
        for rq in md.requirements:
            if rq.kind != kind:
                continue
            if str(rq) in rqs_unique:
                continue
            rqs_unique[str(rq)] = rq
    return rqs_unique.values()

def _generate_metadata_mds(mds: List[Component],
                           firmware_baseuri: str = '',
                           local: bool = False,
                           metainfo: bool = False,
                           allow_unrestricted: bool = True) -> ET.Element:

    # assume all the components have the same parent firmware information
    md = mds[0]
    component = ET.Element('component')
    component.set('type', 'firmware')
    ET.SubElement(component, 'id').text = md.appstream_id
    if md.branch:
        ET.SubElement(component, 'branch').text = md.branch

    # until all front ends support <category> and <name_variant_suffix> append both */
    if metainfo:
        ET.SubElement(component, 'name').text = md.name
        if md.name_variant_suffix:
            ET.SubElement(component, 'name_variant_suffix').text = md.name_variant_suffix
    else:
        ET.SubElement(component, 'name').text = md.name_with_category
    ET.SubElement(component, 'summary').text = md.summary
    if md.description:
        component.append(_xml_from_markdown(md.description))
    for md in mds:
        if md.priority:
            component.set('priority', str(md.priority))

    # provides shared by all releases
    provides: Dict[str, str] = {}
    for md in mds:
        for guid in md.guids:
            if guid.value in provides:
                continue
            child = ET.Element('firmware')
            child.set('type', 'flashed')
            child.text = guid.value
            provides[guid.value] = child
    if provides:
        parent = ET.SubElement(component, 'provides')
        for key in sorted(provides):
            parent.append(provides[key])

    # shared again
    if md.url_homepage:
        child = ET.SubElement(component, 'url')
        child.set('type', 'homepage')
        child.text = md.url_homepage
    if md.icon:
        child = ET.SubElement(component, 'icon')
        child.set('type', 'stock')
        child.text = md.icon
    if md.metadata_license:
        ET.SubElement(component, 'metadata_license').text = md.metadata_license.value
    if md.project_license:
        ET.SubElement(component, 'project_license').text = md.project_license.value
    ET.SubElement(component, 'developer_name').text = md.developer_name

    # screenshot shared by all releases
    screenshots: Dict[str, ET.Element] = {}
    for md in mds:
        if not md.screenshot_url and not md.screenshot_caption:
            continue
        # try to dedupe using the URL and then the caption
        key = md.screenshot_url
        if not key:
            key = md.screenshot_caption
        if key not in screenshots:
            child = ET.Element('screenshot')
            if not screenshots:
                child.set('type', 'default')
            if md.screenshot_caption:
                ET.SubElement(child, 'caption').text = md.screenshot_caption
            if md.screenshot_url:
                if metainfo or not md.screenshot_url_safe:
                    ET.SubElement(child, 'image').text = md.screenshot_url
                else:
                    ET.SubElement(child, 'image').text = md.screenshot_url_safe
            screenshots[key] = child
    if screenshots:
        parent = ET.SubElement(component, 'screenshots')
        for key in screenshots:
            parent.append(screenshots[key])

    # add enumerated categories
    cats: List[str] = []
    for md in mds:
        if not md.category:
            continue
        if md.category.value not in cats:
            cats.append(md.category.value)
        if md.category.fallback and md.category.fallback.value not in cats:
            cats.append(md.category.fallback.value)
    if cats:
        # use a non-standard prefix as we're still using .name_with_category
        if metainfo:
            categories = ET.SubElement(component, 'categories')
        else:
            categories = ET.SubElement(component, 'X-categories')
        for cat in cats:
            ET.SubElement(categories, 'category').text = cat

    # metadata shared by all releases
    metadata: List[Tuple[str, Optional[str]]] = []
    for md in mds:
        if md.inhibit_download:
            metadata.append(('LVFS::InhibitDownload', None))
            break
    for md in mds:
        if md.release_message:
            metadata.append(('LVFS::UpdateMessage', md.release_message))
            if md.release_image:
                if metainfo or not md.screenshot_url_safe:
                    metadata.append(('LVFS::UpdateImage', md.release_image))
                else:
                    metadata.append(('LVFS::UpdateImage', md.release_image_safe))
            break
    for md in mds:
        verfmt = md.verfmt
        if verfmt:
            if verfmt.fallbacks and not _is_verfmt_supported_by_fwupd(md, verfmt):
                for fallback in verfmt.fallbacks.split(','):
                    metadata.append(('LVFS::VersionFormat', fallback))
            metadata.append(('LVFS::VersionFormat', verfmt.value))
            break
    for md in mds:
        if md.protocol:
            metadata.append(('LVFS::UpdateProtocol', md.protocol.value))
            break
    if metadata:
        parent = ET.SubElement(component, 'custom')
        for key, value in metadata:
            child = ET.Element('value')
            child.set('key', key)
            child.text = value
            parent.append(child)

    # add each release
    releases = ET.SubElement(component, 'releases')
    for md in mds:
        if not md.version:
            continue
        rel = ET.SubElement(releases, 'release')
        if md.version:
            if metainfo and _use_hex_release_version(md):
                rel.set('version', hex(int(md.version)))
            else:
                rel.set('version', md.version)
        if md.release_timestamp:
            if metainfo:
                rel.set('date', datetime.date.fromtimestamp(md.release_timestamp).isoformat())
            else:
                rel.set('timestamp', str(md.release_timestamp))
        if md.release_urgency and md.release_urgency != 'unknown':
            rel.set('urgency', md.release_urgency)
        if md.release_tag:
            rel.set('tag', md.release_tag)
        if not metainfo:
            ET.SubElement(rel, 'location').text = firmware_baseuri + md.fw.filename

        # add container checksum
        if not metainfo:
            if md.fw.checksum_signed_sha1 or local:
                csum = ET.SubElement(rel, 'checksum')
                #metadata intended to be used locally won't be signed
                if local:
                    csum.text = md.fw.checksum_upload_sha1
                else:
                    csum.text = md.fw.checksum_signed_sha1
                csum.set('type', 'sha1')
                csum.set('filename', md.fw.filename)
                csum.set('target', 'container')
            if md.fw.checksum_signed_sha256 or local:
                csum = ET.SubElement(rel, 'checksum')
                if local:
                    csum.text = md.fw.checksum_upload_sha256
                else:
                    csum.text = md.fw.checksum_signed_sha256
                csum.set('type', 'sha256')
                csum.set('filename', md.fw.filename)
                csum.set('target', 'container')

        # add content checksum
        if md.checksum_contents_sha1:
            csum = ET.SubElement(rel, 'checksum')
            csum.text = md.checksum_contents_sha1
            csum.set('type', 'sha1')
            csum.set('filename', md.filename_contents)
            csum.set('target', 'content')
        if md.checksum_contents_sha256:
            csum = ET.SubElement(rel, 'checksum')
            csum.text = md.checksum_contents_sha256
            csum.set('type', 'sha256')
            csum.set('filename', md.filename_contents)
            csum.set('target', 'content')

        # add all device checksums
        for csum in md.device_checksums:
            n_csum = ET.SubElement(rel, 'checksum')
            n_csum.text = csum.value
            n_csum.set('type', csum.kind.lower())
            n_csum.set('target', 'device')

        # add long description
        if md.release_description:
            markdown = md.release_description
            if md.issues and not metainfo:
                markdown += '\n'
                markdown += 'Security issues fixed:\n'
                for issue in md.issues:
                    markdown += ' * {}\n'.format(issue.value)
            rel.append(_xml_from_markdown(markdown))

        # add details URL if set
        if md.details_url_with_fallback:
            child = ET.SubElement(rel, 'url')
            child.set('type', 'details')
            child.text = md.details_url_with_fallback

        # add source URL if set
        if md.source_url:
            child = ET.SubElement(rel, 'url')
            child.set('type', 'source')
            child.text = md.source_url

        # add sizes if set
        if md.release_installed_size:
            sz = ET.SubElement(rel, 'size')
            sz.set('type', 'installed')
            sz.text = str(md.release_installed_size)
        if not metainfo and md.release_download_size:
            sz = ET.SubElement(rel, 'size')
            sz.set('type', 'download')
            sz.text = str(md.release_download_size)

        # add issues
        if metainfo and md.issues:
            issues = ET.SubElement(rel, 'issues')
            for issue in md.issues:
                category = ET.SubElement(issues, 'issue')
                category.text = issue.value
                category.set('type', issue.kind)

    # add requires for each allowed vendor_ids
    requires: List[ET.Element] = []
    if not metainfo and not local:

        # create a superset of all vendors (there is typically just one)
        vendor_ids: List[str] = []
        for md in mds:

            # the vendor can upload to any hardware
            vendor = md.fw.vendor_odm
            if vendor.is_unrestricted and allow_unrestricted:
                continue

            # UEFI devices only got vendor-id values assigned in fwupd 1.3.6,
            # which is made worse by earlier versions of fwupd applying the
            # vendor_id restrictions even when the device vendor ID is NULL :/
            if md.protocol and md.protocol.value == 'org.uefi.capsule':
                continue

            # no restrictions in place!
            if not vendor.restrictions:
                vendor_ids.append('XXX:NEVER_GOING_TO_MATCH')
                continue

            # add all the actual vendor_ids
            for res in vendor.restrictions:
                if res.value == '*':
                    continue
                if res.value not in vendor_ids:
                    vendor_ids.append(res.value)

        # allow specifying more than one ID
        if vendor_ids:
            child = ET.Element('firmware')
            child.text = 'vendor-id'
            if len(vendor_ids) == 1:
                child.set('compare', 'eq')
            else:
                child.set('compare', 'regex')
            child.set('version', '|'.join(vendor_ids))
            requires.append(child)

    # add requires for <id>
    for rq in _get_unique_requirements(mds, 'id'):
        child = ET.Element(rq.kind)
        if rq.value:
            child.text = rq.value
        if rq.compare:
            child.set('compare', rq.compare)
        if rq.version:
            child.set('version', rq.version)
        if rq.depth:
            child.set('depth', rq.depth)
        requires.append(child)

    # add requires for <firmware>
    for rq in _get_unique_requirements(mds, 'firmware'):
        child = ET.Element(rq.kind)
        if rq.value:
            child.text = rq.value
        if rq.compare:
            child.set('compare', rq.compare)
        if rq.version:
            child.set('version', rq.version)
        if rq.depth:
            child.set('depth', rq.depth)
        requires.append(child)

    # add a single requirement for <hardware>
    rq_hws: List[str] = []
    for md in mds:
        for rq in md.requirements:
            if rq.kind == 'hardware' and rq.value not in rq_hws:
                rq_hws.append(rq.value)
    if rq_hws:
        child = ET.Element('hardware')
        child.text = '|'.join(rq_hws)
        requires.append(child)

    # requires shared by all releases
    if requires:
        parent = ET.SubElement(component, 'requires')
        for element in requires:
            parent.append(element)

    # keywords shared by all releases
    if metainfo:
        keywords: List[str] = []
        for md in mds:
            for kw in md.keywords:
                if kw.priority != 5:
                    continue
                if kw.value in keywords:
                    continue
                keywords.append(kw.value)
        if keywords:
            parent = ET.SubElement(component, 'keywords')
            for keyword in keywords:
                child = ET.Element('keyword')
                child.text = keyword
                parent.append(child)

    # success
    return component

def _generate_metadata_kind(fws: List[Firmware],
                            firmware_baseuri: str = '',
                            local: bool = False,
                            allow_unrestricted: bool = True) -> bytes:
    """ Generates AppStream metadata of a specific kind """

    root = ET.Element('components')
    root.set('origin', 'lvfs')
    root.set('version', '0.9')

    # build a map of appstream_id:mds
    components: Dict[str, List[Component]] = defaultdict(list)
    for fw in sorted(fws, key=lambda fw: fw.mds[0].appstream_id):
        for md in fw.mds:
            components[md.appstream_id].append(md)

    # process each component in version order, but only include the latest 5
    # releases to keep the metadata size sane
    for appstream_id in sorted(components):
        mds = sorted(components[appstream_id], reverse=True)[:5]
        component = _generate_metadata_mds(mds,
                                           firmware_baseuri=firmware_baseuri,
                                           local=local,
                                           allow_unrestricted=allow_unrestricted)
        root.append(component)

    # dump to file
    return gzip.compress(ET.tostring(root,
                                     encoding='utf-8',
                                     xml_declaration=True,
                                     pretty_print=True))


def _regenerate_and_sign_metadata_remote(r: Remote):

    # already being regenerated
    if r.is_regenerating:
        return

    # not required */
    if not r.is_signed:
        return

    # fix up any remotes that are not dirty, but have firmware that is dirty
    # -- which shouldn't happen, but did...
    if not r.is_dirty:
        for fw in r.fws:
            if not fw.is_dirty:
                continue
            print('Marking remote %s as dirty due to %u' % (r.name, fw.firmware_id))
            r.is_dirty = True
            fw.is_dirty = False

    # not needed
    if not r.is_dirty:
        return
    if not r.filename:
        return
    if not r.filename_newest:
        return

    # claim this
    r.regenerate_ts = datetime.datetime.utcnow()
    db.session.commit()

    # set destination path from app config
    download_dir = app.config['DOWNLOAD_DIR']
    if not download_dir:
        return
    if not os.path.exists(download_dir):
        os.mkdir(download_dir)

    invalid_fns: List[str] = []
    print('Updating: %s' % r.name)

    # create metadata for each remote
    fws_filtered: List[Firmware] = []
    for fw in db.session.query(Firmware):
        if fw.remote.name in ['private', 'deleted']:
            continue
        if not fw.signed_timestamp:
            continue
        if r.check_fw(fw):
            fws_filtered.append(fw)
    settings = _get_settings()
    blob_xmlgz = _generate_metadata_kind(fws_filtered,
                                         firmware_baseuri=settings['firmware_baseuri'],
                                         allow_unrestricted=r.is_public)

    # write metadata-?????.xml.gz
    fn_xmlgz = os.path.join(download_dir, r.filename)
    with open(fn_xmlgz, 'wb') as f:
        f.write(blob_xmlgz)
    invalid_fns.append(fn_xmlgz)

    # write metadata.xml.gz
    fn_xmlgz = os.path.join(download_dir, r.filename_newest)
    with open(fn_xmlgz, 'wb') as f:
        f.write(blob_xmlgz)
    invalid_fns.append(fn_xmlgz)

    # create Jcat item with SHA256 checksum blob
    jcatfile = JcatFile()
    jcatitem = jcatfile.get_item(r.filename)
    jcatitem.add_alias_id(r.filename_newest)
    jcatitem.add_blob(JcatBlobSha1(blob_xmlgz))
    jcatitem.add_blob(JcatBlobSha256(blob_xmlgz))

    # write each signed file
    for blob in ploader.metadata_sign(blob_xmlgz):

        # not required
        if not blob.data:
            continue
        if not blob.filename_ext:
            continue

        # add GPG only to archive for backwards compat with older fwupd
        if blob.kind == JcatBlobKind.GPG:
            fn_xmlgz_asc = fn_xmlgz + '.' + blob.filename_ext
            with open(fn_xmlgz_asc, 'wb') as f:
                f.write(blob.data)
            invalid_fns.append(fn_xmlgz_asc)

        # add to Jcat file too
        jcatitem.add_blob(blob)

    # write jcat file
    fn_xmlgz_jcat = fn_xmlgz + '.jcat'
    with open(fn_xmlgz_jcat, 'wb') as f:
        f.write(jcatfile.save())
    invalid_fns.append(fn_xmlgz_jcat)

    # do this all at once right at the end of all the I/O
    for fn in invalid_fns:
        print('Invalidating {}'.format(fn))
        ploader.file_modified(fn)

    # mark as no longer dirty
    if not r.build_cnt:
        r.build_cnt = 0
    r.build_cnt += 1
    r.is_dirty = False

    # log what we did
    _event_log('Signed metadata {} build {}'.format(r.name, r.build_cnt))

    # only keep the last 6 metadata builds (24h / stable refresh every 4h)
    fns = glob.glob(os.path.join(download_dir, 'firmware-*-{}.*'.format(r.access_token)))
    for fn in sorted(fns):
        try:
            build_cnt = int(fn.split('-')[1])
        except ValueError as _:
            print('ignoring {} in self tests'.format(fn))
            continue
        if build_cnt + 6 > r.build_cnt:
            continue
        os.remove(fn)
        _event_log('Deleted metadata {} build {}'.format(r.name, build_cnt))

    # all firmwares are contained in the correct metadata now
    for fw in fws_filtered:
        fw.is_dirty = False

    # release this
    r.regenerate_ts = None
    db.session.commit()

def _regenerate_and_sign_metadata():
    for r in db.session.query(Remote):
        _regenerate_and_sign_metadata_remote(r)

@tq.task(max_retries=3, default_retry_delay=5, task_time_limit=60)
def _async_regenerate_remote(remote_id):
    r = db.session.query(Remote)\
                  .filter(Remote.remote_id == remote_id)\
                  .filter(Remote.is_dirty)\
                  .first()
    if not r:
        return
    _regenerate_and_sign_metadata_remote(r)

@tq.task(max_retries=3, default_retry_delay=10, task_time_limit=60)
def _async_regenerate_remote_all():
    _regenerate_and_sign_metadata()
