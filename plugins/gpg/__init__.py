#!/usr/bin/python3
# -*- coding: utf-8 -*-
#
# Copyright (C) 2018 Richard Hughes <richard@hughsie.com>
#
# SPDX-License-Identifier: GPL-2.0+
#
# pylint: disable=no-self-use

import os
import gnupg

from jcat import JcatBlobText, JcatBlobKind
from lvfs.pluginloader import PluginBase, PluginError, PluginSettingText, PluginSettingBool
from lvfs import ploader

class Affidavit:

    """ A quick'n'dirty signing server """
    def __init__(self, key_uid=None, homedir='/tmp'):
        """ Set defaults """

        # check exists
        if not os.path.exists(homedir):
            try:
                os.mkdir(homedir)
            except OSError as e:
                raise PluginError from e

        # find correct key ID for the UID
        self._keyid = None
        gpg = gnupg.GPG(gnupghome=homedir, gpgbinary='gpg2')
        gpg.encoding = 'utf-8'
        for privkey in gpg.list_keys(True):
            for uid in privkey['uids']:
                if uid.find(key_uid) != -1:
                    self._keyid = privkey['keyid']
        if not self._keyid:
            raise PluginError('No imported private key for %s' % key_uid)
        self._homedir = homedir

    def create(self, data: bytes) -> bytes:
        """ Create detached signature data """
        gpg = gnupg.GPG(gnupghome=self._homedir, gpgbinary='gpg2')
        return gpg.sign(data, detach=True, keyid=self._keyid)

    def create_detached(self, filename: str) -> str:
        """ Create a detached signature file """
        with open(filename, 'rb') as fin:
            data = fin.read()
            with open(filename + '.asc', 'wb') as f:
                f.write(self.create(data))
        return filename + '.asc'

    def verify(self, data: bytes) -> bool:
        """ Verify that the data was signed by something we trust """
        gpg = gnupg.GPG(gnupghome=self._homedir, gpgbinary='gpg2')
        gpg.encoding = 'utf-8'
        ver = gpg.verify(data)
        if not ver.valid:
            raise PluginError('Firmware was signed with an unknown private key')
        return True

class Plugin(PluginBase):
    def __init__(self):
        PluginBase.__init__(self, 'sign-gpg')
        self.name = 'GPG Signing'
        self.summary = 'Sign files using GnuPG, a free implementation of the OpenPGP standard'

    def settings(self):
        s = []
        s.append(PluginSettingBool('sign_gpg_enable', 'Enabled', False))
        s.append(PluginSettingText('sign_gpg_keyring_dir', 'Keyring Directory',
                                   '/var/www/lvfs/.gnupg'))
        s.append(PluginSettingText('sign_gpg_firmware_uid', 'Signing UID for firmware',
                                   'sign-test@fwupd.org'))
        s.append(PluginSettingText('sign_gpg_metadata_uid', 'Signing UID for metadata',
                                   'sign-test@fwupd.org'))
        return s

    def metadata_sign(self, blob):

        # create the detached signature
        affidavit = Affidavit(self.get_setting('sign_gpg_firmware_uid', required=True),
                              self.get_setting('sign_gpg_keyring_dir', required=True))
        contents_asc = str(affidavit.create(blob))
        return JcatBlobText(JcatBlobKind.GPG, contents_asc)

    def archive_sign(self, blob):

        # create the detached signature
        affidavit = Affidavit(self.get_setting('sign_gpg_firmware_uid', required=True),
                              self.get_setting('sign_gpg_keyring_dir', required=True))
        contents_asc = str(affidavit.create(blob))
        return JcatBlobText(JcatBlobKind.GPG, contents_asc)
